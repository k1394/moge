# -*- coding: utf-8 -*-
"""
墨阁 · 大纲生成 · 编排层
========================================================

这个文件管"怎么问模型、任务怎么排队、结果怎么收回来"。
表结构那一层在 outline_db.py，两边不互相越界。

--------------------------------------------------------
一、跟内化那边不一样的地方（先说清楚，免得照着抄错）
--------------------------------------------------------

内化是"几百张卡 → 分几十批 → 每批出几条零件"，慢工细活，可以跑十分钟。
大纲生成是"一份输入 → 一次长回答"，所以：

    内化：分批 + 逐批落库 + 断点续跑
    大纲：一次调用 + 每个模型一行候选 + 失败就重试那一个模型

多模型不是"多发几条"，而是**同一份输入分别问几个模型**，
每个模型的回答各自独立存一行（计划第五.5：`不同模型不能互相读取结果`）。
所以这里的并发是"模型之间并发"，不是"把一次请求拆开"。

--------------------------------------------------------
二、候选零件为什么要先筛一道
--------------------------------------------------------
计划第四节让系统"从内化剧情库选择逻辑连贯且尽量少重复使用的零件"。
字面上的做法是把**全部**可用零件摆给模型挑，但那不成立：
她的零件库迟早会有几百条，一条几百字，光零件就十几万字，
一次请求直接超出上下文。
所以后端先筛一个池子（默认 40 条），模型在这个池子里按因果链组合。

【筛法为什么是"分类轮转 + 池内按新鲜度排"，而不是"直接取次数最少的 40 条"】
她的零件是按主类归档的（外貌 / 神态 / 打斗 / 暧昧拉扯…）。
直接按次数取 40 条，很可能 38 条都落在同一两个类里 ——
因为那几个类她用得少，次数自然低。
大纲要的是"能串成一条因果链"的零件，得**跨类**才串得起来。
所以先按主类轮转（每轮每类取一条），类内再按次数少的先取。

【她也可能自己指定池子】payload 里带了 plot_ids 就用她的，一个字都不筛。
计划第 1 步明确要求"用户手动选择、添加和移除剧情零件"。

--------------------------------------------------------
三、取消到底能取消到什么程度
--------------------------------------------------------
不能。模型调用是一次阻塞的 HTTP 请求（大纲最长等 600 秒，而且只发一次），
Python 线程没法从外面把它掐断。所以"取消"的真实语义是：

    · 还在排队、还没发出去的模型  → 不发了
    · 已经在路上的                → 会跑完，结果照样落库

界面上必须把这句话说明白，否则她以为点了取消就不会花钱了。
（这也正是"取消/失败/重试不能造成重复结果"这条要求要小心的地方：
  跑完的那个模型结果留着，重试只补真正没跑成的那些，不会重花钱。）
"""

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from backend import db
from backend import classification as cls
from backend import outline_db as odb
from backend import plots_db as pdb


# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROMPT_FILE = "outline.txt"
PROMPT_VERSION = "v1"
PROMPT_VERSION_GENERIC = "v1-generic"

# 模板里必须存在的槽位。少一个就退回通用模板 + 把警告写进任务备注，
# 绝不静默丢内容（跟分类/内化同一条规矩）。
REQUIRED_SLOTS = ("worldview", "characters", "plots", "constraints",
                  "target_words", "learning_examples", "user_prompt")

# 补充提示词也归提示词库管，用自己的一档。
USER_PROMPT_KIND_OUTLINE = "outline"
USER_PROMPT_MAX = cls.USER_PROMPT_MAX

# ---- 任务状态 ----
RUN_QUEUED = "排队中"
RUN_RUNNING = "进行中"
RUN_COMPLETED = "已完成"
RUN_PARTIAL = "部分失败"
RUN_FAILED = "失败"
RUN_CANCELLED = "已取消"
ALL_RUN_STATUS = (RUN_QUEUED, RUN_RUNNING, RUN_COMPLETED, RUN_PARTIAL,
                  RUN_FAILED, RUN_CANCELLED)
RUN_ACTIVE = (RUN_QUEUED, RUN_RUNNING)

# ---- 单个模型的候选状态 ----
CAND_QUEUED = "排队中"
CAND_RUNNING = "生成中"
CAND_DONE = "已完成"
CAND_FAILED = "失败"

# 候选池默认多大。40 条 × 每条几百字 ≈ 一万多字，加上世界观和角色卡，
# 一次请求大概两三万字 —— 主流模型都吃得下，也不至于贵得离谱。
#
# 【这两个数是"上限"，不是"目标"】
# 库里可用零件不够时，有多少用多少（见 plan_candidate_plots 的轮转循环）。
# 所以 21 条零件 + 默认 40，实际就是 21 条全部进池子。
# 上限只在零件堆得比它多时才起作用。
DEFAULT_POOL = 40
# 上限从 80 提到 150：她的原话是"要求 AI 尽可能多地参考素材内化库"。
# 150 条 × 每条几百字 ≈ 四五万字，加世界观角色卡约五万字，
# DeepSeek / 硅基流动这一档的模型（128K 上下文）完全吃得下，
# 一次生成的钱也就几毛。超过 BIG_INPUT_WARN 时预览页会主动提醒她。
MAX_POOL = 150

# 送出去的总字数超过这个数就提醒她（不拦，但要说）。
BIG_INPUT_WARN = 60000

# 单次生成最多挑几个模型（odb.MAX_MODELS_PER_RUN 是同一个数的定义处）。
MAX_MODELS = odb.MAX_MODELS_PER_RUN

# 生成一次最多等多久（秒）。**必须比 llm.TIMEOUT(180) 大得多。**
#
# 为什么：llm 那套默认值是按"短回答"定的（分类、内化一次只吐几百字）。
# 大纲一次要吐 8000 字，单个模型跑一两分钟是常态 ——
# 实测通义千问 108 秒才写完，离 180 秒只剩 72 秒；
# 硅基流动那个 DeepSeek-V3.2（推理模型）稳定超过 180 秒，
# 结果就是每次生成都白等 9 分钟（180×3 次 + 退避）拿个必然失败。
#
# 600 秒 = 10 分钟，给慢模型留够写 8000 字 + 思考头寸。
# 超时会明确写进任务备注，界面也会显示"已等多久 / 最长等多久"，
# 不会让她对着一个不知道还要多久的转圈干等。
OUTLINE_TIMEOUT = 600

# 超时后还重试几次。**大纲设成 1，也就是不重试。**
#
# 理由有两层：
# ① 一个 10 分钟都没回话的请求，再发一次大概率还是 10 分钟没回话 ——
#    重试等于把等待时间翻倍，而她看到的还只是"在跑"。
# ② 超时是"我们这边不等了"，不是"服务端没算"。服务端很可能已经
#    把 8000 字生成完了，我们断线不影响它算完 —— 重试一次就多扣一次钱。
# 与其白花钱等一个不确定的结果，不如早点告诉她"这个模型这次不行，
# 换个快的"。
OUTLINE_MAX_RETRY = 1

_worker_lock = threading.Lock()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------
# 框架级通用模板（**这一份是可以公开的**）
#
# 三层提示词，别混（跟分类、内化完全一样的规矩）：
#   ① 这个常量        写在代码里，框架级通用版，公开仓库里靠它就能跑
#   ② prompts/outline.txt  她自己调的那版（真货），永不出仓库
#   ③ prompts/example.outline.txt  公开仓库里的占位示例
# ----------------------------------------------------------------------

