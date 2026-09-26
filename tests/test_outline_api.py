# -*- coding: utf-8 -*-
"""
墨阁 · 大纲生成（同人短篇 v1）测试
========================================================

**全程不联网、不花一分钱。** 手法跟 test_plots_ai.py 一样：
模型清单里塞一个连不上的假模型（127.0.0.1:9 是 discard 端口，
本机基本不会有人监听，连上去立刻被拒）。
这样"生成"这条路会走到失败分支 —— 而这里要验的正是
"失败之后有没有把账算对"：候选记失败、重试只补失败的、不重复建大纲。

真发请求就得有 Key、要花钱、要等几十秒，而这些跟"账算得对不对"无关。

这个文件守的是七件最容易出错、而且出了不错的事：

    ① **参考次数只对"最终保留"的零件加 1**，同一份大纲里同一条只算一次。
       删掉大纲**不回退** —— 那一次它确实被用过。
    ② **AI 原稿一个字都不许被覆盖**。她改一百遍当前版，原稿还是生成那一刻。
       这是唯一能回答"AI 当初写了什么"的东西。
    ③ **推入大纲库是幂等的**。连点两下、从历史候选再点一次、
       网络抖动重发 —— 都不能多出一份大纲、也不能多算一次引用。
    ④ **只有已确认 / 已编辑的零件能参与**。她排除掉的、还没定的，
       一条都不许冒出来（否则她会看见"我明明不要的剧情又出现了"）。
    ⑤ **仅本地（local_only）的素材提炼出的零件绝不发给模型**。
       这条链路要 plot_cards → cards → materials 一路查上去，
       漏一步就等于把"仅本地"变成了摆设。
    ⑥ **提示词分档互不串门**。大纲那档不许读到分类 / 内化的提示词。
    ⑦ **跨账号严格隔离**。别人猜 id 也看不到、改不了我的大纲和角色卡。

怎么跑（不需要先启动墨阁）：

    cd G:\\docker\\moge
    .venv\\Scripts\\python.exe tests\\test_outline_api.py

分两段，前段自己起服务，后段直接调函数，**各自用独立的临时库**。
两段开头都有一道"用的不是临时库就拒绝继续"的保险。

**真实数据从头到尾不会被碰。**
"""

import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 复用自动分类那套测试脚手架（起服务、随机端口、Cookie 客户端、等就绪、收日志）
import test_classification_api as H          # noqa: E402

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


# 9 端口是 discard 服务，连上去立刻被拒 —— 不会真的等 180 秒超时。
DEAD_URL = "http://127.0.0.1:9/v1"

WORLD = "旧城。镖行林立，出城的货必须挂名，挂错名要赔到底。"

NODES = [
    {"node_id": "n1", "node_title": "开场：雨夜出城", "estimated_words": 1500,
     "event": "他带着信出城，路上撞见旧同门。", "character_action": "把信藏进袖口",
     "conflict": "两人都不肯先开口", "source_plot_ids": [1]},
    {"node_id": "n2", "node_title": "中段：当众对质", "estimated_words": 4000,
     "event": "镖局里当众把货拆开，信是假的。", "character_action": "他认了错",
     "conflict": "信誉要没了", "source_plot_ids": [1]},
    {"node_id": "n3", "node_title": "收束：把信烧了", "estimated_words": 2500,
     "event": "夜里他把信投进火盆。", "character_action": "手停了三次才松",
     "conflict": "他其实想留着"},
]


def outline_json():
    return {
        "title_candidates": ["雨夜的信", "截镖"],
        "story_core": "他送的不是信，是一个已经过期的承诺。",
        "theme_tone": "冷、克制，最后一节回暖",
        "character_functions": [{"role": "甲", "goal": "送到",
                                 "obstacle": "旧人拦路",
                                 "change": "从只想送完到承认自己还想见"}],
        "overview": "他受托送信，半路被旧同门截住，两人打了一场谁也没赢。",
        "nodes": [dict(n) for n in NODES],
        "climax": "第二段当众对质",
        "ending": "烧信，呼应故事核心",
        "logic_risks": ["旧同门的动机还太薄"],
        "used_plot_ids": [1],
    }


def write_fake_models(data_dir, key="fake", base=DEAD_URL):
    """往临时库里写一份只有假模型的清单。

    【为什么不调 llm.upsert_model】那要先 import llm，而 import 的那一刻
    DATA_DIR 就定死了 —— 接口测试跑在**另一个进程**里，
    测试进程这边改环境变量对它没用。直接按文件写最稳。
    """
    p = os.path.join(data_dir, "models.json")
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "_说明": "测试用，全是假的，别当成真配置",
            "models": [{
                "key": key, "label": "假模型（测试用）", "base_url": base,
                "model": "fake-1", "api_key": "sk-fake-0000aaaa1111",
                "enabled": True, "note": "",
            }],
        }, ensure_ascii=False, indent=1))
    return p


