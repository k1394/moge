# -*- coding: utf-8 -*-
"""
墨阁 · 素材切分模块（纯文本，不碰数据库）
========================================================
这个文件只做一件事：把一份正文切成"一条一条"的素材区间。

为什么单独拿出来、不写在数据库那层：
    因为它是"纯函数"—— 给同一段文字，永远算出同一个结果，
    不联网、不读写文件、不知道数据库存在。
    这样测试极其便宜（不用起服务、不用建库），
    而且将来切分规则要改，只改这一个文件，别处不受影响。

========================================================
四条必须理解的设计约束（面试会问）
========================================================

一、切出来的每一段，都是原文的一个「连续区间」
    我们不重新拼句子，也不改写任何字。
    每一段只记两个数字：从第几个字开始、到第几个字结束。
    正文要用的时候，拿这两个数字去原文里切一刀就有了。
    好处：原文永远不需要被复制一份，也就不可能和原文不一致。

二、为什么不能存"段正文"当真相
    如果段正文单独存一份，那"原文"和"段正文"就有两个版本，
    早晚会不一致（改了这边忘了那边）。
    所以我们只存位置，正文永远现算 —— 世上只有一个真相。

三、归一化（normalize）为什么必须固定版本
    换行符有 \\r\\n、\\r、\\n 三种写法；文档里还可能有看不见的空格。
    切分前要统一成一种写法，否则同一段文字在不同电脑上偏移不一样。
    但"统一"这个动作本身会改变文字长度 ——
    所以规则一旦定下就要记版本号（norm_version），不能偷偷改。
    改了版本号，之前所有偏移就全部作废。

四、head_check / tail_check 是干什么的
    偏移是数字。将来归一化规则哪怕改一个字，偏移就全错了，
    而错误是无声的（切出来的正文看着还挺像）。
    所以每段额外记"开头 8 个字 + 结尾 8 个字"。
    将来校验时，就算偏移错位，也能靠这 16 个字认出这原本是哪一段。
    这是免费的保险，不占空间，但能在出事时救命。
"""

import difflib
import hashlib
import re

# ----------------------------------------------------------------------
# 版本号：这两个数字改了就代表"规则变了，旧数据要重新生成"
# ----------------------------------------------------------------------

# 归一化规则版本。v1 的规则见 normalize() 的注释。
NORM_VERSION = "v1"

# 切分规则版本。目前只有 line（按非空行切）/ blank（按空行切）两种。
RULE_VERSION = "v1"

# 校验文本取几个字
CHECK_CHARS = 8

# 状态常量（避免各处写错字）
#
# 这是一条"生命周期"轴：这张卡现在算什么。
# 另一条轴是下面 SOURCE_* 那个"这个主类是谁给的"。
# 两条轴是分开的字段，所以不需要为每种来源组合再造一个状态。
#   例：AI 给了建议还没人看过 = 状态「待确认」+ 来源 ai
#       人确认采纳了这条 AI 建议 = 状态「已确认」+ 来源 human
STATUS_PENDING = "待确认"
STATUS_CONFIRMED = "已确认"
STATUS_INFUSED = "已内化"
STATUS_UNUSED = "暂不用"
STATUS_EXCLUDED = "已排除"
STATUS_FAILED = "分类失败"       # 自动分类试过这张卡，但没成功（要能单独筛出来重试）
ALL_STATUS = [STATUS_PENDING, STATUS_CONFIRMED, STATUS_INFUSED,
              STATUS_UNUSED, STATUS_EXCLUDED, STATUS_FAILED]

# 卡片来源：这条卡的主类是谁定的
SOURCE_INHERITED = "inherited"   # 从原文件来源集合继承（也就是她原来的手工分类）
SOURCE_HUMAN = "human"           # 人手点出来的
SOURCE_AI = "ai"                 # AI 给的建议（自动分类写进去的就是这个）


# ----------------------------------------------------------------------
# 一、归一化
# ----------------------------------------------------------------------