GENERIC_OUTLINE_PROMPT = """你是"墨阁"的同人短篇大纲助手。

作者要写一篇同人短篇。你的任务是根据她给的世界观、角色卡、要求，
以及一批**从她自己的素材库里提炼出来的"剧情零件"**，
设计一份她可以直接照着动笔的细纲。

============================================================
一、四条硬边界（违反了这份大纲就作废）
============================================================

1. 世界观是硬约束。
   零件只能换人物和外壳，绝不能改变世界观规则。
   原作设定、时代、制度、能力规则，一个字都不许动。

2. 角色卡是硬约束。
   角色的行为必须符合她的性格、身份、目标、关系和能力。
   如果情节要求角色发生改变，必须写出**触发这个改变的过程**，
   不能让他"忽然就变成另一个人"。
   角色卡里"必须遵守"和"禁止出现"那两栏是绝对的，不许违反。

3. 一句话梗是核心方向。
   写了就必须围绕它组织故事，**不许换成另一个故事**。
   没写的话，你可以从世界观和角色关系里推一个核心出来，
   但要老实说明这是你推的。

4. 情节设计是她的要求，能满足的必须满足。
   跟世界观或角色卡打架、没法同时满足的，**列出来告诉她**，
   不许静默舍弃，也不许偷偷改掉她的要求。

============================================================
二、剧情零件怎么用
============================================================

零件是结构参考，不是内容。**绝对不许照搬零件里的原句、专有名词、
具体人名地名** —— 那些是别人作品里的东西，换成这个世界观里的人重新写。

零件要串成一条因果链，不是几个不相干的高潮片段。
推荐的组合逻辑（不要每样都用，但缺了因果链一定不行）：

    核心冲突 → 触发事件 → 升级 / 误会 / 阻碍 / 代价
    → 转折或真相揭露 → 收束 / 关系确认 / 开放式余味

【来源必须诚实】某个节点如果用了某条零件，就在那个节点的
source_plot_ids 里写上它的 plot_id。没用就别挂 ——
来源不明的零件比没有来源更糟。
**只准用本次给你的零件编号**，不许自己编号码。

**新鲜度**：给你的零件里有一部分标注了"已被用过 N 次"。
在同样合适的前提下，优先用用得少的。
但**绝不能为了用次数少的而牺牲逻辑** —— 一条次数很低却不合适的零件，
跳过它，并且在 logic_risks 里说明你为什么跳过。

============================================================
三、结构规模必须跟预期字数匹配
============================================================

{target_words}

字数不是最后显示一个数字就完事，它决定结构密度：

· 短篇**不许**出现十几个空洞小节；
· 8000 字比 6000 字多出来的那一两个节点，必须有实际作用
  （多一次转折、多一层代价），而不是把每段字数写大。
· 各节点 estimated_words 加起来要接近预期字数。

============================================================
四、每一段都要写到"能直接动笔"的程度
============================================================

**禁止**用「他们经历了一系列事件」「关系逐渐升温」这种话代替具体情节。
读者不知道发生了什么，作者也不知道该写什么。

每个节点至少说清：在哪、谁在、谁做了什么、冲突是什么、
情绪怎么变的、透露出什么信息、怎么接到下一段。

每一个高潮都要有前置铺垫，每一个转折都要有原因或信息来源。
结局必须回应故事核心，不能突然结束。

预计字数分配跟情节重要程度相称：开场紧一点，中段展开，
高潮和结局留够篇幅。

============================================================
五、输出格式（**只输出一个合法 JSON 对象，不要任何别的话**）
============================================================

{
  "title_candidates": ["标题一", "标题二"],
  "story_core": "一句话说明这篇真正讲什么",
  "theme_tone": "主题和情绪基调",
  "character_functions": [
    {"role": "角色名", "goal": "他想要什么", "obstacle": "什么挡着他",
     "change": "到结尾他变成了什么样"}
  ],
  "overview": "一段完整的因果链，把整篇串起来",
  "nodes": [
    {
      "node_id": "n1",
      "node_title": "这一段叫什么",
      "estimated_words": 900,
      "location_time": "在哪、什么时候",
      "participating_roles": ["角色名"],
      "purpose": "这一段在整篇里的作用",
      "event": "具体发生了什么",
      "character_action": "角色做了什么",
      "conflict": "冲突是什么",
      "emotional_change": "情绪怎么变的",
      "information_revealed": "透露出什么信息",
      "connection_to_next": "怎么接到下一段",
      "source_plot_ids": [12],
      "writing_notes": "写的时候要注意什么"
    }
  ],
  "climax": "高潮和转折是哪一段，为什么",
  "ending": "结局，以及它怎么回应故事核心",
  "logic_risks": ["需要作者确认的地方"]
}

字段说明：
· node_id 从 n1 往后编，**每一段的 node_id 必须不一样**。
· title_candidates 给 1～3 个。
· nodes 的顺序就是正文顺序。
· source_plot_ids 里只能出现本次给你的零件编号。
· logic_risks 里写：你自己觉得可能有问题的地方、跳过某条零件的原因、
  她的要求和世界观/角色卡打架的地方。没有就写空数组。

============================================================
六、作者的世界观与角色卡
============================================================

【世界观】
{worldview}

【角色卡】
{characters}

============================================================
七、本次可以用的剧情零件
============================================================

{plots}

============================================================
八、她的其他要求
============================================================

{constraints}

============================================================
九、她过去的修改习惯（只作参考，不许照抄任何具体内容）
============================================================

{learning_examples}

============================================================
十、她这次特意交代的
============================================================

{user_prompt}

记住最后一遍：**只输出一个合法 JSON 对象**，前后不要写任何解释。
"""


def prompt_template():
    """取这次要用的模板。返回 (模板文本, 来源, 警告语)。

    跟分类、内化同一个设计：来源只有 "file" / "builtin" 两种，
    三样一起返回，"这次到底跑的哪一版"必须可追溯。
    """
    path = os.path.join(ROOT_DIR, "prompts", PROMPT_FILE)
    if not os.path.isfile(path):
        return GENERIC_OUTLINE_PROMPT, "builtin", ""
    try:
        with open(path, encoding="utf-8") as f:
            txt = f.read().strip()
    except Exception as e:                                  # pragma: no cover
        return (GENERIC_OUTLINE_PROMPT, "builtin",
                "prompts/%s 读不了（%s），这次用通用模板" % (PROMPT_FILE, e))
    if not txt:
        return (GENERIC_OUTLINE_PROMPT, "builtin",
                "prompts/%s 是空的，这次用通用模板" % PROMPT_FILE)
    lost = [s for s in REQUIRED_SLOTS if ("{%s}" % s) not in txt]
    if lost:
        return (GENERIC_OUTLINE_PROMPT, "builtin",
                "prompts/%s 少了占位符 %s（那几处内容会被丢掉），"
                "这次改用通用模板" % (PROMPT_FILE, "、".join(lost)))
    return txt, "file", ""


def prompt_version():
    """这次会用哪一版提示词（任务记录里要存它，重跑两次要能对得上）。"""
    _, src, _ = prompt_template()
    return PROMPT_VERSION if src == "file" else PROMPT_VERSION_GENERIC


_SLOT_RE = re.compile(r"\{(" + "|".join(REQUIRED_SLOTS) + r")\}")


def _fill_slots(tpl, **slots):
    """把 {xxx} 换成实际内容。**单遍扫描**，不是逐个 replace。

    【为什么必须单遍】填进去的内容里有**她的小说世界观原文**和
    零件正文，里面出现花括号（写个算式、代码片段）完全不奇怪。
    先替换 worldview 再 replace user_prompt 的话，世界观里恰好写着
    "{user_prompt}" 那几个字的地方会被再替换一次 ——
    把别人的补充要求塞进正文中间，而且谁也不会发现。
    单遍正则只认自己定义的槽位名，每个位置只替换一次。
    （也不能用 str.format：模板正文里有 JSON 示例，大括号会打架。）
    """
    def _sub(m):
        return slots.get(m.group(1), m.group(0))
    return _SLOT_RE.sub(_sub, tpl)


# ----------------------------------------------------------------------
# 补充提示词（复用分类那条线的存储，换一个 kind）
# ----------------------------------------------------------------------

def get_user_prompt(owner):
    return cls.get_user_prompt(owner, kind=USER_PROMPT_KIND_OUTLINE)


def set_user_prompt(owner, content):
    """存她给大纲写的补充提示词。超长直接拒绝，不悄悄截断。"""
    return cls.set_user_prompt(owner, content, kind=USER_PROMPT_KIND_OUTLINE)


# ----------------------------------------------------------------------
# 候选零件：后端筛池
# ----------------------------------------------------------------------

