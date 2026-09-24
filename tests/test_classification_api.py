# -*- coding: utf-8 -*-
"""
墨阁 · 自动分类任务 接口测试
========================================================
测什么：把「点一下自动分类」这条链路完整跑一遍，并守住几条不能破的规矩：

    ① 未登录进不去，别人的文件/任务看不到也动不了
    ② 提交任务立刻返回，不把页面卡住
    ③ 后台真的跑完了，卡片拿到了主类、理由、置信度
    ④ AI 的结果一律是「待确认」，绝不会自己变成「已确认」
    ⑤ 人工确认过的卡片、人工改过主类的卡片，重新分类不会覆盖
    ⑥ 原文一个字都没被改过
    ⑦ 同一个文件同时只能有一个任务；取消、重试都能用
    ⑧ 失败必须留原因（哪一条、为什么），不许悄悄过去

怎么跑（不需要先启动墨阁）：

    cd G:\\docker\\moge
    .venv\\Scripts\\python.exe tests\\test_classification_api.py

它分两段，各自的数据库完全隔离：

    第一段（HTTP）：建一个空临时库 → 在随机端口起一个墨阁服务 →
                    注册测试账号 → 粘贴假文本 → 走接口跑完整流程
    第二段（数据层）：另建一个临时库，直接调数据层函数，
                    把"双开守卫 / 取消 / 失败留痕 / 整批拒收"这些
                    HTTP 上不好稳定复现的边界补齐

**真实数据从头到尾不会被碰。** 而且每一段开头都有一道保险：
一旦发现"用的不是临时库"，脚本立刻拒绝继续，而不是硬着头皮跑下去。
这条规矩是被"测试误删过 12 条真实素材"换来的。
"""

import hashlib
import http.cookiejar
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OK = FAIL = 0


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  [通过] %s  %s" % (name, extra))
    else:
        FAIL += 1
        print("  [失败] %s  %s" % (name, extra))


# ----------------------------------------------------------------------
# 测试用的假文本
#
# 每一行都是现编的句子，并且**刻意让占位规则判得出结果**，
# 这样测试才能断言"第几行应该归到哪一类"。
# 不引用任何真实素材里的句子。
#
# 顺着往下每一行的预期结果（占位规则 v2，八类体系：外貌 · 神态 · 梗 ·
# 暧昧拉扯 · 心理 · 搞笑情节 · 对话台词 · 情节）：
#     1 对话台词  2 神态   3 判不出  4 神态
#     5 心理      6 对话台词 7 判不出  8 判不出
#     9 判不出   10 神态  11 对话台词 12 神态
#    13 判不出   14 外貌  15 建议排除（不给主类）
#
# 【为什么有 5 行是判不出】
# 换到她的八类之后，占位规则只留了它真判得准的几条（对白 / 外貌 / 神态 /
# 心理 / 情节）。第 3、7、9、13 行分别是比喻、环境、感官、设定说明 ——
# 在她的八类里没有落点，要读懂语义才判得出。规则不硬猜是对的：
# 这些正是要交给大模型的活（见 classification.placeholder_rule_judge 的 docstring）。
# 测试把它们断言成判不出，是为了防止以后有人往词表里乱塞词条，让规则开始瞎判。

TEXT = "\n".join([
    "「你来了。」他把杯子搁在桌上。",                       # 1
    "他捏碎了手里的茶杯，仍然笑着说没事。",                   # 2
    "那条河像一条黑绸子，慢慢地流。",                       # 3
    "她笑起来的时候，眼睛弯成两道月牙。",                    # 4
    "他本该把自己拔出来，结束这场荒唐。",                    # 5
    "「别说了。」她抬手打断他。",                          # 6
    "窗外下起了雨，雨点砸在瓦上，一声接着一声。",              # 7
    "那年冬天特别长，长到谁都以为不会再有春天。",              # 8
    "厨房里飘着一种说不上来的香气，屋里有风。",                # 9
    "他把那半块玉佩塞回袖子里，转身走了。",                   # 10
    "「你要是敢走，我就死在这里。」他攥紧了拳头。",             # 11
    "他闭上眼睛，喉结动了一下。",                          # 12
    "这规矩是祖师爷定下的，谁也不能破。",                    # 13
    "他生得一双极冷的眼睛，平日很少笑。",                    # 14
    "12,31,45",                                        # 15
])
LINES = len(TEXT.split("\n"))

# 按上面的设计，逐行预期的主类。None = 占位规则不给主类
WANT_CATS = ["对话台词", "神态", None, "神态",
             "心理", "对话台词", None, None,
             None, "神态", "对话台词", "神态",
             None, "外貌", None]
assert len(WANT_CATS) == LINES


# ----------------------------------------------------------------------
# 隔离：自己起一个用临时数据库的服务
# ----------------------------------------------------------------------

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_server(data_dir, port):
    """起一个用临时库的服务。

    服务输出**写进文件**，不丢进 DEVNULL —— 这是踩过的坑：
    原来 stdout/stderr 全丢掉，服务起不来时只打印一句"服务没起来，测试中止"，
    完全看不出原因。而最常见的原因恰恰是"解释器不对"：
        这个测试起的服务用的是当前解释器（sys.executable），
        它必须装了 uvicorn 和 fastapi，也就是项目自己的
        .venv\\Scripts\\python.exe。
        用系统的 python 跑，服务会在启动那一瞬间报
        "No module named uvicorn" 然后直接退出 —— 现象就是"服务没起来"。
    """
    env = dict(os.environ)
    env["MOGE_DATA_DIR"] = data_dir
    env["PYTHONIOENCODING"] = "utf-8"
    log = open(os.path.join(data_dir, "server.log"), "w", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=PROJECT_DIR, env=env,
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
    )


def dump_server_log(data_dir, tail=15):
    """服务没起来时，把它最后几行输出打出来，别让人对着"没起来"猜。"""
    path = os.path.join(data_dir, "server.log")
    print("  用的解释器：%s" % sys.executable)
    print("  （它必须装了 uvicorn 和 fastapi —— 就是项目自己的"
          " .venv\\Scripts\\python.exe）")
    if not os.path.isfile(path):
        print("  没有服务日志（%s），服务可能在启动前就退出了。" % path)
        return
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().strip().splitlines()
    except Exception as e:                                    # pragma: no cover
        print("  服务日志读不了：%s" % e)
        return
    print("  服务日志（%s）最后 %d 行：" % (path, tail))
    for ln in (lines or ["(空)"])[-tail:]:
        print("    | " + ln)


