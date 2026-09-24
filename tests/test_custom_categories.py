# -*- coding: utf-8 -*-
"""
墨阁 · 自己建的主类 / 小标签 测试
========================================================
背景（她的原话）：
    「允许用户自行创建新的主分类和小标签」

于是多了三件以前没有的事，这个文件就是守住它们：

    ① 主类可以自己建 —— 但**只有自己看得见**（分类体系是一个人的私人语法，
       她的「梗」和别人的「梗」未必是同一件事）。系统那 11 个仍全站共用。
    ② 建出来的主类必须真的能用 —— 也就是**判据要进发给模型的那段提示词**。
       不然她建完、跑一轮、发现一类都没判出来，会以为是模型不行。
    ③ 老库要能升上来 —— categories 表原来是 UNIQUE(set_version, name)
       （全站只许有一个「情感」），必须整容成带 owner_id 的版本。
       她库里那 793 张卡片都存着 primary_category_id，整容时一个都不许错位。

还顺带守住"卡片流按主分类分段"（她要求素材分类库汇总、只按主类归类）——
分段顺序必须跟着主类的 sort 走，未分类排在最后。

怎么跑（不需要先启动墨阁）：

    cd G:\\docker\\moge
    .venv\\Scripts\\python.exe tests\\test_custom_categories.py

分三段，前两段各自用独立的临时库，第三段自己起服务：

    第一段（HTTP）：临时库 + 随机端口起服务，验权限与报错文案
    第二段（数据层）：临时库，直接调函数 —— 整容迁移、边界、判据是否真的发出去
    第三段（分段）：同一个临时库里插几张卡，验 order=category 的顺序

**真实数据从头到尾不会被碰。** 每段开头都有一道"用的不是临时库就拒绝继续"的保险 ——
这条规矩是被"测试误删过 12 条真实素材"换来的。
"""

import hashlib
import json
import os
import shutil
import socket
import sqlite3
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
# 复用自动分类那套测试脚手架（起服务、Cookie 客户端、等就绪）。
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


# 老结构（没有 owner_id，UNIQUE 是 set_version + name）。
# 这段 SQL 是**照着升级前的代码抄的**，别改 —— 改了就测不出真升级路径。
OLD_CATEGORIES_SCHEMA = """
CREATE TABLE categories (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    set_version    TEXT    NOT NULL DEFAULT 'v1',
    name           TEXT    NOT NULL,
    description    TEXT    NOT NULL DEFAULT '',
    aliases        TEXT    NOT NULL DEFAULT '',
    suggested_tags TEXT    NOT NULL DEFAULT '[]',
    parent_id      INTEGER DEFAULT NULL,
    sort           INTEGER NOT NULL DEFAULT 0,
    active         INTEGER NOT NULL DEFAULT 1,
    UNIQUE (set_version, name)
);
"""


# ======================================================================
# 第一段：HTTP（权限 + 报错文案）
# ======================================================================