def plan_candidate_plots(owner, data):
    """决定这次摆给模型看的零件池。

    返回 {"items": [...], "blocked": [...], "source": "auto"/"manual",
          "stuck": [...], "status_counts": {...}}

    auto：按主类轮转 + 类内按参考次数升序（理由见文件头第二节）
    manual：她自己在界面上勾的，一个字都不筛（但仅本地的那几条仍然拦）

    【stuck 是什么、为什么非要有】
    候选池只收「已确认 / 已编辑」。别的状态（最主要就是 AI 内化产出的
    「待确认」）在 list_candidate_plots 的 WHERE 里就被滤掉了 ——
    不进 items、不进 blocked、不出现在任何地方。所以她 AI 内化出 21 条零件、
    去生成大纲时看到的是"没有可用的剧情零件"，却完全不知道那 21 条
    就躺在库里，只差一个"确认"。stuck 就是把这批看不见的零件捞出来，
    让预览页能具体说出是哪几条、卡在哪一步。
    """
    data = data or {}
    # 状态统计与"被状态挡住的清单"：只为让她看得见，不参与筛池逻辑。
    # 两条分支都要带上，所以在最前面算一次。
    status_counts = odb.plot_status_counts(owner)
    stuck = odb.list_blocked_by_status(owner)
    manual = data.get("plot_ids")
    if isinstance(manual, list) and manual:
        ids = odb._int_list(manual, MAX_POOL, "剧情零件")
        items = odb.plot_blocks(owner, ids)
        # 状态不对的（她排除掉的）要挡回去，不能因为手动传了就放行 ——
        # 计划第四.1 节的条件跟"谁来选"无关。
        usable = {p["id"]: p for p in odb.list_candidate_plots(
            owner, include_blocked=True, limit=0)}
        bad = [p["id"] for p in items if p["id"] not in usable
               or usable[p["id"]]["status"] not in odb.PLOT_USABLE_STATUS]
        items = [p for p in items if p["id"] not in bad]
        blocked = [p for p in items if usable.get(p["id"], {}).get("local_only")]
        items = [p for p in items if not usable.get(p["id"], {}).get("local_only")]
        for p in items:
            p["ref_count"] = usable.get(p["id"], {}).get("ref_count", 0)
        return {"items": items, "blocked": blocked, "source": "manual",
                "dropped": bad, "stuck": stuck, "status_counts": status_counts}

    allp = odb.list_candidate_plots(owner, include_blocked=True, limit=0)
    blocked = [p for p in allp if p.get("local_only")]
    pool = [p for p in allp if not p.get("local_only")]

    want = int(data.get("pool_size") or DEFAULT_POOL)
    want = max(5, min(want, MAX_POOL))

    # ---- 按主类轮转 ----
    # 同一类里按"参考次数少的先"排；类的顺序按"这个类里最新那条的 id"倒序，
    # 所以她最近在用的类会先被轮到。
    by_cat = {}
    for p in pool:
        key = p.get("primary_category_id") or 0
        by_cat.setdefault(key, []).append(p)
    for k in by_cat:
        by_cat[k].sort(key=lambda x: (x.get("ref_count") or 0, -x["id"]))
    order = sorted(by_cat.keys(),
                   key=lambda k: -max(x["id"] for x in by_cat[k]))

    picked = []
    round_no = 0
    while len(picked) < want:
        added = False
        for k in order:
            lst = by_cat[k]
            if round_no < len(lst):
                picked.append(lst[round_no])
                added = True
                if len(picked) >= want:
                    break
        if not added:
            break
        round_no += 1

    return {"items": picked, "blocked": blocked, "source": "auto",
            "dropped": [], "stuck": stuck, "status_counts": status_counts}


# ----------------------------------------------------------------------
# 拼提示词
# ----------------------------------------------------------------------

def _worldview_block(text):
    t = (text or "").strip()
    return t if t else "（她没写世界观 —— 这种情况不该发生生成，请先让她补上。）"


def _characters_block(chars):
    """角色卡块。八项一项不落地摊开 —— 少一项模型就多猜一样。"""
    if not chars:
        return "（一张角色卡都没关联。）"
    parts = []
    for i, c in enumerate(chars, 1):
        lines = ["--- 角色 %d：%s ---" % (i, c.get("name") or "（没名字）")]
        for f in ("identity", "personality", "goal", "fear", "relations",
                  "speech"):
            v = (c.get(f) or "").strip()
            if v:
                lines.append("%s：%s" % (odb.CHAR_FIELD_LABELS[f], v))
        must = (c.get("must_do") or "").strip()
        never = (c.get("never_do") or "").strip()
        if must:
            lines.append("必须遵守（绝对不能违反）：%s" % must)
        if never:
            lines.append("禁止出现（绝对不能出现）：%s" % never)
        if len(lines) == 1:
            lines.append("（这张卡只有名字，其他都没填 —— 你不知道的就别编。）")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _plots_block(items):
    """零件块。每条带上"已被用过几次"，让模型自己权衡新鲜度。"""
    if not items:
        return ("（这次没有可用的剧情零件。请完全依据世界观、角色卡和她的要求"
                "设计结构，并在 logic_risks 里说明这一点。）")
    parts = []
    for i, p in enumerate(items, 1):
        n = p.get("ref_count") or 0
        lines = ["--- 剧情零件 %d（plot_id=%s）---" % (i, p.get("id"))]
        if p.get("title"):
            lines.append("名字：%s" % p["title"])
        if p.get("plot_type"):
            lines.append("类型：%s" % p["plot_type"])
        if p.get("category_name"):
            lines.append("归类：%s" % p["category_name"])
        if p.get("summary"):
            lines.append("讲的是什么：%s" % p["summary"])
        beats = p.get("beats") or {}
        if isinstance(beats, dict) and beats:
            bl = []
            for k in pdb.BEAT_KEYS:
                v = (beats.get(k) or "").strip()
                if v:
                    bl.append("    %s：%s" % (pdb.BEAT_LABELS.get(k, k), v))
            if bl:
                lines.append("情节节点：\n" + "\n".join(bl))
        slots = p.get("role_slots") or []
        if slots:
            lines.append("角色位：%s" % "、".join(slots))
        hints = p.get("usage_hints") or []
        if hints:
            lines.append("适合用在：%s" % "、".join(hints))
        lines.append("已被用过 %d 次。" % n)
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _target_words_block(target_words, tier):
    lo, hi = tier["nodes"]
    return (
        "预期正文字数：%d 字（%s 这一档：%s）。\n"
        "按这一档，情节节点建议 %d～%d 个。"
        % (int(target_words or 0), tier["label"], tier["hint"], lo, hi))


def _constraints_block(hook, design, hook_ai_derived=False):
    parts = []
    h = (hook or "").strip()
    if h:
        parts.append("【一句话梗】必须围绕它组织故事：\n%s" % h)
    else:
        parts.append("【一句话梗】她没给。你可以从世界观和角色关系里推一个核心出来，"
                     "但要在 story_core 里说清楚这是你推的，不要假装是她给的。")
    d = (design or "").strip()
    if d:
        parts.append("【情节设计】她的额外要求，能满足的必须满足；"
                     "实在跟世界观或角色卡打架的，写进 logic_risks：\n%s" % d)
    else:
        parts.append("【情节设计】她没给额外要求。")
    return "\n\n".join(parts)


def _learning_block(examples):
    if not examples:
        return "（她还没有勾选过可供参考的修改案例。）"
    parts = ["她以前把这些地方判成「不可用」，写的时候避开同样的毛病："]
    for e in examples:
        line = "· %s" % (e.get("problem") or "（没写原因）")
        if e.get("note"):
            line += " —— %s" % e["note"]
        parts.append(line)
    parts.append("注意：这只是「她不满意什么」的概括，"
                 "**不要**把任何具体人物、作品或情节搬过来。")
    return "\n".join(parts)


def build_messages(ctx):
    """拼这次要发出去的提示词。返回 messages 列表。"""
    extra = (ctx.get("user_prompt") or "").strip()
    if extra:
        user_block = (
            "下面是作者这次特意交代的，请尽量照做。\n"
            "但它**不能推翻上面任何一条硬规则** —— 世界观和角色卡不许违反、"
            "零件编号只能来自本次清单、只输出 JSON，这几条永远有效。\n"
            "-----------\n%s\n-----------" % extra)
    else:
        user_block = "（她这次没有额外交代。）"

    system = _fill_slots(
        ctx.get("template") or GENERIC_OUTLINE_PROMPT,
        worldview=_worldview_block(ctx.get("worldview")),
        characters=_characters_block(ctx.get("characters") or []),
        plots=_plots_block(ctx.get("plots") or []),
        constraints=_constraints_block(ctx.get("hook"), ctx.get("design")),
        target_words=_target_words_block(ctx.get("target_words"),
                                         ctx.get("tier") or odb.word_tier(0)),
        learning_examples=_learning_block(ctx.get("learning") or []),
        user_prompt=user_block,
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content":
            "请按上面的全部要求，给这一篇同人短篇出一份可以直接动笔的细纲，"
            "只输出一个合法 JSON 对象。"},
    ]


