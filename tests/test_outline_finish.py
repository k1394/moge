# -*- coding: utf-8 -*-
"""
墨阁 · 候选的「结束原因」与「结构缺口」
========================================================
测什么：她在大纲页看到的候选卡上那条体检结论
（正常写完 / 被字数上限掐断 / 结构缺结局），
从模型返回一路走到库里的两个新列，对不对。

背景：她 2026-09-26 截图那次"看着正常、其实没写完"——
模型返回里本来带着 finish_reason（length = 撞字数上限被掐断），
但解析响应时只接住了正文和用量，这个字段被丢了，
于是后面所有环节都无从判断"这篇到底写完了没有"。

守的规矩：
    ① 模型返回里的 finish_reason 必须被接住，不能再丢
    ② 老库（补列之前建的）跑一次 migrate 就补上列，并把已经存下来的
       raw_response / content_json **回填**成新字段 —— 不用重新花钱跑一遍
    ③ 回填是幂等的：跑第二遍一行都挑不出来
    ④ 抠不出结束原因时记 unknown（"当时没记"），绝不硬安一个
       "正常写完"上去 —— 把不知道当成没问题，正是这次要修的病
    ⑤ 接口/测试连的不是真实库

怎么跑（不需要先启动墨阁）：

    cd G:\\docker\\moge
    .venv\\Scripts\\python.exe tests\\test_outline_finish.py
"""

import json
import os
import shutil
import sys
import tempfile

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

# 隔离：必须在 import backend 之前把数据目录指到临时目录。
# 本测试要 DROP 重建 outline_candidates，晚一步设置就拆到她的真库上。
_TMP = tempfile.mkdtemp(prefix="moge_ofin_")
os.environ["MOGE_DATA_DIR"] = _TMP

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backend import db, llm, outline_db as odb        # noqa: E402
from backend import outline_ai as oai                 # noqa: E402

OK = FAIL = 0


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  [通过] %s %s" % (name, extra))
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, extra))


def same(name, got, want):
    """显式比值。

    【为什么不用 check 代替】check 的第二个参数是"真值即通过"，
    把期望值（可能是空串、空列表、0）直接递进去，断言等于没写 ——
    这个库踩过一次，所以凡是比值的都用这个函数。
    """
    if got == want:
        check(name, True, repr(got))
    else:
        check(name, False, "期望 %r，实际 %r" % (want, got))


def raw_with(finish, content="{}"):
    """拼一个模型原始返回体（跟上游真实结构一致）。"""
    return json.dumps(
        {"choices": [{"message": {"content": content},
                      "finish_reason": finish}]},
        ensure_ascii=False)


# ----------------------------------------------------------------------
# 库辅助
# ----------------------------------------------------------------------

def _cols(table):
    with db.connect() as conn:
        return {r["name"] for r in conn.execute(
            "PRAGMA table_info(%s)" % table).fetchall()}


def _make_old_table():
    """造一张"补列之前"的 outline_candidates —— 故意不带那两个新列。

    这模拟的就是她的真实库：表在大纲功能上线时建好，
    而 finish_reason / gaps_json 是 2026-09-26 才加的，
    CREATE TABLE IF NOT EXISTS 不会给它补列。
    """
    with db.connect() as conn:
        conn.execute("DROP TABLE IF EXISTS outline_candidates")
        conn.execute("""CREATE TABLE outline_candidates (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id        INTEGER NOT NULL,
            owner_id      TEXT    NOT NULL,
            model_key     TEXT    NOT NULL DEFAULT '',
            content_json  TEXT    NOT NULL DEFAULT '{}',
            raw_response  TEXT    NOT NULL DEFAULT '',
            created_at    TEXT    NOT NULL DEFAULT '')""")


FULL_CONTENT = json.dumps(
    {"story_core": "x", "climax": "y", "ending": "z",
     "nodes": [{"node_title": "一", "event": "有事"}]}, ensure_ascii=False)
NO_ENDING = json.dumps(
    {"story_core": "x", "climax": "y", "ending": "",
     "nodes": [{"node_title": "一", "event": "有事"}]}, ensure_ascii=False)


