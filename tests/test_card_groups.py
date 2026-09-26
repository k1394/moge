# -*- coding: utf-8 -*-
"""
墨阁 · 逻辑素材组 数据层测试
========================================================
测什么：AI 认出「这几段必须合看」之后，从落成提议到合并、到撤销，整条路。

它守的几条规矩（每一条都是"破了很难发现"的那种）：

    ① 一张卡同一时间只能属于一个组（重叠的组只收先到的）
    ② 组里的卡必须真的相邻 —— 中间夹着别的卡就不许合
    ③ 不跨文件（跨文件的区间不是连续原文，合出来是乱码）
    ④ 合并产出的正文必须等于 materials.content[start:end]
    ⑤ 被合掉的卡标「已排除」、并指回新卡（不是删掉）
    ⑥ 撤销之后：原来的卡回来、新卡退场、父子关系清干净
    ⑦ 「忽略」只动组，一张卡片都不碰
    ⑧ 别人的组看不见、也动不了
    ⑨ 全程不碰真实库

【写这个文件时踩过一个坑，值得记下来】
一开始我写的是 check("名字", got, want) —— 以为 check 会自己比较。
可 check 的第二个参数是**条件**，传一个非空字符串或非空 tuple 进去，
它永远为真，于是那条断言等于没写、测试永远绿。
所以这里所有"比较两个值"的地方一律走 same()，不许再用 check 传值。

怎么跑（不需要先启动墨阁）：

    cd G:\\docker\\moge
    .venv\\Scripts\\python.exe tests\\test_card_groups.py

跑完会打印通过/失败条数，有失败就退出码 1。
"""

import os
import shutil
import sys
import tempfile

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

# ----------------------------------------------------------------------
# 隔离：**必须在 import backend.db 之前**把数据目录指到临时目录。
#
# db.DATA_DIR 是在 import 那一刻读环境变量定下来的，
# 晚一步设置，它就已经指向她的真实库了 —— 而下面这些测试会往库里写卡片。
# 这条规矩是被"测试误删过 12 条真实素材"换来的。
# ----------------------------------------------------------------------
_TMP = tempfile.mkdtemp(prefix="moge_groups_")
os.environ["MOGE_DATA_DIR"] = _TMP

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backend import db, classify_db as cdb, segmentation as sg   # noqa: E402

OK = FAIL = 0


def check(name, cond, extra=""):
    """cond 必须是真正的布尔值。传值进来请用 same()。"""
    global OK, FAIL
    if cond:
        OK += 1
        print("  [通过] %s %s" % (name, extra))
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, extra))


def same(name, got, want):
    """比较两个值。**别用 check(名字, got, want)** —— 那个不是比较。"""
    global OK, FAIL
    if got == want:
        OK += 1
        print("  [通过] %s" % name)
    else:
        FAIL += 1
        print("  [失败] %s  得到 %r，期望 %r" % (name, got, want))


def _guard():
    """保险：一旦发现用的不是临时库，立刻停，而不是硬着头皮跑下去。"""
    real = os.path.abspath(db.DATA_DIR)
    if not real.startswith(os.path.abspath(tempfile.gettempdir())):
        print("！！数据目录不是临时目录：%s" % real)
        print("！！拒绝继续 —— 这些测试会写卡片，跑在她的真库上就完了。")
        sys.exit(2)


# ----------------------------------------------------------------------
# 造数据
# ----------------------------------------------------------------------

OWNER = "__t_groups"
OTHER = "__t_groups_other"

LINES = ["第一段的内容", "第二段的内容", "第三段的内容",
         "第四段的内容", "第五段的内容", "第六段的内容"]
TEXT = "\n".join(LINES)

CAT_NAME = "神态"          # 用库里真实存在的主类，不要自己编