def http_tests():
    data_dir = tempfile.mkdtemp(prefix="moge_cc_http_")
    proc = None
    try:
        port = H.free_port()
        base = "http://127.0.0.1:%d" % port
        print("=" * 66)
        print("第一段：HTTP 接口测试（自己建主类 / 小标签）")
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

        A, B = H.Client(base), H.Client(base)
        suffix = hashlib.md5(tempfile.mkdtemp().encode()).hexdigest()[:6]
        A.ok("POST", "/api/auth/register",
             {"username": "甲作者" + suffix, "password": "pw123456",
              "password2": "pw123456"})
        B.ok("POST", "/api/auth/register",
             {"username": "乙作者" + suffix, "password": "pw123456",
              "password2": "pw123456"})

        print("\n【1】建主类")
        opt0 = A.ok("GET", "/api/categories")
        n_sys = len(opt0["categories"])
        check("系统自带 11 个主类", n_sys == 11, "得到 %d" % n_sys)
        check("系统类的 mine 是 false",
              all(c.get("mine") is False for c in opt0["categories"]))

        r = A.ok("POST", "/api/categories",
                 {"name": "  打戏节奏  ", "description": "这一段是在过招，重点是谁占了上风。",
                  "suggested_tags": ["交手"]})
        new_id = r["category"]["id"]
        check("建好了，名字两头的空格被去掉", r["category"]["name"] == "打戏节奏",
              repr(r["category"]["name"]))
        check("返回里直接带了新清单（界面不用再发一次请求）",
              any(c["id"] == new_id for c in r["categories"]), True)
        check("自建的标了 mine=true",
              [c["mine"] for c in r["categories"] if c["id"] == new_id] == [True], True)
        check("自动排到系统类后面（挨着「＋新建」按钮）",
              [c["id"] for c in r["categories"]][-1] == new_id, True)

        print("\n【2】该被拒的四种")
        code, d = A.call("POST", "/api/categories",
                         {"name": "外貌", "description": "想跟系统撞名"})
        check("撞系统的类名 → 400，并告诉她直接用那一条",
              code == 400 and "系统里已经有" in d.get("detail", ""), str(d)[:80])
        code, d = A.call("POST", "/api/categories",
                         {"name": "打戏节奏", "description": "重复"})
        check("跟自己已有的撞名 → 400", code == 400 and "你已经有一条" in d.get("detail", ""),
              str(d)[:70])
        code, d = A.call("POST", "/api/categories",
                         {"name": "空判据的类", "description": "   "})
        check("判据空着 → 400，并说清为什么必须写",
              code == 400 and "判据" in d.get("detail", ""), str(d)[:70])
        code, d = A.call("POST", "/api/categories",
                         {"name": "名" * 21, "description": "太长"})
        check("名字超长 → 400", code == 400 and "最长 20" in d.get("detail", ""),
              str(d)[:70])

        print("\n【3】跨账号：看不见，也动不了")
        optB = B.ok("GET", "/api/categories")
        check("乙看不到甲自建的类", all(c["id"] != new_id for c in optB["categories"]),
              True)
        check("乙那份清单还是 11 个（系统类不受影响）",
              len(optB["categories"]) == 11, "得到 %d" % len(optB["categories"]))
        rb = B.ok("POST", "/api/categories",
                  {"name": "打戏节奏", "description": "乙自己的判据，跟甲不是一回事"})
        check("乙能建一个同名的（各人的命名空间互不打扰）",
              rb["category"]["id"] != new_id, "甲 %s / 乙 %s" % (new_id, rb["category"]["id"]))

        code, d = B.call("DELETE", "/api/categories/%d" % new_id)
        check("乙删甲的 → 404", code == 404, "%s %s" % (code, str(d)[:60]))
        sys_id = opt0["categories"][0]["id"]
        code, d = A.call("DELETE", "/api/categories/%d" % sys_id)
        check("连甲自己都删不动系统类 → 404",
              code == 404 and "系统自带" in d.get("detail", ""), str(d)[:70])

        print("\n【4】停用自己建的")
        ra = A.ok("DELETE", "/api/categories/%d" % new_id)
        check("停用成功，清单里没了",
              all(c["id"] != new_id for c in ra["categories"]), True)
        check("总键数回到 11", len(A.ok("GET", "/api/categories")["categories"]) == 11, True)

        print("\n【5】小标签（走的是原有那条接口）")
        rt = A.ok("POST", "/api/sub-tags", {"name": "追妻火葬场", "action": "add"})
        check("新增标签", "追妻火葬场" in rt.get("message", ""), rt.get("message", ""))
        names = [t["name"] for t in A.ok("GET", "/api/categories")["sub_tags"]]
        check("新增的标签出现在选项里", "追妻火葬场" in names, True)
        rt = A.ok("POST", "/api/sub-tags", {"name": "追妻火葬场", "action": "deactivate"})
        names = [t["name"] for t in A.ok("GET", "/api/categories")["sub_tags"]]
        check("停用之后不再出现在选项里", "追妻火葬场" not in names, True)
        code, d = A.call("POST", "/api/sub-tags", {"name": "标" * 21, "action": "add"})
        check("标签名超长 → 400", code == 400 and "最长 20" in d.get("detail", ""),
              str(d)[:70])
        code, d = B.call("GET", "/api/categories")
        names_b = [t["name"] for t in d["sub_tags"]]
        check("乙的标签里没有甲的（标签本来就是各人一份）",
              "追妻火葬场" not in names_b, True)
    finally:
        H.stop_server(proc)
        shutil.rmtree(data_dir, ignore_errors=True)


# ======================================================================
# 第二段：数据层（整容迁移 + 边界 + 判据真的发出去）
# ======================================================================

