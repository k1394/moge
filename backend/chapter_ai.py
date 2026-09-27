"""正文创作 —— 编排、提示词、模型调用。

一条链路：

    表单（模型 / 背景 / 角色卡 / 本章剧情 / 风格 / 要求 / 关联章节）
      → 组装上下文（角色卡写快照、关联章节按"正文 / 梗概"取）
      → 发一次流式请求
      → 字实时推给页面（livestream，sid = ch-<run_id>）
      → 跑完把结果写进这一次的生成记录
      → 她看着满意 → 前端把正文存进章节（POST /api/chapters/{cid}）

------------------------------------------------------------------
【为什么按 2B 做了"直接写进正文框"，却仍然留一张记录表】

她拍板的是：不做候选制，生成完直接写进右边的正文框（照参考站）。
那"为什么还要记"这件事必须说清楚，不然以后有人会觉得这张表多余：

  ① **后悔药**。直接写进正文 = 原来那段字有被覆盖的风险。
     记录里存着 prev_content（生成前的那一份），点了「还原」就能回去。
  ② **对账**。这一章是哪次生成出来的、用的哪个模型、首字耗时多少、
     有没有被字数上限掐断 —— 一稿一稿写下去，这些必须查得到。
  ③ **不自动落库**。结果先进编辑器，她按「保存本章」才写进 lib。
     这一步是我加的：参考站会自动保存，但墨阁这边的正文是她写了很久的
     东西，让一次误点直接顶掉库里的正文，代价太大。

【为什么长任务要自己传 timeout / max_retry】
   见 llm.py 常量区的注释：默认值（180 / 3）是按"短回答"定的。
   写一章是几千字的活，用它必然稳定超时，而且会把等待时间乘三倍。
   这里按大纲那一档的规矩来：timeout=600、max_retry=1。
   **最坏等待 = timeout × max_retry + 退避**，界面上要照这个数说。
"""

import os
import re
import threading
import traceback

from . import chapter_db as cdb
from . import db
from . import livestream
from . import llm

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROMPT_FILE = "chapter.txt"
PROMPT_VERSION = "v1"
PROMPT_VERSION_GENERIC = "v1-generic"

# 长任务自己传的等待参数。理由见模块顶部。
TIMEOUT = 600
MAX_RETRY = 1

# 模板里必须存在的槽位。少一个就退回通用模板 + 把警告写进任务记录，
# 绝不静默丢内容（跟分类 / 内化 / 大纲同一条规矩）。
REQUIRED_SLOTS = ("background", "characters", "plot", "style",
                  "requirement", "context")
OPTIONAL_SLOTS = ()

_SLOT_RE = re.compile(
    r"\{(" + "|".join(REQUIRED_SLOTS + OPTIONAL_SLOTS) + r")\}")

MAX_MODELS = 1     # 正文是单模型（她表单里就是"模型选择（必选）"，单选）


# ----------------------------------------------------------------------
# 通用模板
#
# 【这一份是会同步到公开仓库的】所以里面不许出现任何真实提示词、
# 私人书名、角色名。真实那一份在 prompts/chapter.txt（gitignored）。
# ----------------------------------------------------------------------

GENERIC_CHAPTER_PROMPT = """你是"墨阁"的正文写作助手。请按下面的材料写出这一章的正文。

==================== 故事背景 ====================
{background}

==================== 角色卡 ====================
{characters}

==================== 本章剧情 ====================
{plot}

==================== 写作风格 ====================
{style}

==================== 写作要求 ====================
{requirement}

==================== 前文上下文 ====================
{context}

==================== 写作纪律 ====================
1. 只写这一章的正文，从本章剧情说的那件事开始写起。
2. 角色卡里写了的设定必须照办，尤其是「必须遵守」和「禁止出现的行为」两项。
3. 前文上下文是给你接上前后用的，不要复述它，也不要另起炉灶换背景。
4. 写作风格和写作要求优先于你的个人习惯。
5. 直接给正文。不要写「好的」「以下是」这类开场话，不要写章节编号，
   不要用「（此处省略）」之类的话跳过情节。
6. 一段一件事，多写人物之间实打实的动作和对白，少写空泛的形容词。
7. 篇幅按本章剧情的信息量决定，一般 2000～4000 字。
"""


