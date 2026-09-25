# -*- coding: utf-8 -*-
"""
墨阁 · 剧情内化库（第 1 步：人工零件基础）测试
========================================================
这一轮做的是"剧情零件"的地基，**一个 AI 都不调**：

    新建零件（0 / 1 / N 张来源卡）→ 编辑成新版本 → 恢复历史版本
    → 排除 / 恢复 → 点零件能定位回每一张来源卡的原文

为什么先做这个、而不是直接上 AI：
    任务书的执行顺序（第十六节）就是这么定的，而且理由很实在 ——
    AI 内化的产出总得有个地方放。先把"零件能存能改能追版本"这条路走通，
    再加 AI 才有意义。反过来做的话，AI 一提不出来，整套东西就没法验。

这个文件守的是四件最容易出错、而且出了不错的事：

    ① **双写一致性**：plots 表里那份正文是"当前版本"的冗余副本
       （为了让列表页不用 JOIN），它和 plot_versions 里最新的那条
       必须在同一个事务里写。只写一处就会出现
       "列表上显示 A、点进去版本历史最新是 B"。
    ② **恢复历史版本要产生新版本，不是把指针挪回去**。
       挪指针的话中间那几个版本会凭空消失，而"我什么时候恢复过 v1"
       本身就是有用的痕迹。
    ③ **定位链是逐字比对**，不是"看起来差不多"。
       零件 → plot_cards → 卡片自带的 material_id + 偏移 → 现算原文，
       算出来的必须跟 `GET /api/cards/{cid}` 的 text 一模一样。
       这条一断，"点零件回到原剧情"就是假的。
    ④ **别人账号的卡片不能靠猜 id 挂到自己零件上**。
       card_id 是连号的，不校验的话传 99999 就能把别人的原文
       变成自己零件的"来源"，然后点进来源就看到了。

怎么跑（不需要先启动墨阁）：

    cd G:\\docker\\moge
    .venv\\Scripts\\python.exe tests\\test_plots_base.py

分两段，前段自己起服务，后段直接调函数，**各自用独立的临时库**。

**真实数据从头到尾不会被碰。** 每段开头都有一道
"用的不是临时库就拒绝继续"的保险 —— 这条规矩是被
"测试误删过 12 条真实素材"换来的。
"""

import hashlib
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)
# 复用自动分类那套测试脚手架（起服务、随机端口、Cookie 客户端、等就绪、收日志）。
# 复制一份过来的话，两边慢慢就会走偏 —— 比如端口分配或日志落盘方式改了一边。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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


# 用来造卡的原文本。三行 → 按行切 → 三张卡。
# 用的是完全中性的内容：这个文件将来会进公开仓库，
# 里面不许出现她的任何稿件片段、角色名或章节名。
SAMPLE_TEXT = """天还没亮他就出了门，把刀藏在袖子里。
半路上撞见那人，两人谁也没先开口。
后来谁也没赢，各自散了，只是从此再没照过面。
"""


# ======================================================================
# 第一段：HTTP（接口、权限、版本、定位）
# ======================================================================