def make_cards(owner, text, title):
    """存一份素材，并按行切成卡片（区间正好是每一行）。返回 (material_id, [卡id])"""
    r = db.save_material(title, text, owner=owner)
    mid = r["id"]

    offs, pos = [], 0
    for line in text.split("\n"):
        offs.append((pos, pos + len(line)))
        pos += len(line) + 1

    ids = []
    ts = db.now_str()
    with db.connect() as conn:
        for s, e in offs:
            cur = conn.execute(
                """INSERT INTO cards
                   (owner_id, material_id, start_offset, end_offset,
                    source_text_hash, segment_id, source, status,
                    operation_group_id, created_at, updated_at)
                   VALUES (?,?,?,?,?,NULL,?,?,?,?,?)""",
                (owner, mid, s, e, sg.text_hash(text[s:e]),
                 sg.SOURCE_INHERITED, sg.STATUS_PENDING, "", ts, ts))
            ids.append(cur.lastrowid)
    return mid, ids


def card_state(cid, owner=OWNER):
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM cards WHERE id=? AND owner_id=?",
                         (cid, owner)).fetchone()
        return dict(r) if r else None


def card_text(cid, owner=OWNER):
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM cards WHERE id=? AND owner_id=?",
                         (cid, owner)).fetchone()
        content = conn.execute("SELECT content FROM materials WHERE id=?",
                               (r["material_id"],)).fetchone()["content"]
        return content[r["start_offset"]:r["end_offset"]]


def group_row(gid):
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM card_groups WHERE id=?", (gid,)).fetchone()
        return dict(r) if r else None


def material_text(mid, owner=OWNER):
    with db.connect() as conn:
        return conn.execute("SELECT content FROM materials WHERE id=? AND owner_id=?",
                            (mid, owner)).fetchone()["content"]


# ======================================================================
# 一、写提议
# ======================================================================

def test_proposals():
    print("\n一、写提议")
    mid, ids = make_cards(OWNER, TEXT, "组测试素材")
    other_mid, other_ids = make_cards(OTHER, TEXT, "别人的素材")
    mid2, ids2 = make_cards(OWNER, "另外一份\n素材内容\n第三行\n", "第二份素材")

    c1, c2, c3, c4, c5, c6 = ids
    o1 = other_ids[0]
    d1 = ids2[0]

    cats = {c["name"]: c["id"] for c in cdb.list_categories(OWNER)}
    check("测试用的主类名在库里有", CAT_NAME in cats)

    res = cdb.create_group_proposals(OWNER, mid, None, [
        # 正常的两组
        {"card_ids": [c1, c2], "action": "merge",
         "primary_category": CAT_NAME, "tags": ["心动"],
         "reason": "对白与回应", "confidence": 0.92},
        {"card_ids": [c3, c4, c5], "action": "review",
         "primary_category": None, "tags": [], "reason": "边界拿不准",
         "confidence": 0.4},
        # 跟第一组抢 c2 → 整条丢掉（一张卡只能属于一个组）
        {"card_ids": [c2, c3], "action": "merge", "reason": "重叠", "confidence": 0.9},
        # 跨文件 → 丢掉
        {"card_ids": [c6, d1], "action": "merge", "reason": "跨文件", "confidence": 0.9},
        # 别人的卡 → 丢掉
        {"card_ids": [c6, o1], "action": "merge", "reason": "别人的卡", "confidence": 0.9},
        # 只有一张 → 丢掉
        {"card_ids": [c6], "action": "merge", "reason": "只有一张", "confidence": 0.9},
        # 中间夹着别的卡（c4、c5 在 c3 和 c6 之间）→ 丢掉
        {"card_ids": [c3, c6], "action": "merge", "reason": "中间有缝", "confidence": 0.9},
    ], cats, {})

    same("两条正常的组写进去了", res["created"], 2)
    same("五条不合规的组被丢掉", res["skipped"], 5)

    cnt = cdb.count_groups(OWNER, mid)
    same("统计：待确认 2 条", (cnt[cdb.GROUP_PENDING], cnt["total"]), (2, 2))
    same("别人的素材下一个组都没有", cdb.count_groups(OTHER)["total"], 0)

    g = cdb.list_groups(OWNER, mid, limit=10)[0]
    with db.connect() as conn:
        mats = {conn.execute("SELECT material_id FROM cards WHERE id=?",
                             (i,)).fetchone()[0] for i in g["card_ids"]}
    same("第一组的成员都在同一个文件里", len(mats), 1)

    return mid, ids


