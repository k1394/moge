# -*- coding: utf-8 -*-
"""
墨阁 · 实时通道：把「模型正在吐的字」一路送到页面上
========================================================

--------------------------------------------------------
一、它解决什么问题
--------------------------------------------------------
在它之前，任务跑起来之后页面是**死的**：背后在跑分类的线程一声不响地
发请求、等结果，页面每 2～2.5 秒问一次"跑完了吗"。于是她看到的是

    任务在跑…（三分钟一动不动）

而这三种情况在页面上长得一模一样：
    · 模型在想（正常，就是慢）
    · 上游把连接挂死了（要等超时才报错）
    · 服务商已经返回了一半、程序在慢慢解析

有了这条通道，她能看到字在往外蹦 —— 一眼就能分清上面三种。
这个"一眼"是它的全部意义，别的都是附带。

--------------------------------------------------------
二、为什么是"推"而不是"轮询"
--------------------------------------------------------
她的原话：「来多少字就显示多少字，不要做任何拦截、缓冲或分块后统一输出」。

轮询天生做不到这一点：轮询的间隔就是最大的缓冲。
哪怕把间隔缩到 0.2 秒，那也是"攒够 0.2 秒再给你看"，
而且请求数会炸。所以这里用一条常驻连接（SSE），
服务端拿到一块就立刻写出去、立刻 flush。

--------------------------------------------------------
三、【关键】这里也必须"不缓冲"
--------------------------------------------------------
「不缓冲」不是只改前端就行的事。一条字要经过四道手：

    上游 SSE  →  llm.chat_stream 逐行读  →  这个模块的队列  →  浏览器逐块画

任何一道攒着不发，她看到的就是"卡半天，然后一次性冒出来一大段"。
所以：
    · llm 那边逐行读、收到一块就回调（不许把整个响应读完再切）
    · 这个模块收到就通知正在等的那条连接（不许等到队列有 N 条再发）
    · 接口层每 yield 一块就 flush（带 X-Accel-Buffering: no）
    · 前端每读到一个块就 append（不许 debounce、不许合并）

--------------------------------------------------------
四、有上限，但**不静默**
--------------------------------------------------------
任务可能吐几十万字（大纲四份一起跑）。队列不能无限吃内存，
所以有上限；但超上限时**必须让她知道**，不能像没发生过：

    · 挤掉的块记进 dropped（字数 + 块数）
    · 后来才连上来的浏览器，一进来就先收到一条 head 事件，带着 dropped
    · 界面上因此能写出「前面 N 字已滚出缓冲」，而不是假装完整

这跟"候选卡里不许有任何静默截断"是同一条规矩。
"""

import threading
import time

# 单条流最多留多少个事件 / 多少字。
# 给得比较宽：一份大纲 8000 字、四个模型一起跑也就三万多字，
# 加上分类分批的原始 JSON，正常任务离上限很远。
MAX_EVENTS = 20000
MAX_CHARS = 400000

# 任务结束后保留多久（秒）。
# 留着的原因：她可能刷新页面、或者点开另一个标签页再切回来 ——
# 那时任务已经跑完了，但流还应该能让她看到"刚才跑出来的是什么"。
KEEP_DONE_SEC = 600