def prompt_template():
    """取这次要用的模板。返回 (模板文本, 来源, 警告语)。

    来源只有 "file" / "builtin" 两种 —— "这次跑的哪一版"必须可追溯。
    """
    path = os.path.join(ROOT_DIR, "prompts", PROMPT_FILE)
    if not os.path.isfile(path):
        return GENERIC_CHAPTER_PROMPT, "builtin", ""
    try:
        with open(path, encoding="utf-8") as f:
            txt = f.read().strip()
    except Exception as e:                                  # pragma: no cover
        return (GENERIC_CHAPTER_PROMPT, "builtin",
                "prompts/%s 读不了（%s），这次用通用模板" % (PROMPT_FILE, e))
    if not txt:
        return (GENERIC_CHAPTER_PROMPT, "builtin",
                "prompts/%s 是空的，这次用通用模板" % PROMPT_FILE)
    lost = [s for s in REQUIRED_SLOTS if ("{%s}" % s) not in txt]
    if lost:
        return (GENERIC_CHAPTER_PROMPT, "builtin",
                "prompts/%s 少了占位符 %s（那几处内容会被丢掉），"
                "这次改用通用模板" % (PROMPT_FILE, "、".join(lost)))
    return txt, "file", ""


def prompt_version():
    _, src, _ = prompt_template()
    return PROMPT_VERSION if src == "file" else PROMPT_VERSION_GENERIC


def _fill_slots(tpl, **slots):
    """把 {xxx} 换成实际内容。**单遍扫描**，不是逐个 replace。

    理由见 outline_ai._fill_slots：她的小说原文里有花括号不奇怪，
    逐个 replace 会把后填的内容塞进前面那段文字中间，而且不报错。
    """
    def _sub(m):
        return slots.get(m.group(1), m.group(0))
    return _SLOT_RE.sub(_sub, tpl)


def stream_id_for(run_id):
    """这次正文任务在实时通道里的名字。

    前端订阅、后端推送都从这一个出口拿 —— 两边各拼一个字符串的话，
    改前缀时会变成"一切正常、就是没有字出来"，而那种坏法不报错。
    """
    return "ch-%s" % (run_id,)


# ----------------------------------------------------------------------
# 组装上下文
# ----------------------------------------------------------------------

def _model_label(key):
    m = llm.get_model(key) or {}
    return m.get("label") or m.get("model") or key or ""


def _char_block(chars):
    """角色卡 → 给模型看的一段字。

    用她那张卡上已有的八项，一项不落地写出来。空的项**不写**
    （写一堆"身份：（空）"只会占上下文、还会让模型以为这里要求留白）。
    """
    if not chars:
        return ("（这次没有关联角色卡。人物的名字和设定只能从本章剧情里推，"
                "拿不准的地方不要自己加设定。）")
    out = []
    for c in chars:
        lines = ["◆ %s" % (c.get("name") or "（无名）")]
        for f in ("identity", "personality", "goal", "fear", "relations",
                  "speech", "must_do", "never_do"):
            v = (c.get(f) or "").strip()
            if not v:
                continue
            lines.append("  %s：%s" % (cdb_char_label(f), v))
        note = (c.get("note") or "").strip()
        if note:
            lines.append("  备注：%s" % note)
        out.append("\n".join(lines))
    return "\n\n".join(out)


def cdb_char_label(f):
    """角色卡字段的中文名。**只有一处定义**（借 outline_db 那张表）。"""
    try:
        from . import outline_db as odb
        return odb.CHAR_FIELD_LABELS.get(f, f)
    except Exception:                                       # pragma: no cover
        return f