# ======================================================================
# 二、列出来给界面用
# ======================================================================

def test_list(mid):
    print("\n二、列出来给界面用")
    gs = cdb.list_groups(OWNER, mid, limit=10)
    same("共两条组", len(gs), 2)
    check("按原文顺序排（第一条在前面）",
          gs[0]["start_offset"] < gs[1]["start_offset"])

    g0 = gs[0]
    check("带上成员卡的正文（界面不用再逐条拉）",
          all(m.get("text") for m in g0["members"]))
    same("成员数跟 card_ids 对得上",
         len(g0["members"]), len(g0["card_ids"]))
    same("主类名也解出来了", g0["category_name"], CAT_NAME)
    same("组的字数是区间长度",
         g0["chars"], g0["end_offset"] - g0["start_offset"])
    same("还没合并时不带 merged_card_id", g0["merged_card_id"], None)
    same("组里的副标签也带出来", g0["tags"], ["心动"])
    same("reason 原样带出来", g0["reason"], "对白与回应")


# ======================================================================
# 三、确认合并
# ======================================================================

def test_confirm(mid, ids):
    print("\n三、确认合并")
    c1, c2 = ids[0], ids[1]
    gid = cdb.list_groups(OWNER, mid, limit=10)[0]["id"]

    s1, e2 = card_state(c1)["start_offset"], card_state(c2)["end_offset"]
    res = cdb.confirm_group(gid, OWNER)
    check("合并成功", res.get("ok") is True)

    new_id = res.get("card_id")
    nr = card_state(new_id)
    check("新卡建出来了", nr is not None)

    content = material_text(mid)
    same("新卡正文 = materials.content[start:end]（不是拼接出来的）",
         card_text(new_id), content[s1:e2])
    same("新卡区间头尾接得上",
         (nr["start_offset"], nr["end_offset"]), (s1, e2))
    check("新卡正文 hash 跟区间对得上",
          sg.verify(content, nr["start_offset"], nr["end_offset"],
                    nr["source_text_hash"])["ok"] is True)

    for c in (c1, c2):
        st = card_state(c)
        same("被合掉的卡 #%d 标成已排除" % c, st["status"], sg.STATUS_EXCLUDED)
        same("被合掉的卡 #%d 指回了新卡" % c, st["parent_card_id"], new_id)

    g = group_row(gid)
    same("组的状态变成已合并", g["status"], cdb.GROUP_MERGED)
    same("组记下了合并出的卡号", g["merged_card_id"], new_id)
    check("组记下了变更号（撤销要用）", bool(g["merged_change_id"]))

    check("重复确认会被拦住（不能合第二次）",
          cdb.confirm_group(gid, OWNER).get("ok") is False)
    check("别人的账号动不了这一组",
          cdb.confirm_group(gid, OTHER).get("ok") is False)

    # 已合并的组成员被占着，新提议不许再碰这几张卡
    r2 = cdb.create_group_proposals(OWNER, mid, None, [
        {"card_ids": sorted([c1, ids[2]]), "action": "merge",
         "reason": "抢已合并的卡", "confidence": 0.9}])
    same("已合并组的成员不会被新的组抢走", r2["created"], 0)

    return gid, new_id


# ======================================================================
# 四、撤销
# ======================================================================