def wait_ready(base, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(base + "/api/health"), timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def stop_server(proc):
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass


class Client(object):
    """一个"浏览器" = 一个 Cookie 罐 = 一个账号。

    要验"别人看不到你的任务"，就必须同时有两个互不串味的登录状态。
    浏览器里这是两个无痕窗口，测试里就是两个 CookieJar。
    """

    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.jar),
        )

    def call(self, method, path, data=None):
        """发请求，返回 (状态码, JSON 或原始文字)。

        刻意不抛异常：401 / 404 / 400 都是**要被断言的结果**，不是"出错"。
        """
        p = urllib.parse.quote(path, safe="/?=&#")
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(self.base + p, data=body, method=method)
        if body:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=120) as r:
                raw = r.read()
                code = r.status
        except urllib.error.HTTPError as e:
            raw = e.read()
            code = e.code
        try:
            return code, json.loads(raw.decode("utf-8"))
        except Exception:
            return code, raw.decode("utf-8", "replace")

    def ok(self, method, path, data=None):
        code, d = self.call(method, path, data)
        if code >= 300:
            raise AssertionError("%s %s → HTTP %s %s" % (method, path, code, d))
        return d


def wait_run_done(cli, mid, timeout=60):
    """轮询等任务结束。返回最后一次拿到的状态。"""
    end = time.time() + timeout
    st = None
    while time.time() < end:
        st = cli.ok("GET", "/api/materials/%d/classification-status" % mid)
        if st.get("state") != "running":
            return st
        time.sleep(0.2)
    return st


# ======================================================================
# 第一段：HTTP 接口
# ======================================================================