def build_context(owner, refs, warns):
    """按关联章节取上下文。返回 (文本, 明细列表)。

    【截断为什么必须说出来】
    每章只取 REF_CHAPTER_CHARS 字、总共不超过 REF_TOTAL_CHARS。
    不告诉她的下场：她以为"AI 看过全章"，然后拿结果去对账对不上
    （明明第 3 章写过的东西它却不知道），还以为是模型不行。
    所以明细里每章都带 truncated 标记，界面和任务记录里都要显出来。
    """
    refs = refs or []
    if not refs:
        return "", []
    detail, parts, used = [], [], 0
    for r in refs:
        rid = r.get("id") if isinstance(r, dict) else r
        mode = (r.get("mode") if isinstance(r, dict) else None) or cdb.WORD_MODE_SUMMARY
        ch = cdb.get_chapter(owner, rid, with_content=True)
        if not ch:
            continue
        if mode == cdb.WORD_MODE_CONTENT:
            text = (ch.get("content") or "").strip()
            label = "正文"
            if not text:
                # 正文是空的（只写了梗概）→ 退回梗概，并说明为什么
                text = (ch.get("summary") or "").strip()
                label = "梗概（这一章正文还是空的）"
        else:
            text = (ch.get("summary") or "").strip()
            label = "梗概"
        if not text:
            detail.append({"id": ch["id"], "title": ch["title"], "mode": mode,
                           "chars": 0, "truncated": False,
                           "note": "这一章没有可用的%s。" % label})
            continue
        left = cdb.REF_TOTAL_CHARS - used
        truncated = False
        if len(text) > cdb.REF_CHAPTER_CHARS:
            text = text[:cdb.REF_CHAPTER_CHARS]
            truncated = True
        if len(text) > left:
            text = text[:max(0, left)]
            truncated = True
        if not text:
            detail.append({"id": ch["id"], "title": ch["title"], "mode": mode,
                           "chars": 0, "truncated": True,
                           "note": "上下文总量已经满了，这一章没能带上。"})
            warns.append("关联章节的总字数到上限了，「%s」没能带上 ——"
                         "想让它进来就把其它关联章节去掉几个。"
                         % (ch["title"] or ("第 %s 章" % ch["id"])))
            continue
        used += len(text)
        parts.append("【%s · %s】\n%s"
                     % (ch["title"] or ("第 %s 章" % ch["id"]), label, text))
        detail.append({"id": ch["id"], "title": ch["title"], "mode": mode,
                       "label": label, "chars": len(text),
                       "full_chars": len(ch.get("content") or ch.get("summary") or ""),
                       "truncated": truncated})
        if truncated:
            warns.append("关联章节「%s」太长，只带了前 %d 字（它的%s更长的部分没发出去）。"
                         % (ch["title"] or ("第 %s 章" % ch["id"]),
                            len(text), label))
    return "\n\n".join(parts), detail


def _from(body, chapter, key):
    """这次要发的值：请求里给了就用请求里的，没给就用章节存着的。

    【为什么要这一道】这几项是**章节资料**（存在库里），
    但生成时也要允许她"临时改一版再跑"。前端总是把当前框里的值传上来，
    所以正常路径下走的是 body；走 chapter 那条是给"从记录里重跑"用的
    （retry_run 只传了模型 key，其余全靠章节存的那份）。
    """
    v = body.get(key)
    if v is None:
        v = chapter.get(key)
    return str(v or "")