class _Stream:
    """一条流：一个任务（或一次试跑）对应一条。"""

    def __init__(self, sid, meta):
        self.sid = sid
        self.meta = dict(meta or {})
        # 用 Condition 而不是 Queue：订阅者要能"没有新事件时挂在等"，
        # 而不是忙等（忙等会把 CPU 烧在一个纯转发的事情上）。
        self.cond = threading.Condition()
        self.events = []          # [{"t": "chunk", ...}, ...]
        self.chars = 0            # 当前留在缓冲里的字数
        self.dropped_chars = 0    # 被挤掉的字数（必须让她看见）
        self.dropped_blocks = 0
        self.done = False
        self.ended_at = 0.0
        self.closed_at = 0.0

    # ---- 生产者侧 ----

    def push(self, ev):
        """塞一块。**立刻唤醒正在等的连接。**"""
        with self.cond:
            if self.done:
                # 任务已经收尾了还在塞 —— 说明调用方顺序写反了。
                # 不抛异常（那会把任务搞挂），但也不接受。
                return False
            self.events.append(ev)
            self.chars += len(ev.get("text") or "")
            self._trim()
            self.cond.notify_all()
            return True

    def _trim(self):
        """超上限就从**最老的**开始丢，并把丢掉的量记下来。

        为什么丢最老的而不是最新的：她要的是"现在在吐什么"，
        前面已经滚过去的可以不要；反过来丢掉刚来的字，
        页面上就永远停在开头，反而像卡住了。
        """
        while self.events and (len(self.events) > MAX_EVENTS
                               or self.chars > MAX_CHARS):
            old = self.events.pop(0)
            n = len(old.get("text") or "")
            self.chars -= n
            if n:
                self.dropped_chars += n
            self.dropped_blocks += 1

    def close(self):
        with self.cond:
            self.done = True
            self.ended_at = time.time()
            self.cond.notify_all()

    # ---- 消费者侧 ----

    def drain_from(self, idx):
        """把 idx 之后的事件拿走。返回 (事件列表, 是否已收尾)。

        收尾判断里带 len(events)：任务结束那一刻可能还有几块没被取走，
        必须让它们先送出去，再结束连接 —— 否则最后几块会丢，
        而"最后几块"往往正好是收尾的那几句话。
        """
        with self.cond:
            out = self.events[idx:]
            return out, self.done, len(self.events), self.dropped_chars


_streams = {}
_registry_lock = threading.Lock()


def _reap():
    """把早就结束、又过了保留期的流清掉。"""
    now = time.time()
    dead = []
    for sid, st in _streams.items():
        if st.done and st.ended_at and now - st.ended_at > KEEP_DONE_SEC:
            dead.append(sid)
    for sid in dead:
        _streams.pop(sid, None)
    return len(dead)


def open_stream(sid, meta=None):
    """开一条流。同一个 sid 再来一次 = 重开（覆盖）。"""
    sid = str(sid or "").strip()
    if not sid:
        return None
    with _registry_lock:
        _reap()
        st = _Stream(sid, meta)
        _streams[sid] = st
        return st


def has_stream(sid):
    with _registry_lock:
        return str(sid or "") in _streams


def push(sid, ev):
    """往一条流里塞一个事件。流不存在就安静地丢掉。

    为什么不存在时只是丢掉、不报错：流是"看"用的，不是"传"用的 ——
    任务的正确性一点都不依赖它（结果照旧落库）。
    要是因为它不在就让整批分类失败，等于为了副屏把主屏砸了。
    """
    with _registry_lock:
        st = _streams.get(str(sid or ""))
    if st is None:
        return False
    return st.push(ev)


def chunk(sid, text, **extra):
    """塞一段模型吐出来的文字。"""
    ev = {"t": "chunk", "text": text}
    if extra:
        ev.update(extra)
    return push(sid, ev)


def note(sid, text, level="info", **extra):
    """塞一条说明（阶段、模型、警告…）。不算模型吐的字。"""
    ev = {"t": "note", "text": text, "level": level}
    if extra:
        ev.update(extra)
    return push(sid, ev)


def close(sid, **extra):
    """任务收尾。"""
    st = None
    with _registry_lock:
        st = _streams.get(str(sid or ""))
    if st is None:
        return False
    if extra:
        ev = {"t": "end"}
        ev.update(extra)
        st.push(ev)
    st.close()
    return True


