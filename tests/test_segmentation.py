# -*- coding: utf-8 -*-
"""
墨阁 · 切分模块单元测试
========================================================
测的是 backend/segmentation.py 里的纯函数。

为什么这个测试可以"随便跑"、不用起服务、不用临时库：
    因为这些函数不读写数据库、不联网、不碰文件 ——
    给一段文字，算出一个结果。这类函数叫"纯函数"。
    纯函数是最好测的：不用担心把真实数据搞坏。

怎么跑：
    cd G:\\docker\\moge
    .venv\\Scripts\\python.exe tests\\test_segmentation.py

一共 11 组、50 项检查。
"""

import os
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backend import segmentation as sg          # noqa: E402

OK = FAIL = 0


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  [通过] %s  %s" % (name, extra))
    else:
        FAIL += 1
        print("  [失败] %s  %s" % (name, extra))


# 每一条都不能省的"总不变式"：
# 切出来的每一段正文，必须严格等于原文的那一段切片。
# 这条一旦破了，整套东西就不可信了（因为卡片正文不再是原文）。
def assert_slices_exact(text, segs, label):
    bad = []
    for s in segs:
        piece = text[s["start"]:s["end"]]
        if piece != s["text"]:
            bad.append(s["seq"])
    check("%s：每段正文都严格等于原文切片" % label, not bad,
          "" if not bad else "出问题的段号：%s" % bad[:5])


# ----------------------------------------------------------------------

def t_normalize():
    print("\n【1】归一化")
    check("\\r\\n 换成 \\n",
          sg.normalize("甲\r\n乙") == "甲\n乙")
    check("单个 \\r 也换成 \\n",
          sg.normalize("甲\r乙") == "甲\n乙")
    check("去掉文档首尾空白",
          sg.normalize("\n\n  甲\n乙  \n\n") == "甲\n乙")
    check("保留正文内部的换行",
          sg.normalize("甲\n\n\n乙").count("\n") == 3)
    check("不动全角标点（不把「，」换成「,」）",
          sg.normalize("甲，乙。丙") == "甲，乙。丙")
    check("归一化两次结果一样（幂等）",
          sg.normalize(sg.normalize("甲\r\n乙")) == sg.normalize("甲\r\n乙"))

    rep = sg.normalize_report("甲\r\n\r\n乙 ")
    check("归一化报告能说清改了什么",
          rep["crlf"] == 2 and rep["changed"] is True,
          "crlf=%s trimmed=%s" % (rep["crlf"], rep["trimmed_chars"]))


def t_lines_span():
    print("\n【2】行与位置")
    t = "甲甲\n乙\n\n丙丙丙"
    lines = sg.lines_with_span(t)
    check("行数正确", len(lines) == 4, "得到 %d" % len(lines))
    check("第 0 行位置 [0,2)", lines[0][1] == 0 and lines[0][2] == 2)
    check("第 1 行位置 [3,4)（换行符算一个字符，但不属于任何一行）",
          lines[1][1] == 3 and lines[1][2] == 4)
    check("第 2 行是空行", lines[2][0] == "")
    # 「甲甲\n乙\n\n丙丙丙」的位置：甲甲[0,2) 乙[3,4) 空[5,5) 丙丙丙[6,9)
    # 注意空行自己占一个位置（就是那个换行符的位置），所以第 3 行从 6 开始
    check("第 3 行位置 [6,9)", lines[3][1] == 6 and lines[3][2] == 9,
          "实际 [%d,%d)" % (lines[3][1], lines[3][2]))
    # 关键：所有行的内容拼起来（补回换行）必须还原原文
    rebuilt = "\n".join(x[0] for x in lines)
    check("按位置拼回去能还原原文", rebuilt == t)


def t_core_span():
    print("\n【3】去掉行首行尾空白")
    t = "\u2002某某"                     # U+2002 是微信读书导出常见的"空格"
    segs = sg.build_segments(t, "line")
    check("行首的 U+2002 被去掉", segs[0]["text"] == "某某",
          "实际：%r" % segs[0]["text"])
    check("但位置仍然指向原文（start 往后挪）",
          t[segs[0]["start"]:segs[0]["end"]] == "某某")
    assert_slices_exact(t, segs, "去空白")


def t_split_line():
    print("\n【4】按非空行切")
    t = "甲\n\n乙\n丙\n\n\n丁"
    segs = sg.build_segments(t, "line")
    check("空行不算段", len(segs) == 4, "得到 %d" % len(segs))
    check("正文依次是 甲/乙/丙/丁",
          [s["text"] for s in segs] == ["甲", "乙", "丙", "丁"])
    check("序号从 1 开始", segs[0]["seq"] == 1)
    assert_slices_exact(t, segs, "按行切")


def t_split_blank():
    print("\n【5】按空行切")
    t = "甲\n乙\n\n丙\n\n丁\n戊"
    segs = sg.build_segments(t, "blank")
    check("连续非空行合成一段", len(segs) == 3, "得到 %d" % len(segs))
    check("第一段是「甲\\n乙」", segs[0]["text"] == "甲\n乙",
          "实际：%r" % segs[0]["text"])
    check("第二段是「丙」", segs[1]["text"] == "丙")
    check("记下了行号范围",
          segs[0]["line_from"] == 1 and segs[0]["line_to"] == 2)
    assert_slices_exact(t, segs, "按空行切")