def normalize(text):
    """把正文统一成一种固定写法。返回新字符串，不改原字符串。

    v1 规则（只有这四条，刻意保守 —— 改得越少越安全）：

      1. 换行统一成 \\n
         Windows 记事本换行是 \\r\\n（两个字符），Mac 老格式是 \\r（一个字符）。
         不统一的话，"第 1000 个字"在不同来源上指的不是同一个位置。

      2. 去掉整个文档首尾的空白
         开头多几个空行、结尾多几个空行，纯粹是导出噪音。

      3. 保留正文内部的换行
         换行是段落边界，是切分的依据，绝对不能压掉。

      4. 不动中文标点、不把全角转半角、不删任何正文内容
         这是刻意的：这一版宁可留着多余字符，也不能冒险删掉她真正要的东西。
         全角半角互换看起来无害，但会把「（」和「(」变成一个，
         而她可能就是要靠这个做区分。
    """
    if not text:
        return ""
    s = text.replace("\r\n", "\n").replace("\r", "\n")
    return s.strip()


def normalize_report(text):
    """给用户看的"归一化做了什么"报告。

    她零基础，看到"已归一化"四个字会问"归一化是什么"。
    所以这里直接报数字：省掉了几次 \\r、去掉了几行首尾空行。
    """
    t = text or ""
    n = normalize(t)
    t2 = t.replace("\r\n", "\n").replace("\r", "\n")
    return {
        "norm_version": NORM_VERSION,
        "before_chars": len(t),
        "after_chars": len(n),
        "crlf": t.count("\r\n"),
        "cr": t.count("\r") - t.count("\r\n"),
        "trimmed_chars": len(t2) - len(n),
        "changed": n != t,
        "rule": "换行统一为 \\n；去掉文档首尾空白；保留内部换行；不动标点与全角半角",
    }


# ----------------------------------------------------------------------
# 二、把正文按行拆开，同时记住每行的位置
# ----------------------------------------------------------------------

def lines_with_span(text):
    """把正文拆成行，并记下每一行在原文里的起止位置。

    返回 [(行内容, 行起点, 行终点), ...]
    行终点不包含那个换行符本身 —— 换行符是"分隔符"，不属于任何一行。

    为什么不能用 enumerate(text.split("\\n")) 然后自己数：
        "自己数"要一行行累加长度，很容易差一个字符（换行符算不算？）。
       差一个字符，整篇的偏移就全部错位，而且切出来的正文看着还挺正常 ——
       这种错误最难发现。所以位置一律由这里统一算出来。
    """
    out = []
    pos = 0
    for raw in (text or "").split("\n"):
        start = pos
        end = pos + len(raw)
        out.append((raw, start, end))
        pos = end + 1          # +1 跳过那个换行符
    return out


def _core_span(raw, start):
    """算出一行的"有效核心"位置：去掉前后的空白，只保留真正的字。

    例：「 某某」 → 有效区间只覆盖"某某"，不包含前面那个空格。

    为什么要去：微信读书导出的每行前面常带一个空格，
    留着的话界面上一堆缩进，而且"开头 8 个字"的校验文本会变成空格 + 7 个字。

    为什么可以这么去：切出来的仍然是一个连续区间，
    只是这个区间比整行短一点 —— 正文照样是原文原样切出来的，没有改写。
    """
    if not raw.strip():
        return None                      # 整行都是空白 → 没有核心
    lead = len(raw) - len(raw.lstrip())
    trail = len(raw) - len(raw.rstrip())
    return (start + lead, start + len(raw) - trail)


# ----------------------------------------------------------------------
# 三、两种切法
# ----------------------------------------------------------------------

def split_by_line(text):
    """按「非空行」切：每一行（去掉首尾空白）就是一段。

    适合：每行本身就是一条内容的文件。
    例：微信读书的划线导出 —— 每一行是一条划线。
    """
    segs = []
    for raw, start, _end in lines_with_span(text):
        core = _core_span(raw, start)
        if core:
            segs.append({"start": core[0], "end": core[1], "line_from": None})
    for i, s in enumerate(segs):
        s["seq"] = i + 1
    return segs