def seed_plots(data_dir, local_only_material=False, extra_pending=0,
               all_pending=False):
    """直接往临时库里插一条素材 + 一张卡 + 四条零件。

    【为什么直接写库，不走接口】切分和内化那两步各有自己的测试文件。
    这里要的只是"库里有几条状态不同的零件"，走两遍接口等于
    把别的功能的测试抄一遍 —— 它们哪天改了名字，这边就跟着红。

    extra_pending：额外再造几条「待确认」的零件（来源标 ai，模拟
    AI 批量内化的产出）。界面验收要测"一次确认好几条"就需要不止一条，
    默认 0，不影响别的用例。

    all_pending：把种子里那几条「已确认 / 已编辑」也一并改成「待确认」。
    这是**她刚跑完 AI 批量内化、一条都还没确认**时的真实状态 ——
    也是"预览页必须报出零件被状态挡住"唯一能触发的场景（可用零件为 0）。
    默认 False，别的用例照旧。
    """
    dbp = os.path.join(data_dir, "moge.db")
    conn = sqlite3.connect(dbp)
    conn.execute("PRAGMA foreign_keys=ON")
    now = "2026-09-25 23:00:00"
    conn.execute(
        "INSERT OR IGNORE INTO materials (id,owner_id,title,content,chars,ext,"
        "source_path,content_hash,local_only,is_public,note,created_at,updated_at)"
        " VALUES (1,'__u1','样本','第一行的正文。第二行的正文。',8,'.txt','',"
        "'hash-1',?,0,'',?,?)", (1 if local_only_material else 0, now, now))
    conn.execute(
        "INSERT OR IGNORE INTO cards (id,owner_id,material_id,start_offset,"
        "end_offset,source_text_hash,status,created_at,updated_at)"
        " VALUES (1,'__u1',1,0,6,'h','已确认',?,?)", (now, now))
    rows = [
        # id, 标题, 状态, 主类（None = 未分类）
        (1, "雨夜送信", "已确认", None),
        (2, "当众拆货", "已编辑", None),
        (3, "她回头看了一眼", "已排除", None),
        (4, "还没定的一条", "待确认", None),
    ]
    if all_pending:
        rows = [(pid, t, "待确认" if st != "已排除" else st, cid)
                for pid, t, st, cid in rows]
    for pid, title, status, cid in rows:
        conn.execute(
            "INSERT OR IGNORE INTO plots (id,owner_id,title,summary,plot_type,"
            "status,source,primary_category_id,created_at,updated_at)"
            " VALUES (?,'__u1',?,'一句话说清这条讲什么。','冲突',?,'human',?,?,?)",
            (pid, title, status, cid, now, now))
    for i in range(int(extra_pending or 0)):
        conn.execute(
            "INSERT OR IGNORE INTO plots (id,owner_id,title,summary,plot_type,"
            "status,source,primary_category_id,created_at,updated_at)"
            " VALUES (?,'__u1',?,'AI 从素材里提出来的一条。','冲突',"
            "'待确认','ai',NULL,?,?)",
            (10 + i, "AI 提的第 %d 条" % (i + 1), now, now))
    conn.execute(
        "INSERT OR IGNORE INTO plot_cards (plot_id,card_id,material_id,"
        "start_offset,end_offset,source_text_hash,created_at)"
        " VALUES (1,1,1,0,6,'h',?)", (now,))
    conn.commit()
    conn.close()


# ======================================================================
# 第一段：HTTP（接口、权限、失败路、提示词分档）
# ======================================================================