def build_input(owner, chapter, body):
    """把表单搓成一份**可以直接发给模型**的输入，并留下完整快照。

    快照（input_json）里存的是"当时发给模型的是什么" ——
    她跑完之后改了表单，回头看这次记录还得能对上账。
    """
    warns = []
    bg = _from(body, chapter, "background").strip()
    plot = _from(body, chapter, "plot").strip()
    style = _from(body, chapter, "style").strip()
    require = _from(body, chapter, "requirement").strip()
    if len(bg) > cdb.BACKGROUND_MAX:
        warns.append("故事背景超过 %d 字，只带了前 %d 字。"
                     % (cdb.BACKGROUND_MAX, cdb.BACKGROUND_MAX))
        bg = bg[:cdb.BACKGROUND_MAX]
    if len(plot) > cdb.PLOT_MAX:
        warns.append("本章剧情超过 %d 字，只带了前 %d 字。"
                     % (cdb.PLOT_MAX, cdb.PLOT_MAX))
        plot = plot[:cdb.PLOT_MAX]
    if len(style) > cdb.STYLE_MAX:
        warns.append("写作风格超过 %d 字，只带了前 %d 字。"
                     % (cdb.STYLE_MAX, cdb.STYLE_MAX))
        style = style[:cdb.STYLE_MAX]
    if len(require) > cdb.REQUIRE_MAX:
        warns.append("写作要求超过 %d 字，只带了前 %d 字。"
                     % (cdb.REQUIRE_MAX, cdb.REQUIRE_MAX))
        require = require[:cdb.REQUIRE_MAX]

    # 角色卡 / 关联章节同理：请求里给了就用请求里的。
    # 【必须用 is None 判断，不能用 or】她显式传 [] 的意思是"这次一个都不关联"，
    # 用 `or` 的话空列表会被当成"没传"，又把章节里存的那几个捡回来 ——
    # 她刚清掉的关联章节会自己冒出来，而且不报错。
    raw_chars = body.get("character_ids")
    if raw_chars is None:
        raw_chars = chapter.get("char_ids") or []
    clean_ids = []
    for x in raw_chars:
        try:
            clean_ids.append(int(x))
        except (TypeError, ValueError):
            continue
    char_ids = clean_ids[:20]

    raw_refs = body.get("refs")
    if raw_refs is None:
        raw_refs = chapter.get("refs") or []
    refs = cdb._clean_refs(owner, raw_refs)
    context, ref_detail = build_context(owner, refs, warns)

    from . import outline_db as odb
    chars = odb.get_characters(owner, char_ids)

    return {
        "background": bg,
        "plot": plot,
        "style": style,
        "requirement": require,
        "char_ids": char_ids,
        "char_snapshot": [{"id": c["id"], "name": c["name"]} for c in chars],
        "refs": refs,
        "ref_detail": ref_detail,
        "context": context,
        "chapter_title": chapter.get("title") or "",
        "chapter_id": chapter.get("id"),
        "warns": warns,
    }, chars


def build_messages(inp, chars):
    tpl, src, warn = prompt_template()
    if warn:
        inp["warns"].append(warn)
    system = _fill_slots(
        tpl,
        background=inp["background"] or "（没写。只按下面别的内容写。）",
        characters=_char_block(chars),
        plot=inp["plot"] or "（没写。按故事背景和角色卡自然往下写。）",
        style=inp["style"] or "（没写。用自然、干净的叙事语言。）",
        requirement=inp["requirement"] or "（没写。按上面几条写作纪律来。）",
        context=inp["context"] or "（这是第一章，前面没有内容。）",
    )
    user = ("请写出「%s」这一章的正文。直接给正文，不要任何开场话。"
            % (inp.get("chapter_title") or "本章"))
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}], src


# ----------------------------------------------------------------------
# 发起与执行
# ----------------------------------------------------------------------