def _seed_old_rows():
    """三条老候选：一条被掐断、一条缺结局、一条原始返回是乱码。"""
    rows = (
        (FULL_CONTENT, raw_with("length")),
        (NO_ENDING, raw_with("stop")),
        ("{}", "半个 {"),
    )
    with db.connect() as conn:
        for i, (cj, raw) in enumerate(rows):
            conn.execute(
                "INSERT INTO outline_candidates (run_id, owner_id, model_key,"
                " content_json, raw_response, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (1, "__u1", "m%d" % (i + 1), cj, raw, "2026-09-01"))


def _all_rows():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM outline_candidates ORDER BY id").fetchall()]


def _seed_run_with_candidate():
    """造一条「已完成、但被字数上限掐断」的候选，返回 run_id。

    直接插库：候选本来是调模型跑出来的，测试里不花那个钱。
    """
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO outline_runs (owner_id, status, total_models,"
            " done_models, created_at) VALUES (?,?,?,?,?)",
            ("__u1", "已完成", 1, 1, "2026-09-26"))
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO outline_candidates (run_id, owner_id, model_key,"
            " model_name, status, content_json, warnings_json, gaps_json,"
            " finish_reason, output_chars, elapsed_ms, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, "__u1", "m1", "测试模型", "已完成", NO_ENDING,
             '["这一版被字数上限掐断了，模型没写完。"]',
             '["结局"]', "length", 1200, 3000, "2026-09-26"))
    return rid


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------