def test_undo(mid, ids, gid, new_id):
    print("\n四、撤销合并")
    c1, c2 = ids[0], ids[1]

    res = cdb.undo_group_merge(gid, OWNER)
    check("撤销成功", res.get("ok") is True)
    same("撤销之后原来的卡回来了",
         [card_state(c)["status"] for c in (c1, c2)],
         [sg.STATUS_PENDING, sg.STATUS_PENDING])
    same("撤销之后父子关系清干净了（不留指向空处的关系）",
         [card_state(c)["parent_card_id"] for c in (c1, c2)], [None, None])
    same("撤销之后合并出来的那张卡退场了",
         card_state(new_id)["status"], sg.STATUS_EXCLUDED)
    same("组回到待确认", group_row(gid)["status"], cdb.GROUP_PENDING)
    same("组里不再挂着已合并的痕迹",
         (group_row(gid)["merged_card_id"], group_row(gid)["merged_change_id"]),
         (None, None))
    check("再撤一次会被拦住",
          cdb.undo_group_merge(gid, OWNER).get("ok") is False)


# ======================================================================
# 五、忽略
# ======================================================================

def test_ignore(mid, ids):
    print("\n五、忽略一组（一张卡片都不许动）")
    c3, c4, c5 = ids[2], ids[3], ids[4]
    gid = cdb.list_groups(OWNER, mid, limit=10)[1]["id"]

    snap = [card_state(c)["status"] for c in (c3, c4, c5)]
    res = cdb.ignore_group(gid, OWNER)
    check("忽略成功", res.get("ok") is True)
    same("组状态变成已忽略", group_row(gid)["status"], cdb.GROUP_IGNORED)
    same("卡片状态一张都没动",
         [card_state(c)["status"] for c in (c3, c4, c5)], snap)

    check("已忽略的组不能再确认",
          cdb.confirm_group(gid, OWNER).get("ok") is False)

    # 忽略之后这几张卡被"放出来"了，可以被新的组收
    r = cdb.create_group_proposals(OWNER, mid, None, [
        {"card_ids": sorted([c3, c4]), "action": "merge",
         "reason": "重新提一次", "confidence": 0.8}])
    same("被忽略的卡可以重新组（驳回不该把卡锁死）", r["created"], 1)


# ======================================================================
# 六、全部合完，原文不动
# ======================================================================

def test_multi_confirm(mid):
    print("\n六、一组一组地合，原文一个字不变")
    gs = cdb.list_groups(OWNER, mid, limit=20)
    pending = [g for g in gs if g["status"] == cdb.GROUP_PENDING]
    check("至少有一组等着合", len(pending) >= 1)

    n_ok = 0
    for g in pending:
        if cdb.confirm_group(g["id"], OWNER).get("ok"):
            n_ok += 1
    same("待确认的组都能合掉", n_ok, len(pending))

    with db.connect() as conn:
        live = conn.execute(
            "SELECT COUNT(*) FROM cards WHERE owner_id=? AND material_id=?"
            " AND status<>?", (OWNER, mid, sg.STATUS_EXCLUDED)).fetchone()[0]
    check("合完之后库里还有活着的卡", live >= 1)

    same("原文一个字都没变", material_text(mid), TEXT)

    # 所有组的区间必须真的等于某一批原文，不能有一处错位
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT start_offset, end_offset FROM card_groups"
            " WHERE owner_id=? AND material_id=? AND status=?",
            (OWNER, mid, cdb.GROUP_MERGED)).fetchall()
    content = material_text(mid)
    bad = [r["start_offset"] for r in rows
           if content[r["start_offset"]:r["end_offset"]].strip() == ""]
    same("每个组的区间都能取到真实原文", bad, [])


# ======================================================================

def main():
    _guard()
    print("临时库：%s" % db.DATA_DIR)

    cdb.migrate()

    mid, ids = test_proposals()
    test_list(mid)
    gid, new_id = test_confirm(mid, ids)
    test_undo(mid, ids, gid, new_id)
    test_ignore(mid, ids)
    test_multi_confirm(mid)

    print("\n通过 %d 项，失败 %d 项" % (OK, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(code)