def data_layer_tests():
    data_dir = tempfile.mkdtemp(prefix="moge_cc_data_")
    dbp = os.path.join(data_dir, "moge.db")

    # ---- 先手搓一个"升级前"的库：老结构的 categories + 指向它的卡片 ----
    con = sqlite3.connect(dbp)
    con.executescript(OLD_CATEGORIES_SCHEMA)
    con.execute("INSERT INTO categories (id,set_version,name,description,sort,active)"
                " VALUES (1,'v2','外貌','老版本的描述',0,1)")
    con.execute("INSERT INTO categories (id,set_version,name,description,sort,active)"
                " VALUES (2,'v2','神态','老版本的描述',1,1)")
    con.execute("INSERT INTO categories (id,set_version,name,description,sort,active)"
                " VALUES (7,'v1','情绪心理','上一版体系留下来的',0,1)")
    con.commit()
    con.close()

    os.environ["MOGE_DATA_DIR"] = data_dir
    from backend import db, classify_db as cls, classification as auto

    if os.path.abspath(data_dir) not in os.path.abspath(db.DB_PATH):
        print("！！不是临时库：", db.DB_PATH)
        print("！！为避免动到真实数据，测试拒绝继续。")
        return

    print("\n" + "=" * 66)
    print("第二段：数据层（整容迁移 / 边界 / 判据进提示词）")
    print("临时库目录：", data_dir)
    print("=" * 66)

    print("\n【6】老库整容")
    rep = cls.migrate(verbose=False)
    check("识别出老结构并整容了", rep["categories_rebuilt"] == "categories",
          str(rep["categories_rebuilt"]))
    check("动手之前先备份了整个库",
          bool(rep["backup"]) and os.path.exists(os.path.join(data_dir, rep["backup"])),
          str(rep["backup"]))
    with db.connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(categories)")}
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='categories'"
                           ).fetchone()[0]
    check("新结构带 owner_id", "owner_id" in cols, sorted(cols)[:4])
    check("★ UNIQUE 改成带 owner_id 的（否则两个账号建不了同名类）",
          "UNIQUE (owner_id, set_version, name)" in sql, True)

    allc = {c["name"]: c for c in cls.all_categories()}
    check("老 id 原样保留（卡片上存的数字不搬家）",
          allc["外貌"]["id"] == 1 and allc["神态"]["id"] == 2,
          "外貌=%s 神态=%s" % (allc["外貌"]["id"], allc["神态"]["id"]))
    check("系统自带那 11 个补齐了",
          sum(1 for c in allc.values()
              if c["set_version"] == cls.CATEGORY_SET_VERSION
              and c["owner_id"] == "") == 11, True)
    check("v1 的旧类没被删、仍是停用（历史卡片还认得出名字）",
          allc["情绪心理"]["active"] == 0, str(allc["情绪心理"]["active"]))
    check("迁移重复跑是安全的（不会又整一次）",
          cls.migrate(verbose=False)["categories_rebuilt"] is None, True)

    # 卡片上那个 primary_category_id 还指得对吗
    m = db.save_material("测试素材甲", "第一段正文\n\n第二段正文", ".txt", owner="local")
    mid = m["id"] if isinstance(m, dict) else m
    ts = cls.now_str()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO cards (owner_id, material_id, start_offset, end_offset,"
            " primary_category_id, status, created_at, updated_at)"
            " VALUES ('local',?,0,5,1,'待确认',?,?)", (mid, ts, ts))
    items = cls.list_cards("local", order="seq", limit=10)["items"]
    check("★ 老卡片的主类还查得到名字（399 张卡不会变成空白）",
          items and items[0]["category_name"] == "外貌",
          "得到 %r" % (items[0]["category_name"] if items else None))

    print("\n【7】自己建主类")
    o1, o2 = "__u901", "__u902"
    c = cls.create_category(o1, "  打戏节奏  ", "  这一段是在过招，重点是谁占了上风。  ",
                            ["交手", "追杀"])
    check("两头空格被去掉", c["name"] == "打戏节奏" and c["description"].startswith("这一段"),
          repr(c["name"]) + " / " + repr(c["description"][:8]))
    check("标了 mine=true", c["mine"] is True)
    check("自己看得见", "打戏节奏" in [x["name"] for x in cls.list_categories(o1)], True)
    check("别人看不见", "打戏节奏" not in [x["name"] for x in cls.list_categories(o2)], True)
    check("不传 owner 只给系统那套（老口径不变）",
          len(cls.list_categories()) == 11, "得到 %d" % len(cls.list_categories()))
    check("系统类的 mine=false",
          all(x["mine"] is False for x in cls.list_categories(o1)
              if x["owner_id"] == ""), True)

    for name, desc, why in [("外貌", "想撞系统类", "撞系统类"),
                            ("打戏节奏", "重复", "撞自己已启用的"),
                            ("空判据", "   ", "判据空着"),
                            ("名" * 21, "太长", "名字超长"),
                            ("判据太长", "字" * (cls.CATEGORY_DESC_MAX + 1), "判据超长")]:
        try:
            cls.create_category(o1, name, desc)
            check("%s → 必须拒绝" % why, False, "居然建成功了")
        except ValueError as e:
            check("%s → 拒绝，理由说得清" % why, len(str(e)) > 6, str(e)[:46])

    c2 = cls.create_category(o2, "打戏节奏", "乙自己的判据")
    check("两个账号能各建一个同名的（命名空间隔离）",
          c2["id"] != c["id"], "甲 %s / 乙 %s" % (c["id"], c2["id"]))

    print("\n【8】停用 / 复活")
    check("停用自己的",
          (cls.set_category_active(o1, c["id"], False) or {}).get("active") == 0)
    check("停用后清单里没了",
          "打戏节奏" not in [x["name"] for x in cls.list_categories(o1)], True)
    check("停用系统的 → 返回 None（不许动）",
          cls.set_category_active(o1, allc["外貌"]["id"], False) is None, True)
    again = cls.create_category(o1, "打戏节奏", "改过的判据")
    check("★ 再用同名建 → 复活的是**同一条**（历史卡片上的引用跟着回来）",
          again["id"] == c["id"], "老 %s / 新 %s" % (c["id"], again["id"]))
    check("判据跟着更新了", again["description"] == "改过的判据", again["description"])
    check("复活后又能选到了",
          "打戏节奏" in [x["name"] for x in cls.list_categories(o1)], True)

    print("\n【9】小标签（数据层函数）")
    t = cls.create_sub_tag(o1, "  追妻火葬场  ")
    check("新建标签", t["name"] == "追妻火葬场" and t["revived"] is False, str(t)[:60])
    try:
        cls.create_sub_tag(o1, "追妻火葬场")
        check("已启用时再建 → 拒绝", False, "居然建成功了")
    except ValueError as e:
        check("已启用时再建 → 拒绝", "已经有一个" in str(e), str(e))
    cls.set_sub_tag_active(o1, t["id"], False)
    t2 = cls.create_sub_tag(o1, "追妻火葬场")
    check("★ 停用后再建 → 复活同一条（老卡片上的标签跟着回来）",
          t2["revived"] is True and t2["id"] == t["id"], "id=%s" % t2["id"])
    for name, why in [("", "空名"), ("标" * (cls.SUB_TAG_NAME_MAX + 1), "超长")]:
        try:
            cls.create_sub_tag(o1, name)
            check("%s → 拒绝" % why, False, "居然建成功了")
        except ValueError as e:
            check("%s → 拒绝" % why, len(str(e)) > 4, str(e)[:40])
    check("乙的标签里没有它",
          "追妻火葬场" not in [x["name"] for x in cls.list_sub_tags(o2)], True)
    check("停用别人的标签 → 返回 None",
          cls.set_sub_tag_active(o2, t["id"], False) is None, True)

    print("\n【10】★ 自建主类的判据真的进了发给模型的那段提示词")
    # 现建一条干净的来测：上面【8】把「打戏节奏」的判据改过了，
    # 拿那条测会验成一个早就过期的句子（第一版就踩了这个坑）。
    fresh = cls.create_category(
        o1, "过招节奏", "这一段是两个人在交手，重点看谁占了上风。", ["交手"])
    cats = cls.list_categories(o1)
    tags = [x["name"] for x in cls.list_sub_tags(o1, active_only=True)]
    msgs, _src, _warn = auto.build_messages(
        cats, tags, "测试素材甲", [{"card_id": 1, "text": "他一剑劈开了对方的长枪。"}])
    sysmsg = msgs[0]["content"]
    check("它的名字在判据清单里", "过招节奏" in sysmsg, True)
    check("★ 它的判据原文也进去了（模型就是靠这句判的）",
          "重点看谁占了上风" in sysmsg, True)
    check("它建议的副标签也进去了", "交手" in sysmsg, True)
    check("系统那 11 个仍然都在", all(x["name"] in sysmsg
                                     for x in cls.list_categories()), True)
    check("停用一条之后，它就不再出现在判据里（免得模型去选一个已停用的类）",
          (cls.set_category_active(o1, fresh["id"], False)
           or {}).get("active") == 0
          and "过招节奏" not in auto.build_messages(
              cls.list_categories(o1), tags, "测试素材甲",
              [{"card_id": 1, "text": "x"}])[0][0]["content"], True)

    return data_dir