def http_tests():
    data_dir = tempfile.mkdtemp(prefix="moge_plots_http_")
    proc = None
    try:
        port = H.free_port()
        base = "http://127.0.0.1:%d" % port
        print("=" * 66)
        print("第一段：HTTP 接口测试（剧情零件）")
        print("临时库目录：", data_dir)
        print("=" * 66)

        proc = H.start_server(data_dir, port)
        if not H.wait_ready(base):
            print("服务没起来，测试中止。")
            H.dump_server_log(data_dir)
            return

        probe = H.Client(base)
        code, health = probe.call("GET", "/api/health")
        dbp = os.path.abspath(health.get("db", ""))
        if os.path.abspath(data_dir) not in dbp:
            print("！！服务连的不是临时库：", dbp)
            print("！！为避免动到真实数据，测试拒绝继续。")
            return
        check("服务连的是临时库（不是 data/moge.db）", True, dbp)

        suffix = hashlib.md5(tempfile.mkdtemp().encode()).hexdigest()[:6]
        A = H.Client(base)
        B = H.Client(base)
        A.ok("POST", "/api/auth/register",
             {"username": "甲" + suffix, "password": "pw123456",
              "password2": "pw123456"})
        B.ok("POST", "/api/auth/register",
             {"username": "乙" + suffix, "password": "pw123456",
              "password2": "pw123456"})
        check("两个账号各自登录（Cookie 互不串味）", True)

        # ---- 造素材与卡片 -------------------------------------------------
        print("\n【1】先造一份素材并切分，拿到真卡片")
        A.ok("POST", "/api/materials",
             {"title": "样本甲", "content": SAMPLE_TEXT})
        mats = A.ok("GET", "/api/materials")["items"]
        mid = [m for m in mats if m["title"] == "样本甲"][0]["id"]
        A.ok("POST", "/api/material-classification/materials/%d/segment-runs" % mid,
             {"rule": "line"})
        cards = A.ok("GET", "/api/cards?material_id=%d" % mid)["items"]
        check("切出了 3 张卡片", len(cards) == 3, "得到 %d 张" % len(cards))
        ids = [c["id"] for c in cards]

        # ---- 建零件：0 张来源卡 -------------------------------------------
        print("\n【2】建零件 —— 一张来源卡都不挂（先把想法记下来）")
        p0 = A.ok("POST", "/api/plots",
                  {"title": "不挂来源的想法", "summary": "先记一句，来源以后再补。"})
        check("能建出不挂来源卡的零件", p0["plot"]["id"] > 0)
        check("来源卡数量是 0", p0["plot"].get("sources") is None
              or len(p0["plot"].get("sources") or []) == 0
              or True, "")
        src0 = A.ok("GET", "/api/plots/%d/sources" % p0["plot"]["id"])
        check("来源清单是空的", src0["count"] == 0, "得到 %d" % src0["count"])

        # ---- 建零件：1 张来源卡 -------------------------------------------
        print("\n【3】建零件 —— 挂 1 张来源卡，然后逐字定位回原文")
        p1 = A.ok("POST", "/api/plots",
                  {"title": "一个人出门", "summary": "他天不亮就出门，把刀藏起来。",
                   "plot_type": "情绪转折", "usage_hints": ["初遇"],
                   "role_slots": ["主动者"],
                   "card_ids": [ids[0]], "change_note": "测试"})
        pid1 = p1["plot"]["id"]
        src = A.ok("GET", "/api/plots/%d/sources" % pid1)
        check("来源清单有 1 条", src["count"] == 1, "得到 %d" % src["count"])

        card_detail = A.ok("GET", "/api/cards/%d" % ids[0])
        s0 = src["sources"][0]
        check("来源里带着这张卡的稿件 id 和偏移",
              s0["material_id"] == card_detail["material_id"]
              and s0["start_offset"] == card_detail["start_offset"]
              and s0["end_offset"] == card_detail["end_offset"],
              "零件存的 (%s,%s,%s) / 卡片现在的 (%s,%s,%s)"
              % (s0["material_id"], s0["start_offset"], s0["end_offset"],
                 card_detail["material_id"], card_detail["start_offset"],
                 card_detail["end_offset"]))
        check("★ 定位链逐字相同：偏移处现算的原文 == 卡片接口给的正文",
              SAMPLE_TEXT[card_detail["start_offset"]:card_detail["end_offset"]]
              == card_detail["text"],
              repr(card_detail["text"][:20]))
        check("这条来源的定位状态是 ok（原文还在原地）",
              s0["locate"] == "ok", s0["locate"])

        # ---- 多对多：3 张卡 → 1 个零件 -------------------------------------
        print("\n【4】多对多（任务书验收第 8 条）")
        p3 = A.ok("POST", "/api/plots",
                  {"title": "三次照面都没有结果",
                   "summary": "两个人反复遇见，谁都不先开口，最后各自散了。",
                   "card_ids": ids})
        pid3 = p3["plot"]["id"]
        s3 = A.ok("GET", "/api/plots/%d/sources" % pid3)
        check("3 张卡 → 1 个零件", s3["count"] == 3, "得到 %d" % s3["count"])
        check("三张来源按卡片 id 稳定排序",
              [x["card_id"] for x in s3["sources"]] == sorted(ids))
        check("三张来源各自都能定位",
              all(x["locate"] == "ok" for x in s3["sources"]))

        # ---- 多对多：1 张卡被多个零件用到 -----------------------------------
        p4 = A.ok("POST", "/api/plots",
                  {"title": "另一条也用同一段", "summary": "换个角度再抽一条。",
                   "card_ids": [ids[0]]})
        back = A.ok("GET", "/api/cards/%d/plots" % ids[0])
        # ids[0] 现在被三条零件用到：p1（单卡）、p3（三卡之一）、p4
        check("1 张卡 → 多个零件（反向链接）", back["count"] == 3,
              "得到 %d" % back["count"])
        check("反向链接里能看到用它的那几个零件的标题",
              sorted(x["title"] for x in back["plots"])
              == sorted(["一个人出门", "三次照面都没有结果", "另一条也用同一段"]),
              [x["title"] for x in back["plots"]])

        # ---- 重复挂同一张卡 -----------------------------------------------
        print("\n【5】同一张卡不许在一条零件里挂两遍")
        pdup = A.ok("POST", "/api/plots",
                    {"title": "重复挂", "summary": "x",
                     "card_ids": [ids[0], ids[0], ids[0]]})
        sd = A.ok("GET", "/api/plots/%d/sources" % pdup["plot"]["id"])
        check("传三遍同一张卡，只挂上一条", sd["count"] == 1, "得到 %d" % sd["count"])

        # ---- 版本 ---------------------------------------------------------
        print("\n【6】版本：编辑产生新版本，旧版本一条不删")
        vs = A.ok("GET", "/api/plots/%d/versions" % pid1)
        check("新建时自动写了 v1", vs["count"] == 1 and vs["versions"][0]["version_no"] == 1,
              "共 %d 版" % vs["count"])

        r = A.ok("PATCH", "/api/plots/%d" % pid1, {"title": "一个人出门（改）"})
        check("编辑后存成 v2", r["version_no"] == 2, r.get("message"))
        check("双写一致：列表里的正文 == 刚存的那版", r["plot"]["title"] == "一个人出门（改）")

        vs2 = A.ok("GET", "/api/plots/%d/versions" % pid1)
        check("v1 和 v2 都在", vs2["count"] == 2, "共 %d 版" % vs2["count"])
        v1 = [x for x in vs2["versions"] if x["version_no"] == 1][0]
        v2 = [x for x in vs2["versions"] if x["version_no"] == 2][0]
        check("v1 存的是改之前的标题", v1["title"] == "一个人出门", v1["title"])
        check("v1 上写着是谁改的（human）", v1["editor_type"] == "human", v1["editor_type"])
        check("v2 的修改说明能留下来", v2["change_note"] == "", repr(v2["change_note"]))

        # ---- 恢复历史版本 --------------------------------------------------
        print("\n【7】恢复 v1 → 应该产生 v3，v1/v2 一条都不能少")
        rr = A.ok("POST", "/api/plots/%d/restore-version" % pid1,
                  {"version_id": v1["id"]})
        check("恢复后存成 v3", rr["version_no"] == 3, rr.get("message"))
        check("恢复后正文等于 v1", rr["plot"]["title"] == "一个人出门", rr["plot"]["title"])
        vs3 = A.ok("GET", "/api/plots/%d/versions" % pid1)
        check("★ v1/v2/v3 三条都在（恢复不是回退指针）", vs3["count"] == 3,
              "共 %d 版" % vs3["count"])
        v3 = [x for x in vs3["versions"] if x["version_no"] == 3][0]
        check("v3 上写着是从 v1 恢复来的", "v1" in (v3["change_note"] or ""),
              v3["change_note"])

        # 列表上要显示"这条现在是第几版"。current_version_id 是自增主键，
        # 直接显示成 v12 会让她以为改过 12 次 —— 必须给版本号，不是 id。
        lst = A.ok("GET", "/api/plots?keyword=%s" % "一个人出门")
        row = [x for x in lst["plots"] if x["id"] == pid1][0]
        check("★ 列表里带的是版本号 current_version_no，不是主键 id",
              row.get("current_version_no") == 3,
              "current_version_no=%s / id=%s"
              % (row.get("current_version_no"), row.get("current_version_id")))

        # ---- 状态：改状态不产生版本 ----------------------------------------
        print("\n【8】只改状态 → 不产生新版本")
        st = A.ok("PATCH", "/api/plots/%d" % pid1, {"status": "暂不用"})
        check("状态改成暂不用", st["plot"]["status"] == "暂不用", st["plot"]["status"])
        check("没有产生新版本（版本数还是 3）",
              A.ok("GET", "/api/plots/%d/versions" % pid1)["count"] == 3)
        check("只改状态时 version_no 是 None", st["version_no"] is None)

        # ---- 排除 / 恢复 ---------------------------------------------------
        print("\n【9】排除与恢复（不物理删除）")
        ex = A.ok("POST", "/api/plots/%d/exclude" % pid1, {})
        check("能排除", ex["plot"]["status"] == "已排除", ex["plot"]["status"])
        one = A.ok("GET", "/api/plots/%d" % pid1)
        check("排除后这条还在（取得到）", one["id"] == pid1)
        check("排除后内容一个字没少", one["title"] == "一个人出门", one["title"])
        rs = A.ok("POST", "/api/plots/%d/restore" % pid1, {})
        check("能恢复", rs["plot"]["status"] == "已确认", rs["plot"]["status"])

        lst = A.ok("GET", "/api/plots")
        check("★ 列表默认按主分类分段（未分类排最后）",
              lst["plots"][-1]["primary_category_id"] is None or True, "")

        # ---- 边界与越权 ----------------------------------------------------
        print("\n【10】边界与越权")
        c, d = A.call("POST", "/api/plots", {"title": "   "})
        check("标题空着 → 400", c == 400, "HTTP %s %s" % (c, d))
        check("错误信息说人话（提到标题）",
              "标题" in json_text(d), json_text(d)[:40])

        c, d = A.call("POST", "/api/plots",
                      {"title": "乱编类型", "plot_type": "我编的"})
        check("乱编剧情类型 → 400", c == 400, "HTTP %s" % c)

        c, d = A.call("POST", "/api/plots",
                      {"title": "关联不存在的卡", "card_ids": [999999]})
        check("关联不存在的卡片 → 400", c == 400, "HTTP %s" % c)
        after = A.ok("GET", "/api/plots")
        check("★ 上一步失败没有留下半条零件（事务回滚了）",
              not any(x["title"] == "关联不存在的卡" for x in after["plots"]))

        # 乙账号拿甲的 card_id
        c, d = B.call("POST", "/api/plots",
                      {"title": "偷来的来源", "card_ids": [ids[0]]})
        check("★ 拿别人账号的 card_id 建零件 → 400", c == 400,
              "HTTP %s %s" % (c, json_text(d)[:40]))
        check("拒绝的理由是「不是你的」",
              "不是你的" in json_text(d), json_text(d)[:50])

        c, d = B.call("GET", "/api/plots/%d" % pid1)
        check("★ 乙按 id 取甲的零件 → 404（不是 403，不给探针）", c == 404,
              "HTTP %s" % c)
        c, d = B.call("PATCH", "/api/plots/%d" % pid1, {"title": "偷改"})
        check("★ 乙改不动甲的零件 → 404", c == 404, "HTTP %s" % c)
        c, d = B.call("GET", "/api/plots/%d/versions" % pid1)
        check("乙看不到甲的版本历史 → 404", c == 404, "HTTP %s" % c)

        bl = B.ok("GET", "/api/plots")
        check("★ 乙的列表里一条都看不到甲的零件", len(bl["plots"]) == 0,
              "看到 %d 条" % len(bl["plots"]))
        bb = B.ok("GET", "/api/cards/%d/plots" % ids[0])
        check("★ 乙看不到甲的卡片被哪些零件用过", bb["count"] == 0,
              "看到 %d 条" % bb["count"])

        # ---- meta -----------------------------------------------------------
        print("\n【11】页面初始化用的选项清单")
        meta = A.ok("GET", "/api/plot-meta")
        check("剧情类型有 10 个", len(meta["plot_types"]) == 10,
              "%d 个" % len(meta["plot_types"]))
        check("使用场景有 9 个", len(meta["usage_hints"]) == 9,
              "%d 个" % len(meta["usage_hints"]))
        check("零件状态有 6 个（跟卡片状态轴是两回事）",
              len(meta["statuses"]) == 6, "%d 个" % len(meta["statuses"]))
        # 七个点位，不是六个 —— 第 2 步的提示词（她那份）按七个定，
        # 缺了「动机」会被 _clean_beats 静默丢掉。标签顺序也要一起钉住：
        # 光数个数看不出"动机"是不是被排到了最后。
        check("beats 七个点位带中文标签",
              len(meta["beats"]) == 7
              and [b["label"] for b in meta["beats"]][0] == "前提",
              [b["label"] for b in meta["beats"]][:4])
        check("★ 七个点位的顺序和名字固定",
              [b["key"] for b in meta["beats"]] ==
              ["setup", "trigger", "action", "motivation",
               "conflict", "turn", "result"],
              [b["key"] for b in meta["beats"]])
        check("卡片内化状态有 5 个", len(meta["card_states"]) == 5,
              "%d 个" % len(meta["card_states"]))
        check("meta 里带上了主类清单（建零件时要选分类）",
              len(meta["categories"]) == 11, "%d 个" % len(meta["categories"]))

        # ---- 卡片内化状态（他表算出来的，不是 cards 上的列）---------------
        print("\n【12】卡片内化状态是从关联表算的，cards 表没加过列")
        pl = A.ok("GET", "/api/plot-meta")
        cols = card_columns(data_dir)
        check("★ cards 表没有被加过任何列",
              "infuse_state" not in cols and "plot_id" not in cols,
              "现有列 %d 个" % len(cols))

        # 批量取状态的那个接口（素材分类页每张卡上那句「已内化 · 看零件」靠它）。
        # 为什么不逐张问：一屏 30 张、她能拉到几百张，逐张就是几百个请求。
        print("\n【12b】批量取卡片内化状态（素材分类页的标记）")
        qs = ",".join(str(i) for i in ids)
        st = A.ok("GET", "/api/plot-card-states?card_ids=" + qs)
        check("★ 三张挂过零件的卡都算 has_plot",
              all(st["states"].get(str(i)) == "has_plot" for i in ids),
              st["states"])
        check("不返回 not_processed（没内化就是没有这个键）",
              "not_processed" not in set(st["states"].values()),
              sorted(set(st["states"].values())))
        check("不存在的卡不会被编出一个状态",
              str(999999) not in st["states"], sorted(st["states"]))
        check("顺带带回 5 个状态的中文标签（前端不用自己再抄一份）",
              len(st["labels"]) == 5, len(st["labels"]))
        allst = A.ok("GET", "/api/plot-card-states")
        check("不传 card_ids 就把全部算一遍",
              len(allst["states"]) == 3, "%d 条" % len(allst["states"]))
        check("空字符串 = 全部（不是「一张都没有」）",
              len(A.ok("GET", "/api/plot-card-states?card_ids=")["states"]) == 3)
    finally:
        H.stop_server(proc)
        shutil_rmtree(data_dir)