def subscribe(sid, poll=0.25, idle_timeout=1800):
    """订阅一条流。返回一个生成器，逐个吐出事件（dict）。

    【为什么先给一条 head】
    浏览器可能是任务跑了一半才点进来的，也可能刷新后重连。
    它需要先知道三件事：这条流是谁的、前面有没有被挤掉过内容、还在不在跑。
    不说清楚的话，她会看到一个只剩后半截的文字区，
    并且以为模型就是从那里开始吐的。
    """
    sid = str(sid or "").strip()
    with _registry_lock:
        st = _streams.get(sid)
    if st is None:
        yield {"t": "gone",
               "text": "这条流已经不在了（任务太久之前结束，或者服务重启过）。"}
        return

    # 一进来就把"这条流是谁、前面丢没丢过"告诉她
    yield {"t": "head", "sid": sid, "meta": st.meta,
           "dropped_chars": st.dropped_chars,
           "dropped_blocks": st.dropped_blocks, "done": st.done}

    idx = 0
    last_data = time.time()      # 最后一次真的收到字
    last_beat = time.time()      # 最后一次往外写东西（含心跳）
    while True:
        with st.cond:
            if idx >= len(st.events) and not st.done:
                st.cond.wait(timeout=poll)
            events, done, total, dropped = st.drain_from(idx)
        for ev in events:
            yield ev
        idx = total
        now = time.time()
        if events:
            last_data = last_beat = now
        else:
            # 长时间一个字都没有，可能是任务线程卡死了。
            # 但**不要急着关连接**：任务可能只是模型在"想"
            # （大纲的首字经常要几十秒，推理模型更久）。
            # 这里定期回一条心跳，免得中间的反代把空闲连接掐掉。
            if now - last_beat > 15:
                last_beat = now
                yield {"t": "idle"}
            if now - last_data > idle_timeout:
                yield {"t": "note", "level": "warn",
                       "text": "超过 %d 分钟没有任何动静，连接断开。"
                               % int(idle_timeout // 60)}
                break
        if done and idx >= total:
            break

    yield {"t": "finish"}


def reset_for_tests():
    """测试用：清空所有流。"""
    with _registry_lock:
        _streams.clear()


# ----------------------------------------------------------------------
# 自测：只测纯内存逻辑，不联网、不碰数据库
# ----------------------------------------------------------------------

def _self_check():
    global MAX_CHARS                   # 下面要临时压小它，好让裁剪真发生
    ok = True
    keep_chars = MAX_CHARS

    def check(name, got, want):
        nonlocal ok
        if got != want:
            ok = False
            print("  [x] %s：得到 %r，期望 %r" % (name, got, want))
        else:
            print("  [v] %s" % name)

    reset_for_tests()
    open_stream("s1", {"purpose": "probe"})
    chunk("s1", "你")
    chunk("s1", "好")
    close("s1", ok=True)

    evs = list(subscribe("s1"))
    kinds = [e["t"] for e in evs]
    check("先给一条 head", kinds[0], "head")
    check("最后有 finish", kinds[-1], "finish")
    check("两块字都在，顺序不变",
          "".join(e.get("text") or "" for e in evs if e["t"] == "chunk"), "你好")
    check("结束时带上收尾信息",
          [e.get("ok") for e in evs if e["t"] == "end"], [True])

    # 流不存在时不能抛异常，要回一条说得清的话
    reset_for_tests()
    evs = list(subscribe("没有这条"))
    check("流不在时回一条 gone", evs[0]["t"], "gone")
    check("往不存在的流里塞东西不报错",
          push("没有这条", {"t": "chunk", "text": "x"}), False)

    # 超上限：丢最老的，但要把丢掉的量记下来（不许静默）
    reset_for_tests()
    MAX_CHARS = 25                     # 临时压小，好让裁剪真发生
    try:
        st = open_stream("s2")
        for i in range(5):
            chunk("s2", "字" * 10)     # 50 字，超过 25 → 开始丢最老的
        check("挤掉的量被记下来了（不是静默丢）", st.dropped_chars > 0, True)
        check("挤掉的块数也记了", st.dropped_blocks > 0, True)
        check("留下的都是最新的",
              [e["text"][0] for e in st.events if e.get("text")][-1], "字")
        close("s2")                    # 不关的话订阅会一直挂着等新事件
        evs = list(subscribe("s2"))
        head = [e for e in evs if e["t"] == "head"][0]
        check("后来连上来的人也能知道丢过内容", head["dropped_chars"] > 0, True)
    finally:
        MAX_CHARS = keep_chars

    # 收尾之后不许再塞（顺序写反了要能看出来，但不许抛）
    reset_for_tests()
    open_stream("s3")
    close("s3")
    check("收尾后再塞会被拒绝", chunk("s3", "迟到的字"), False)

    print()
    print("自测：%s" % ("全部通过" if ok else "有失败"))
    return ok


if __name__ == "__main__":                                   # pragma: no cover
    import sys
    sys.exit(0 if _self_check() else 1)