def t_auto_rule():
    print("\n【6】自动判断切法")
    a = sg.auto_rule("甲\n乙\n丙")
    check("一个空行都没有 → 按非空行切", a["rule"] == "line", a["reason"][:34])

    # 空行是"条目分界"：每行之间空一行
    a = sg.auto_rule("甲\n\n乙\n\n丙\n\n丁")
    check("空行只做逐行分隔 → 按非空行切", a["rule"] == "line",
          a["reason"][:40])

    # 空行只出现在章节之间、章内是一大堆连续行（典型的小说划线笔记形态）
    body = "\n".join("这是第%d句划线内容，长度差不多。" % i for i in range(40))
    a = sg.auto_rule("第一章 甲\n\n" + body + "\n\n第二章 乙\n\n" + body)
    check("空行是章节级分界（切出来是巨块）→ 按非空行切",
          a["rule"] == "line", a["reason"][:56])

    # 空行均匀：每组 2 行、组间空行
    a = sg.auto_rule("甲一\n甲二\n\n乙一\n乙二\n\n丙一\n丙二")
    check("空行均匀地把多行合成完整条目 → 按空行切",
          a["rule"] == "blank", a["reason"][:40])

    check("判断理由不是空话（有具体数字）",
          any(ch.isdigit() for ch in a["reason"]))


def t_noise():
    print("\n【7】噪音识别")
    cases = [
        ("【工作表：Sheet1】", "high", "表格残留"),
        ("第四章 雨夜", "high", "章节标题"),
        ("第一十九章 破阵", "high", "章节标题"),
        ("638个笔记", "high", "统计信息"),
        ("来自微信读书", "high", "来源杂讯"),
        ("《某书》", "high", "书名行"),
        ("212 135 54 145.8", "high", "纯数字符号"),
    ]
    for text, want_level, want_reason in cases:
        segs = sg.build_segments(text, "line")
        got_level = segs[0]["noise_level"]
        got_reason = segs[0]["noise_reason"]
        check("「%s」判为 %s" % (text[:14], want_reason),
              got_level == want_level and want_reason in got_reason,
              "实际：%s / %s" % (got_level, got_reason))

    # 正文里出现「第N章」不算标题（有引号，是人物在说话）
    segs = sg.build_segments("他笑道：“第三章 的事以后再说。”", "line")
    check("正文里的「第N章」不会被误判成标题",
          segs[0]["noise_level"] == "", "实际：%s" % segs[0]["noise_reason"])

    # 开头十行的短行 → 疑似书名/作者行
    # 注意：这条规则只在「文档够长」时才生效（否则"头部"这个概念不成立），
    #       所以这里必须给一份 20 行以上的文档才测得到它。
    head = ["《某书》", "", "\u2002某某", ""]
    head += ["正文第 %d 行，内容写得比较长不会被当成短行处理。" % i
             for i in range(1, 23)]
    segs = sg.build_segments("\n".join(head), "line")
    check("开头十行内的短行判为头部短行（默认排除）",
          segs[1]["noise_level"] == "high" and "头部短行" in segs[1]["noise_reason"],
          "%s / %s" % (segs[1]["noise_level"], segs[1]["noise_reason"]))

    # 但极短行本身只是提示，不排除 —— 短对白是精华，不能因为"短"就排掉
    segs = sg.build_segments("正文" * 20 + "\n\n秣陵", "line")
    check("短文档里的短行不判头部短行，只提示",
          segs[1]["noise_level"] == "hint" and "头部短行" not in segs[1]["noise_reason"],
          "%s / %s" % (segs[1]["noise_level"], segs[1]["noise_reason"]))

    # 正常内容不能被误伤
    segs = sg.build_segments("他捏碎了手里的茶杯，仍然笑着说没事。", "line")
    check("正常正文不被判为噪音", segs[0]["noise_level"] == "")


def t_verify():
    print("\n【8】原文校验")
    t = "甲乙丙丁戊己庚辛壬癸子丑寅卯"
    segs = sg.build_segments(t, "line")
    s = segs[0]
    check("正常情况下校验通过",
          sg.verify(t, s["start"], s["end"], s["source_text_hash"],
                    s["head_check"], s["tail_check"])["ok"])
    check("起点挪一个字 → 指纹不一致",
          "指纹" in sg.verify(t, s["start"] + 1, s["end"],
                             s["source_text_hash"])["reason"])
    check("起点终点相等 → 拒绝",
          sg.verify(t, 3, 3)["ok"] is False)
    check("终点超出原文长度 → 拒绝",
          sg.verify(t, 0, 999)["ok"] is False)
    check("起点是负数 → 拒绝",
          sg.verify(t, -1, 5)["ok"] is False)
    check("首尾校验文本不一致 → 报出来",
          "开头" in sg.verify(t, s["start"], s["end"], None, "甲乙丙丁戊己庚X")["reason"])
    check("没传指纹只查位置时也能用",
          sg.verify(t, 0, 4)["ok"] is True)