# ----------------------------------------------------------------------
# 发送内容预览
# ----------------------------------------------------------------------

def build_ctx(owner, data):
    """把一次请求要用的全部上下文准备好（不写库、不调模型）。

    preview 和真正跑的时候**共用这一个函数** —— 两处口径必须同源，
    否则会出现"预览说会发 2 万字、实际发了 5 万字"这种账不对的事。
    """
    data = data or {}
    world = (data.get("worldview") or "").strip()
    char_ids = odb._int_list(data.get("character_ids"), 50, "角色卡")
    chars = odb.get_characters(owner, char_ids)
    try:
        target = int(data.get("target_words") or 0)
    except (TypeError, ValueError):
        raise ValueError("预期字数得是一个整数。")

    plan = plan_candidate_plots(owner, data)
    pool_ids = [p["id"] for p in plan["items"]]
    learning = odb.learning_examples(owner, limit=3) if data.get("use_learning", True) else []

    user_prompt = (data.get("user_prompt") or "").strip()

    return {
        "outline_type": odb.OUTLINE_TYPE_TW,
        "worldview": world,
        "world_name": (data.get("world_name") or "").strip(),
        "worldview_id": data.get("worldview_id") or None,
        "character_ids": char_ids,
        "characters": chars,
        "hook": (data.get("one_sentence_hook") or "").strip(),
        "design": (data.get("plot_design") or "").strip(),
        "target_words": target,
        "tier": odb.word_tier(target),
        "plots": plan["items"],
        "pool_ids": pool_ids,
        "blocked": plan["blocked"],
        "pool_source": plan["source"],
        "dropped": plan.get("dropped") or [],
        # 库里哪些零件"在、但状态够不上可用"（最典型：AI 内化出来的「待确认」）。
        # 预览页拿它报数 —— 不报的话，她只会看到"没有可用的剧情零件"，
        # 然后以为 AI 不肯用她辛苦做出来的内化库。
        "stuck": plan.get("stuck") or [],
        "plot_status_counts": plan.get("status_counts") or {},
        "learning": learning,
        "user_prompt": user_prompt,
    }


def preview_input(owner, data):
    """"这次到底会发出去什么"的完整预览。一个字都不写库，也不调模型。

    计划第十一节第 6 条明确要求：`用户点击生成前显示将发送的字数、模型
    和隐私提示`。所以这里把三样都算出来。
    """
    ctx = build_ctx(owner, data)

    # ---- 校验 ----
    problems = []
    if not ctx["worldview"]:
        problems.append({"field": "worldview",
                         "message": "世界观是必填的。可以直接粘贴，也可以上传 txt。"})
    if not ctx["characters"]:
        problems.append({"field": "characters",
                         "message": "至少要关联一张角色卡。"})
    if ctx["target_words"] < odb.TARGET_WORDS_MIN:
        problems.append({"field": "target_words",
                         "message": "预期字数至少 %d 字。" % odb.TARGET_WORDS_MIN})
    if ctx["target_words"] > odb.TARGET_WORDS_WARN:
        problems.append({"field": "target_words", "level": "warn",
                         "message": "%d 字已经接近中短篇了，结构规模会跟短篇很不一样。"
                                    "确认的话可以直接开始。"
                                    % ctx["target_words"]})
    if not ctx["plots"]:
        stuck = ctx["stuck"]
        if stuck:
            # 有零件、但一条都够不上可用 —— 这是最容易被误读成
            # "AI 不肯参考我的内化库"的情形。所以报成 bad（红色）、
            # 说清是几条、卡在哪个状态、去哪儿改。
            by = {}
            for x in stuck:
                by[x.get("status") or "?"] = by.get(x.get("status") or "?", 0) + 1
            how = "、".join("%s %d 条" % (k, v) for k, v in by.items())
            problems.append({
                "field": "plots",
                "message": "你库里有 %d 条剧情零件，可这次一条都用不上 —— 他们的状态是"
                           "：%s。候选池只收「已确认」和「已编辑」，"
                           "因为这些零件是 AI 提的、还没经过你的眼。"
                           "先去【剧情内化】把它们确认（弹层里或列表上都有确认按钮），"
                           "再回来生成 —— 不然这次 AI 只拿得到世界观和角色卡，"
                           "你内化的素材一条都参考不到。"
                           % (len(stuck), how)})
        else:
            problems.append({"field": "plots", "level": "warn",
                             "message": "一条可用的剧情零件都没有。"
                                        "大纲会完全靠世界观和角色卡推 —— "
                                        "先去【剧情内化】把零件确认几条会更好。"})

    # ---- 模型 ----
    keys = data.get("model_keys") or []
    if isinstance(keys, str):
        keys = [keys]
    public = {m["key"]: m for m in cls.llm.public_models()} \
        if hasattr(cls, "llm") else {}
    models = []
    for k in keys[:MAX_MODELS]:
        try:
            cfg = cls.pick_model(k if isinstance(k, str) else k.get("key"))
        except ValueError as e:
            models.append({"key": str(k), "label": str(k), "usable": False,
                           "reason": str(e)})
            continue
        models.append({"key": cfg["key"], "label": cfg.get("label") or cfg["key"],
                       "model": cfg.get("model") or "", "usable": True})
    if len(keys) > MAX_MODELS:
        problems.append({"field": "model_keys",
                         "message": "一次最多挑 %d 个模型，后面那几个没算进来。"
                                    % MAX_MODELS})
    if not models:
        problems.append({"field": "model_keys",
                         "message": "一个模型都没选。去「模型设置」里至少配一个。"})
    elif not any(m["usable"] for m in models):
        problems.append({"field": "model_keys",
                         "message": "选中的模型都用不了（多半是还没填 API Key）。"})

    # ---- 发送字数 ----
    tpl, src, warn = prompt_template()
    probe = dict(ctx)
    probe["template"] = tpl
    msgs = build_messages(probe)
    chars = sum(len(m["content"]) for m in msgs)
    if chars > BIG_INPUT_WARN:
        problems.append({"field": "size", "level": "warn",
                         "message": "这次要发出去约 %d 字，比较长。"
                                    "可以把零件池调小一点，或者少挑几条零件。"
                                    % chars})

    blocked = ctx["blocked"]
    return {
        "ok": not any(p.get("level") != "warn" for p in problems),
        "problems": problems,
        "worldview_chars": len(ctx["worldview"]),
        "world_name": ctx["world_name"],
        "character_ids": ctx["character_ids"],
        "characters": [{"id": c["id"], "name": c["name"],
                        "filled": c["filled"], "field_total": c["field_total"]}
                       for c in ctx["characters"]],
        "hook": ctx["hook"],
        "design": ctx["design"],
        "target_words": ctx["target_words"],
        "tier": {"key": ctx["tier"]["key"], "label": ctx["tier"]["label"],
                 "nodes": list(ctx["tier"]["nodes"]),
                 "hint": ctx["tier"]["hint"]},
        "plots": [{"id": p["id"], "title": p["title"], "plot_type": p["plot_type"],
                   "category_name": p.get("category_name") or "",
                   "ref_count": p.get("ref_count") or 0}
                  for p in ctx["plots"]],
        "plot_source": ctx["pool_source"],
        "blocked_plots": [{"id": p["id"], "title": p["title"]}
                          for p in blocked],
        # "在库里、但状态够不上可用"的那批（最典型就是 AI 内化出来的「待确认」）。
        # 光报一个数她还不知道自己该去改哪几条，所以连名字一起给。
        "stuck_plots": [{"id": p["id"], "title": p["title"],
                         "status": p["status"]} for p in ctx["stuck"]],
        "plot_status_counts": ctx["plot_status_counts"],
        # 库里可用零件总数（各状态相加，不查库）。她要"尽可能多参考"，
        # 就得先知道自己手上到底有多少条能用。
        "usable_total": sum(n for s, n in ctx["plot_status_counts"].items()
                            if s in odb.PLOT_USABLE_STATUS),
        "pool_want": int(data.get("pool_size") or DEFAULT_POOL),
        "pool_max": MAX_POOL,
        "learning_cases": len(ctx["learning"]),
        "models": models,
        "send_chars": chars,
        "prompt_version": PROMPT_VERSION if src == "file" else PROMPT_VERSION_GENERIC,
        "prompt_src": src,
        "prompt_warning": warn,
        "privacy": _privacy_note(ctx, chars),
        "messages_preview": msgs[0]["content"],
    }