def main():
    print("=" * 66)
    print("候选的结束原因与结构缺口")
    print("=" * 66)

    # ---------------- 【1】 ----------------
    print("\n【1】结束原因翻成人话")
    same("stop 说成正常写完", llm.finish_label("stop"), "正常写完")
    same("length 说成被字数上限掐断",
         llm.finish_label("length"), "被字数上限掐断")
    same("content_filter", llm.finish_label("content_filter"), "被内容策略拦下")
    same("空串必须说「当时没记」，不能说成「正常写完」",
         llm.finish_label(""), "当时没记结束原因")
    same("unknown 也说「当时没记」",
         llm.finish_label("unknown"), "当时没记结束原因")
    same("没见过的值照实说，不猜", llm.finish_label("weird"), "结束了（weird）")

    # ---------------- 【2】 ----------------
    print("\n【2】结构缺口只回几个短词")
    full = {"story_core": "x", "climax": "y", "ending": "z",
            "nodes": [{"node_title": "一", "event": "有事"}]}
    same("结构齐的时候什么都不说", odb.structure_gaps(full), [])
    miss = dict(full)
    miss["ending"] = ""
    same("缺结局", odb.structure_gaps(miss), ["结局"])
    same("空大纲四样全缺",
         odb.structure_gaps({}), ["故事核心", "高潮", "结局", "所有段落"])
    same("一段都没有时不再逐段报",
         odb.structure_gaps({"story_core": "x", "climax": "y", "ending": "z"}),
         ["所有段落"])
    holed = {"story_core": "x", "climax": "y", "ending": "z",
             "nodes": [{"node_title": "甲", "event": ""},
                       {"node_title": "乙", "event": "有"}]}
    same("有段落没内容要指名道姓",
         odb.structure_gaps(holed), ["段落内容（甲）"])

    # ---------------- 【3】 ----------------
    print("\n【3】从存下来的原始返回里往回抠")
    same("length 抠得出来", odb.guess_finish_reason(raw_with("length")), "length")
    same("不是 JSON 就返回空", odb.guess_finish_reason("这不是 JSON"), "")
    same("没有 choices 也返回空", odb.guess_finish_reason('{"choices":[]}'), "")
    same("空原始返回不炸", odb.guess_finish_reason(""), "")

    # ---------------- 【4】 ----------------
    print("\n【4】解析响应时把结束原因接住（这次修的病根）")
    text, usage, fr = llm._pick_content(raw_with("stop", "你好"), "k")
    same("正文接住了", text, "你好")
    same("结束原因也接住了", fr, "stop")
    same("被掐断的那次照样接住",
         llm._pick_content(raw_with("length", "半截"), "k")[2], "length")
    no_fr = json.dumps({"choices": [{"message": {"content": "x"}}]})
    same("上游没回这个字段时给空串", llm._pick_content(no_fr, "k")[2], "")
    rc = json.dumps({"choices": [{"message": {"content": None,
                                              "reasoning_content": "想"},
                                  "finish_reason": "stop"}]})
    same("content 为 None 的老分支没坏", llm._pick_content(rc, "k")[0], "想")
    check("返回值是三元组（改签名没漏掉调用方）",
          isinstance(llm._pick_content(raw_with("stop"), "k"), tuple)
          and len(llm._pick_content(raw_with("stop"), "k")) == 3)

    # ---------------- 【5】 ----------------
    print("\n【5】新库：建表就带这两列")
    odb.migrate()
    cols = _cols("outline_candidates")
    check("finish_reason 在", "finish_reason" in cols)
    check("gaps_json 在", "gaps_json" in cols)

    # ---------------- 【6】 ----------------
    print("\n【6】接口下发：这两个字段真的到得了前端")
    rid = _seed_run_with_candidate()
    r = oai.get_run(rid, "__u1")
    c = r["candidates"][0]
    # get_run 用的是显式字段白名单（不是 SELECT *），最容易在这一步漏掉
    same("任务里的候选带上了结束原因的原始值", c["finish_reason"], "length")
    same("也带上了翻好的中文，界面不用自己认英文",
         c["finish_label"], "被字数上限掐断")
    same("结构缺口一起下发", c["gaps"], ["结局"])
    full = oai.get_candidate(c["id"], "__u1")
    same("点开单份候选也带结束原因",
         full["finish_label"], "被字数上限掐断")
    same("单份候选的缺口也在", full["gaps"], ["结局"])
    check("原始返回仍然不下发（又长又乱，不该进前端）",
          "raw_response" not in full)

    # ---------------- 【7】 ----------------
    print("\n【7】老库：跑一次 migrate 就补列并回填（她真实库走的就是这条）")
    _make_old_table()
    _seed_old_rows()
    check("补列之前确实没有 finish_reason",
          "finish_reason" not in _cols("outline_candidates"))
    odb.migrate()
    cols = _cols("outline_candidates")
    check("补列之后 finish_reason 有了", "finish_reason" in cols)
    check("补列之后 gaps_json 有了", "gaps_json" in cols)

    rows = _all_rows()
    same("三条老数据一条没丢", len(rows), 3)
    same("被掐断的那条，结束原因补成了 length",
         rows[0]["finish_reason"], "length")
    # 【fallback 的类型要紧】_loads 的返回值类型由 fallback 决定
    # （isinstance(v, type(fallback))）——传 None 会把"解出来是空列表"
    # 也判成兜底、直接回 None。生产代码一律传 [] / {}，测试照生产来。
    same("结构完整的那条不报缺口",
         odb._loads(rows[0]["gaps_json"], []), [])
    same("缺结局的那条报出结局",
         odb._loads(rows[1]["gaps_json"], []), ["结局"])
    same("原始返回是乱码时记 unknown，不硬安「正常写完」",
         rows[2]["finish_reason"], "unknown")
    same("乱码那条的结构缺口照算",
         odb._loads(rows[2]["gaps_json"], []),
         ["故事核心", "高潮", "结局", "所有段落"])

    # ---------------- 【7】 ----------------
    print("\n【8】回填是幂等的（不然热重载会一直重算）")
    same("再跑一次，一行都挑不出来", odb.backfill_candidate_meta(), 0)
    same("值一个都没被改动",
         [r["finish_reason"] for r in _all_rows()],
         [r["finish_reason"] for r in rows])
    same("缺口也没被重写",
         [r["gaps_json"] for r in _all_rows()],
         [r["gaps_json"] for r in rows])

    print("\n" + "=" * 66)
    print("通过 %d 项，失败 %d 项" % (OK, FAIL))
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(rc)