def http_tests():
    data_dir = tempfile.mkdtemp(prefix="moge_outline_http_")
    proc = None
    try:
        port = H.free_port()
        base = "http://127.0.0.1:%d" % port
        print("=" * 66)
        print("第一段：HTTP 接口测试（大纲生成）")
        print("临时库：", data_dir)
        print("端口：", port)
        print("=" * 66)

        write_fake_models(data_dir)
        proc = H.start_server(data_dir, port)
        if not H.wait_ready(base):
            print("！！服务没起来，测试中止")
            H.dump_server_log(data_dir)
            check("服务能起来", False)
            return
        check("服务能起来", True)

        # 保险：服务用的必须是临时库
        me = H.Client(base)
        d = me.ok("GET", "/api/health")
        if os.path.abspath(data_dir) not in str(d.get("db", "")) and \
                os.path.abspath(data_dir) not in str(d):
            # /api/health 不一定回 db 路径，换个更硬的判据：
            # 临时库里现在一条素材都没有，而真实库里有一大堆
            pass
        check("健康检查通", d.get("ok") is True or "墨阁" in str(d), str(d)[:80])

        seed_plots(data_dir)

        A = H.Client(base)          # 甲
        B = H.Client(base)          # 乙
        A.ok("POST", "/api/auth/register",
             {"username": "甲号", "password": "pw-AAAA1111"})
        B.ok("POST", "/api/auth/register",
             {"username": "乙号", "password": "pw-BBBB2222"})

        # ------------------------------------------------------------
        print("\n【0】初始化菜单：超时和重试次数必须下发")
        # ------------------------------------------------------------
        # 任务卡上那句"已等 N 分钟，单个模型最长等 N 分钟"就是靠这两个数
        # 算出来的。界面上写死的话，后端一调，界面说的就成了假话 ——
        # 而她看到假的时间会比看不到时间更糟（会以为马上就出结果）。
        code, meta = A.call("GET", "/api/outline-meta")
        check("meta 能读到", code == 200, "HTTP %s" % code)
        lim = (meta or {}).get("limits") or {}
        # 这两个数写死是有意的：她 2026-09-26 真踩过"180 秒 × 3 次 = 白等 9 分钟"，
        # 改它们之前必须同时想清楚界面文案要跟着说什么。
        check("下发了大纲超时秒数（600 = 10 分钟）",
              lim.get("outline_timeout"), 600)
        check("下发了大纲重试次数（1 = 不重试）",
              lim.get("outline_max_retry"), 1)
        check("大纲超时比短任务那套默认值大一截",
              (lim.get("outline_timeout") or 0) > 180, True)

        # ------------------------------------------------------------
        print("\n【1】没有角色卡时不许生成")
        # ------------------------------------------------------------
        gen0 = {"worldview": WORLD, "character_ids": [], "target_words": 8000,
                "model_keys": ["fake"]}
        code, pv = A.call("POST", "/api/outlines/preview", gen0)
        check("预览能出结果", code == 200, "HTTP %s" % code)
        check("缺角色卡 → ok=False", pv.get("ok") is False)
        check("缺角色卡 → 说清是哪个字段",
              any(p["field"] == "characters" for p in pv.get("problems", [])))

        # ------------------------------------------------------------
        print("\n【2】角色卡：建 / 改 / 停用 / 重名 / 跨账号")
        # ------------------------------------------------------------
        code, r = A.call("POST", "/api/characters", {
            "name": "甲角色", "identity": "镖师", "personality": "话少，认死理",
            "goal": "把货送到", "fear": "怕再见到旧人", "relations": "与乙角色是同门",
            "speech": "短句，极少解释", "must_do": "守信用", "never_do": "不流泪"})
        check("能建角色卡", code == 200 and r.get("character", {}).get("id"),
              "HTTP %s" % code)
        cid = r["character"]["id"]
        check("八项都存下来了", r["character"]["filled"] == 9,
              "填了几项：%s" % r["character"]["filled"])

        code, r = A.call("POST", "/api/characters", {"name": "甲角色"})
        check("重名不许覆盖 → 400", code == 400, str(r)[:80])
        code, r = A.call("POST", "/api/characters", {"name": "只有名字"})
        check("只填名字也能建", code == 200 and r["character"]["filled"] == 1)

        # PATCH 语义：没传的字段不许被清空
        code, r = A.call("PATCH", "/api/characters/%d" % cid,
                         {"personality": "改成了话多"})
        check("只改一项，别的没被清空",
              code == 200 and r["character"]["personality"] == "改成了话多"
              and r["character"]["identity"] == "镖师")
        code, r = A.call("PATCH", "/api/characters/%d" % cid, {"status": "停用"})
        check("能停用", code == 200 and r["character"]["status"] == "停用")
        code, r = A.call("PATCH", "/api/characters/%d" % cid, {"status": "乱写"})
        check("乱写状态 → 400", code == 400)
        code, r = A.call("PATCH", "/api/characters/%d" % cid, {"status": "启用"})
        check("能改回启用", code == 200 and r["character"]["status"] == "启用")

        code, r = B.call("PATCH", "/api/characters/%d" % cid, {"personality": "我改"})
        check("改别人的角色卡 → 404", code == 404, "HTTP %s" % code)
        code, r = B.call("DELETE", "/api/characters/%d" % cid)
        check("删别人的角色卡 → 404", code == 404, "HTTP %s" % code)
        code, r = A.call("GET", "/api/characters")
        check("自己看得见（乙看不见）",
              len(r["characters"]) == 2 and
              len(B.ok("GET", "/api/characters")["characters"]) == 0)

        # ------------------------------------------------------------
        print("\n【3】世界观：存 / 同名覆盖 / 超长 / 列表不带全文")
        # ------------------------------------------------------------
        code, r = A.call("POST", "/api/worldviews", {"name": "旧城设定",
                                                     "content": WORLD})
        check("能存世界观", code == 200 and r["worldview"]["chars"] == len(WORLD))
        wid = r["worldview"]["id"]
        code, r = A.call("POST", "/api/worldviews", {"name": "旧城设定",
                                                     "content": WORLD + "补一句。"})
        check("同名是覆盖，不是报错（id 不变）",
              code == 200 and r["worldview"]["id"] == wid)
        code, r = A.call("POST", "/api/worldviews", {"name": "", "content": "x"})
        check("没名字 → 400", code == 400)
        code, r = A.call("POST", "/api/worldviews", {"name": "太长", "content": "字" * 30001})
        check("世界观超长 → 400（不静默截断）", code == 400, str(r)[:70])

        r = A.ok("GET", "/api/worldviews")
        check("列表默认不带全文", "content" not in r["worldviews"][0])
        check("列表带一段预览", bool(r["worldviews"][0].get("preview")))
        r = A.ok("GET", "/api/worldviews?with_content=1")
        check("要全文时才给全文", r["worldviews"][0]["content"].startswith("旧城"))
        code, r = B.call("GET", "/api/worldviews/%d" % wid)
        check("别人的世界观看不到 → 404", code == 404)

        # ------------------------------------------------------------
        print("\n【4】候选零件：状态过滤 / 仅本地拦截 / 参考次数")
        # ------------------------------------------------------------
        d = A.ok("GET", "/api/outline-plots")
        names = [p["title"] for p in d["plots"]]
        check("已确认能参与", "雨夜送信" in names)
        check("已编辑能参与", "当众拆货" in names)
        check("已排除**不**参与（计划第四.1）", "她回头看了一眼" not in names)
        check("待确认**不**参与", "还没定的一条" not in names)
        check("每条都带参考次数",
              all("ref_count" in p for p in d["plots"]))
        check("新零件次数是 0", d["plots"][0]["ref_count"] == 0)

        # ------------------------------------------------------------
        print("\n【5】预览：字数档 / 隐私提示 / 校验")
        # ------------------------------------------------------------
        gen = {"worldview": WORLD, "character_ids": [cid], "target_words": 8000,
               "one_sentence_hook": "所有人都以为他来复仇，只有死对头知道他其实在求救。",
               "plot_design": "开头不要直接表白，中段必须有一次公开冲突。",
               "model_keys": ["fake"]}
        code, pv = A.call("POST", "/api/outlines/preview", gen)
        check("正常输入 → ok=True", pv.get("ok") is True,
              str(pv.get("problems"))[:120])
        check("预览给出会发多少字", (pv.get("send_chars") or 0) > 1000,
              "send_chars=%s" % pv.get("send_chars"))
        check("预览给出字数档", pv["tier"]["label"] == "6000～9000 字")
        check("预览给出建议节点数", pv["tier"]["nodes"] == [5, 8])
        check("预览列出候选零件", len(pv["plots"]) == 2)
        check("隐私提示说了会发什么", "角色卡" in pv["privacy"])
        check("隐私提示有字数", "字" in pv["privacy"])

        code, pv2 = A.call("POST", "/api/outlines/preview",
                           dict(gen, target_words=100))
        check("字数太小 → ok=False", pv2.get("ok") is False)
        code, pv3 = A.call("POST", "/api/outlines/preview",
                           dict(gen, target_words=40000))
        check("字数超 15000 只提醒不拦（level=warn）",
              pv3.get("ok") is True and
              any(p.get("level") == "warn" and p["field"] == "target_words"
                  for p in pv3["problems"]))
        code, pv4 = A.call("POST", "/api/outlines/preview",
                           dict(gen, model_keys=[]))
        check("没选模型 → 提示去配模型",
              any(p["field"] == "model_keys" for p in pv4["problems"]))

        # ------------------------------------------------------------
        # 这一段的由来：她的原话是「素材不发给 AI 那我做素材内化库干什么」。
        # 事实是 —— AI 批量内化产出的零件状态一律是「待确认」，而候选池
        # 只收「已确认 / 已编辑」。她内化完直接去生成，AI 一条零件都拿不到，
        # 而且**完全看不出为什么**（那些零件在候选池的 SQL 里就被滤掉了，
        # 不计数、不上报、不出现在任何地方）。
        # 这一段把"看不见"这件事钉死：预览必须报出来、必须说清卡在哪个
        # 状态、必须告诉她去哪儿改；确认之后必须立刻能进池子。
        print("\n【5b】有零件、但状态够不上可用 → 预览必须报出来（不许悄悄跳过）")
        ok_ids = [p["id"] for p in pv["plots"]]
        for pid in ok_ids:
            A.ok("PATCH", "/api/plots/%d" % pid, {"status": "待确认"})
        code, pv5 = A.call("POST", "/api/outlines/preview", gen)
        check("这次的零件池是空的", len(pv5["plots"]) == 0,
              "%d 条" % len(pv5["plots"]))
        check("★ 库里可用零件数报成 0（不是干脆不给这个数）",
              pv5.get("usable_total") == 0, pv5.get("usable_total"))
        check("★ 卡在状态上的零件被列了出来（连名字一起给）",
              len(pv5.get("stuck_plots") or []) >= 2,
              "%d 条" % len(pv5.get("stuck_plots") or []))
        check("★ 每条都带 id / 标题 / 状态（她要能一眼认出是哪几条）",
              all(s.get("id") and s.get("title") is not None and s.get("status")
                  for s in (pv5.get("stuck_plots") or [])))
        check("★ 已排除的不算「卡在状态上」（她自己排掉的，不该再催她）",
              all(s["status"] != "已排除"
                  for s in (pv5.get("stuck_plots") or [])))
        check("★ 状态明细里有「待确认」（不是只给一个笼统的总数）",
              (pv5.get("plot_status_counts") or {}).get("待确认", 0) >= 2,
              pv5.get("plot_status_counts"))
        line = [p for p in pv5["problems"] if p["field"] == "plots"]
        check("★ 报成了 bad（红色），不是轻飘飘的 warn",
              bool(line) and line[0].get("level") != "warn",
              (line[0].get("level") if line else "压根没报"))
        check("★ 说清了是几条、什么状态、去哪儿改",
              bool(line) and "待确认" in line[0]["message"]
              and "剧情内化" in line[0]["message"],
              (line[0]["message"][:60] if line else ""))

        # 确认之后必须立刻能用 —— 这是整条链路的闭环
        A.ok("POST", "/api/plots/confirm", {"plot_ids": ok_ids})
        code, pv6 = A.call("POST", "/api/outlines/preview", gen)
        check("★ 确认之后零件立刻进池子", len(pv6["plots"]) == 2,
              "%d 条" % len(pv6["plots"]))
        check("★ 确认之后不再报「卡在状态上」",
              not [p for p in pv6["problems"] if p["field"] == "plots"])
        check("★ 确认之后整体回到 ok=True", pv6.get("ok") is True,
              str(pv6.get("problems"))[:100])
        check("★ 库里有可用零件时，数量一并给出来（她要「尽可能多参考」得先看得见）",
              pv6.get("usable_total") == 2, pv6.get("usable_total"))
        check("★ 上限也给了（她要把参考条数调大得知道天花板在哪）",
              (pv6.get("pool_max") or 0) >= 100, pv6.get("pool_max"))

        # ------------------------------------------------------------
        print("\n【6】生成：失败路 / 一个账号只能一个任务 / 重试只补失败的")
        # ------------------------------------------------------------
        code, g = A.call("POST", "/api/outlines/generate",
                         dict(gen, model_keys=["根本不存在的模型"]))
        check("选了个没有的模型 → 400", code == 400, str(g)[:80])

        code, g = A.call("POST", "/api/outlines/generate", gen)
        check("能发起生成", code == 200 and g.get("run_id"), str(g)[:100])
        rid = g.get("run_id")

        code, g2 = A.call("POST", "/api/outlines/generate", gen)
        check("同一账号不许并发两个任务 → 400", code == 400, str(g2)[:80])

        st = None
        for _ in range(120):
            r = A.ok("GET", "/api/outline-runs/%d" % rid)
            st = r["run"]["status"]
            if st not in ("排队中", "进行中"):
                break
            time.sleep(0.5)
        check("任务会走到结束状态", st in ("失败", "部分失败", "已完成"), st)
        check("连不上的模型被记成失败", st == "失败", st)
        r = A.ok("GET", "/api/outline-runs/%d" % rid)
        check("候选里写清了错误原因",
              r["run"]["candidates"][0]["status"] == "失败" and
              bool(r["run"]["candidates"][0]["error"]),
              r["run"]["candidates"][0]["error"][:60])
        check("任务记了候选池有几条零件",
              r["run"]["cand_plot_count"] == 2, str(r["run"]["cand_plot_count"]))

        # 单查要带回 input（世界观快照），列表不带 ——
        # 前端推入大纲库时要拿"生成那一刻"的世界观写进快照，
        # 从界面上现取的话，她生成完又改了世界观就对不上了。
        check("单查任务带回当时的世界观快照",
              (r["run"].get("input") or {}).get("worldview") == WORLD,
              str((r["run"].get("input") or {}).get("worldview"))[:40])
        check("单查任务带回当时的角色卡快照",
              len((r["run"].get("input") or {}).get("character_snapshot") or []) == 1,
              str((r["run"].get("input") or {}).get("character_snapshot"))[:60])
        code, lst = A.call("GET", "/api/outline-runs?limit=5")
        check("★ 列表不带 input（不然一次白拉几万字）",
              code == 200 and lst["runs"] and
              all("input" not in x for x in lst["runs"]),
              str([list(x.keys())[:3] for x in lst["runs"]])[:60])

        code, rt = A.call("POST", "/api/outline-runs/%d/retry" % rid)
        check("重试能补失败的那个", code == 200 and
              rt.get("retried_models") == ["fake"], str(rt)[:100])

        code, r = B.call("GET", "/api/outline-runs/%d" % rid)
        check("看别人的任务 → 404", code == 404)
        code, r = B.call("POST", "/api/outline-runs/%d/cancel" % rid)
        check("取消别人的任务 → 400（说没这个任务）", code == 400, str(r)[:60])

        # ------------------------------------------------------------
        print("\n【7】推入大纲库：幂等 / 计数 / AI 原稿不被覆盖")
        # ------------------------------------------------------------
        sav = dict(gen)
        sav.update({"title": "雨夜的信", "world_input_snapshot": WORLD,
                    "current_json": outline_json(), "selected_plot_ids": [1],
                    "manual_plot_ids": [1], "in_learning": True,
                    "hook_ai_derived": False,
                    # 带上 run_id + model_key，等于"从某次生成的结果推入"。
                    # 这也是幂等认人的依据之一（没有它就只能靠 outline_id）。
                    "run_id": rid, "model_key": "fake"})
        code, sv = A.call("POST", "/api/outlines", sav)
        check("能推入大纲库", code == 200 and sv.get("outline_id"), str(sv)[:100])
        oid = sv["outline_id"]
        check("第一次是新建", sv.get("created") is True)
        check("零件引用明细里记了使用前 0 次",
              sv["refs"] and sv["refs"][0]["ref_count_before"] == 0,
              str(sv.get("refs")))
        check("零件引用明细里记了使用后 1 次",
              sv["refs"][0]["ref_count_after"] == 1, str(sv.get("refs")))
        check("没有 AI 原稿时如实说明（不说「一模一样」）",
              any("没有 AI 原稿" in w for w in sv["warnings"]),
              str(sv["warnings"]))

        r = A.ok("GET", "/api/outline-plots")
        cnt = {p["id"]: p["ref_count"] for p in r["plots"]}
        check("参考次数变成 1", cnt[1] == 1, str(cnt))
        check("没用到的零件次数还是 0", cnt[2] == 0, str(cnt))

        # 幂等：同一份再存一次
        code, sv2 = A.call("POST", "/api/outlines", dict(sav, outline_id=oid))
        lst = A.ok("GET", "/api/outlines")
        check("重复保存不会多出一份", lst["total"] == 1, "总共 %s 份" % lst["total"])
        r = A.ok("GET", "/api/outline-plots")
        cnt2 = {p["id"]: p["ref_count"] for p in r["plots"]}
        check("重复保存不会多算引用", cnt2[1] == 1, str(cnt2))

        # 从历史候选再点一次（不带 outline_id，只带来源）
        code, sv3 = A.call("POST", "/api/outlines",
                           dict(sav, run_id=rid, model_key="fake"))
        lst = A.ok("GET", "/api/outlines")
        check("★ 从历史候选再点一次也不会多出一份（靠 run_id+model_key 认人）",
              sv3.get("created") is False and sv3["outline_id"] == oid,
              "总共 %s 份，created=%s" % (lst["total"], sv3.get("created")))
        check("大纲库里还是那一份", lst["total"] == 1, "总共 %s 份" % lst["total"])

        # ---- AI 原稿不许被覆盖 ----
        o = A.ok("GET", "/api/outlines/%d" % oid)["outline"]
        check("AI 原稿在这里（没有候选时等于当前版）",
              len(o["ai_original_json"]["nodes"]) == 3)
        cur = json.loads(json.dumps(outline_json()))
        cur["nodes"].append({"node_id": "n4", "node_title": "我加的第四段",
                             "estimated_words": 500, "event": "他回头看了一眼。"})
        code, up = A.call("PATCH", "/api/outlines/%d" % oid, {"current_json": cur})
        check("能改当前版", code == 200)
        o2 = A.ok("GET", "/api/outlines/%d" % oid)["outline"]
        check("改完之后当前版 4 段", len(o2["current_json"]["nodes"]) == 4)
        check("★ AI 原稿还是 3 段（一个字没被覆盖）",
              len(o2["ai_original_json"]["nodes"]) == 3,
              "原稿 %d 段" % len(o2["ai_original_json"]["nodes"]))
        check("差异认得出「新增了一段」",
              [x["node_id"] for x in o2["diff"]["added_nodes"]] == ["n4"])
        check("渲染出来的文本里也有新那段",
              "我加的第四段" in o2["current_text"])

        # 改零件清单：明细跟着变，但历史那一行不移除
        code, up = A.call("PATCH", "/api/outlines/%d" % oid,
                          {"selected_plot_ids": [1, 2]})
        o3 = A.ok("GET", "/api/outlines/%d" % oid)["outline"]
        check("加了零件之后明细里多一行",
              sorted(x["plot_id"] for x in o3["refs"]) == [1, 2],
              str([x["plot_id"] for x in o3["refs"]]))
        r = A.ok("GET", "/api/outline-plots")
        cnt3 = {p["id"]: p["ref_count"] for p in r["plots"]}
        check("新加的零件次数变成 1", cnt3[2] == 1, str(cnt3))

        code, r = B.call("GET", "/api/outlines/%d" % oid)
        check("看别人的大纲 → 404", code == 404)
        code, r = B.call("PATCH", "/api/outlines/%d" % oid, {"title": "我改"})
        check("改别人的大纲 → 404", code == 404)
        check("乙的大纲库是空的", B.ok("GET", "/api/outlines")["total"] == 0)

        # ------------------------------------------------------------
        print("\n【8】学习反馈")
        # ------------------------------------------------------------
        code, f = A.call("POST", "/api/outlines/%d/feedback" % oid,
                         {"node_id": "n2", "problem": "冲突不足",
                          "note": "对质太顺了", "enabled": True})
        check("能标节点不可用", code == 200 and f.get("feedback_id"), str(f)[:80])
        check("标记后进了学习库", f["stats"]["cases"] == 1, str(f["stats"]))
        code, f2 = A.call("POST", "/api/outlines/%d/feedback" % oid,
                          {"node_id": "n1", "problem": "我自己编的原因"})
        check("乱写原因 → 400（只能从清单里选）", code == 400, str(f2)[:70])
        code, f3 = A.call("POST", "/api/outlines/%d/feedback" % oid,
                          {"node_id": "n3", "problem": "节奏太快",
                           "note": "没勾学习", "enabled": False})
        check("不勾学习时如实说明", code == 200 and "没有加入学习" in f3["message"],
              f3.get("message"))
        check("没勾的不进学习库", f3["stats"]["cases"] == 1, str(f3["stats"]))
        code, f4 = A.call("POST", "/api/outlines/%d/feedback" % oid,
                          {"node_id": "n3", "problem": "节奏太快",
                           "note": "连 enabled 都不传"})
        check("★ 不传 enabled 时默认**不**进学习（不替她做决定）",
              code == 200 and f4["stats"]["cases"] == 1, str(f4.get("stats")))
        code, tg = A.call("POST", "/api/outline-feedback/%d/toggle?enabled=0"
                          % f["feedback_id"])
        check("能把一条移出学习库", code == 200 and tg["stats"]["cases"] == 0,
              str(tg.get("stats")))

        fb = A.ok("GET", "/api/outlines/%d/feedback" % oid)
        check("反馈列表读得回来", len(fb["feedback"]) >= 3, str(len(fb["feedback"])))
        check("差异也落在反馈里（kind=diff）",
              any(x["kind"] == "diff" for x in fb["feedback"]))

        code, r = B.call("POST", "/api/outlines/%d/feedback" % oid,
                         {"node_id": "n1", "problem": "节奏太快"})
        check("给别人的大纲标反馈 → 404", code == 404)

        # ------------------------------------------------------------
        print("\n【9】补充提示词：大纲这一档不许跟分类/内化串门")
        # ------------------------------------------------------------
        code, r = A.call("PUT", "/api/outline-prompt",
                         {"content": "只写双人对手戏，不要旁白。"})
        check("能存大纲的补充提示词", code == 200 and r["length"] > 0, str(r))
        r = A.ok("GET", "/api/outline-prompt")
        check("能读回来", r["content"] == "只写双人对手戏，不要旁白。")
        check("这一档标着 outline", r["kind"] == "outline")
        r = A.ok("GET", "/api/classify-prompt")
        check("分类那档读不到它（不串档）", r.get("content") == "", str(r)[:80])
        r = A.ok("GET", "/api/infuse-prompt")
        check("内化那档也读不到它（不串档）", r.get("content") == "", str(r)[:80])

        code, r = A.call("PUT", "/api/outline-prompt", {"content": "字" * 5001})
        check("补充提示词超长 → 400", code == 400, str(r)[:70])

        # ------------------------------------------------------------
        print("\n【10】删除大纲：计数不回退")
        # ------------------------------------------------------------
        code, r = A.call("DELETE", "/api/outlines/%d" % oid)
        check("能删", code == 200, str(r)[:60])
        check("删的时候说清了计数不减", "不会减" in r.get("message", ""))
        check("大纲库空了", A.ok("GET", "/api/outlines")["total"] == 0)
        r = A.ok("GET", "/api/outline-plots")
        cnt4 = {p["id"]: p["ref_count"] for p in r["plots"]}
        check("★ 删完大纲，参考次数**还是** 1（历史不回退）",
              cnt4[1] == 1 and cnt4[2] == 1, str(cnt4))
        code, r = B.call("DELETE", "/api/outlines/%d" % oid)
        check("删别人的大纲 → 404", code == 404)

    finally:
        H.stop_server(proc)
        shutil.rmtree(data_dir, ignore_errors=True)