def create_run(owner, chapter_id, body):
    """建一次生成任务。返回 (run_dict, error_message)。

    校验都在这里做完 —— 参数不对**根本不建任务**：
    建了再失败的话，她会在记录里看到一条"失败了"，
    而真正的原因只是"模型没选"。
    """
    body = body or {}
    ch = cdb.get_chapter(owner, chapter_id, with_content=True)
    if not ch:
        return None, "没有这一章。"

    model_key = str(body.get("model_key") or "").strip()
    if not model_key:
        return None, "先选一个模型。写正文必须指定模型 —— 不然不知道该找谁写。"
    usable = {m["key"]: m for m in llm.usable_models()}
    if model_key not in usable:
        m = llm.get_model(model_key) or {}
        if m and not (m.get("api_key") or "").strip():
            return None, ("「%s」还没填 API Key，写不了。"
                          "去「模型设置」里填上，或者换一个模型。"
                          % (m.get("label") or model_key))
        return None, "「%s」不在可用模型清单里，换一个。" % model_key

    if not str(body.get("plot") or "").strip():
        return None, "「本章剧情」是空的 —— 没有这一章要写什么，模型只能瞎编。"

    try:
        inp, chars = build_input(owner, ch, body)
    except ValueError as e:
        return None, str(e)

    cfg = dict(usable[model_key])
    payload = {
        "model_key": model_key,
        "model_name": cfg.get("label") or model_key,
        "model_version": cfg.get("model") or "",
        "prompt_version": prompt_version(),
        "input": inp,
    }
    run = cdb.create_run(owner, ch["id"], payload)
    if not run:
        return None, "建任务失败。"

    sid = stream_id_for(run["id"])
    livestream.open_stream(sid, {"kind": "chapter", "run_id": run["id"],
                                 "chapter_id": ch["id"]})
    livestream.note(sid, "任务是「%s」· 用 %s（%s）"
                    % (ch["title"] or ("第 %s 章" % ch["id"]),
                       payload["model_name"], payload["model_version"] or "未填真名"))
    for w in inp["warns"]:
        livestream.note(sid, w, level="warn")

    th = threading.Thread(target=_run_worker,
                          args=(run["id"], owner, cfg, inp, chars), daemon=True)
    th.start()
    run["stream_id"] = sid
    return run, ""


def _run_worker(run_id, owner, cfg, inp, chars):
    """后台线程。什么都不许抛出去 —— 抛出去线程就死了，任务永远停在「进行中」。"""
    sid = stream_id_for(run_id)
    try:
        _execute(run_id, owner, cfg, inp, chars, sid)
    except Exception as e:                                  # pragma: no cover
        traceback.print_exc()
        try:
            with db.connect() as conn:
                cdb.finish_run(conn, run_id, status=cdb.RUN_FAILED,
                               error="任务跑了但中途出错：%s" % e)
        except Exception:
            pass
        livestream.note(sid, "任务中途出错：%s" % e, level="bad")
    finally:
        # 关必须放 finally：漏关的话那条连接会一直挂着等一个永远不来的结尾，
        # 页面上就是"字停住了但还在转圈" —— 比看不到流更让人不安。
        livestream.close(sid)


def _cancelled(run_id):
    with db.connect() as conn:
        r = conn.execute("SELECT status FROM chapter_runs WHERE id=?",
                         (run_id,)).fetchone()
    return bool(r) and r["status"] == cdb.RUN_CANCELLED