def split_by_blank(text):
    """按「空行」切：连续的非空行合成一段，空行是段与段的分界。

    适合：条目之间用空行隔开的文件。
    例：一份手工整理的摘录，一条素材写完空一行再写第二条。
    """
    groups = []
    cur = []
    for idx, (raw, start, _end) in enumerate(lines_with_span(text)):
        core = _core_span(raw, start)
        if core:
            cur.append((core, idx))
        elif cur:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)

    segs = []
    for g in groups:
        first_core, first_idx = g[0]
        last_core, last_idx = g[-1]
        segs.append({"start": first_core[0], "end": last_core[1],
                     "line_from": first_idx + 1, "line_to": last_idx + 1})
    for i, s in enumerate(segs):
        s["seq"] = i + 1
    return segs


RULES = {
    "line": {
        "name": "按非空行切",
        "desc": "每一行算一条。适合每行本身是一条内容的文件（比如划线导出）。",
        "fn": split_by_line,
    },
    "blank": {
        "name": "按空行切",
        "desc": "连续的非空行合成一条，空行做分界。适合条目之间空一行隔开的文件。",
        "fn": split_by_blank,
    },
}


def _median(nums):
    if not nums:
        return 0
    a = sorted(nums)
    return a[len(a) // 2]


def _p90(nums):
    """第 90 百分位。用来看"最大的那批有多长"，比平均值抗极值。

    为什么不看最大值：一条异常的超长记录就会把判断带偏。
    p90 的意思是"十块里有九块不超过这个长度"。
    """
    if not nums:
        return 0
    a = sorted(nums)
    return a[min(int(len(a) * 0.9), len(a) - 1)]


# 自动判断用的两个阈值
TOO_COARSE_MEDIAN = 300    # 中位条长超过它 → 一块太粗，不是一条素材
TOO_COARSE_P90 = 300       # 十块里有九块都超过它 → 同上
MONSTER_CHARS = 800        # 出现这么大的块 → 分隔符不可靠


def auto_rule(text):
    """自动判断该用哪种切法，并把判断理由说清楚。

    规则（v2）—— 判据从"中位数"升级为"中位数 + p90"，原因是实测踩过一次：

        实测踩过一次：某份小说划线笔记按空行切，中位条长只有 50 字（看着很合理），
        但 p90 是 1481 字、最长一块 5378 字。
        也就是说这份文件的空行「有的地方是章节分界、有的地方是条目分界」，
        按空行切出来的东西一半是素材条、一半是整章，没法用。
        只盯中位数会完全看不出来。

    规则：
      1. 一个空行都没有 → 按非空行切（唯一选择）
      2. 按空行切的条数 == 非空行数 → 两者等价，选更简单的按非空行切
      3. 按空行切出现超大块（>800 字）或 九成块都超过 300 字
         → 空行分隔不可靠，改按非空行切
      4. 其余情况 → 按空行切（多行作为一个整体条目更合理）
    """
    t = text or ""
    n_blank = sum(1 for raw, _s, _e in lines_with_span(t) if not raw.strip())
    line_segs = split_by_line(t)
    n_line = len(line_segs)
    med_line = _median([s["end"] - s["start"] for s in line_segs])

    if n_blank == 0:
        return {
            "rule": "line", "auto": True,
            "reason": "这份文件一个空行都没有，只能按非空行切：%d 行 → %d 条。"
                      % (n_line, n_line),
            "blank_lines": 0, "non_blank_lines": n_line,
            "median_line": med_line,
        }

    blank_segs = split_by_blank(t)
    n_blank_seg = len(blank_segs)
    sizes = sorted(s["end"] - s["start"] for s in blank_segs)
    med_blank = _median(sizes)
    p90_blank = _p90(sizes)
    max_blank = sizes[-1] if sizes else 0

    base = {
        "blank_lines": n_blank, "non_blank_lines": n_line,
        "blank_segments": n_blank_seg,
        "median_blank": med_blank, "p90_blank": p90_blank,
        "max_blank": max_blank, "median_line": med_line,
        "max_line": max((s["end"] - s["start"] for s in line_segs), default=0),
    }

    if n_blank_seg == n_line:
        base.update({
            "rule": "line", "auto": True,
            "reason": "检测到 %d 个空行，但它们只是把每一行隔开，"
                      "两种切法结果相同（都是 %d 条），按非空行切。"
                      % (n_blank, n_line),
        })
        return base

    if max_blank > MONSTER_CHARS or p90_blank > TOO_COARSE_P90:
        why = []
        if max_blank > MONSTER_CHARS:
            why.append("最长一块 %d 字" % max_blank)
        if p90_blank > TOO_COARSE_P90:
            why.append("十块里有九块超过 300 字（p90 = %d）" % p90_blank)
        base.update({
            "rule": "line", "auto": True,
            "reason": "检测到 %d 个空行，但这份文件的空行分隔不均匀：%s。"
                      "按空行切只能切出 %d 块，其中有 %s —— "
                      "那不叫一条素材，那叫一章。所以按非空行切："
                      "%d 行 → %d 条（中位 %d 字，最长 %d 字）。"
                      % (n_blank, "、".join(why), n_blank_seg,
                         "、".join(why), n_line, n_line, med_line,
                         base.get("max_line", 0)),
        })
        return base

    base.update({
        "rule": "blank", "auto": True,
        "reason": "检测到 %d 个空行，且连续多行能合成完整条目 —— "
                  "按空行切成 %d 条，中位 %d 字、p90 %d 字，都在合理范围内，"
                  "所以按空行切。" % (n_blank, n_blank_seg, med_blank, p90_blank),
    })
    return base


# ----------------------------------------------------------------------
# 四、噪音行识别
#
# 分两档，这个区分很重要：
#   high  = 高置信度噪音，默认排除（但仍可一键恢复）
#   hint  = 只是提示，不排除（因为可能是真内容，交给人判断）
#
# 为什么不敢把所有"疑似"都自动排除：
#   自动排除错了，她要一条条去恢复，比不排除还麻烦。
#   所以只有"看了标题就知道肯定不是素材"的，才默认排除。
# ----------------------------------------------------------------------

_NOISE_HIGH = [
    # 表格残留：xlsx 导出会留下这类标记
    ("表格残留", re.compile(r"工作表\s*[:：]|\[?\s*Sheet\s*\d*\s*\]?", re.I)),
    # 章节标题：是结构标记，不是素材内容
    # 为什么不限制长度：微信读书的标题有的很短（「第四章 雨夜」），
    #   有的很长（「第一十一章 一解：（霭霭停云、蒙蒙时雨……）」）。
    #   限长度会漏掉长标题（实测漏了 4 个），所以改用「不能含对话引号」来兜底 ——
    #   章节标题不会是人物在说话，正文里几乎都有引号。
    #   万一还是误判，界面上可以一键恢复（默认排除 ≠ 删除）。
    ("章节标题", re.compile(
        r"^\s*第\s*[0-9零一二三四五六七八九十百千两]+\s*[章节回卷部篇]"
        r"(?![^\n]*[「」“”\"'])" )),
    # 统计行：阅读软件导出的元信息
    ("统计信息", re.compile(r"个笔记|条笔记|人划线|位读者|笔记\s*$")),
    # 来源杂讯
    ("来源杂讯", re.compile(r"来自微信读书|微信读书|版权所有|侵权|免责声明|未经许可|转载")),
    # 纯数字 / 纯符号行
    ("纯数字符号", re.compile(r"^[\s\d\.\-—–_/\\:%×xX+＝=~·|]+$")),
]

# 整行只是一个书名：《某书》
_BOOK_LINE = re.compile(r"^《[^《》]{1,30}》$")

# 极短且不含任何标点的行
_SHORT_BARE = re.compile(r"^[^。，、！？：；…—「」“”\"'（）()《》\s]{1,6}$")

# 头部若干行内的短行（书名/作者行都在这里）
HEAD_LINES = 10

# 「头部短行」这条规则只在文档足够长时才生效。
# 为什么：它的作用是「在一篇长文里挑出开头的书名 / 作者 / 统计行」。
#   一份只有两三行的文档谈不上"头部" —— 它整篇都是头部，
#   用这条规则会把正文当噪音排掉（单元测试真实抓到过这个误判：
#   正文 + 空行 + 「秣陵」两行的小文件，第二行被判成了头部短行）。
#   短文档里的短行仍然会被「极短行」那条规则提示（hint），只是不自动排除。
HEAD_MIN_LINES = 20


def detect_noise(text, seg, total_lines=None):
    """判断一段是不是噪音。返回 (level, reason)。

    level 有三档：
        "high"  默认排除
        "hint"  只提示
        None    正常内容

    total_lines 是整份文档的非空行数，只有「头部短行」那条规则用得到。
    不传的话这里自己数一遍（单独调用方便；批量切分时由上层传进来，省时间）。
    """
    s = seg.get("text", "")
    if s is None:
        s = (text or "")[seg["start"]:seg["end"]]
    stripped = s.strip()
    if not stripped:
        return "high", "空白行"

    for label, pat in _NOISE_HIGH:
        if pat.search(stripped):
            return "high", label

    if _BOOK_LINE.match(stripped):
        return "high", "书名行"

    # 开头十行以内的短行 → 大概率是书名/作者/统计行
    # 前提：文档得够长，否则"头部"这个概念不成立（见 HEAD_MIN_LINES 的说明）
    lf = seg.get("line_from")
    if lf is not None and lf <= HEAD_LINES:
        if total_lines is None:
            total_lines = sum(1 for raw, _s, _e in lines_with_span(text)
                              if raw.strip())
        if total_lines >= HEAD_MIN_LINES and \
                len(stripped) <= 8 and \
                not re.search(r"[。，、！？：；…—「」“”\"'（）()]", stripped):
            return "high", "头部短行（疑似书名/作者行）"

    if _SHORT_BARE.match(stripped):
        return "hint", "极短行，请确认是不是标题或条目名"

    return None, ""


# ----------------------------------------------------------------------
# 五、把切分结果整理成一条条完整信息
# ----------------------------------------------------------------------

def _hash(text):
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()


def text_hash(text):
    """正文指纹。给外部（比如分类库那层）用的公开名字。

    为什么要有这个别名：数据库层要用它算卡片的 source_text_hash，
    直接调 _hash 这种"下划线开头"的函数不好 ——
    下划线在 Python 里的约定是"这是本模块内部用的，外面别碰"。
    """
    return _hash(text)


def build_segments(text, rule, with_text=True):
    """按指定切法切一遍，给每段补上哈希、校验文本、噪音标记。

    返回的每一段都包含：
        seq            第几条（从 1 开始）
        start / end    在原文里的起止位置（含前不含后，和 Python 切片一致）
        chars          字数
        text           正文（现算的，不是存的）
        source_text_hash   正文哈希
        head_check / tail_check   首尾各 8 字，用来兜底校验
        noise_level / noise_reason
        line_from / line_to       行号范围（1 开始，给人看的）
    """
    if rule not in RULES:
        rule = "line"
    raw_segs = RULES[rule]["fn"](text or "")

    # 整份文档的非空行数，只算一次 —— 别在循环里反复数（663 段就是 663 次全篇扫描）
    total_lines = sum(1 for raw, _s, _e in lines_with_span(text or "")
                      if raw.strip())

    out = []
    for i, s in enumerate(raw_segs):
        piece = (text or "")[s["start"]:s["end"]]
        lf = s.get("line_from")
        lt = s.get("line_to")
        if lf is None:
            # 按行切：行号就是序号
            lf = lt = s["seq"]
        seg = {
            "seq": i + 1,
            "start": s["start"],
            "end": s["end"],
            "chars": len(piece),
            "source_text_hash": _hash(piece),
            "head_check": piece[:CHECK_CHARS],
            "tail_check": piece[-CHECK_CHARS:] if len(piece) > CHECK_CHARS else piece,
            "line_from": lf,
            "line_to": lt,
        }
        if with_text:
            seg["text"] = piece
        lvl, why = detect_noise(text, {"text": piece, "line_from": lf},
                                total_lines=total_lines)
        seg["noise_level"] = lvl or ""
        seg["noise_reason"] = why
        out.append(seg)
    return out


def preview(text, rule=None, with_text=True, text_limit=None):
    """切分预览。这是"第 0 步"的核心：让人先亲眼看见切得对不对。

    rule 传 None 就自动判断；传了就按指定的来（用户可以手动改选）。

    text_limit 是给列表页用的：只回前 N 条正文，避免一次回 663 条把页面撑爆。
    """
    auto = auto_rule(text)
    use = rule if rule in RULES else auto["rule"]

    segs = build_segments(text, use, with_text=with_text)

    if text_limit is not None:
        for s in segs[text_limit:]:
            s.pop("text", None)

    noise_high = [s for s in segs if s["noise_level"] == "high"]
    noise_hint = [s for s in segs if s["noise_level"] == "hint"]

    # 按原因归类，界面上显示"29 行章节标题、1 行统计信息……"
    summary = {}
    for s in noise_high:
        summary[s["noise_reason"]] = summary.get(s["noise_reason"], 0) + 1

    lens = [s["chars"] for s in segs]
    alts = {}
    for k, v in RULES.items():
        raw = v["fn"](text or "")
        sizes = [s["end"] - s["start"] for s in raw]
        alts[k] = {"name": v["name"], "desc": v["desc"], "count": len(raw),
                   "median": _median(sizes), "p90": _p90(sizes),
                   "max": max(sizes) if sizes else 0,
                   "recommended": k == auto["rule"]}
    return {
        "chars": len(text or ""),
        "norm_version": NORM_VERSION,
        "rule_version": RULE_VERSION,
        "auto_rule": auto["rule"],
        "auto_reason": auto["reason"],
        "auto_detail": {k: auto.get(k) for k in
                        ("blank_lines", "non_blank_lines", "blank_segments",
                         "median_blank", "p90_blank", "max_blank",
                         "median_line", "max_line") if k in auto},
        "rule": use,
        "rule_name": RULES[use]["name"],
        "rule_reason": auto["reason"] if use == auto["rule"] else
                       "手动改选为「%s」。自动判断的原始理由：%s"
                       % (RULES[use]["name"], auto["reason"]),
        "counts": {
            "total": len(segs),
            "blank_lines": auto.get("blank_lines", 0),
            "non_blank_lines": auto.get("non_blank_lines", 0),
            "noise_high": len(noise_high),
            "noise_hint": len(noise_hint),
            "will_create_cards": len(segs) - len(noise_high),
            "min_chars": min(lens) if lens else 0,
            "median_chars": _median(lens),
            "max_chars": max(lens) if lens else 0,
        },
        "noise_summary": summary,
        "segments": segs,
        "alternatives": alts,
    }


# ----------------------------------------------------------------------
# 六、校验：卡片到底还对不对得上原文
# ----------------------------------------------------------------------

def verify(text, start, end, expected_hash=None, head=None, tail=None):
    """检查一段区间是不是真的对应原文的那段内容。

    检查四项（任何一项不过就报错，绝不"差不多就行"）：
        1. 起止位置合法（start < end，且没超出原文长度）
        2. 切出来的正文哈希和当初记的一致
        3. 开头 8 个字一致
        4. 结尾 8 个字一致

    为什么要查这么细：偏移是数字，一旦错位，切出来的正文
    可能"看着还挺像"但其实已经错了。只有校验文本能抓住这种错。
    """
    t = text or ""
    if start is None or end is None:
        return {"ok": False, "reason": "缺少位置信息"}

    start, end = int(start), int(end)
    if start < 0:
        return {"ok": False, "reason": "起点是负数"}
    if start >= end:
        return {"ok": False, "reason": "起点不小于终点（区间为空）"}
    if end > len(t):
        return {"ok": False, "reason": "终点 %d 超出原文长度 %d" % (end, len(t))}

    piece = t[start:end]
    if expected_hash and _hash(piece) != expected_hash:
        return {"ok": False, "reason": "正文与记录的内容指纹不一致",
                "text": piece}
    if head and piece[:CHECK_CHARS] != head:
        return {"ok": False, "reason": "开头校验文本不一致", "text": piece}
    if tail and (piece[-CHECK_CHARS:] if len(piece) > CHECK_CHARS else piece) != tail:
        return {"ok": False, "reason": "结尾校验文本不一致", "text": piece}
    return {"ok": True, "reason": "", "text": piece}


# ----------------------------------------------------------------------
# 七、近重复检测（纯字符级，不上向量模型）
#
# 第一版刻意不用向量：
#   向量能发现"意思像但用词不同"（捏碎茶杯 vs 暴怒），
#   但第一版要解决的是"同一句话出现在两个地方"这种字面重复，
#   纯字符比较就够，而且不用下载模型、不用建索引、毫秒级。
#
# 关键：只提示，绝不自动删或自动合并。
#   因为两条相似素材，可能一条是完整段、一条是能单独用的短句，
#   两个都该留 —— 这种事只有人能判断。
# ----------------------------------------------------------------------

_PUNCT = re.compile(r"[\s，。、！？：；…—–\-「」“”\"'（）()《》〈〉·|,\.!\?:;\[\]{}]")

# 短于这个长度的，不参与"包含"判断（太短容易误报）
MIN_CONTAIN_CHARS = 10

# 相似度到这个程度才算"高度相似"
SIMILAR_RATIO = 0.88


def _squash(s):
    """去掉所有空白和标点，只留下字。用于"只差标点就算重复"的判断。"""
    return _PUNCT.sub("", s or "")


def squash(s):
    """给外部用的公开名字，见 text_hash 的说明。"""
    return _squash(s)


def find_duplicates(items, max_pairs=300, max_compare=200000):
    """在一批卡片里找近重复。

    items 是 [{"id":..., "text":...}, ...]
    返回 [{"a_id","b_id","a_text","b_text","kind","detail","score"}, ...]

    kind 四种（正是规格里列的四种形态）：
        same          去标点空格后完全相同
        contain       一条把另一条整段包含进去了
        similar       去标点后高度相似
        punct_only    只差标点或空格

    性能考虑：不做全量两两比较（600 多条就是 20 万次），
        先按"长度档"分桶，只在同档和相邻档之间比 —— 长度差太远的不可能是同一条。
    """
    norm = []
    for it in items:
        t = it.get("text") or ""
        norm.append({"id": it.get("id"), "text": t, "squash": _squash(t),
                     "len": len(_squash(t))})

    pairs = []
    seen_pairs = set()
    compares = 0

    def add(a, b, kind, detail, score):
        key = (min(a["id"], b["id"]), max(a["id"], b["id"]))
        if key in seen_pairs:
            return
        seen_pairs.add(key)
        pairs.append({
            "a_id": a["id"], "b_id": b["id"],
            "a_text": a["text"], "b_text": b["text"],
            "kind": kind, "detail": detail, "score": round(score, 3),
        })

    # ---- 第一类：去标点后完全相同（一次哈希分组搞定，O(n)）----
    buckets = {}
    for it in norm:
        if it["len"] >= 4:
            buckets.setdefault(it["squash"], []).append(it)
    for key, group in buckets.items():
        if len(group) > 1:
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a, b = group[i], group[j]
                    same_chars = (a["text"].strip() == b["text"].strip())
                    add(a, b,
                        "same" if same_chars else "punct_only",
                        "正文完全相同" if same_chars else "只差标点或空格",
                        1.0)

    # ---- 第二类：长包含短 ----
    # 做法：把每条卡切成"连续 6 个字"的碎片，建一张 碎片→卡片 的索引。
    # 只有共享碎片的卡片才可能是包含关系（否则短的那条根本不在长的里面）。
    # 为什么不两两全比：600 多条两两比是 20 万次，每次比字符串，
    # 页面会卡好几秒。用碎片索引把候选压到几十对，毫秒级出结果。
    by_len = sorted([x for x in norm if x["len"] >= MIN_CONTAIN_CHARS],
                    key=lambda x: -x["len"])
    gram_index = {}
    for it in by_len:
        sq = it["squash"]
        grams = {sq[i:i + 6] for i in range(len(sq) - 5)}
        cands = {}
        for g in grams:
            for longer in gram_index.get(g, []):
                cands[longer["id"]] = longer
        for longer in cands.values():
            if it["squash"] and it["squash"] in longer["squash"]:
                add(longer, it, "contain",
                    "较长的一条把较短的一条整段包含了（差 %d 字）"
                    % (longer["len"] - it["len"]), 1.0)
        for g in grams:
            gram_index.setdefault(g, []).append(it)

    # ---- 第三类：高度相似（只在长度相近的桶里比）----
    buckets2 = {}
    for it in norm:
        if it["len"] >= 6:
            buckets2.setdefault(it["len"] // 10, []).append(it)

    keys = sorted(buckets2)
    stop = False
    for k in keys:
        if stop:
            break
        group = buckets2[k] + buckets2.get(k + 1, [])
        for i in range(len(group)):
            if stop:
                break
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if a["squash"] == b["squash"]:
                    continue                  # 上面已经处理过
                m = max(a["len"], b["len"])
                if m == 0:
                    continue
                if abs(a["len"] - b["len"]) > m * 0.4:
                    continue                  # 长度差太多，直接跳过
                compares += 1
                if compares > max_compare:
                    stop = True
                    break
                ratio = difflib.SequenceMatcher(
                    None, a["squash"], b["squash"]).ratio()
                if ratio >= SIMILAR_RATIO:
                    add(a, b, "similar",
                        "去标点后相似度 %.0f%%" % (ratio * 100), ratio)
        if len(pairs) >= max_pairs:
            break

    pairs.sort(key=lambda p: -p["score"])
    return pairs[:max_pairs]


# ----------------------------------------------------------------------
# 自测：python backend/segmentation.py
# 直接双击也能跑（会拿一段内置文本试一遍，不碰数据库）
# ----------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    demo = """《某书》

 某某
 638个笔记

 第四章 雨夜

他记得那天的风是从北边来的，刮了整整一夜也没停。
天亮时院子里的水缸结了薄薄一层冰。

 第六章 归途

她在门口站了很久，最后什么也没说，转身进去了。
门在身后合上，发出一声很轻的响。

 来自微信读书"""

    print("归一化报告：", normalize_report(demo))
    auto = auto_rule(demo)
    print("自动判断：", auto["rule"], "| 理由：", auto["reason"])
    print()
    pv = preview(demo)
    print("共 %d 段，其中高置信度噪音 %d 段，将生成卡片 %d 段"
          % (pv["counts"]["total"], pv["counts"]["noise_high"],
             pv["counts"]["will_create_cards"]))
    print("噪音归类：", pv["noise_summary"])
    print()
    for s in pv["segments"]:
        mark = {"high": "✗排除", "hint": "?提示", "": "  "}[s["noise_level"]]
        print("%2d %s [%4d,%4d] %s  %s"
              % (s["seq"], mark, s["start"], s["end"],
                 s["noise_reason"][:14].ljust(14), s["text"][:40]))
    print()
    print("校验第 4 段：", verify(demo, *[pv["segments"][3][k] for k in ("start", "end")],
                                pv["segments"][3]["source_text_hash"],
                                pv["segments"][3]["head_check"],
                                pv["segments"][3]["tail_check"])["ok"])
    print("故意把起点挪 1 个字校验：", verify(
        demo, pv["segments"][3]["start"] + 1, pv["segments"][3]["end"],
        pv["segments"][3]["source_text_hash"])["reason"])
    print()
    dup = find_duplicates([
        {"id": 1, "text": "他捏碎了手里的茶杯，仍然笑着说没事。"},
        {"id": 2, "text": "他捏碎了手里的茶杯,仍然笑着说没事"},
        {"id": 3, "text": "他捏碎了手里的茶杯，仍然笑着说没事。可是他知道，自己已经撑不住了。"},
        {"id": 4, "text": "窗外的雨下了一整夜。"},
    ])
    for p in dup:
        print("重复：", p["a_id"], "↔", p["b_id"], p["kind"], p["detail"])