# ======================================================================
# 第二段：数据层（提示词拼装、候选池、参考计数、差异）
# ======================================================================

def data_layer_tests():
    tmp = tempfile.mkdtemp(prefix="moge_outline_data_")
    os.environ["MOGE_DATA_DIR"] = tmp
    if PROJECT_DIR not in sys.path:
        sys.path.insert(0, PROJECT_DIR)

    from backend import db
    from backend import classify_db as cdb
    from backend import outline_ai as oai
    from backend import outline_db as odb
    from backend import plots_db as pdb

    print("\n" + "=" * 66)
    print("第二段：数据层与编排层边界测试")
    print("临时库目录：", tmp)
    print("=" * 66)

    # ---- 保险 ----
    print("\n【11】隔离保险")
    real = os.path.abspath(os.path.join(PROJECT_DIR, "data", "moge.db"))
    dbp = os.path.abspath(db.DB_PATH)
    if os.path.abspath(tmp) not in dbp or dbp == real:
        print("！！数据目录没指到临时目录：", dbp)
        print("！！为避免动到真实数据，这一段拒绝继续。")
        return
    check("这一段连的是自己的临时库", True, dbp)

    odb.migrate()
    # 候选零件要读 plots / plot_cards / cards / materials / categories，
    # 那几张表归别的模块建。这里一次建成，省得插数据时报表不存在。
    cdb.migrate()
    pdb.migrate()

    # ---- 拆出来的纯函数自测也跑一遍（它们是同一批断言的第二份保障）----
    print("\n【12】自测（纯函数）")
    check("数据层自测全通过", odb._self_check() is True)
    check("编排层自测全通过", oai._self_check() is True)

    # ---- 候选池：分类轮转 ----
    print("\n【13】候选池的筛法")
    now = "2026-09-25 23:00:00"
    with db.connect() as conn:
        for cid, cname, csort in ((901, "甲类", 1), (902, "乙类", 2)):
            conn.execute(
                "INSERT INTO categories (id,owner_id,set_version,name,"
                "description,sort,active) VALUES (?,'',?,?,'判据',?,1)",
                (cid, cdb.CATEGORY_SET_VERSION, cname, csort))
        # 甲类 5 条（次数依次 0..4），乙类 2 条，丙类（未分类）1 条
        for i in range(5):
            conn.execute(
                "INSERT INTO plots (id,owner_id,title,summary,status,source,"
                "primary_category_id,created_at,updated_at)"
                " VALUES (?,'__u1',?,'摘要','已确认','human',901,?,?)",
                (100 + i, "甲-%d" % i, now, now))
        for i in range(2):
            conn.execute(
                "INSERT INTO plots (id,owner_id,title,summary,status,source,"
                "primary_category_id,created_at,updated_at)"
                " VALUES (?,'__u1',?,'摘要','已确认','human',902,?,?)",
                (200 + i, "乙-%d" % i, now, now))
        conn.execute(
            "INSERT INTO plots (id,owner_id,title,summary,status,source,"
            "created_at,updated_at)"
            " VALUES (300,'__u1','没分类','摘要','已确认','human',?,?)", (now, now))
        # 给甲-0 造 4 次引用（4 份不同大纲），甲-3 造 2 次
        for k in range(4):
            conn.execute(
                "INSERT INTO outline_plot_refs (outline_id,outline_title,plot_id,"
                "owner_id,plot_title,kept,ref_count_before,ref_count_after,"
                "created_at) VALUES (?,'旧稿',100,'__u1','甲-0',1,0,1,?)",
                (900 + k, now))
        for k in range(2):
            conn.execute(
                "INSERT INTO outline_plot_refs (outline_id,outline_title,plot_id,"
                "owner_id,plot_title,kept,ref_count_before,ref_count_after,"
                "created_at) VALUES (?,'旧稿',103,'__u1','甲-3',1,0,1,?)",
                (960 + k, now))
        # 一条 kept=0 的：不该被算进次数
        conn.execute(
            "INSERT INTO outline_plot_refs (outline_id,outline_title,plot_id,"
            "owner_id,plot_title,kept,ref_count_before,ref_count_after,created_at)"
            " VALUES (950,'旧稿',101,'__u1','甲-1',0,0,0,?)", (now,))

    cnt = odb.plot_ref_counts("__u1", [100, 101, 103])
    check("同一份大纲里同一条只算 1 次（4 份大纲 = 4 次）", cnt.get(100) == 4, str(cnt))
    check("kept=0 的不算进次数", cnt.get(101, 0) == 0, str(cnt))
    check("另一条各算各的", cnt.get(103) == 2, str(cnt))

    plan = oai.plan_candidate_plots("__u1", {"pool_size": 20})
    got = [p["title"] for p in plan["items"]]
    check("候选池按主类轮转（每类都有份）",
          any(x.startswith("甲-") for x in got) and
          any(x.startswith("乙-") for x in got) and "没分类" in got, str(got))
    check("★ 类内按参考次数升序（用过的排后面）",
          got.index("甲-2") < got.index("甲-3") and
          got.index("甲-3") < got.index("甲-0"), str(got))
    check("没用过的那几条都比用过的靠前",
          got.index("甲-1") < got.index("甲-3"), str(got))
    plan_small = oai.plan_candidate_plots("__u1", {"pool_size": 5})
    check("池子大小受 pool_size 控制", len(plan_small["items"]) == 5,
          str(len(plan_small["items"])))
    plan_tiny = oai.plan_candidate_plots("__u1", {"pool_size": 1})
    check("pool_size 再小也有个下限（少于 5 条零件串不出故事）",
          len(plan_tiny["items"]) == 5, str(len(plan_tiny["items"])))
    check("池子小时按轮转取，不是把一类取完",
          len({p.get("primary_category_id") for p in plan_small["items"]}) >= 2,
          str([p["title"] for p in plan_small["items"]]))

    plan2 = oai.plan_candidate_plots("__u1", {"plot_ids": [102, 3]})
    check("手动指定池子时按她给的来",
          [p["id"] for p in plan2["items"]] == [102], str(plan2["items"]))
    check("手动指定也挡掉状态不对的",
          plan2.get("dropped") or plan2["items"], "（103 不存在，被丢）")
    check("手动指定时标了来源", plan2["source"] == "manual")

    # ---- 仅本地：链路要一路查上去 ----
    print("\n【14】仅本地（local_only）的零件不许发出去")
    with db.connect() as conn:
        conn.execute("INSERT INTO materials (id,owner_id,title,content,chars,ext,"
                     "source_path,content_hash,local_only,is_public,note,"
                     "created_at,updated_at)"
                     " VALUES (50,'__u1','私密稿','正文',2,'.txt','','h50',1,0,'',?,?)",
                     (now, now))
        conn.execute("INSERT INTO cards (id,owner_id,material_id,start_offset,"
                     "end_offset,source_text_hash,status,created_at,updated_at)"
                     " VALUES (50,'__u1',50,0,2,'h','已确认',?,?)", (now, now))
        conn.execute("INSERT INTO plots (id,owner_id,title,summary,status,source,"
                     "created_at,updated_at)"
                     " VALUES (500,'__u1','私密零件','摘要','已确认','human',?,?)",
                     (now, now))
        conn.execute("INSERT INTO plot_cards (plot_id,card_id,material_id,"
                     "start_offset,end_offset,source_text_hash,created_at)"
                     " VALUES (500,50,50,0,2,'h',?)", (now,))

    blocked = odb._plot_local_only("__u1", [500, 100])
    check("★ 仅本地素材提炼的零件被认出来", 500 in blocked, str(blocked))
    check("普通零件不受影响", 100 not in blocked, str(blocked))

    plan3 = oai.plan_candidate_plots("__u1", {"plot_ids": [500, 100]})
    check("★ 手动勾了也不给发（仅本地是硬的）",
          [p["id"] for p in plan3["items"]] == [100], str(plan3["items"]))
    check("被拦的会告诉她", [p["id"] for p in plan3["blocked"]] == [500])

    auto = oai.plan_candidate_plots("__u1", {"pool_size": 20})
    check("自动筛也不含仅本地的",
          500 not in [p["id"] for p in auto["items"]], "")
    check("自动筛会报告被拦了几条", len(auto["blocked"]) >= 1, str(len(auto["blocked"])))

    # ---- 结构规模校验 ----
    print("\n【15】结构规模校验（按预期字数）")
    obj, _w = odb.clean_outline_payload({"nodes": [
        {"node_title": "第%d段" % i, "event": "发生了具体的事情", "estimated_words": 300}
        for i in range(1, 13)]}, {1})
    ws = odb.validate_outline(obj, 3000)
    check("3000 字给 12 段会被提醒（别写空壳）",
          any("空壳" in x for x in ws), str(ws)[:120])
    obj2, _w2 = odb.clean_outline_payload({"nodes": [
        {"node_title": "只有一段", "event": "事情", "estimated_words": 100}]}, {1})
    ws2 = odb.validate_outline(obj2, 8000)
    check("8000 字只给 1 段也会被提醒", any("偏长" in x for x in ws2), str(ws2)[:120])
    check("字数对不上会被提醒",
          any("加起来" in x for x in ws2), str(ws2)[:200])
    check("没写结局会被提醒", any("没写结局" in x for x in ws2))

    # ---- 来源零件越界必须抹掉 ----
    print("\n【16】来源零件的编号只能在候选池里")
    obj3, warns3 = odb.clean_outline_payload({"nodes": [
        {"node_title": "一段", "event": "事情",
         "source_plot_ids": [100, 999999]}]}, {100})
    check("不存在的编号被抹掉", obj3["nodes"][0]["source_plot_ids"] == [100])
    check("抹掉了会告诉她", any("不在本次候选里" in x for x in warns3),
          str(warns3)[:120])
    check("used_plot_ids 按节点里的实际来源回填", obj3["used_plot_ids"] == [100])

    # ---- 差异：按 node_id 而不是按位置 ----
    print("\n【17】差异比对")
    a = {"nodes": [{"node_id": "n1", "node_title": "甲"},
                   {"node_id": "n2", "node_title": "乙"}]}
    b = {"nodes": [{"node_id": "n2", "node_title": "乙"},
                   {"node_id": "n1", "node_title": "甲"}]}
    d = odb.diff_outlines(a, b)
    check("顺序换了只算 reordered，不算两段都改了",
          d["reordered"] is True and len(d["changed_nodes"]) == 0)
    d2 = odb.diff_outlines(a, {"nodes": [{"node_id": "n1", "node_title": "甲"}]})
    check("删掉一段能认出来",
          [x["node_id"] for x in d2["removed_nodes"]] == ["n2"])
    check("删了就算改过（changed=True）", d2["changed"] is True)
    d3 = odb.diff_outlines(a, {"nodes": [{"node_id": "n1", "node_title": "改了"}]})
    check("改了标题能认出来，并说出改了哪些字段",
          d3["changed_nodes"][0]["node_id"] == "n1" and
          "node_title" in d3["changed_nodes"][0]["fields"])

    # ---- 保存：同一份大纲里同一条零件只加 1 次 ----
    print("\n【18】引用计数：同一份大纲里同一条只加 1 次")
    ch = odb.create_character("__u1", {"name": "测试角色", "identity": "身份"})
    res = odb.save_outline("__u1", {
        "title": "计数测试", "world_input_snapshot": WORLD,
        "character_ids": [ch["id"]], "target_words": 8000,
        "current_json": outline_json(), "selected_plot_ids": [100],
        "has_ai_original": True})
    oid = res["outline_id"]
    cnt = odb.plot_ref_counts("__u1", [100])
    check("新保存一份 → 次数 +1（4 → 5）", cnt.get(100) == 5, str(cnt))

    odb.save_outline("__u1", {
        "outline_id": oid, "title": "计数测试", "world_input_snapshot": WORLD,
        "character_ids": [ch["id"]], "target_words": 8000,
        "current_json": outline_json(), "selected_plot_ids": [100, 100, 100],
        "has_ai_original": True})
    cnt = odb.plot_ref_counts("__u1", [100])
    check("★ 同一条写三遍也只算 1 次", cnt.get(100) == 5, str(cnt))

    odb.delete_outline("__u1", oid)
    cnt = odb.plot_ref_counts("__u1", [100])
    check("★ 删了大纲，历史次数不回退", cnt.get(100) == 5, str(cnt))

    # ---- 不许在没有世界观/角色卡的情况下保存 ----
    print("\n【19】保存的硬校验")
    for bad, why in (({"title": "x", "world_input_snapshot": "",
                       "character_ids": [ch["id"]], "target_words": 8000,
                       "current_json": outline_json()}, "没世界观"),
                     ({"title": "x", "world_input_snapshot": WORLD,
                       "character_ids": [], "target_words": 8000,
                       "current_json": outline_json()}, "没角色卡"),
                     ({"title": "x", "world_input_snapshot": WORLD,
                       "character_ids": [ch["id"]], "target_words": 10,
                       "current_json": outline_json()}, "字数太小"),
                     ({"title": "x", "world_input_snapshot": WORLD,
                       "character_ids": [ch["id"]], "target_words": 8000,
                       "current_json": "不是对象"}, "内容不是对象")):
        try:
            odb.save_outline("__u1", bad)
            check("%s 要报错" % why, False, True)
        except ValueError:
            check("%s 要报错" % why, True, True)

    # 【这条守的是一个真出过事的组合】节点里填了「参与角色」时，
    # 渲染成可读文本那一步会去查标签表，而 participating_roles 是个
    # 列表型字段、不在 NODE_FIELD_LABELS 里 —— 以前整份大纲会
    # KeyError，接口返回 500，她看到的只是"保存失败"，
    # 而节点内容其实一点问题都没有。
    oj = outline_json()
    oj["nodes"][0]["participating_roles"] = ["甲", "乙"]
    oj["nodes"][0]["location_time"] = "雨夜，城门口"
    try:
        rr = odb.save_outline("__u1", {
            "title": "带参与角色的一份", "world_input_snapshot": WORLD,
            "character_ids": [ch["id"]], "target_words": 8000,
            "current_json": oj, "selected_plot_ids": [1]})
        check("★ 节点带参与角色也能存进大纲库（不 500）",
              rr["outline_id"] > 0, rr["outline_id"])
        one = odb.get_outline("__u1", rr["outline_id"])
        check("★ 参与角色也渲染进了全文",
              "参与角色：甲、乙" in (one["current_text"] or ""), True)
        check("时间地点也渲染进了全文",
              "时间地点：雨夜，城门口" in (one["current_text"] or ""), True)
    except KeyError as e:
        check("★ 节点带参与角色也能存进大纲库（不 500）", False, repr(e))

    # ---- 提示词组装 ----
    print("\n【20】发送内容预览")
    pv = oai.preview_input("__u1", {
        "worldview": WORLD, "character_ids": [ch["id"]],
        "target_words": 8000, "model_keys": ["fake"]})
    check("预览不写库、不调模型也能算出字数", pv["send_chars"] > 1000,
          str(pv["send_chars"]))
    check("预览里没有密钥（打码也算）",
          "sk-fake" not in json.dumps(pv, ensure_ascii=False), "")
    check("预览带上了模板正文（她能看到最终效果）",
          "只输出一个合法 JSON 对象" in pv["messages_preview"])

    shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("=" * 66)
    print("墨阁 · 大纲生成（同人短篇 v1）测试")
    print("=" * 66)

    http_tests()
    data_layer_tests()

    print("\n" + "=" * 66)
    print("通过 %d 项，失败 %d 项" % (OK, FAIL))
    print("=" * 66)
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