def _execute(run_id, owner, cfg, inp, chars, sid):
    msgs, src = build_messages(inp, chars)

    with db.connect() as conn:
        cdb.set_run(conn, run_id, status=cdb.RUN_RUNNING)

    buf = []
    stopped = False
    result = None
    for ev in llm.chat_stream(cfg, msgs, temperature=0.8,
                              timeout=TIMEOUT, max_retry=MAX_RETRY,
                              purpose="chapter"):
        if _cancelled(run_id):
            stopped = True
            break
        if ev["t"] == "chunk":
            txt = ev.get("text") or ""
            if not txt:
                continue
            buf.append(txt)
            # 来一块推一块 —— 中间不做任何合并、攒批、节流（她的硬要求）。
            livestream.chunk(sid, txt, model=cfg.get("label") or cfg.get("key") or "")
        elif ev["t"] == "done":
            result = ev.get("result") or {}
        elif ev["t"] == "error":
            with db.connect() as conn:
                cdb.finish_run(conn, run_id, status=cdb.RUN_FAILED,
                               error=ev.get("message") or "生成失败。")
            livestream.note(sid, ev.get("message") or "生成失败。", level="bad")
            return

    text = "".join(buf)
    if stopped:
        # 她自己点了停。已经吐出来的字**留着** —— 那已经花钱了，
        # 丢掉的话她连"写到哪儿了"都看不到。
        with db.connect() as conn:
            cdb.finish_run(conn, run_id, status=cdb.RUN_CANCELLED,
                           ai_content=text, chars=len(text),
                           error="你自己停的。已经写出来的部分留在这儿了。")
        livestream.note(sid, "已停下。已经写出来的 %d 字留在记录里了。" % len(text),
                        level="warn")
        return

    if not result:
        with db.connect() as conn:
            cdb.finish_run(conn, run_id, status=cdb.RUN_FAILED,
                           ai_content=text, chars=len(text),
                           error="连接断在半路，没拿到完整的返回。")
        livestream.note(sid, "连接断在半路，这次可能只写了一半。", level="bad")
        return

    usage = result.get("usage") or {}
    with db.connect() as conn:
        cdb.finish_run(
            conn, run_id, status=cdb.RUN_DONE,
            ai_content=text, chars=len(text),
            finish_reason=result.get("finish_reason") or "",
            ttft_ms=int(result.get("ttft_ms") or 0),
            elapsed_ms=int(result.get("elapsed_ms") or 0),
            usage_json=usage,
            model_version=result.get("model") or cfg.get("model") or "")
    # 被字数上限掐断是最常见的一种"看着写完了、其实是半截"。
    # 必须当场说 —— 她照着这份稿往下写，人物会在半路上断掉。
    fr = (result.get("finish_reason") or "").lower()
    if fr == "length":
        livestream.note(sid, "模型撞到单次字数上限被掐断了，这一稿是半截的。"
                             "接着点一次生成，或者把本章剧情拆细一点。", level="bad")
    else:
        livestream.note(sid, "写完了，%d 字。上面那段已经在编辑器里了。" % len(text))


def retry_run(owner, run_id):
    """用同样的输入再跑一次（换个模型也行）。"""
    run = cdb.get_run(owner, run_id)
    if not run:
        return None, "没有这次生成记录。"
    inp = run.get("input") or {}
    body = {
        "model_key": run.get("model_key"),
        "background": inp.get("background") or "",
        "plot": inp.get("plot") or "",
        "style": inp.get("style") or "",
        "requirement": inp.get("requirement") or "",
        "character_ids": inp.get("char_ids") or [],
        "refs": inp.get("refs") or [],
    }
    return create_run(owner, run["chapter_id"], body)


def reap_orphan_runs():
    """把上次运行留下的"还在跑"的任务收尾。

    后台任务跑在进程内的线程里，进程一没线程就没了，但记录里还写着
    running —— 界面上会永远显示"正在写"而字不动，新任务也会被挡住。
    """
    try:
        with db.connect() as conn:
            cur = conn.execute(
                "UPDATE chapter_runs SET status=?, error=?, finished_at=?"
                " WHERE status=?",
                (cdb.RUN_FAILED, "服务重启了，这次生成中断了。",
                 cdb.now_str(), cdb.RUN_RUNNING))
            return cur.rowcount
    except Exception:                                       # pragma: no cover
        return 0


def _self_check():
    ok = True

    def eq(a, b, what):
        nonlocal ok
        if a != b:
            ok = False
            print("  [x] %s: %r != %r" % (what, a, b))
        else:
            print("  [v] %s" % what)

    eq(stream_id_for(7), "ch-7", "实时通道名字")
    eq(len([s for s in REQUIRED_SLOTS
            if ("{%s}" % s) not in GENERIC_CHAPTER_PROMPT]), 0,
       "通用模板六个槽位都在")
    t = _fill_slots("A{plot}B{style}", plot="1", style="2")
    eq(t, "A1B2", "槽位单遍替换")
    # 填进去的内容里带花括号也不能被二次替换（这正是单遍扫描的意义）
    t2 = _fill_slots("A{plot}B{style}", plot="{style}", style="XX")
    eq(t2, "A{style}BXX", "填进去的花括号不会被再替换一次")
    # 不认识的 {xxx} 原样留着 —— 她正文里写个算式不该被吃掉
    eq(_fill_slots("算 {a+b} 的值", plot="P"), "算 {a+b} 的值",
       "不认识的花括号原样保留")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if _self_check() else 1)