def http_tests():
    data_dir = tempfile.mkdtemp(prefix="moge_cls_")
    proc = None
    try:
        port = free_port()
        base = "http://127.0.0.1:%d" % port
        print("=" * 66)
        print("第一段：HTTP 接口测试")
        print("临时库目录：", data_dir)
        print("=" * 66)

        proc = start_server(data_dir, port)
        if not wait_ready(base):
            print("服务没起来，测试中止。")
            dump_server_log(data_dir)
            return

        probe = Client(base)

        # ---- 保险：确认这个服务用的确实是临时库 --------------------
        print("\n【0】隔离保险")
        code, health = probe.call("GET", "/api/health")
        dbp = os.path.abspath(health.get("db", ""))
        if os.path.abspath(data_dir) not in dbp:
            print("！！服务连的不是临时库：", dbp)
            print("！！为避免动到真实数据，测试拒绝继续。")
            return
        check("服务连的是临时库（不是 data/moge.db）", True, dbp)

        # ---- 未登录 -------------------------------------------------
        print("\n【1】未登录一律进不去")
        for path, method in [
                ("/api/materials/1/auto-classify", "POST"),
                ("/api/materials/1/classification-status", "GET"),
                ("/api/materials/1/classification-retry", "POST"),
                ("/api/materials/1/classification-cancel", "POST"),
                ("/api/classification-runs", "GET"),
                ("/api/classification-runs/1", "GET"),
                ("/api/classification-runs/1/items", "GET"),
                ("/api/classification-states", "GET")]:
            code, _ = probe.call(method, path)
            check("未登录 %s %s → 401" % (method, path), code == 401,
                  "HTTP %s" % code)

        # ---- 账号与入库 ---------------------------------------------
        print("\n【2】账号与入库")
        A = Client(base)
        ua = "cls_a_" + secrets.token_hex(3)
        A.ok("POST", "/api/auth/register", {"username": ua, "password": "pw123456"})
        m = A.ok("POST", "/api/materials", {"title": "自动分类测试稿",
                                           "content": TEXT})
        mid = m["id"]
        content_before = A.ok("GET", "/api/materials/%d" % mid)["content"]
        md5_before = hashlib.md5(content_before.encode("utf-8")).hexdigest()
        check("入库成功，正文 %d 字" % len(TEXT), content_before == TEXT)

        # ---- 分类前的状态 -------------------------------------------
        print("\n【3】分类前的状态")
        st = A.ok("GET", "/api/materials/%d/classification-status" % mid)
        check("还没分类过 → state=never", st["state"] == "never", st["state"])
        check("还没切分过 → 卡片数 0（自动分类会自己补一次切分）",
              st["cards"] == 0, str(st["cards"]))
        check("按钮文案是「还没分类过」", st["status_text"] == "还没分类过",
              st["status_text"])

        states = A.ok("GET", "/api/classification-states")
        check("总素材库能一次拿到全部文件的状态",
              str(mid) in {str(k) for k in states["items"]},
              "共 %d 份" % len(states["items"]))
        check("接口如实说了现在有没有配好模型（这个测试库里没配）",
              states["llm_ready"] is False and "还没配置" in states["note"],
              "可用的模型：%s" % (states.get("usable_models"),))
        check("顺带把模型清单带出来了（前端选模型要用）",
              len(states.get("models") or []) >= 5,
              "%d 个" % len(states.get("models") or []))
        check("没配模型时默认退回本地占位规则",
              states["default_classifier"] == "placeholder",
              states["default_classifier"])

        # 配好 Key 之后，usable_models 里必须带着**人话名字**。
        # 这一条是补的回归测试：原来这里只返回 key 字符串（["qwen-plus"]），
        # 界面拿到之后没法显示成「已配好 1 个（通义千问 Plus）」，
        # 只能显示空括号或者一串机器名 —— 从截图上才看出来。
        A.ok("POST", "/api/models", {"key": "qwen-plus", "api_key": "sk-fake-for-test"})
        s2 = A.ok("GET", "/api/classification-states")
        um = s2.get("usable_models") or []
        check("配好 Key 后 usable_models 里有了这一条",
              len(um) == 1 and um[0].get("key") == "qwen-plus",
              str(um)[:80])
        check("每一条都带着能给人看的中文名（不是光秃秃的 key）",
              bool(um[0].get("label")) and um[0]["label"] != um[0]["key"],
              "label=%r" % (um[0].get("label"),))
        check("默认模型指向刚配好的那个",
              s2.get("default_model") == "qwen-plus", s2.get("default_model"))
        check("配好之后默认方法自动变成大模型",
              s2.get("default_classifier") == "llm", s2.get("default_classifier"))
        check("说明行改成「模型会把素材发出去」这种老实说法",
              s2.get("llm_ready") is True and "服务商" in s2.get("note", ""),
              (s2.get("note") or "")[:50])

        # 密钥绝不能跟着这个接口跑到浏览器里 —— 前端拿到就等于公开。
        blob = json.dumps(s2, ensure_ascii=False)
        check("这个接口里一个字都不含密钥原文",
              "sk-fake-for-test" not in blob
              and all("api_key" not in u for u in um),
              "出现了密钥字段" if "api_key" in blob else "")
        # 只留一份打码版供界面显示
        check("models 里的密钥是打码的",
              all(("api_key" not in m) and m.get("has_key") is True
                  for m in s2["models"] if m["key"] == "qwen-plus"),
              str([m.get("api_key_masked") for m in s2["models"]
                   if m["key"] == "qwen-plus"])[:40])

        # 用完撤掉，免得影响后面「没配模型」相关的分支
        A.ok("POST", "/api/models/delete", {"key": "qwen-plus"})
        s3 = A.ok("GET", "/api/classification-states")
        check("撤掉之后又回到没配模型的状态",
              s3["llm_ready"] is False and s3["default_classifier"] == "placeholder",
              str(s3.get("usable_models")))

        # ---- 【3.5】模型设置：改地址 / 清空密钥 / 新增 ---------------------
        #
        # 这一整段是照着她报的三个问题写的：
        #   ① 填错了想删掉那个密钥，删不掉
        #   ② 充的是第三方（硅基流动）的额度，填进官方那一条就报错
        #   ③ GPT 之类要走中转站，得有地方填自定义地址
        # 三条的共性都是"地址那一栏得能改"，以前界面上根本没有这一栏。
        print("\n【3.5】模型设置：改地址、清空密钥、新增一条")

        # 先把刚才撤掉的那条按真实配置加回来（走「添加」，不是「保存」）
        A.ok("POST", "/api/models/add", {
            "key": "qwen-plus", "label": "通义千问 Plus（阿里云百炼）",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen-plus", "api_key": "sk-first-key"})
        lst = A.ok("GET", "/api/models")
        mrow = [m for m in lst["items"] if m["key"] == "qwen-plus"][0]
        check("清单里能看到这条的 API 地址（界面要能改它）",
              bool(mrow.get("base_url")), mrow.get("base_url"))
        check("也能看到模型名", bool(mrow.get("model")), mrow.get("model"))

        # 「保存」只许改已有的那条，不许顺手新建。
        # 不然她在另一个标签页删掉某条之后，这边旧表单一点保存，
        # 那条会被一个没有地址没有模型名的空壳复活 —— 看着像"我明明删了"。
        code, d = A.call("POST", "/api/models",
                         {"key": "从来没加过的", "api_key": "sk-x"})
        check("保存一个不存在的条目要被挡住（新建请走「添加」）",
              code == 400 and "添加" in str(d), "%s %s" % (code, str(d)[:60]))
        check("被挡住之后清单里没多出空壳",
              "从来没加过的" not in [m["key"] for m in
                                     A.ok("GET", "/api/models")["items"]], True)

        # 把它改成第三方地址 —— 这就是"我充的是硅基流动"的正解：
        # 不是换 Key，是把地址一起换掉。
        A.ok("POST", "/api/models", {
            "key": "qwen-plus",
            "base_url": "https://api.siliconflow.cn/v1",
            "model": "deepseek-ai/DeepSeek-V3.2"})
        mrow = [m for m in A.ok("GET", "/api/models")["items"]
               if m["key"] == "qwen-plus"][0]
        check("能改 API 地址（接第三方要用这个）",
              mrow["base_url"] == "https://api.siliconflow.cn/v1",
              mrow["base_url"])
        check("能改模型名（第三方名字带厂商前缀）",
              mrow["model"] == "deepseek-ai/DeepSeek-V3.2", mrow["model"])
        check("改了地址之后 Key 还在（不会连钥匙一起弄丢）",
              mrow["has_key"] is True, str(mrow.get("has_key")))

        # 清空密钥：配置留着，只把钥匙抹掉
        A.ok("POST", "/api/models/clear-key", {"key": "qwen-plus"})
        mrow = [m for m in A.ok("GET", "/api/models")["items"]
               if m["key"] == "qwen-plus"][0]
        check("清空密钥之后 has_key 变成 False", mrow["has_key"] is False, True)
        check("清空密钥之后地址还留着（不用重新填）",
              mrow["base_url"] == "https://api.siliconflow.cn/v1", mrow["base_url"])
        check("清空密钥之后模型名还留着",
              mrow["model"] == "deepseek-ai/DeepSeek-V3.2", mrow["model"])
        st_after = A.ok("GET", "/api/classification-states")
        check("清空密钥之后这条就不算可用了",
              "qwen-plus" not in [u["key"] for u in (st_after.get("usable_models") or [])],
              str(st_after.get("usable_models")))
        check("清空密钥之后默认方法退回本地规则",
              st_after["default_classifier"] == "placeholder",
              st_after["default_classifier"])

        # 新增一条：中转站的场景
        A.ok("POST", "/api/models/add", {
            "key": "my-relay", "label": "我的中转站",
            "base_url": "https://relay.example.com/v1",
            "model": "gpt-4o-mini", "api_key": "sk-relay-key",
            "note": "自己加的"})
        allkeys = [m["key"] for m in A.ok("GET", "/api/models")["items"]]
        check("能新增一条自定义的模型（中转站走这个口子）",
              "my-relay" in allkeys, str(allkeys))
        st_p = A.ok("GET", "/api/classification-states")
        check("新加的那条填了 Key 就能直接用",
              "my-relay" in [u["key"] for u in (st_p.get("usable_models") or [])],
              str(st_p.get("usable_models")))
        check("新加的那条带着自己填的名字",
              [u.get("label") for u in st_p["usable_models"]
               if u["key"] == "my-relay"] == ["我的中转站"],
              str([u.get("label") for u in st_p["usable_models"]]))

        code, d = A.call("POST", "/api/models/add",
                         {"key": "my-relay", "label": "想覆盖别人"})
        check("新增时撞名要报错，不许悄悄覆盖",
              code == 400 and "已经有一条" in str(d), "%s %s" % (code, str(d)[:60]))
        check("撞名之后原来那条没被改掉",
              [m["label"] for m in A.ok("GET", "/api/models")["items"]
               if m["key"] == "my-relay"] == ["我的中转站"], True)
        A.ok("POST", "/api/models/delete", {"key": "my-relay"})

        # ---- 【3.6】补充提示词 -------------------------------------------
        #
        # 她要的：分类页有个能写提示词的地方，上限 5000 字，
        # 写了就在点开始分类时一起发给大模型。
        print("\n【3.6】补充提示词（上限 5000 字）")
        r0 = A.ok("GET", "/api/classify-prompt")
        check("一开始是空的", r0["content"] == "", repr(r0["content"]))
        check("接口告诉界面上限是 5000 字", r0["max"] == 5000, r0["max"])

        MINE = "这份稿子里的「他」都指师兄，别按第三人称叙述判"
        r1 = A.ok("POST", "/api/classify-prompt", {"content": MINE})
        check("能存下来", r1["content"] == MINE, repr(r1["content"]))
        check("顺带把字数报回来（界面要显示 3/5000）",
              r1["chars"] == len(MINE), r1["chars"])
        check("重新读一次拿到的是同一份",
              A.ok("GET", "/api/classify-prompt")["content"] == MINE, True)

        code, d = A.call("POST", "/api/classify-prompt", {"content": "字" * 5001})
        check("超过 5000 字要被拒绝，不能悄悄截断",
              code == 400 and "5000" in str(d), "%s %s" % (code, str(d)[:70]))
        check("被拒绝之后，原来那份没被改坏",
              A.ok("GET", "/api/classify-prompt")["content"] == MINE, True)

        # 提示词是跟账号走的：另一个账号不该看到她的
        B = Client(base)
        ub = "cls_b_" + secrets.token_hex(3)
        B.ok("POST", "/api/auth/register", {"username": ub, "password": "pw123456"})
        check("别的账号看不到她的补充提示词",
              B.ok("GET", "/api/classify-prompt")["content"] == "", True)
        B.ok("POST", "/api/classify-prompt", {"content": "B 自己的要求"})
        check("B 写了自己的之后，A 的那份没被动",
              A.ok("GET", "/api/classify-prompt")["content"] == MINE, True)

        A.ok("POST", "/api/classify-prompt", {"content": ""})
        check("能清空", A.ok("GET", "/api/classify-prompt")["content"] == "", True)

        # ---- 提交任务 -----------------------------------------------
        print("\n【4】提交任务：立刻返回，不阻塞")

        # 先验一条分支：接口**默认走大模型**，而这个测试库一个模型都没配。
        # 这种情况必须在她点下按钮的那一刻就被挡住（400 + 指路去填 Key），
        # 而不是建个任务、闪一下变成「失败」。
        code, d = A.call("POST", "/api/materials/%d/auto-classify" % mid)
        check("没配模型时点自动分类 → 400，并且说清去哪儿填 Key",
              code == 400 and "模型设置" in str(d),
              "HTTP %s %s" % (code, str(d)[:70]))

        # 后面这些测的是任务链路本身，所以显式指定用占位规则跑
        t0 = time.time()
        r = A.ok("POST", "/api/materials/%d/auto-classify" % mid,
                 {"classifier": "placeholder"})
        elapsed = time.time() - t0
        check("提交成功并返回任务号", r.get("run_id"), "任务 #%s" % r.get("run_id"))
        check("HTTP 立刻返回（不阻塞页面）", elapsed < 5.0,
              "耗时 %.2f 秒" % elapsed)
        run_id = r["run_id"]

        # 立刻查一次状态：这时候应该是"进行中"或者已经跑完（本地规则很快）
        st = A.ok("GET", "/api/materials/%d/classification-status" % mid)
        check("刚提交就能查到进度", st["state"] in ("running", "done"),
              "%s / %.0f%%" % (st["status_text"], st["progress"] * 100))

        # ---- 等它跑完 -----------------------------------------------
        print("\n【5】等任务跑完")
        st = wait_run_done(A, mid)
        check("任务完成", st["state"] == "done",
              "%s（%d/%d）" % (st["status_text"], st["run"]["done_items"],
                              st["run"]["total_items"]))
        check("自动补了一次切分，切出 %d 张卡片" % LINES, st["cards"] == LINES,
              "卡片 %d" % st["cards"])
        check("每一条都处理了（没失败）", st["run"]["failed_items"] == 0,
              "失败 %d" % st["run"]["failed_items"])

        # ---- 【5.5】这次是谁判的 + 心跳 ---------------------------------
        #
        # 她要拿同一批素材试不同模型，所以"这次用了哪个"必须能从状态里读到；
        # 心跳则是回答"它还活着吗" —— 数字不动有两种可能（在跑 / 早死了），
        # 光看 done_items 分不出来。
        print("\n【5.5】状态里能看出「谁判的」和「心跳」")
        rn = st.get("run") or {}
        check("状态里带着这次用的分类器（占位规则 = placeholder）",
              rn.get("classifier") == "placeholder", repr(rn.get("classifier")))
        check("占位规则不记模型（它没用模型）",
              (rn.get("model_key") or "") == "", repr(rn.get("model_key")))
        check("占位规则记的是它自己的版本号，不是提示词版本",
              rn.get("prompt_version") == "v0", repr(rn.get("prompt_version")))
        check("跑完之后有心跳时间", bool(rn.get("heartbeat_at")),
              repr(rn.get("heartbeat_at")))
        check("心跳对外给的是「多久以前」（前端不该自己算时间差）",
              isinstance(rn.get("stale_seconds"), int) and rn["stale_seconds"] >= 0,
              repr(rn.get("stale_seconds")))

        # 跑之前那一刻不该有心跳（否则界面会显示"最后更新 0 秒前"在骗她）
        fresh = A.ok("GET", "/api/materials/%d/classification-status" % mid)
        check("跑完之后状态还是 done（没被心跳那步改坏）",
              fresh["state"] == "done", fresh["state"])

        # ---- 结果对不对 ---------------------------------------------
        print("\n【6】结果：主类、理由、置信度、状态")
        cards = A.ok("GET", "/api/cards?material_id=%d&limit=100" % mid)
        check("卡片数 %d" % LINES, cards["total"] == LINES, str(cards["total"]))

        # 卡片是按原文顺序返回的（order=seq），所以下标 = 行号 - 1
        items = sorted(cards["items"], key=lambda c: c["start_offset"])
        wrong = []
        for i, want in enumerate(WANT_CATS):
            got = items[i]["category_name"] or None
            if got != want:
                wrong.append("第%d行 期望%s 实际%s" % (i + 1, want, got))
        check("每一行的主类都符合占位规则的预期", not wrong,
              "；".join(wrong) if wrong else "%d 行全对" % LINES)

        check("AI 给的主类都带理由",
              all(c["ai_reason"] for c in items if c["category_name"]),
              (items[0]["ai_reason"] or "")[:40])
        check("AI 给的主类都带置信度",
              all(c["ai_confidence"] is not None
                  for c in items if c["category_name"]),
              str(items[0]["ai_confidence"]))
        # 判不出的行（WANT_CATS 里是 None 的，第 15 行"建议排除"除外 ——
        # 它有自己的断言）不能有主类，也不能硬给置信度。
        # 为什么这条重要：错的类名比空白更有害 —— 空白她会看一眼，
        # 错的类名会披着「AI 建议」的外衣混过去。
        no_cat_idx = [i for i, w in enumerate(WANT_CATS) if w is None and i != 14]
        bad_unsure = ["第%d行" % (i + 1) for i in no_cat_idx
                      if items[i]["category_name"] not in ("", None)
                      or items[i]["ai_confidence"] is not None]
        check("判不出来的行：没有主类、也没硬给置信度", not bad_unsure,
              "；".join(bad_unsure) if bad_unsure
              else "判不出的 %d 行都对" % len(no_cat_idx))

        # 表格残留行：只写"建议排除"，卡片状态不动
        noise_card = items[14]
        check("纯数字行 → 理由里写「建议排除」，主类空着",
              "建议排除" in (noise_card["ai_reason"] or "")
              and not noise_card["category_name"],
              (noise_card["ai_reason"] or "")[:40])
        check("AI 不会自己把卡片标成「已排除」",
              noise_card["status"] == "待确认", noise_card["status"])

        # ---- 最重要的两条底线 ---------------------------------------
        print("\n【7】两条底线：不冒充人工确认、不覆盖人工成果")
        check("AI 不会自己把任何一张卡片标成「已确认」",
              all(c["status"] != "已确认" for c in items),
              "已确认 %d 张" % sum(1 for c in items if c["status"] == "已确认"))
        # 三种状态各归各位：判出主类 → 待确认；建议排除 → 待确认（那也是结论）；
        # 真判不出 → 分类失败。混在一起她就没法筛"要我自己处理的"。
        want_status = []
        for i, w in enumerate(WANT_CATS):
            if w is not None or i == 14:
                want_status.append((i, "待确认"))
            else:
                want_status.append((i, "分类失败"))
        bad_st = ["第%d行 期望%s 实际%s" % (i + 1, ws, items[i]["status"])
                  for i, ws in want_status if items[i]["status"] != ws]
        check("状态按「判出来了 / 判不出」分别落在待确认 / 分类失败", not bad_st,
              "；".join(bad_st) if bad_st else "%d 行全对" % LINES)

        # 人工确认第 1 张卡（主类改成和 AI 不一样的，才能看出有没有被覆盖）
        opt = A.ok("GET", "/api/categories")
        cats = {c["name"]: c["id"] for c in opt["categories"]}
        c0 = items[0]
        A.ok("POST", "/api/cards/batch-update",
             {"card_ids": [c0["id"]], "category_id": cats["情节"],
              "status": "已确认"})
        confirmed = A.ok("GET", "/api/cards/%d" % c0["id"])
        check("人工确认成功（主类被改成情节）",
              confirmed["category_name"] == "情节"
              and confirmed["status"] == "已确认",
              "%s / %s" % (confirmed["category_name"], confirmed["status"]))

        # 人工改主类但**不**确认第 2 张（状态仍是待确认）
        c1 = items[1]
        A.ok("POST", "/api/cards/batch-update",
             {"card_ids": [c1["id"]], "category_id": cats["暧昧拉扯"]})
        edited = A.ok("GET", "/api/cards/%d" % c1["id"])
        check("人工改过主类后，卡片来源被记成 human（防止被 AI 覆盖）",
              edited["source"] == "human", edited["source"])

        # 再来一次自动分类（重新分类）
        r2 = A.ok("POST", "/api/materials/%d/auto-classify" % mid,
                  {"classifier": "placeholder"})
        check("跑完之后可以再次提交（重新分类）", r2.get("run_id"),
              "新任务 #%s" % r2.get("run_id"))
        st2 = wait_run_done(A, mid)
        check("第二次任务跑完", st2["state"] == "done",
              "%s %d/%d" % (st2["status_text"], st2["run"]["done_items"],
                            st2["run"]["total_items"]))

        after0 = A.ok("GET", "/api/cards/%d" % c0["id"])
        check("已确认的卡片：主类没有被 AI 覆盖",
              after0["category_name"] == "情节", after0["category_name"])
        check("已确认的卡片：状态还是「已确认」",
              after0["status"] == "已确认", after0["status"])
        after1 = A.ok("GET", "/api/cards/%d" % c1["id"])
        check("人工改过主类的卡片：主类没有被 AI 覆盖",
              after1["category_name"] == "暧昧拉扯", after1["category_name"])
        check("人工改过主类的卡片：来源还是 human",
              after1["source"] == "human", after1["source"])

        # ---- 任务明细 -----------------------------------------------
        print("\n【8】任务明细与任务历史")
        det = A.ok("GET", "/api/classification-runs/%d/items?limit=1000" % run_id)
        check("明细条数 = 任务总条数", det["total"] == LINES, str(det["total"]))
        check("明细里每条都写了处理状态",
              all(x["status"] == "done" for x in det["items"]),
              str({x["status"] for x in det["items"]}))
        check("明细能追到具体是哪张卡",
              all(x["card_id"] for x in det["items"]))

        runs = A.ok("GET", "/api/classification-runs?material_id=%d" % mid)
        check("任务历史里有两次任务", len(runs["items"]) >= 2,
              "共 %d 次" % len(runs["items"]))
        one = A.ok("GET", "/api/classification-runs/%d" % run_id)
        check("单个任务能查到版本号（以后查旧结果靠它）",
              bool(one["model_version"]) and bool(one["prompt_version"]),
              "%s / %s" % (one["model_version"], one["prompt_version"]))
        check("任务上记了用的是占位规则",
              "placeholder" in one["model_version"], one["model_version"])

        # ---- 取消：没有在跑的时候不能取消 ---------------------------
        print("\n【9】取消")
        code, d = A.call("POST", "/api/materials/%d/classification-cancel" % mid)
        check("已经跑完了就不能取消 → 400", code == 400,
              "HTTP %s %s" % (code, str(d)[:60]))

        # ---- 原文一字未改 -------------------------------------------
        print("\n【10】原文一字未改")
        content_after = A.ok("GET", "/api/materials/%d" % mid)["content"]
        check("自动分类前后，materials.content 逐字节一致",
              hashlib.md5(content_after.encode("utf-8")).hexdigest() == md5_before,
              "%d 字" % len(content_after))

        # ---- 多账号隔离 ---------------------------------------------
        print("\n【11】多账号隔离")
        B = Client(base)
        ub = "cls_b_" + secrets.token_hex(3)
        B.ok("POST", "/api/auth/register", {"username": ub, "password": "pw123456"})
        code, _ = B.call("GET", "/api/materials/%d/classification-status" % mid)
        check("B 看 A 文件的分类状态 → 404", code == 404, "HTTP %s" % code)
        code, _ = B.call("POST", "/api/materials/%d/auto-classify" % mid,
                         {"classifier": "placeholder"})
        check("B 给 A 的文件提任务 → 400", code == 400, "HTTP %s" % code)
        code, _ = B.call("GET", "/api/classification-runs/%d" % run_id)
        check("B 查 A 的任务 → 404", code == 404, "HTTP %s" % code)
        code, _ = B.call("GET", "/api/classification-runs/%d/items" % run_id)
        check("B 查 A 的任务明细 → 404", code == 404, "HTTP %s" % code)
        bstates = B.ok("GET", "/api/classification-states")
        check("B 的状态表里没有 A 的文件",
              str(mid) not in {str(k) for k in bstates["items"]},
              "B 有 %d 份素材" % len(bstates["items"]))
        check("A 的卡片没被 B 动过",
              A.ok("GET", "/api/cards/%d" % c0["id"])["status"] == "已确认")

    finally:
        stop_server(proc)
        shutil.rmtree(data_dir, ignore_errors=True)


# ======================================================================
# 第二段：数据层边界
#
# 为什么这些不放在 HTTP 段里测：
#   "同时跑两个任务""取消""分类器报错"这三件事都需要一个**跑得慢**或者
#   **会报错**的分类器。HTTP 段用的是服务里装好的那个占位规则，
#   它又快又不出错，这些边界在接口上没法稳定复现 ——
#   靠"抢时间"去撞出一个冲突，测出来的结果是薛定谔的。
#   所以这一段直接调数据层，把测试用的分类器自己装进去。
# ======================================================================

def data_layer_tests():
    tmp = tempfile.mkdtemp(prefix="moge_cls_data_")

    # 必须在 import backend 之前设好环境变量。
    # 这正是那条老教训：db.DATA_DIR 是 import 那一刻读的环境变量，
    # 之后再改就没用了。这一段是本文件里第一次 import backend，
    # 所以在这里设是有效的 —— 下面还会再核一遍。
    os.environ["MOGE_DATA_DIR"] = tmp
    if PROJECT_DIR not in sys.path:
        sys.path.insert(0, PROJECT_DIR)

    from backend import db, segmentation as sg
    from backend import classify_db as cls
    from backend import classification as auto

    print("\n" + "=" * 66)
    print("第二段：数据层边界测试")
    print("临时库目录：", tmp)
    print("=" * 66)

    # ---- 保险 ------------------------------------------------------
    print("\n【12】隔离保险")
    real = os.path.abspath(os.path.join(PROJECT_DIR, "data", "moge.db"))
    dbp = os.path.abspath(db.DB_PATH)
    if os.path.abspath(tmp) not in dbp or dbp == real:
        print("！！数据目录没指到临时目录：", dbp)
        print("！！为避免动到真实数据，这一段拒绝继续。")
        return
    check("这一段连的是自己的临时库", True, dbp)

    # ---- 装三个测试专用分类器 --------------------------------------
    #
    # 为什么能这么装：分类器是一张注册表（CLASSIFIERS），
    # 装进去一个就多一种"判断方式"。以后接真模型也是往这张表里加一个，
    # 上层代码一行都不用改 —— 这里顺便把这件事验了。

    class SlowClassifier(auto.PlaceholderClassifier):
        """每次判一批先睡 0.4 秒。用来制造"任务还在跑"的真实状态。"""
        name = "slow_test"
        model_version = "slow-test-v1"

        def classify_batch(self, items, ctx):
            time.sleep(0.4)
            return auto.PlaceholderClassifier.classify_batch(self, items, ctx)

    class BoomClassifier(auto.PlaceholderClassifier):
        """一判就炸。用来验"失败必须留原因"。"""
        name = "boom_test"
        model_version = "boom-test-v1"

        def classify_batch(self, items, ctx):
            raise RuntimeError("模拟模型超时")

    class BodyClassifier(auto.PlaceholderClassifier):
        """判得好好的，但把正文一起返回。用来验"整批拒收"。"""
        name = "body_test"
        model_version = "body-test-v1"

        def classify_batch(self, items, ctx):
            out = auto.PlaceholderClassifier.classify_batch(self, items, ctx)
            for o in out:
                o["text"] = "我顺手把正文重写了一遍"
            return out

    auto.CLASSIFIERS["slow_test"] = SlowClassifier()
    auto.CLASSIFIERS["boom_test"] = BoomClassifier()
    auto.CLASSIFIERS["body_test"] = BodyClassifier()

    def cat_id(name):
        for c in cls.list_categories():
            if c["name"] == name:
                return c["id"]
        raise AssertionError("没有这个主类：%s" % name)

    cls.migrate()
    auto.migrate(verbose=True)

    owner = db.owner_of(1)
    other = db.owner_of(2)
    mid = db.save_material("数据层测试稿", TEXT, owner=owner)["id"]
    other_mid = db.save_material("别人的稿子", TEXT, owner=other)["id"]
    cls.apply_split(mid, owner)
    cls.apply_split(other_mid, other)

    with db.connect() as conn:
        n_cards = conn.execute("SELECT COUNT(*) FROM cards WHERE material_id=?",
                               (mid,)).fetchone()[0]
    check("切出 %d 张卡片" % LINES, n_cards == LINES, str(n_cards))

    def wait_cards_settle(limit=20, quiet=0.6):
        """等到卡片不再变化（连续 quiet 秒没变化）。

        【为什么必须有这一步 —— 这是踩过的偶发失败】
        取消是"在批次边界停"：一个**已经开始的批次还会跑完并把结果写回卡片**。
        所以"任务状态已经变成已取消"不代表"这一批的写入已经落地"。
        不等它落地就往下走，后面的断言会被这批"迟到"的写入污染。

        具体症状：同一份测试，"卡片被标成分类失败"那一条
        有时数到 15 张、有时 5 张、有时 6 张 —— 数字随机器负载变。
        根因就是 slow_test 那个"睡 0.4 秒"的批次还在路上，
        它写回时把一部分卡片从「分类失败」改回了「待确认」。

        数三个能反映"AI 到底写过没写"的量当指纹：
        卡片总张数、分类失败张数、AI 来源张数。连续两轮一样就认为停了。
        """
        end, last, stamp = time.time() + limit, None, time.time()
        while time.time() < end:
            with db.connect() as conn:
                r = conn.execute(
                    "SELECT COUNT(*) n,"
                    " SUM(CASE WHEN status=? THEN 1 ELSE 0 END) f,"
                    " SUM(CASE WHEN source=? THEN 1 ELSE 0 END) a"
                    " FROM cards WHERE material_id=?",
                    (sg.STATUS_FAILED, sg.SOURCE_AI, mid)).fetchone()
            cur = (r["n"], r["f"], r["a"])
            if cur != last:
                last, stamp = cur, time.time()
            elif time.time() - stamp >= quiet:
                return True
            time.sleep(0.15)
        return False

    # ---- 双开守卫 --------------------------------------------------
    print("\n【13】同一个文件同时只能有一个任务")
    r1, id1 = auto.create_run(owner, mid, classifier_name="slow_test",
                              background=True)
    r2, id2 = auto.create_run(owner, mid, classifier_name="slow_test",
                              background=True)
    check("第一个任务建起来了", r1.get("ok") is True, "任务 #%s" % id1)
    check("第二个被拒，理由是「已经有一个在跑」",
          r2.get("ok") is False and r2.get("reason") == "already_running",
          str(r2.get("message"))[:50])
    check("被拒时把正在跑的任务号告诉了她",
          r2.get("run_id") == id1, "指向 #%s" % r2.get("run_id"))

    # ---- 取消 ------------------------------------------------------
    print("\n【14】取消：在批次边界停，已处理的部分保留")
    c = auto.cancel_run(id1, owner)
    check("能接受取消请求", c.get("ok") is True, str(c.get("message"))[:40])
    end = time.time() + 20
    run1 = None
    while time.time() < end:
        run1 = auto.get_run(id1, owner)
        if run1["status"] not in auto.RUN_ACTIVE:
            break
        time.sleep(0.2)
    check("任务最终停在「已取消」", run1["status"] == auto.RUN_CANCELLED,
          run1["status_text"])
    check("取消的任务不会假装自己跑完了",
          run1["done_items"] + run1["failed_items"] <= run1["total_items"],
          "%d+%d / %d" % (run1["done_items"], run1["failed_items"],
                          run1["total_items"]))
    # 状态变「已取消」≠ 在途那一批已经落地。等它落定再往下走，
    # 否则后面几条断言会随机器负载随机数错（见 wait_cards_settle 的注释）。
    check("取消之后，在途的那一批也落地了（卡片不再变化）",
          wait_cards_settle())

    # 取消之后要能重新提交
    r3, id3 = auto.create_run(owner, mid, classifier_name=None, background=False)
    run3 = auto.get_run(id3, owner)
    check("取消之后还能再提交任务（不卡死）", r3.get("ok") is True)
    check("用占位规则跑完了", run3["status"] == auto.RUN_COMPLETED,
          "%s %d/%d" % (run3["status_text"], run3["done_items"],
                        run3["total_items"]))

    # ---- 失败留痕 --------------------------------------------------
    print("\n【15】失败要留原因，不能悄悄过去")
    # 先把上一轮的成绩清干净，好让失败任务有东西可处理
    with db.connect() as conn:
        conn.execute("UPDATE cards SET primary_category_id=NULL, ai_reason='',"
                     " ai_confidence=NULL, source=?, status=? "
                     "WHERE material_id=?",
                     (sg.SOURCE_INHERITED, sg.STATUS_PENDING, mid))

    r4, id4 = auto.create_run(owner, mid, classifier_name="boom_test",
                              background=False)
    run4 = auto.get_run(id4, owner)
    check("分类器报错 → 任务标记为失败", run4["status"] == auto.RUN_FAILED,
          run4["status_text"])
    check("失败原因留在任务上", "模拟模型超时" in (run4["error"] or ""),
          (run4["error"] or "")[:50])
    det4 = auto.list_items(id4, owner)
    check("每条明细都记了失败原因",
          det4["total"] == run4["total_items"] and
          all(x["status"] == "failed" and "模拟模型超时" in x["error"]
              for x in det4["items"]),
          "%d 条" % det4["total"])
    with db.connect() as conn:
        n_failed_cards = conn.execute(
            "SELECT COUNT(*) FROM cards WHERE material_id=? AND status=?",
            (mid, sg.STATUS_FAILED)).fetchone()[0]
    check("卡片被标成「分类失败」，可以单独筛出来",
          n_failed_cards == run4["total_items"], "%d 张" % n_failed_cards)

    # ---- 返回带正文 → 整批拒收 --------------------------------------
    print("\n【16】分类器返回正文 → 整批拒收（这条最重要）")
    with db.connect() as conn:
        conn.execute("UPDATE cards SET source=?, status=? WHERE material_id=?",
                     (sg.SOURCE_INHERITED, sg.STATUS_PENDING, mid))
    r5, id5 = auto.create_run(owner, mid, classifier_name="body_test",
                              background=False)
    run5 = auto.get_run(id5, owner)
    check("带正文的返回 → 任务失败", run5["status"] == auto.RUN_FAILED,
          run5["status_text"])
    check("拒绝理由说清了是「带了正文」",
          "带了正文" in (run5["error"] or ""), (run5["error"] or "")[:60])
    with db.connect() as conn:
        written = conn.execute(
            "SELECT COUNT(*) FROM cards WHERE material_id=? AND source=?",
            (mid, sg.SOURCE_AI)).fetchone()[0]
    check("一条正文都没被写进卡片（整批拒收，没有挑着用）",
          written == 0, "被写成 AI 建议的卡片：%d" % written)

    # ---- 重试 ------------------------------------------------------
    print("\n【17】重试：只重跑上次失败的那些")

    def wait_done(rid, limit=30):
        """等一个后台任务跑完（重试是后台跑的，不阻塞界面）。"""
        end = time.time() + limit
        r = None
        while time.time() < end:
            r = auto.get_run(rid, owner)
            if r["status"] not in auto.RUN_ACTIVE:
                break
            time.sleep(0.2)
        return r

    # 第一层：不指定分类器时，重试**沿用上次用的那个**。
    # 上次是 body_test（故意返回正文的那个），所以这次重试也该失败 ——
    # 这恰好证明"沿用"生效了。
    # 【为什么这条值得单独立一测】
    # 中途偷偷换成别的分类器，是最难发现的那类错：结果看起来"也像那么回事"，
    # 但同一份文件里一半卡片是 A 判的、一半是 B 判的，而她不可能会知道。
    res = auto.retry_run(mid, owner, classifier_name=None)
    check("能发起重试", res.get("ok") is True, str(res.get("message"))[:50])
    check("重试只认上次失败的那几条", res.get("retried_only_failed") is True,
          "共 %d 条" % res.get("total_items"))
    run6 = wait_done(res["run_id"])
    check("不指定分类器时，重试沿用上次那个（body_test → 还是失败）",
          run6["classifier"] == "body_test"
          and run6["status"] == auto.RUN_FAILED,
          "%s / %s" % (run6.get("classifier"), run6["status_text"]))
    check("重试任务记着它是从哪一次重试出来的",
          run6["retry_of_run_id"] == id5, str(run6["retry_of_run_id"]))

    # 第二层：这次明确指定用占位规则 → 应该真跑完，并把结果写回卡片。
    res2 = auto.retry_run(mid, owner, classifier_name="placeholder")
    check("明确指定占位规则再重试 → 能发起", res2.get("ok") is True,
          str(res2.get("message"))[:50])
    run7 = wait_done(res2["run_id"])
    check("明确指定占位规则重试 → 跑完了",
          run7["status"] == auto.RUN_COMPLETED,
          "%s %d/%d" % (run7["status_text"], run7["done_items"],
                        run7["total_items"]))
    # 重试是后台跑的：等它把在途的一批也写完，再数结果。
    wait_cards_settle()
    with db.connect() as conn:
        n_done = conn.execute(
            "SELECT COUNT(*) FROM cards WHERE material_id=? AND source=?",
            (mid, sg.SOURCE_AI)).fetchone()[0]
    check("重试之后卡片拿到了 AI 建议", n_done > 0, "%d 张" % n_done)

    # ---- AI 不覆盖人工成果（数据层再验一遍）------------------------
    print("\n【18】人工成果不被覆盖（换个角度再验一次）")
    with db.connect() as conn:
        rows = conn.execute("SELECT id FROM cards WHERE material_id=?"
                            " ORDER BY start_offset LIMIT 2", (mid,)).fetchall()
    cid_a, cid_b = rows[0]["id"], rows[1]["id"]
    cls.update_cards(owner, [cid_a], {"primary_category_id": cat_id("情节"),
                                     "status": sg.STATUS_CONFIRMED})
    cls.update_cards(owner, [cid_b], {"primary_category_id": cat_id("暧昧拉扯")})
    with db.connect() as conn:
        conn.execute("UPDATE cards SET primary_category_id=NULL, ai_confidence=NULL,"
                     " ai_reason='', source=?, status=? "
                     "WHERE material_id=? AND id NOT IN (?,?)",
                     (sg.SOURCE_INHERITED, sg.STATUS_PENDING, mid, cid_a, cid_b))
    auto.create_run(owner, mid, classifier_name=None, background=False)
    a_card = cls.get_card(cid_a, owner)
    b_card = cls.get_card(cid_b, owner)
    check("已确认的卡片：主类没变、状态没变、来源没变",
          a_card["category_name"] == "情节"
          and a_card["status"] == sg.STATUS_CONFIRMED
          and a_card["source"] == sg.SOURCE_HUMAN,
          "%s / %s / %s" % (a_card["category_name"], a_card["status"],
                            a_card["source"]))
    check("人工改过主类的卡片：主类没被覆盖",
          b_card["category_name"] == "暧昧拉扯", b_card["category_name"])

    # ---- 补充提示词：进任务，而且存的是快照 --------------------------
    #
    # 为什么必须是快照：她跑完一轮觉得不准，会去改那句提示词再跑第二轮。
    # 如果任务记录里只存"用没用"，回头就再也答不出"第一轮到底怎么问的" ——
    # 而那正是她对比两轮时唯一的变量。
    print("\n【18.5】补充提示词：随任务走，存的是快照")
    ROUND1 = "第一轮的要求：拿不准的一律留空"
    r18, id18 = auto.create_run(owner, mid, classifier_name="placeholder",
                                user_prompt=ROUND1, background=False)
    run18 = auto.get_run(id18, owner)
    check("任务记录里带着这一轮的补充提示词",
          run18.get("user_prompt") == ROUND1, repr(run18.get("user_prompt"))[:60])

    ROUND2 = "第二轮改成了：宁可猜一个也别留空"
    auto.set_user_prompt(owner, ROUND2)
    r19, id19 = auto.create_run(owner, mid, classifier_name="placeholder",
                                background=False)
    run19 = auto.get_run(id19, owner)
    check("第二轮用的是新那句", run19.get("user_prompt") == ROUND2,
          repr(run19.get("user_prompt"))[:60])
    check("第一轮的记录没被后来的改动覆盖（存的是快照不是引用）",
          auto.get_run(id18, owner).get("user_prompt") == ROUND1, True)

    # 不传 user_prompt 时应该用她存着的那份
    r20, id20 = auto.create_run(owner, mid, classifier_name="placeholder",
                                background=False)
    check("不显式传的时候用她存的那份",
          auto.get_run(id20, owner).get("user_prompt") == ROUND2, True)

    # 超长必须当场拒绝，不许截断
    got = auto.create_run(owner, mid, classifier_name="placeholder",
                          user_prompt="字" * 5001)
    check("超 5000 字的补充提示词不许建任务",
          got[0].get("ok") is False and got[1] is None,
          str(got[0].get("reason")))
    auto.set_user_prompt(owner, "")

    # ---- 进程留下的"还在跑"的任务要收尾 -----------------------------
    #
    # 这条对应一个真事故：开发时改了后端代码，服务热重载，
    # 正在跑的后台线程跟着没了 —— 而任务表里还写着 running，
    # 界面上永远显示「分类中」。她没有任何办法知道它其实早死了。
    print("\n【18.6】服务重启后，僵尸任务要能被收尾")
    rz, idz = auto.create_run(owner, mid, classifier_name="placeholder",
                              background=False)
    with db.connect() as conn:
        # 手工造一个"跑了一半就断电"的现场：
        # 任务还在跑，一部分条目已完成、一部分还没轮到
        conn.execute("UPDATE classification_runs SET status=?, error='',"
                     " finished_at='' WHERE id=?",
                     (auto.RUN_RUNNING, idz))
        conn.execute("UPDATE classification_items SET status=? WHERE run_id=?"
                     " AND seq<=2", (auto.ITEM_DONE, idz))
        conn.execute("UPDATE classification_items SET status=?, error=''"
                     " WHERE run_id=? AND seq>2", (auto.ITEM_PENDING, idz))
        n_pending = conn.execute(
            "SELECT COUNT(*) FROM classification_items WHERE run_id=?"
            " AND status=?", (idz, auto.ITEM_PENDING)).fetchone()[0]
    check("造好了现场：任务在跑、还有没轮到的条目", n_pending > 0,
          "%d 条没轮到" % n_pending)

    n_reaped = auto.reap_orphan_runs()
    check("收尾函数动了至少一条", n_reaped >= 1, "%d 条" % n_reaped)
    runz = auto.get_run(idz, owner)
    check("僵尸任务被标成失败（不再是永远转圈的 running）",
          runz["status"] == auto.RUN_FAILED, runz["status"])
    check("失败原因说清了是服务重启导致的",
          "中断" in (runz["error"] or ""), (runz["error"] or "")[:60])
    check("备注里告诉她重试能接着跑、不用重花钱",
          "接着跑" in (runz["note"] or ""), (runz["note"] or "")[:60])
    with db.connect() as conn:
        left = conn.execute(
            "SELECT COUNT(*) FROM classification_items WHERE run_id=?"
            " AND status=?", (idz, auto.ITEM_PENDING)).fetchone()[0]
        kept = conn.execute(
            "SELECT COUNT(*) FROM classification_items WHERE run_id=?"
            " AND status=?", (idz, auto.ITEM_DONE)).fetchone()[0]
    check("没轮到的条目被记成待重试（这样重试才会只补这些）",
          left == 0, "还剩 %d 条 pending" % left)
    check("已经判好的条目不动（重试不会把付过钱的再判一遍）",
          kept > 0, "%d 条 kept" % kept)

    # 再收一遍不该有副作用（启动时每次都调，必须幂等）
    auto.reap_orphan_runs()
    check("再收一遍不动已经收尾过的任务",
          auto.get_run(idz, owner)["status"] == auto.RUN_FAILED, True)


    # ---- 隔离再验一遍 ----------------------------------------------
    print("\n【19】多用户再验一遍（数据层）")
    with db.connect() as conn:
        n_other_ai = conn.execute(
            "SELECT COUNT(*) FROM cards WHERE material_id=? AND source=?",
            (other_mid, sg.SOURCE_AI)).fetchone()[0]
    check("给 A 的分类任务没碰 B 的卡片", n_other_ai == 0,
          "B 那边被写成 AI 建议的：%d" % n_other_ai)

    # ---- 原文一字未改 ----------------------------------------------
    print("\n【20】原文一字未改")
    got = db.get_material(mid, owner=owner)["content"]
    check("整段测试跑下来 materials.content 一个字符都没变",
          got == TEXT, "%d 字" % len(got))

    shutil.rmtree(tmp, ignore_errors=True)


# ======================================================================

def main():
    print("=" * 66)
    print("墨阁 · 自动分类任务 测试")
    print("=" * 66)

    # 第一段：走 HTTP，用自己起的临时服务
    http_tests()

    # 第二段：直接调数据层。
    # 顺序很重要 —— 这一段是本文件第一次 import backend，
    # 它会先把 MOGE_DATA_DIR 指到自己的临时目录再 import，
    # 所以 db.DB_PATH 从一开始就落在临时目录里。
    data_layer_tests()

    print("\n" + "=" * 66)
    print("通过 %d 项，失败 %d 项" % (OK, FAIL))
    print("=" * 66)
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