# ======================================================================
# 第三段：卡片流按主分类分段
# ======================================================================

def section_order_tests(data_dir):
    """用同一个临时库继续跑。手插几张卡，验分段顺序。

    为什么不走「上传 → 切分 → 分类」那条真实链路：
    那条链路要经过切分规则和模型，任何一步改了都会让这个测试变红，
    而它想守的只有一件事 —— **顺序**。所以直接把卡片按已知的主类插进去。
    """
    from backend import db, classify_db as cls

    print("\n" + "=" * 66)
    print("第三段：卡片流按主分类分段")
    print("=" * 66)

    owner = "__u903"
    m = db.save_material("测试素材乙", "甲甲\n\n乙乙\n\n丙丙", ".txt", owner=owner)
    mid = m["id"] if isinstance(m, dict) else m
    ts = cls.now_str()

    cats = cls.list_categories(owner)
    by_name = {c["name"]: c for c in cats}
    # 故意乱序插入：先「情节」（sort=10），再「外貌」（sort=0），再未分类
    plan = [("情节", 0), ("外貌", 6), ("情节", 12), (None, 18), ("外貌", 24)]
    with db.connect() as conn:
        for name, off in plan:
            cid = by_name[name]["id"] if name else None
            conn.execute(
                "INSERT INTO cards (owner_id, material_id, start_offset, end_offset,"
                " primary_category_id, status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,'待确认',?,?)",
                (owner, mid, off, off + 6, cid, ts, ts))

    r = cls.list_cards(owner, order="category", limit=50)
    seq = [(x["category_name"] or "未分类") for x in r["items"]]
    check("total = 5", r["total"] == 5, str(r["total"]))
    check("★ 同类聚在一起，顺序跟着主类的 sort 走（外貌在情节前面）",
          seq == ["外貌", "外貌", "情节", "情节", "未分类"], str(seq))
    check("★ 未分类排在最后（它是尾巴，不是一类）", seq[-1] == "未分类", str(seq))
    check("段计数跟各段实际条数对得上",
          r["cat_counts"].get(str(by_name["外貌"]["id"])) == 2
          and r["cat_counts"].get(str(by_name["情节"]["id"])) == 2
          and r["cat_counts"].get("") == 1, str(r["cat_counts"]))

    r2 = cls.list_cards(owner, order="seq", limit=50)
    check("按原文顺序那一档还是老样子（没被 JOIN 改坏）",
          [x["start_offset"] for x in r2["items"]] == [0, 6, 12, 18, 24],
          str([x["start_offset"] for x in r2["items"]]))
    r3 = cls.list_cards(owner, order="chars", limit=50)
    check("按最长排序也能跑", r3["total"] == 5, str(r3["total"]))
    r4 = cls.list_cards(owner, category_id=by_name["外貌"]["id"], order="category",
                        limit=50)
    check("按主类筛 + 分段排序同时用也没问题", r4["total"] == 2, str(r4["total"]))
    check("筛了某一类之后，段计数仍给全部类（否则其他类全变 0）",
          len(r4["cat_counts"]) == 3, str(r4["cat_counts"]))

    # 分段只影响顺序，不该动数据
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM cards WHERE owner_id=?",
                         (owner,)).fetchone()[0]
    check("分段排序不写库（只读）", n == 5, "卡片数 %d" % n)


# ======================================================================

def main():
    print("=" * 66)
    print("墨阁 · 自己建的主类 / 小标签 测试")
    print("=" * 66)

    # 第一段不 import backend（服务是子进程起的），先把 HTTP 跑掉；
    # 第二段才 import backend —— 它一进来就把 db.DB_PATH 定死成当时的
    # MOGE_DATA_DIR，所以顺序不能反（反了就会连上真实的 data/moge.db）。
    http_tests()
    data_dir = data_layer_tests()
    if data_dir:
        section_order_tests(data_dir)
        shutil.rmtree(data_dir, ignore_errors=True)

    print("\n" + "=" * 66)
    print("通过 %d 项，失败 %d 项" % (OK, FAIL))
    print("=" * 66)
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