def t_duplicates():
    print("\n【9】近重复检测")
    items = [
        {"id": 1, "text": "他捏碎了手里的茶杯，仍然笑着说没事。"},
        {"id": 2, "text": "他捏碎了手里的茶杯,仍然笑着说没事"},        # 只差标点
        {"id": 3, "text": "他捏碎了手里的茶杯，仍然笑着说没事。可他已经撑不住了。"},  # 包含 1
        {"id": 4, "text": "窗外的雨下了一整夜，没有停的意思。"},
        {"id": 5, "text": "他捏碎手里的茶杯，仍然笑着说没事。"},        # 高度相似
    ]
    pairs = sg.find_duplicates(items)
    kinds = {}
    for p in pairs:
        kinds.setdefault(p["kind"], []).append((p["a_id"], p["b_id"]))

    def has(kind, a, b):
        key = (min(a, b), max(a, b))
        return any(min(x, y) == key[0] and max(x, y) == key[1]
                   for x, y in kinds.get(kind, []))

    check("1↔2 判为「只差标点或空格」", has("punct_only", 1, 2),
          str(kinds.get("punct_only")))
    check("3↔1 判为「长包含短」", has("contain", 3, 1),
          str(kinds.get("contain")))
    check("1↔5 判为「高度相似」", has("similar", 1, 5),
          str(kinds.get("similar")))
    check("4 不和任何一条撞上",
          all(4 not in (p["a_id"], p["b_id"]) for p in pairs))
    check("只提示不删除（函数是纯计算，不改入参）",
          len(items) == 5 and items[0]["text"].endswith("没事。"))
    check("空输入返回空结果", sg.find_duplicates([]) == [])


def t_stability():
    print("\n【10】稳定性与规模")
    big = "\n".join("这是第 %d 条素材内容，长度大致相同用来测试稳定性。" % i
                    for i in range(2000))
    segs1 = sg.build_segments(big, "line")
    segs2 = sg.build_segments(big, "line")
    check("同一段文字切两次结果完全一致",
          [(s["start"], s["end"]) for s in segs1] ==
          [(s["start"], s["end"]) for s in segs2])
    check("2000 行切成 2000 条", len(segs1) == 2000, "得到 %d" % len(segs1))
    check("最后一条的终点等于原文长度",
          segs1[-1]["end"] == len(big), "end=%s len=%s" % (segs1[-1]["end"], len(big)))
    check("所有条长度都大于 0", all(s["chars"] > 0 for s in segs1))


def t_real_shape():
    print("\n【11】模拟小说划线笔记的真实形态（章节 + 空行 + 划线段）")
    parts = ["《某书》", "", "\u2002作者名", "\u2002638个笔记", ""]
    for c in range(1, 6):
        parts.append("第%d章 章名%d" % (c, c))
        parts.append("")
        parts += ["这是第%d章的第%d条划线，内容大致这么长。" % (c, i)
                  for i in range(1, 21)]
        parts.append("")
    parts.append("\u2002来自微信读书")
    t = "\n".join(parts)

    a = sg.auto_rule(t)
    check("自动选按非空行切（空行是章节级分界）", a["rule"] == "line",
          a["reason"][:60])

    pv = sg.preview(t)
    check("切出 109 条原始行（106 条正文 + 书名/作者/统计 3 条）",
          pv["counts"]["total"] == 109, "得到 %d" % pv["counts"]["total"])
    check("识别出 5 个章节标题",
          pv["noise_summary"].get("章节标题") == 5,
          str(pv["noise_summary"]))
    check("书名行、作者行、统计行、来源行都排除了",
          pv["counts"]["noise_high"] == 9,
          "得到 %d，明细 %s" % (pv["counts"]["noise_high"], pv["noise_summary"]))
    check("预计生成 100 张卡片（109 - 9 条噪音）",
          pv["counts"]["will_create_cards"] == 100,
          "得到 %d" % pv["counts"]["will_create_cards"])
    assert_slices_exact(t, pv["segments"], "真实形态")

    # 预览里给出的两种切法对比数字要能自洽
    alt = pv["alternatives"]
    check("两种切法的条数都报出来了",
          alt["line"]["count"] == 109 and alt["blank"]["count"] == 13,
          "line=%d blank=%d" % (alt["line"]["count"], alt["blank"]["count"]))
    check("标出了推荐的那种", alt["line"]["recommended"] is True)


def main():
    print("=" * 64)
    print("墨阁 · 切分模块单元测试（纯函数，不碰数据库、不起服务）")
    print("=" * 64)

    t_normalize()
    t_lines_span()
    t_core_span()
    t_split_line()
    t_split_blank()
    t_auto_rule()
    t_noise()
    t_verify()
    t_duplicates()
    t_stability()
    t_real_shape()

    print()
    print("=" * 64)
    print("通过 %d 项，失败 %d 项" % (OK, FAIL))
    print("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