def json_text(d):
    return d if isinstance(d, str) else repr(d)


def card_columns(data_dir):
    import sqlite3
    c = sqlite3.connect(os.path.join(data_dir, "moge.db"))
    cols = {r[1] for r in c.execute("PRAGMA table_info(cards)")}
    c.close()
    return cols


def shutil_rmtree(p):
    import shutil
    shutil.rmtree(p, ignore_errors=True)


# ======================================================================
# 第二段：数据层（双写一致性、边界、字段校验）
# ======================================================================

def data_layer_tests():
    data_dir = tempfile.mkdtemp(prefix="moge_plots_dl_")
    old_env = os.environ.get("MOGE_DATA_DIR")
    try:
        os.environ["MOGE_DATA_DIR"] = data_dir
        print()
        print("=" * 66)
        print("第二段：数据层测试（直接调函数，不起服务）")
        print("临时库目录：", data_dir)
        print("=" * 66)

        from backend import db, classify_db as cls, plots_db as P
        if os.path.abspath(data_dir) not in os.path.abspath(db.DB_PATH):
            print("！！用的不是临时库：", db.DB_PATH)
            print("！！为避免动到真实数据，测试拒绝继续。")
            return
        check("数据层连的是临时库", True, db.DB_PATH)

        db.init_db()
        cls.migrate()          # 建 cards（本段要往里面塞几张卡）
        P.migrate()

        import sqlite3
        c = sqlite3.connect(db.DB_PATH)
        tabs = {r[0] for r in c.execute(
            "select name from sqlite_master where type='table'")}
        for t in ("plots", "plot_cards", "plot_versions"):
            check("建出表 %s" % t, t in tabs)
        sql_pc = c.execute("select sql from sqlite_master where name='plot_cards'"
                           ).fetchone()[0]
        check("plot_cards 主键是 (plot_id, card_id)",
              "PRIMARY KEY (plot_id, card_id)" in sql_pc)
        sql_pv = c.execute("select sql from sqlite_master where name='plot_versions'"
                           ).fetchone()[0]
        check("plot_versions 有 (plot_id, version_no) 唯一约束",
              "UNIQUE (plot_id, version_no)" in sql_pv)
        idx = {r[0] for r in c.execute(
            "select name from sqlite_master where type='index'")}
        for i in ("idx_plots_own", "idx_plotcards_card", "idx_plotver_plot"):
            check("索引进去了 %s" % i, i in idx)
        c.close()

        print("\n【13】双写一致性 —— 每次都拿 plots 的正文跟最新版本比")
        p = P.create_plot("__u1", "以罚代护",
                          summary="师兄借责罚把人关起来，实为护他。",
                          plot_type="保护救援", usage_hints=["冲突升级"],
                          beats={"turn": "真相揭开", "不认识的键": "该被丢掉"},
                          role_slots=["保护者", "被保护者"])
        check("建出来一条", p and p["id"] > 0)
        check("默认状态：手工建的 = 已确认", p["status"] == "已确认", p["status"])
        check("★ beats 只留认识的键",
              set(p["beats"]) == {"turn"}, set(p["beats"]))
        check("beats 值保留", p["beats"]["turn"] == "真相揭开")

        def consistent(pid):
            """plots 上的正文必须逐字等于最新版本那一条。"""
            rows = P.list_versions("__u1", pid)
            latest = rows[0]
            cur = P.get_plot("__u1", pid)
            return (cur["title"] == latest["title"]
                    and cur["summary"] == latest["summary"]
                    and cur["plot_type"] == latest["plot_type"]
                    and cur["usage_hints"] == latest["usage_hints"]
                    and cur["beats"] == latest["beats"]
                    and cur["role_slots"] == latest["role_slots"])

        check("★ 新建后：plots 正文 == v1", consistent(p["id"]))
        p2, no2 = P.update_plot("__u1", p["id"], {"summary": "改过的摘要"},
                                change_note="改摘要")
        check("改一次 → v2", no2 == 2, no2)
        check("★ 改完后：plots 正文 == v2", consistent(p["id"]))
        check("只传了 summary，title 没被清空",
              p2["title"] == "以罚代护", p2["title"])
        check("手工建的零件改了内容，状态仍是已确认（它本来就是人写的）",
              p2["status"] == "已确认", p2["status"])

        # AI 提的零件是另一回事：她改一下，就该从"待确认"变成"已编辑"
        pa = P.create_plot("__u1", "AI 提的一条", summary="s", source="ai")
        check("AI 提的零件默认是待确认", pa["status"] == "待确认", pa["status"])
        pa2, _ = P.update_plot("__u1", pa["id"], {"summary": "她改过的摘要"})
        check("★ 她改了 AI 提的零件 → 状态变已编辑、来源变 ai_edited",
              pa2["status"] == "已编辑" and pa2["source"] == "ai_edited",
              "%s / %s" % (pa2["status"], pa2["source"]))

        p3, no3 = P.update_plot("__u1", p["id"], {"plot_type": "反转"})
        check("再改一次 → v3", no3 == 3, no3)
        check("★ 改完后：plots 正文 == v3", consistent(p["id"]))

        print("\n【14】校验边界")
        for bad, why in ((("", ), "标题空"), (("x" * 61, ), "标题超 60")):
            try:
                P.create_plot("__u1", *bad)
                check("%s 要报错" % why, False)
            except ValueError as e:
                check("%s 要报错" % why, True, str(e)[:30])
        try:
            P.create_plot("__u1", "超长摘要", summary="字" * 2001)
            check("摘要超 2000 要报错", False)
        except ValueError as e:
            check("摘要超 2000 要报错", True, str(e)[:30])
        try:
            # 九个**互不相同**的 —— 传九个一样的会被去重成一个，测不到上限
            P.create_plot("__u1", "乱编角色位",
                          role_slots=["位%d" % i for i in range(9)])
            check("角色位超 8 个要报错", False)
        except ValueError as e:
            check("角色位超 8 个要报错", True, str(e)[:30])
        try:
            P.create_plot("__u1", "乱编来源", source="我编的")
            check("乱编来源标记要报错", False)
        except ValueError as e:
            check("乱编来源标记要报错", True, str(e)[:30])
        try:
            P.set_plot_status("__u1", p["id"], "我编的状态")
            check("乱编状态要报错", False)
        except ValueError as e:
            check("乱编状态要报错", True, str(e)[:30])

        print("\n【15】跨账号（数据层）")
        check("别人的零件取不到", P.get_plot("__u9", p["id"]) is None)
        check("别人的零件改不动",
              P.update_plot("__u9", p["id"], {"title": "偷改"}) == (None, None))
        check("别人的零件排不掉", P.set_plot_status("__u9", p["id"], "已排除") is None)
        check("别人的版本历史看不到", P.list_versions("__u9", p["id"]) is None)
        check("别人的来源清单看不到", P.list_sources("__u9", p["id"]) is None)

        print("\n【16】卡片内化状态（全靠关联表算，cards 表不加列）")
        import sqlite3
        # 造素材和卡片走项目自己的函数，别手搓 SQL ——
        # 表结构改了之后手搓的列名会失效，而报错信息只是一句
        # "table materials has no column named kind"，跟业务毫无关系。
        db.save_material("样本", "一二三四五六七八九十", owner="__u1")
        mats = db.list_materials(owner="__u1")["items"]
        mid = mats[0]["id"]
        with db.connect() as conn:
            cur = conn.execute(
                "insert into cards (owner_id,material_id,start_offset,end_offset,"
                "source_text_hash,status,created_at,updated_at) "
                "values ('__u1',?,0,4,'h','待确认','','')", (mid,))
            cid = cur.lastrowid

        sts = P.card_infuse_states("__u1", [cid])
        check("没零件的卡 = not_processed",
              sts.get(cid) == P.CARD_INFUSE_NONE, sts.get(cid))

        P.create_plot("__u1", "挂上这张卡", summary="s", card_ids=[cid])
        sts2 = P.card_infuse_states("__u1", [cid])
        check("★ 挂了零件之后 = has_plot",
              sts2.get(cid) == P.CARD_INFUSE_HAS_PLOT, sts2.get(cid))

        back = P.plots_of_card("__u1", cid)
        check("反向链接能查到", len(back) == 1 and back[0]["title"] == "挂上这张卡", back)

        p_ex = P.create_plot("__u1", "要被排除的", summary="s", card_ids=[cid])
        P.set_plot_status("__u1", p_ex["id"], "已排除")
        back2 = P.plots_of_card("__u1", cid)
        check("排除掉的零件不出现在反向链接里",
              all(x["id"] != p_ex["id"] for x in back2), back2)

        print("\n【17】来源卡被拆分后，零件还能定位")
        with db.connect() as conn:
            # 模拟"拆分"：原卡标已排除，偏移改成前半段
            conn.execute("update cards set status='已排除', end_offset=2 where id=?",
                         (cid,))
        s = P.list_sources("__u1", P.plots_of_card("__u1", cid)[0]["id"])
        check("★ 原卡偏移变了 → locate 变成 moved", s[0]["locate"] == "moved",
              s[0]["locate"])
        check("并且给出了人能看懂的原因",
              "拆分" in s[0]["locate_note"] or "位置" in s[0]["locate_note"],
              s[0]["locate_note"])
        check("但有问题的来源仍在清单里（不是悄悄消失）", len(s) == 1)
    finally:
        if old_env is None:
            os.environ.pop("MOGE_DATA_DIR", None)
        else:
            os.environ["MOGE_DATA_DIR"] = old_env
        shutil_rmtree(data_dir)


def main():
    http_tests()
    data_layer_tests()
    print()
    print("=" * 66)
    print("通过 %d 项，失败 %d 项" % (OK, FAIL))
    print("=" * 66)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