def _privacy_note(ctx, chars):
    """计划第十一节第 6 条要的隐私提示。用大白话写清楚发的是什么。"""
    n = len(ctx["plots"])
    parts = [
        "这次会把下面这些内容发给你选的模型服务商：",
        "· 世界观 %d 字" % len(ctx["worldview"]),
        "· 角色卡 %d 张" % len(ctx["characters"]),
        "· 剧情零件 %d 条" % n,
    ]
    if ctx["hook"]:
        parts.append("· 一句话梗")
    if ctx["design"]:
        parts.append("· 情节设计")
    if ctx["user_prompt"]:
        parts.append("· 你写的补充提示词")
    parts.append("合计约 %d 字。" % chars)
    if ctx["blocked"]:
        parts.append(
            "另外有 %d 条零件来自标了「仅本地」的素材，已经自动排除、不会发出。"
            % len(ctx["blocked"]))
    parts.append("这些都是你自己的数据，除了你选的模型之外不会给任何人。")
    return "\n".join(parts)


# ----------------------------------------------------------------------
# 发起任务
# ----------------------------------------------------------------------

def create_run(owner, data, background=True, retry_of_run_id=None,
               model_keys=None, ctx=None):
    """发起一次生成。返回 (结果字典, run_id)。

    立刻返回 run_id，活儿在后台线程里干（计划第十四.11：
    `不要把大纲生成任务放在长时间同步 HTTP 请求中`）。
    """
    data = data or {}

    # ---- 世界观 / 角色卡 / 字数的硬校验，在**建任务之前**做 ----
    # 放在这儿而不是线程里，是为了让她立刻看到"哪儿没填"，
    # 而不是等半分钟之后从任务状态里读出"失败"。
    ctx = ctx or build_ctx(owner, data)
    if not ctx["worldview"]:
        return {"ok": False, "reason": "no_worldview",
                "message": "世界观是必填的 —— 粘贴一段或者传个 txt 都行。"}, None
    if not ctx["characters"]:
        return {"ok": False, "reason": "no_character",
                "message": "至少要关联一张角色卡。"}, None
    if ctx["target_words"] < odb.TARGET_WORDS_MIN:
        return {"ok": False, "reason": "bad_words",
                "message": "预期字数至少 %d 字。" % odb.TARGET_WORDS_MIN}, None

    keys = model_keys if model_keys is not None else (data.get("model_keys") or [])
    if isinstance(keys, str):
        keys = [keys]
    keys = [str(k).strip() for k in keys if str(k or "").strip()]
    if not keys:
        try:
            keys = [cls.pick_model(None)["key"]]
        except ValueError as e:
            return {"ok": False, "reason": "no_model",
                    "message": str(e)}, None
    keys = keys[:MAX_MODELS]

    ok_keys, bad = [], []
    for k in keys:
        try:
            cls.pick_model(k)
            ok_keys.append(k)
        except ValueError as e:
            bad.append({"key": k, "message": str(e)})
    if not ok_keys:
        return {"ok": False, "reason": "no_model",
                "message": bad[0]["message"] if bad else "选中的模型都用不了。"}, None

    # 提示词快照 + 提示词库引用（跟内化一个规矩：任务级快照）
    prompt_id = 0
    try:
        prompt_id = int(data.get("prompt_id") or 0)
    except (TypeError, ValueError):
        prompt_id = 0
    if prompt_id:
        try:
            item = cls.resolve_prompt_for_use(owner, prompt_id,
                                             kind=USER_PROMPT_KIND_OUTLINE)
        except ValueError as e:
            return {"ok": False, "reason": "no_prompt", "message": str(e)}, None
        if not item:
            return {"ok": False, "reason": "no_prompt",
                    "message": "找不到你挑的那条提示词，可能已经被删了。"}, None
        ctx["user_prompt"] = item["content"]
        ctx["prompt_ref_id"] = item["id"]
        ctx["prompt_name"] = item.get("name") or ""
        ctx["prompt_owner"] = item.get("owner_id") or ""
    else:
        ctx["prompt_ref_id"] = 0
        ctx["prompt_name"] = ""
        ctx["prompt_owner"] = ""
        ctx["user_prompt"] = odb._txt(ctx.get("user_prompt"), USER_PROMPT_MAX,
                                      "补充提示词")

    tpl, src, warn = prompt_template()
    ctx["template"] = tpl
    ctx["prompt_src"] = src
    ctx["prompt_version"] = PROMPT_VERSION if src == "file" else PROMPT_VERSION_GENERIC

    # 发送字数（预览里已经算过，这里再算一次存进任务里）
    msgs = build_messages(ctx)
    send_chars = sum(len(m["content"]) for m in msgs)

    input_snapshot = {
        "worldview": ctx["worldview"],
        "world_name": ctx["world_name"],
        "worldview_id": ctx["worldview_id"],
        "character_ids": ctx["character_ids"],
        "character_snapshot": ctx["characters"],
        "one_sentence_hook": ctx["hook"],
        "plot_design": ctx["design"],
        "target_words": ctx["target_words"],
        "pool_source": ctx["pool_source"],
        "blocked_plot_ids": [p["id"] for p in ctx["blocked"]],
    }

    ts = now_str()
    with _worker_lock:
        with db.connect() as conn:
            # 【一个账号同时只能有一个大纲任务在跑】
            # 跟内化那边同一个判断：她连点两下、或者两个页面同时提交，
            # 两份任务各自并发打模型，钱是双份的，而且她自己都不知道。
            row = conn.execute(
                "SELECT id FROM outline_runs WHERE owner_id=? AND status IN (%s)"
                % ",".join("?" * len(RUN_ACTIVE)),
                [owner] + list(RUN_ACTIVE)).fetchone()
            if row and not retry_of_run_id:
                return {"ok": False, "reason": "busy",
                        "message": "已经有一个大纲任务在跑了（#%d）。"
                                   "等它跑完，或者先去把它取消。" % row["id"]}, None
            cur = conn.execute(
                """INSERT INTO outline_runs
                   (owner_id, outline_type, input_json, target_words,
                    model_keys_json, prompt_version, prompt_ref_id, prompt_name,
                    user_prompt, user_prompt_len, cand_plot_ids_json, status,
                    total_models, total_input_chars, retry_of_run_id,
                    created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (owner, ctx["outline_type"], odb._dumps(input_snapshot),
                 ctx["target_words"], odb._dumps(ok_keys),
                 ctx["prompt_version"], ctx["prompt_ref_id"], ctx["prompt_name"],
                 ctx["user_prompt"], len(ctx["user_prompt"]),
                 odb._dumps(ctx["pool_ids"]), RUN_QUEUED, len(ok_keys),
                 send_chars, retry_of_run_id, ts))
            run_id = cur.lastrowid

    if background:
        t = threading.Thread(target=_run_worker, args=(run_id, owner),
                             name="outline-run-%d" % run_id, daemon=True)
        t.start()
        return {"ok": True, "run_id": run_id, "status": RUN_QUEUED,
                "model_count": len(ok_keys), "models": ok_keys,
                "send_chars": send_chars,
                "skipped_models": bad,
                "message": "已开始，%d 个模型正在分别生成。" % len(ok_keys)}, run_id

    _execute(run_id, owner)
    return {"ok": True, "run_id": run_id, "status": RUN_COMPLETED}, run_id


def _run_worker(run_id, owner):
    """后台线程的入口。什么都兜住 —— 线程里冒出来的异常没人接。"""
    try:
        _execute(run_id, owner)
    except Exception as e:                                   # pragma: no cover
        import traceback
        traceback.print_exc()
        try:
            with db.connect() as conn:
                _set_run(conn, run_id,
                         status=RUN_FAILED,
                         error="任务跑了但中途出错：%s" % e,
                         finished_at=now_str())
        except Exception:
            pass


def _set_run(conn, run_id, **fields):
    if not fields:
        return
    sets = ", ".join("%s=?" % k for k in fields)
    conn.execute("UPDATE outline_runs SET %s WHERE id=?" % sets,
                 list(fields.values()) + [run_id])


def _cancelled(run_id):
    with db.connect() as conn:
        r = conn.execute("SELECT status FROM outline_runs WHERE id=?",
                         (run_id,)).fetchone()
    return bool(r) and r["status"] == RUN_CANCELLED


# ----------------------------------------------------------------------
# 真正干活
# ----------------------------------------------------------------------

def _execute(run_id, owner):
    with db.connect() as conn:
        run = conn.execute("SELECT * FROM outline_runs WHERE id=?",
                           (run_id,)).fetchone()
        if not run:
            return
        if run["status"] == RUN_CANCELLED:
            return
        model_keys = odb._loads(run["model_keys_json"], [])
        input_snapshot = odb._loads(run["input_json"], {})
        pool_ids = odb._loads(run["cand_plot_ids_json"], [])
        prompt_version = run["prompt_version"] or prompt_version()
        user_prompt = run["user_prompt"] or ""

    # ---- 模板在任务开头解析一次就固定下来 ----
    # 为什么不在每个模型里现读文件：她跑的过程中改了 prompts/outline.txt，
    # 同一个任务的几个模型就会用两版提示词生成 —— 结果并排放着，
    # 而她以为差别只是模型不同。
    tpl, src, warn = prompt_template()

    ctx = {
        "template": tpl,
        "prompt_src": src,
        "worldview": input_snapshot.get("worldview") or "",
        "characters": input_snapshot.get("character_snapshot") or [],
        "hook": input_snapshot.get("one_sentence_hook") or "",
        "design": input_snapshot.get("plot_design") or "",
        "target_words": int(input_snapshot.get("target_words") or 0),
        "tier": odb.word_tier(input_snapshot.get("target_words") or 0),
        "plots": odb.plot_blocks(owner, pool_ids),
        "user_prompt": user_prompt,
        "learning": odb.learning_examples(owner, limit=3),
        "model_keys": model_keys,
        "prompt_version": prompt_version,
    }
    # 零件块要把参考次数带上（模型要按"用过几次"权衡新鲜度）
    counts = odb.plot_ref_counts(owner, pool_ids) if pool_ids else {}
    for p in ctx["plots"]:
        p["ref_count"] = counts.get(p["id"], 0)

    with db.connect() as conn:
        _set_run(conn, run_id, status=RUN_RUNNING, started_at=now_str(),
                 heartbeat_at=now_str(), error=warn or "")
        for mk in model_keys:
            conn.execute(
                """INSERT OR IGNORE INTO outline_candidates
                   (run_id, owner_id, model_key, model_name, prompt_version,
                    status, created_at) VALUES (?,?,?,?,?,?,?)""",
                (run_id, owner, mk, _model_label(mk), prompt_version,
                 CAND_QUEUED, now_str()))

    # ---- 多模型并发 ----
    # 为什么并发：她挑四个模型，串行跑要等四倍时间（一次一两分钟），
    # 而四个模型的输入完全一样、互不依赖。
    # 为什么有上限（MAX_MODELS=4）：并发打同一家的限流，八个请求
    # 多半集体 429。而且八份候选她也比不过来。
    todo = [k for k in model_keys]
    if todo:
        with ThreadPoolExecutor(max_workers=min(MAX_MODELS, len(todo))) as ex:
            for mk in todo:
                if _cancelled(run_id):
                    break
                ex.submit(_run_one_model, run_id, owner, mk, ctx)

    # ---- 汇总 ----
    with db.connect() as conn:
        r = conn.execute("SELECT status FROM outline_runs WHERE id=?",
                         (run_id,)).fetchone()
        cur_status = r["status"] if r else RUN_RUNNING
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM outline_candidates"
            " WHERE run_id=? GROUP BY status", (run_id,)).fetchall()
        by = {x["status"]: x["n"] for x in rows}
        done = by.get(CAND_DONE, 0)
        failed = by.get(CAND_FAILED, 0)
        if cur_status == RUN_CANCELLED:
            # 取消之后**不改状态**（她自己点的取消，界面已经显示了），
            # 但已经把结果补写进去了 —— 跑完的不浪费。
            _set_run(conn, run_id, done_models=done, failed_models=failed,
                     heartbeat_at=now_str(), finished_at=now_str())
        else:
            if done and not failed:
                st = RUN_COMPLETED
            elif done and failed:
                st = RUN_PARTIAL
            else:
                st = RUN_FAILED
            errs = [x["error"] for x in conn.execute(
                "SELECT error FROM outline_candidates WHERE run_id=?"
                " AND status=? AND error<>''", (run_id, CAND_FAILED)).fetchall()]
            _set_run(conn, run_id, status=st, done_models=done,
                     failed_models=failed,
                     error="；".join(errs[:2])[:2000],
                     heartbeat_at=now_str(), finished_at=now_str())


def _model_label(key):
    try:
        m = cls.llm.get_model(key)
        return (m.get("label") or key) if m else key
    except Exception:                                        # pragma: no cover
        return key


def _run_one_model(run_id, owner, model_key, ctx):
    """一个模型的一次生成。**每个模型只碰自己那一行候选**，
    绝不读别的模型的结果（计划第五.5：不同模型不能互相读取结果）。"""
    t0 = time.time()
    label = _model_label(model_key)
    with db.connect() as conn:
        conn.execute("UPDATE outline_candidates SET status=?, model_name=?"
                     " WHERE run_id=? AND model_key=?",
                     (CAND_RUNNING, label, run_id, model_key))
        _set_run(conn, run_id, heartbeat_at=now_str())

    def _fail(msg):
        with db.connect() as conn:
            conn.execute(
                "UPDATE outline_candidates SET status=?, error=?, elapsed_ms=?"
                " WHERE run_id=? AND model_key=?",
                (CAND_FAILED, (msg or "")[:4000], int((time.time() - t0) * 1000),
                 run_id, model_key))

    try:
        cfg = cls.pick_model(model_key)
    except ValueError as e:
        _fail(str(e))
        return

    msgs = build_messages(ctx)
    input_chars = sum(len(m["content"]) for m in msgs)
    # 超时和重试次数显式给。吃 llm 的默认（180 秒 × 3 次）等于必然白等 9 分钟，
    # 理由见 OUTLINE_TIMEOUT / OUTLINE_MAX_RETRY 那两段注释。
    _opts = dict(temperature=0.7, timeout=OUTLINE_TIMEOUT,
                 max_retry=OUTLINE_MAX_RETRY)
    try:
        out = cls.llm.chat(cfg, msgs, json_mode=True, **_opts)
    except Exception as e:
        # json_mode 不被支持时退一次（跟分类、内化同一套兜底）
        if getattr(e, "status", None) == 400:
            try:
                out = cls.llm.chat(cfg, msgs, json_mode=False, **_opts)
            except Exception as e2:
                _fail(str(e2))
                return
        else:
            _fail(str(e))
            return

    raw = out.get("content") or ""
    if not raw.strip():
        _fail("模型返回了空内容（可能是被截断或触发了内容策略）。")
        return

    # ---- 解析 + 校验 ----
    known = {p["id"] for p in ctx["plots"]}
    try:
        payload = _extract_json_object(raw)
        obj, warns = odb.clean_outline_payload(payload, known)
    except ValueError as e:
        _fail("模型的返回没法当成大纲用：%s" % e)
        return
    except Exception as e:                                   # pragma: no cover
        _fail("读模型返回时出错：%s" % e)
        return

    warns = list(warns) + odb.validate_outline(obj, ctx["target_words"], known)

    # ---- 结束原因 ----
    # 「结构体检」只能看出"缺结局、缺高潮"这类**内容形态**问题；
    # 它看不出"这篇是被硬掐断的" —— 而后者才是最会骗人的一种：
    # 正文半截，可每个字段单独看都合法、模型自填的预计字数也照旧。
    # 所以撞到字数上限时，把这条提醒插到**最前面**，让她扫一眼就知道
    # 这版不能直接拿去写。
    finish = (out.get("finish_reason") or "").strip() or cls.llm.FINISH_UNKNOWN
    if finish == cls.llm.FINISH_LENGTH:
        warns.insert(0, "这一版被字数上限掐断了，模型没写完 —— "
                        "最后一段是断的。要么少参考几条重跑，"
                        "要么把它当草稿接着往下补。")
    elif finish == cls.llm.FINISH_FILTER:
        warns.insert(0, "这一版被内容策略拦下了，内容不完整。")

    # 结构缺口单独算一份存下来：列表接口 /api/outline-runs 不带 content_json，
    # 而候选卡上那行"体检结论"要在列表里就显示得出来。
    gaps = odb.structure_gaps(obj)

    text = odb.render_outline_text(obj)
    usage = out.get("usage") or {}
    names = {p["id"]: (p.get("title") or "") for p in ctx["plots"]}

    with db.connect() as conn:
        conn.execute(
            """UPDATE outline_candidates SET status=?, content_json=?,
               content_text=?, used_plot_ids_json=?, used_plot_names_json=?,
               warnings_json=?, raw_response=?, input_chars=?, output_chars=?,
               input_tokens=?, output_tokens=?, finish_reason=?, gaps_json=?,
               elapsed_ms=?, model_name=?, prompt_version=?
               WHERE run_id=? AND model_key=?""",
            (CAND_DONE, odb._dumps(obj), text,
             odb._dumps(obj.get("used_plot_ids") or []),
             odb._dumps([names.get(i, "") for i in (obj.get("used_plot_ids") or [])]),
             odb._dumps(warns), raw[:200000], input_chars, len(raw),
             int(usage.get("prompt_tokens") or 0),
             int(usage.get("completion_tokens") or 0), finish, odb._dumps(gaps),
             int((time.time() - t0) * 1000), label,
             ctx.get("prompt_version") or "", run_id, model_key))
        _set_run(conn, run_id, heartbeat_at=now_str())


# ----------------------------------------------------------------------
# 读模型返回
# ----------------------------------------------------------------------

class _NotOutlineJson(Exception):
    """抠出来的东西顶层不是对象。

    【为什么要有这么个小异常】不能直接用 ValueError 当标记 ——
    json.loads 解不开时抛的 JSONDecodeError **就是** ValueError 的子类，
    用 ValueError 区分"JSON 语法错"和"顶层类型不对"会把两者混成一种：
    于是"前面有废话、后面有废话"这种最常见的返回会走不到"截大括号重试"
    那一步，直接判成失败。血泪教训，别合回去。
    """
    pass


def _extract_json_object(text):
    """从模型返回的文本里抠出 JSON 对象。

    模型很爱在 JSON 外面裹东西：```json 围栏、「好的，这就给你」、
    末尾再补一句「以上」。这些都是常态，不是极端情况。
    三步走：剥围栏 → 直接 parse → 找第一个 { 到最后一个 } 截出来 parse。
    """
    s = (text or "").strip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl >= 0:
            s = s[nl + 1:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3].rstrip()

    def _as_obj(d):
        if isinstance(d, dict):
            return d
        if isinstance(d, list):
            return {"nodes": d}          # 只给了节点数组，包一层
        raise _NotOutlineJson("返回的顶层是 %s，不是对象。" % type(d).__name__)

    try:
        return _as_obj(json.loads(s))
    except _NotOutlineJson as e:
        raise ValueError(str(e))
    except Exception:
        pass

    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        try:
            return _as_obj(json.loads(s[i:j + 1]))
        except _NotOutlineJson as e:
            raise ValueError(str(e))
        except Exception:
            pass

    raise ValueError("模型的返回里找不到 JSON 对象。它说的前 200 字：%s" % s[:200])


# ----------------------------------------------------------------------
# 任务：读 / 取消 / 重试
# ----------------------------------------------------------------------

def _run_dict(row, with_input=False):
    if row is None:
        return None
    inp = odb._loads(row["input_json"], {})
    d = {
        "id": row["id"],
        "outline_type": row["outline_type"],
        "status": row["status"],
        "target_words": row["target_words"],
        "model_keys": odb._loads(row["model_keys_json"], []),
        "prompt_version": row["prompt_version"],
        "prompt_name": row["prompt_name"],
        "user_prompt_len": row["user_prompt_len"],
        "cand_plot_count": len(odb._loads(row["cand_plot_ids_json"], [])),
        "blocked_plot_count": len(inp.get("blocked_plot_ids") or []),
        "world_chars": len(inp.get("worldview") or ""),
        "character_count": len(inp.get("character_snapshot") or []),
        "has_hook": bool(inp.get("one_sentence_hook")),
        "has_design": bool(inp.get("plot_design")),
        "total_models": row["total_models"],
        "done_models": row["done_models"],
        "failed_models": row["failed_models"],
        "total_input_chars": row["total_input_chars"],
        "saved_outline_id": row["saved_outline_id"],
        "retry_of_run_id": row["retry_of_run_id"],
        "note": row["note"],
        "error": row["error"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }
    # 【为什么只有单查才带 input】
    # 里面是世界观原文（最多三万字）和角色卡快照。列表一次给 8 条，
    # 全带上就是几十万字的白流量，而她列表上根本不用看这些。
    #
    # 【为什么前端需要它】她把一版候选推入大纲库时，要写进大纲的
    # 「世界观快照」必须是**生成那一刻**的那一份。从界面上现取的话，
    # 她要是生成完又改了世界观，存进大纲的就成了她新写的那段 ——
    # 跟 AI 实际看到的不一样，以后复盘时怎么都对不上。
    if with_input:
        d["input"] = inp
    return d


def get_run(run_id, owner):
    try:
        rid = int(run_id)
    except (TypeError, ValueError):
        return None
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM outline_runs WHERE id=? AND owner_id=?",
                           (rid, owner)).fetchone()
        if not row:
            return None
        d = _run_dict(row, with_input=True)
        cands = conn.execute(
            "SELECT id, model_key, model_name, status, error, used_plot_ids_json,"
            " used_plot_names_json, warnings_json, output_chars, elapsed_ms,"
            " finish_reason, gaps_json, adopted, outline_id FROM outline_candidates"
            " WHERE run_id=? ORDER BY id", (rid,)).fetchall()
    d["candidates"] = []
    for c in cands:
        d["candidates"].append({
            "id": c["id"], "model_key": c["model_key"],
            "model_name": c["model_name"], "status": c["status"],
            "error": c["error"][:600] if c["error"] else "",
            "used_plot_ids": odb._loads(c["used_plot_ids_json"], []),
            "used_plot_names": odb._loads(c["used_plot_names_json"], []),
            "warnings": odb._loads(c["warnings_json"], []),
            "output_chars": c["output_chars"],
            "elapsed_ms": c["elapsed_ms"],
            # 候选卡上那条"体检结论"靠这两个 —— 结束原因翻成中文，
            # 结构缺口直接给一串短词。都不让她自己拼英文单词。
            "finish_reason": c["finish_reason"] or "",
            "finish_label": cls.llm.finish_label(c["finish_reason"]),
            "gaps": odb._loads(c["gaps_json"], []),
            "adopted": bool(c["adopted"]),
            "outline_id": c["outline_id"],
        })
    return d


def list_runs(owner, limit=20):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM outline_runs WHERE owner_id=?"
            " ORDER BY id DESC LIMIT ?", (owner, int(limit))).fetchall()
    return [_run_dict(r) for r in rows]


def get_candidate(candidate_id, owner):
    try:
        cid = int(candidate_id)
    except (TypeError, ValueError):
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM outline_candidates WHERE id=? AND owner_id=?",
            (cid, owner)).fetchone()
        if not row:
            return None
        d = dict(row)
        # raw_response **不下发**（又长又乱，她看的是结构化那几个字段）
        d.pop("raw_response", None)
        d["content_json"] = odb._loads(row["content_json"], {})
        d["used_plot_ids"] = odb._loads(row["used_plot_ids_json"], [])
        d["used_plot_names"] = odb._loads(row["used_plot_names_json"], [])
        d["warnings"] = odb._loads(row["warnings_json"], [])
        d["gaps"] = odb._loads(row["gaps_json"], [])
        d["finish_label"] = cls.llm.finish_label(row["finish_reason"])
        return d


def cancel_run(run_id, owner):
    """取消。**已经在路上的模型不会被打断** —— 见文件头第三节。

    所以这里如实告诉她会花掉多少钱：跑完的那几个结果会留着。
    """
    try:
        rid = int(run_id)
    except (TypeError, ValueError):
        raise ValueError("没说是哪个任务。")
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM outline_runs WHERE id=?"
                           " AND owner_id=?", (rid, owner)).fetchone()
        if not row:
            raise ValueError("没有这个大纲任务")
        if row["status"] not in RUN_ACTIVE:
            raise ValueError("这个任务已经结束了（%s），不用再取消。" % row["status"])
        _set_run(conn, rid, status=RUN_CANCELLED, finished_at=now_str())
        conn.execute("UPDATE outline_candidates SET status=?, error=?"
                     " WHERE run_id=? AND status=?",
                     (CAND_FAILED, "任务被取消了，这个模型没有发出去。",
                      rid, CAND_QUEUED))
    return True


def retry_run(run_id, owner, background=True):
    """重试。**只补真正没跑成的模型**，跑好的绝不重花钱。

    跟内化的 retry 是同一个思路：判断依据是"这个模型花过钱没有"，
    不是"结果好不好看"。一个候选只要 status=已完成，哪怕她嫌弃它，
    重试也不该再问一遍。
    """
    try:
        rid = int(run_id)
    except (TypeError, ValueError):
        raise ValueError("没说是哪个任务。")
    with db.connect() as conn:
        run = conn.execute("SELECT * FROM outline_runs WHERE id=? AND owner_id=?",
                           (rid, owner)).fetchone()
        if not run:
            raise ValueError("没有这个大纲任务")
        if run["status"] in RUN_ACTIVE:
            raise ValueError("这个任务还在跑，等它结束再说。")
        rows = conn.execute(
            "SELECT model_key, status FROM outline_candidates WHERE run_id=?",
            (rid,)).fetchall()
        failed = [r["model_key"] for r in rows if r["status"] != CAND_DONE]
        if not failed:
            raise ValueError("这次几个模型都跑成了，没有要补的。")

        # 沿用原任务那份输入和那句补充提示词 —— 不取"她现在写着什么"。
        data = {
            "worldview": odb._loads(run["input_json"], {}).get("worldview") or "",
            "world_name": odb._loads(run["input_json"], {}).get("world_name") or "",
            "worldview_id": odb._loads(run["input_json"], {}).get("worldview_id"),
            "character_ids": odb._loads(run["input_json"], {}).get("character_ids") or [],
            "one_sentence_hook": odb._loads(run["input_json"], {}).get("one_sentence_hook") or "",
            "plot_design": odb._loads(run["input_json"], {}).get("plot_design") or "",
            "target_words": run["target_words"],
            "plot_ids": odb._loads(run["cand_plot_ids_json"], []),
            "user_prompt": run["user_prompt"] or "",
        }

    res, new_id = create_run(owner, data, background=background,
                             retry_of_run_id=rid, model_keys=failed)
    if not res.get("ok"):
        raise ValueError(res.get("message") or "重试失败")
    res["retried_models"] = failed
    res["source_run_id"] = rid
    return res, new_id


def reap_orphan_runs(reason="服务重启了"):
    """服务重启时收尾僵尸任务。

    跟内化那边同一个必要性：任务状态存在库里，而跑任务的线程在内存里。
    服务一重启，线程没了，库里那行还写着"进行中" ——
    界面会一直转圈，而且新任务会被"已经有一个在跑"挡住，
    她就卡死了，唯一的出路是手工改库。
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM outline_runs WHERE status IN (%s)"
            % ",".join("?" * len(RUN_ACTIVE)), list(RUN_ACTIVE)).fetchall()
        for r in rows:
            _set_run(conn, r["id"], status=RUN_FAILED,
                     error=reason + "，这次任务没有跑完。可以点重试继续。",
                     finished_at=now_str())
        conn.execute(
            "UPDATE outline_candidates SET status=?, error=?"
            " WHERE status IN (?,?)",
            (CAND_FAILED, reason + "，没有跑完。", CAND_QUEUED, CAND_RUNNING))
        return len(rows)


# ----------------------------------------------------------------------
# 自测：只测纯函数，不联网、不写库
# ----------------------------------------------------------------------

def _self_check():                                          # pragma: no cover
    ok = True

    def check(name, got, want):
        nonlocal ok
        if got != want:
            ok = False
            print("  [x] %s：得到 %r，期望 %r" % (name, got, want))
        else:
            print("  [v] %s" % name)

    tpl = GENERIC_OUTLINE_PROMPT
    check("通用模板七个槽位一个不少",
          [s for s in REQUIRED_SLOTS if ("{%s}" % s) not in tpl], [])
    check("通用模板一个密钥都没有", "sk-" in tpl, False)

    # 单遍替换：填进去的内容里带 {user_prompt} 也不能被二次替换
    out = _fill_slots("A={worldview} B={user_prompt}",
                      worldview="正文里写着 {user_prompt} 这几个字",
                      user_prompt="她的要求")
    check("填进去的 {user_prompt} 不会被二次替换",
          "正文里写着 {user_prompt} 这几个字" in out, True)
    check("真正的槽位被替换了", "B=她的要求" in out, True)

    # 抠 JSON：三种裹法都要能抠出来
    for raw, why in (('```json\n{"nodes":[]}\n```', "带围栏"),
                     ('好的：{"nodes":[]} 以上', "前后有废话"),
                     ('{"nodes":[]}', "干净 JSON")):
        try:
            d = _extract_json_object(raw)
            check("%s 能抠出来" % why, isinstance(d, dict), True)
        except ValueError:
            check("%s 能抠出来" % why, False, True)
    try:
        _extract_json_object("完全不是 JSON")
        check("抠不出 JSON 要报错", False, True)
    except ValueError:
        check("抠不出 JSON 要报错", True, True)

    check("只给数组时会包成 nodes",
          _extract_json_object('[{"node_title":"甲"}]'), {"nodes": [{"node_title": "甲"}]})

    # 提示词版本必须动态取
    check("提示词版本有动态来源",
          prompt_version() in (PROMPT_VERSION, PROMPT_VERSION_GENERIC), True)

    # 状态表自洽
    check("任务状态表里没有重复", len(ALL_RUN_STATUS), len(set(ALL_RUN_STATUS)))
    check("进行中只有排队中和进行中", tuple(RUN_ACTIVE), (RUN_QUEUED, RUN_RUNNING))
    check("补充提示词这一档是 outline", USER_PROMPT_KIND_OUTLINE, "outline")

    # 空输入不该拼出崩掉的消息
    msgs = build_messages({"template": tpl, "worldview": "", "characters": [],
                           "plots": [], "hook": "", "design": "",
                           "target_words": 8000, "tier": odb.word_tier(8000),
                           "learning": [], "user_prompt": ""})
    check("空输入也能拼出两条消息", len(msgs), 2)
    check("空输入时世界观有兜底说法",
          "没写世界观" in msgs[0]["content"], True)
    check("空输入时零件有兜底说法",
          "没有可用的剧情零件" in msgs[0]["content"], True)
    check("目标字数的档位写进去了",
          "5～8 个" in msgs[0]["content"], True)

    _privacy = _privacy_note({"worldview": "x" * 100, "characters": [{}],
                              "plots": [{}], "hook": "h", "design": "",
                              "user_prompt": "", "blocked": [{"id": 1}]}, 500)
    check("隐私提示会说仅本地的被排除", "仅本地" in _privacy, True)

    # ---- 超时与重试 ---------------------------------------------------
    # 这两个数错了就退回老毛病：8000 字的大纲必然超时，
    # 超时还重试 3 次 → 白等 9 分钟拿一个必然失败，还可能多扣几笔钱。
    check("大纲超时远大于 llm 默认值（不然 8000 字必超时）",
          OUTLINE_TIMEOUT > cls.llm.TIMEOUT * 2, True)
    check("大纲只发一次、不重试", OUTLINE_MAX_RETRY, 1)
    check("大纲最坏等待不超过 15 分钟（超了会被当成卡死）",
          OUTLINE_TIMEOUT * OUTLINE_MAX_RETRY <= 900, True)
    check("分类/内化那套默认值仍然会重试（短问答多试一次划算）",
          cls.llm.MAX_RETRY >= 2, True)

    print()
    print("编排层自测：%s" % ("全部通过" if ok else "有失败"))
    return ok


if __name__ == "__main__":                                  # pragma: no cover
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(0 if _self_check() else 1)
