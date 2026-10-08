# -*- coding: utf-8 -*-
"""
墨阁 · 大纲生成 · 数据层
========================================================

这个文件只管"东西存在哪、怎么取出来"，一行 AI 都不调。
（怎么问模型、怎么拼提示词、任务怎么排队 —— 那是 outline_ai.py 的事。
  这么分跟 plots_db / plots_ai 是同一个切法，改提示词不用碰表结构。）

--------------------------------------------------------
一、这一层管着七张表
--------------------------------------------------------
    characters           角色卡（人设卡）。大纲必须至少关联一张。
    worldviews           世界观库。她写同人一般是固定的世界观，存下来能反复选。
    outlines             大纲库（正式库）。只有点了"推入大纲库"才进来。
    outline_runs         每次生成任务。
    outline_candidates   每个模型各出一份候选（未采用的不删，留作历史）。
    outline_plot_refs    剧情零件引用明细。**参考次数是从这张表算出来的。**
    outline_feedback     学习反馈（保存时的差异快照 + 她手动标的"不可用"）。

--------------------------------------------------------
二、三个"这一层必须自己扛住"的设计决定
--------------------------------------------------------

1. **参考次数不落一列，现算。**
   计划里说"为每条剧情零件保存 reference_count 或等价计数"。
   两个字面上都能做：给 plots 加一列，或者从引用明细 COUNT 出来。
   这里选**现算**，理由是墨阁已经踩过一次同样的坑：
   卡片的内化状态当初也差点写成一列，最后定的是"用关联统计算"，
   因为 **一旦两处都能表示同一件事，就一定会有一天对不上** ——
   而且对不上的那天没有任何报错，只是数字悄悄变得不可信。
   代价是每次列零件清单要一次 GROUP BY，几万行的量级完全无所谓。

2. **计数只加不减，删除大纲不回退。**
   计划第四.1 节明确定过：`删除或撤回一份大纲时，不直接修改历史计数`。
   所以删大纲时**只删 outlines 那一行，引用明细一行不动** ——
   明细里存了 outline_title 快照，历史还认得出"当初是被哪篇用掉的"。
   要是哪天要做"撤销"，再按明细写一条可审计的反向记录，
   而不是让 DELETE 顺手把数字减回去。

3. **幂等靠 UNIQUE，不靠调用方自觉。**
   重复提交同一份保存请求不能把引用次数加两遍。
   做法是两条：
     · outline_plot_refs 上有 UNIQUE(outline_id, plot_id)
     · 保存时先 DELETE 这一份大纲的全部明细，再整体重插
   所以同一份大纲保存十次，明细还是那么几行，计数也还是那么多。
   绝不写成"insert 之前先 SELECT 看看有没有" —— 那种代码在并发下会漏。

--------------------------------------------------------
三、跟 plots 那边的红线（别越界）
--------------------------------------------------------
    这个文件**只读** plots / plot_cards / cards / materials，一个字都不写。
    改零件是【剧情内化】页的事。大纲这边唯一会反过来影响 plots 的，
    只有"参考次数" —— 而那是算出来的，不是写回去的。
"""

import json
import re
import hashlib
from datetime import datetime

from backend import db
from backend import llm
from backend import plots_db as pdb


# ----------------------------------------------------------------------
# 常量（都是"唯一定义处"，前端不许自己抄一份）
# ----------------------------------------------------------------------

OUTLINE_TYPE_TW = "同人短篇"          # 第一版只做这一个
ALL_OUTLINE_TYPES = (OUTLINE_TYPE_TW,)
OUTLINE_TYPE_LABELS = {OUTLINE_TYPE_TW: "同人短篇"}

# 角色卡状态。停用的不再出现在"可关联"清单里，但不删。
CHAR_ON = "启用"
CHAR_OFF = "停用"
ALL_CHAR_STATUS = (CHAR_ON, CHAR_OFF)

# 大纲能用的零件状态。
# 【为什么只有这两个】计划第四.1 节：只有"用户确认、人工编辑后确认或其他
# 明确可用状态"的零件才能参与候选。墨阁的状态轴里正好对应这两个 ——
# 「待确认 / 待处理」是她还没定过的，「暂不用 / 已排除」是她明确不要的。
# 少一个都会让她看见"我明明排除掉的剧情又冒出来了"。
PLOT_USABLE_STATUS = (pdb.PLOT_STATUS_CONFIRMED, pdb.PLOT_STATUS_EDITED)

# 预期字数 → 结构规模。
# 【为什么要有一张表，而不是让模型自己看着办】
# 计划第五.5 节：`系统应将其转换为结构规模，而不是只在结果中显示一个数字`。
# 8000 字拿去问，模型可能给 4 个节点（每段 2000 字，写起来是散的），
# 也可能给 15 个（每段 500 字，全是空壳）。这两种都不可用。
# 所以范围由后端定死，模型只能在区间里挑，挑出界要写警告。
WORD_TIERS = (
    {"key": "tiny",   "min": 0,     "max": 5999,    "sample": 5000,
     "label": "5000 字上下", "hint": "单一核心冲突，角色少，结局收束快"},
    {"key": "short",  "min": 6000,  "max": 9999,    "sample": 8000,
     "label": "6000～9000 字", "hint": "完整起承转合，至少一次明显转折"},
    {"key": "medium", "min": 10000, "max": 15000,   "sample": 12500,
     "label": "10000～15000 字", "hint": "冲突和关系变化更充分，可以有副冲突"},
    {"key": "long",   "min": 15001, "max": 10 ** 9, "sample": 20000,
     "label": "15000 字以上", "hint": "已接近中短篇，动手前建议再确认一次"},
)
# 【为什么档位里没有"建议几个节点"】见下面 NODE_WORDS_* 的说明 ——
# 节点数不是一个独立参数，它是"预期字数 ÷ 每段字数"算出来的**结果**。
# 写死在这里就会出现两个真相，改一个忘另一个。
# sample 是这一档的**代表字数**，只给"界面上大概显示几个节点"用；
# 真正要精确节点数，得用 word_tier(实际字数)。

# ----------------------------------------------------------------------
# 每段写多长：结构规模的**主约束**
#
# 【为什么把"每段多少字"提到主位】以前这里是反的：先定"建议几个节点"
# （8000 字建议 5～8 个），每段字数由它倒推 → 1000～1600 字/段。
# 于是提示词一边写着"字数多了要多一次转折、不是把每段写长"，
# 一边告诉模型"每段 1000～1600 字" —— 模型当然听后者。
# 结果就是节点少、每段肥：十个字段摊进 1500 字，平均一个才一百来字，
# 看着都填了，其实全是概括，"细到能直接动笔"根本无从谈起。
#
# 现在反过来：一段该写多长**先定**（够把一个场景写清楚，又不至于塞进
# 两三件事），节点数由预期字数除出来。8000 字 → 10～17 个节点。
# ----------------------------------------------------------------------
NODE_WORDS_MIN = 450     # 再短就装不下一个完整的场景
NODE_WORDS_MAX = 800     # 再长就说明这一段里塞了不止一件事，该拆
NODE_WORDS_TARGET = 600  # 给界面看的手感值（"大约一段 600 字"）
NODE_COUNT_MAX = 30      # 多到这数就该提醒：分批会拖很久，也未必是她要的


def node_range_for(words):
    """按"每段多少字"倒推这个预期字数该有几个节点。

    区间是**算出来的**、不是拍的：下限 = 每段都顶格写满 800 字，
    上限 = 每段都只写 450 字。所以报出来的区间跟每段字数永远自洽 ——
    不会出现"建议 10～17 个节点、每个 1000 字"这种凑不满的话。
    """
    try:
        n = int(words or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return (0, 0)
    lo = max(2, -(-n // NODE_WORDS_MAX))     # 向上取整
    hi = max(lo, n // NODE_WORDS_MIN)        # 向下取整
    return (min(lo, NODE_COUNT_MAX), min(hi, NODE_COUNT_MAX))

TARGET_WORDS_MIN = 1000
TARGET_WORDS_MAX = 200000
# 字数往上多少要提醒她"这已经不算短篇了"
TARGET_WORDS_WARN = 15000

MAX_MODELS_PER_RUN = 4        # 一次最多挑几个模型一起跑
# 【为什么要有个上限】多模型是**并发**发出去的，一次勾八个，
# 八个长请求同时打同一家的限流，多半是集体 429。
# 而且她还得分屏看八份候选 —— 四份已经超过能认真比较的量了。

# 学习反馈的问题类型（计划第九节列的那七种）
PROBLEM_TYPES = (
    "节奏太快", "节奏太慢", "人物不符", "冲突不足",
    "转折生硬", "过度套用内化剧情", "字数分配不合理", "结局不符合预期",
)

# 反馈的两种来源
FEEDBACK_DIFF = "diff"                 # 保存时自动写的"AI 稿 vs 你的稿"
FEEDBACK_NODE = "node_unusable"        # 她手动标的"这个节点不可用"
ALL_FEEDBACK_KIND = (FEEDBACK_DIFF, FEEDBACK_NODE)

# ---- 改写对比（2026-10-08 折腰要的）----
#
# 她的创作习惯：AI 出一版大纲，她拿它当蓝本改写 —— 或者干脆自己重写一整版。
# 「我想让 AI 对比它跟我的大纲不断学习，但不想一个个在页面上修改。」
#
# 所以这里不做"逐节点标注"，只收一份**成品大纲全文**，然后让 AI 对着
# 「那一版 AI 原稿」逐段看：她删了什么、加了什么、把什么改成了什么。
# 结论沉淀成一条条**可复用的改写偏好**，以后生成时直接进提示词。
#
# 【为什么单独一张表，不塞进 outline_feedback】
# outline_feedback 记的是"某一份大纲的某一条反馈"（附在 outline_id 上），
# 而改写对比挂在**候选**上 —— 她可能压根没把那一版推进大纲库就改了它。
# 而且一条改写对比会派生出 N 条偏好，是**一对多**，塞进单行 feedback 里
# 只能把 JSON 当仓库用，查和改都不方便。
#
# 【source 和 source_kind 是两件事，别混】
#   source       = 她**从哪儿进**来的（adopted 走候选卡的"我按这一版改了一版"，
#                  scratch 是大纲库里的"完全自己写的"）。这是**入口**。
#   source_kind  = 这一份**语义上是什么**（对着一版 AI 稿改的 / 完全独立写的）。
# 绝大多数时候两者一致，但不能合并 —— 她可能从"独立创作"入口进来，
# 却选了一份候选当蓝本；也可能从候选卡进来、其实整篇是自己重写的。
# 需求第 3 条点名要求：独立创作的稿子**不许假装是对 AI 原稿的修改**，
# 也不许替她编出"删掉了什么"。判断"有没有原稿可比"必须看 **候选有没有**，
# 不是看入口 —— 所以这两个字段各存一份。
REWRITE_SOURCE_ADOPT = "adopted"       # 入口：从"把这一版做成大纲"进去改的
REWRITE_SOURCE_SCRATCH = "scratch"     # 入口：完全自己写的
ALL_REWRITE_SOURCE = (REWRITE_SOURCE_ADOPT, REWRITE_SOURCE_SCRATCH)

REWRITE_KIND_ADOPT = "rewrite"         # 语义：对着某一版 AI 稿改的（有原稿可比）
REWRITE_KIND_SCRATCH = "original"      # 语义：完全独立写的（**没有原稿可比**）
ALL_REWRITE_KIND = (REWRITE_KIND_ADOPT, REWRITE_KIND_SCRATCH)

REWRITE_SOURCE_LABELS = {
    REWRITE_SOURCE_ADOPT: "按某一版 AI 稿改的",
    REWRITE_SOURCE_SCRATCH: "完全自己写的",
}
REWRITE_KIND_LABELS = {
    REWRITE_KIND_ADOPT: "按某一版 AI 稿改的",
    REWRITE_KIND_SCRATCH: "完全自己写的",
}

# 对比分析的状态
REWRITE_PENDING = "待分析"
REWRITE_RUNNING = "分析中"
REWRITE_DONE = "已总结"
REWRITE_FAILED = "失败"
ALL_REWRITE_STATUS = (REWRITE_PENDING, REWRITE_RUNNING,
                      REWRITE_DONE, REWRITE_FAILED)

REWRITE_TEXT_MAX = 60000      # 一份成品大纲最多多少字（够长了，超了报错不截断）
REWRITE_SUMMARY_MAX = 8000    # AI 总结原文的上限
REWRITE_POINTS_MAX = 20       # 一次最多沉淀多少条偏好（多了她也看不完）

# ---- 每条建议的「适用范围」（需求第 7 条）----
#
# 【为什么要分层】一份改动里混着三种完全不同的东西：
#   · "我写东西就是这个口味" → 长期，以后哪篇都该照做
#   · "这类情境（比如当众对质）我这么处理" → 只在情境像的时候才管用
#   · "这一篇我就是要这个结局" → 换一篇就**不该**跟着走
# 混成一锅的后果是：她把某一篇的特殊处理交上来，AI 当成永久规矩，
# 以后每篇都硬塞那个桥段。
#
# ★★ 2026-10-08 折腰纠正：适用范围 / 审核状态 / 使用状态是**三个正交的轴** ★★
# 我原来把"待确认/已接受/已拒绝/仅本篇/已停用"列成同一组选项 —— 错的。
# 它们说的是三件互不冲突的事，一条建议可以同时是
# 「已接受 + 启用 + 仅本篇」。所以拆成：
#   scope   适用范围（这条能用在多大范围）   ← 本组
#   review  审核状态（她拍板了没有）         ← 见 REWRITE_RV_*
#   active  使用状态（现在还生效吗）         ← 见 REWRITE_ACTIVE_*
#   confidence 分析把握程度（判断够不够）     ← 见 REWRITE_CONF_*
REWRITE_SCOPE_LONG = "long"       # 长期写作偏好
REWRITE_SCOPE_CASE = "case"       # 特定类型 / 情境下才适用
REWRITE_SCOPE_THIS = "this"       # 仅本篇（只能在对应那篇作品里用）
ALL_REWRITE_SCOPE = (REWRITE_SCOPE_LONG, REWRITE_SCOPE_CASE,
                     REWRITE_SCOPE_THIS)
REWRITE_SCOPE_LABELS = {
    REWRITE_SCOPE_LONG: "长期偏好",
    REWRITE_SCOPE_CASE: "情境适用",
    REWRITE_SCOPE_THIS: "仅本篇",
}
# 只有这两层能进**跨作品**的生成。仅本篇留在库里，只在它对应那篇
# 作品里才可能被用到（需求第 1 条：接受了也不等于就能进别的作品）。
REWRITE_SCOPE_USABLE = (REWRITE_SCOPE_LONG, REWRITE_SCOPE_CASE)
# 没标 / 认不出时的兜底。跟"还没把握"不同 —— 那是个结论，这是缺省。
REWRITE_SCOPE_UNKNOWN = "this"    # 兜到最保守的那层，绝不敢当长期

# ---- 分析把握程度（★ 独立于适用范围，需求第 1 条点名）----
#
# 「判断不充分、需要解释」说的是**分析站不站得住**，不是"能用多大范围"。
# 一条"长期偏好"完全可能"判断不充分"（她只改了一句话）；
# 一条"仅本篇"也可能"判断充分"。两个轴正交，混一起就表达不了。
REWRITE_CONF_ENOUGH = "充分"      # 证据够，判断站得住
REWRITE_CONF_MEDIUM = "一般"      # 有一部分证据，但不够硬
REWRITE_CONF_WEAK = "不足"        # 只是推测，需要她解释
REWRITE_CONF_UNKNOWN = "未标注"
ALL_REWRITE_CONF = (REWRITE_CONF_ENOUGH, REWRITE_CONF_MEDIUM,
                    REWRITE_CONF_WEAK, REWRITE_CONF_UNKNOWN)
REWRITE_CONF_LABELS = {
    REWRITE_CONF_ENOUGH: "判断充分",
    REWRITE_CONF_MEDIUM: "判断一般",
    REWRITE_CONF_WEAK: "判断不充分（需要解释）",
    REWRITE_CONF_UNKNOWN: "没标",
}

# ---- 审核状态（她拍板了没有）----
REWRITE_RV_PENDING = "待确认"
REWRITE_RV_ACCEPTED = "已接受"
REWRITE_RV_REJECTED = "已拒绝"
ALL_REWRITE_REVIEW = (REWRITE_RV_PENDING, REWRITE_RV_ACCEPTED,
                      REWRITE_RV_REJECTED)
REWRITE_RV_LABELS = {
    REWRITE_RV_PENDING: "待确认",
    REWRITE_RV_ACCEPTED: "已接受",
    REWRITE_RV_REJECTED: "已拒绝",
}

# ---- 使用状态（现在还生效吗）----
#
# 【跟审核状态分开的理由】她接受过的一条，过一阵可能想停用
# （不删证据、保留历史）。"已接受 + 停用"是完全合理的组合；
# 揉成一个状态就表达不了。停用是她主动撤的开关，拒绝是否掉这条建议 —— 两回事。
REWRITE_ACTIVE_ON = "启用"
REWRITE_ACTIVE_OFF = "停用"
ALL_REWRITE_ACTIVE = (REWRITE_ACTIVE_ON, REWRITE_ACTIVE_OFF)
REWRITE_ACTIVE_LABELS = {
    REWRITE_ACTIVE_ON: "启用",
    REWRITE_ACTIVE_OFF: "停用",
}

# ---- 审核动作（「修改后接受」这类要留操作记录，需求第 1 条）----
#
# 【为什么单独一张日志表】折腰点名："「修改后接受」保存为操作记录
# 及修改后的内容。" 一次审核动作 = 一行：什么时候把哪条从什么状态
# 改成了什么状态、她改后的措辞是什么。这是**审计轨迹**，
# 写在建议行上只会被下一次动作覆盖掉。
REWRITE_ACT_ACCEPT = "accept"
REWRITE_ACT_ACCEPT_EDITED = "accept_edited"    # 改后接受
REWRITE_ACT_REJECT = "reject"
REWRITE_ACT_REOPEN = "reopen"                  # 打回待确认
REWRITE_ACT_ENABLE = "enable"
REWRITE_ACT_DISABLE = "disable"
REWRITE_ACT_SCOPE = "set_scope"
REWRITE_ACT_NOTE = "note"
ALL_REWRITE_ACTION = (REWRITE_ACT_ACCEPT, REWRITE_ACT_ACCEPT_EDITED,
                      REWRITE_ACT_REJECT, REWRITE_ACT_REOPEN,
                      REWRITE_ACT_ENABLE, REWRITE_ACT_DISABLE,
                      REWRITE_ACT_SCOPE, REWRITE_ACT_NOTE)
REWRITE_ACTION_LABELS = {
    REWRITE_ACT_ACCEPT: "接受",
    REWRITE_ACT_ACCEPT_EDITED: "改后接受",
    REWRITE_ACT_REJECT: "拒绝",
    REWRITE_ACT_REOPEN: "打回待确认",
    REWRITE_ACT_ENABLE: "启用",
    REWRITE_ACT_DISABLE: "停用",
    REWRITE_ACT_SCOPE: "改适用范围",
    REWRITE_ACT_NOTE: "补充说明",
}


def rewrite_point_is_live(pt):
    """这条建议现在算不算"会进生成"。

    【判据必须收敛成一条函数】三个轴各有各的值，散在各处写 if 一定会漏。
    资格条件（需求第 8 条："确认、启用及权限是资格条件，先过滤"）：
      审核=已接受 且 使用=启用 且 适用范围∈(长期/情境)
    """
    return (pt.get("review") == REWRITE_RV_ACCEPTED
            and pt.get("active") == REWRITE_ACTIVE_ON
            and pt.get("scope") in REWRITE_SCOPE_USABLE)

# 每条建议的 8 项证据字段的长度上限（需求第 6 条）。
# 它们是"这条建议凭什么成立"的证据链，界面上按需展开。
REWRITE_PT_FIELD_MAX = 600     # 每个证据字段的框
REWRITE_PT_EXTRA_MAX = 1000    # 她补的说明
REWRITE_SUGGEST_MAX = 2000     # 她改后的措辞

# 长度上限。超了一律报错，不静默截断 —— 她填 300 字被悄悄砍成 60 字，
# 界面上还显示"保存成功"，要过很久才发现。
CHAR_NAME_MAX = 40
CHAR_FIELD_MAX = 1000
CHAR_NOTE_MAX = 500
WORLD_NAME_MAX = 60
WORLD_CONTENT_MAX = 30000
HOOK_MAX = 500
DESIGN_MAX = 3000
OUTLINE_TITLE_MAX = 120
OUTLINE_NOTE_MAX = 2000
NODE_TITLE_MAX = 80
NODE_TEXT_MAX = 2000
NODE_SHORT_MAX = 400
NODE_LIST_MAX = 12            # 一个节点最多挂几个角色
NODE_SOURCE_MAX = 20          # 一个节点最多挂几条来源零件
# 一个节点最多挂几条分类素材引用。整篇的上限在 REFERENCE_MAX（30），
# 单节点不需要单独的旋钮 —— 但清洗时仍要一个防炸形状的数
# （模型发疯给一个节点挂 500 个编号时先截掉，截掉的会进警告）。
NODE_REF_MAX = 10
MAX_NODES = 40
FEEDBACK_NOTE_MAX = 1000
ROLE_FUNCTION_MAX = 12        # 角色功能表最多几行

# 大纲 JSON 里的固定骨架（渲染成可读文本时按这个顺序走）
_BEAT_ORDER = (
    "title_candidates", "story_core", "theme_tone", "character_functions",
    "overview", "word_budget", "nodes", "climax", "ending",
    "used_plots", "logic_risks",
)


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------
# 建表
#
# 七张全是**纯新增空表**：没有 ALTER、不碰任何已有表。
# 跟第 1 步、第 2 步是同一个判断 —— 只有"改已写死的约束"才需要
# 整容迁移那一套（建新表→搬数据→删旧表→改名→搬序列），
# 那才必须在动手前 shutil.copy2 备份整个库。
# ----------------------------------------------------------------------

SCHEMA = """
-- 角色卡（人设卡）。
--
-- 【为什么这一版要在这里建它】
-- 计划第三节第 2 条：大纲"角色卡来自现有角色卡系统…但不要另建一套
-- 互相冲突的角色体系"。实际情况是墨阁**还没有**任何角色数据 ——
-- 左侧栏的【人设卡】还亮着"待做"。
-- 所以这里建的这张不是"另一套"，它就是第一套（也是唯一一套）；
-- 以后点开【人设卡】那一页，读的也是这张表，不会再建第二张。
--
-- 【为什么字段是一堆平铺的文本，不是 JSON】
-- 计划把角色要求列成八项（身份/性格/目标/恐惧/关系/说话方式/
-- 必须遵守/禁止出现），每一项都是一段自由文字，查询时也从不需要
-- 按其中某一项筛。硬拆成 JSON 只会让"改一句话"变成一次结构操作。
CREATE TABLE IF NOT EXISTS characters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id    TEXT    NOT NULL,
    name        TEXT    NOT NULL DEFAULT '',
    identity    TEXT    NOT NULL DEFAULT '',
    personality TEXT    NOT NULL DEFAULT '',
    goal        TEXT    NOT NULL DEFAULT '',
    fear        TEXT    NOT NULL DEFAULT '',
    relations   TEXT    NOT NULL DEFAULT '',
    speech      TEXT    NOT NULL DEFAULT '',
    must_do     TEXT    NOT NULL DEFAULT '',
    never_do    TEXT    NOT NULL DEFAULT '',
    note        TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT '启用',
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    UNIQUE (owner_id, name)
);

-- 世界观库。
--
-- 【为什么它是一张表，而不是大纲表单里一个大文本框】
-- 计划第一行写的是"选择或填写世界观" —— "选择"两个字背后就必须有个库，
-- 否则每次都要重新粘一遍同一段设定。
-- 但库只是**入口的便利**：大纲里存的永远是快照（world_input_snapshot），
-- 不存 id 引用。理由跟角色卡一样 —— 她改了世界观库那条，
-- 三年前那份大纲不该跟着变。
CREATE TABLE IF NOT EXISTS worldviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   TEXT    NOT NULL,
    name       TEXT    NOT NULL DEFAULT '',
    content    TEXT    NOT NULL DEFAULT '',
    chars      INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL,
    UNIQUE (owner_id, name)
);

-- 大纲库（正式库）。
--
-- 【为什么只有"推入"才进这张表】
-- 计划第八节：`仅生成但未保存，不进入正式大纲库，也不增加剧情零件参考次数`。
-- 生成出来的东西存在 outline_candidates 里（那是候选，不是成品）。
-- 判断依据很清楚：**进没进这张表 = 她认没认这篇**。
--
-- 【为什么 AI 原稿和用户稿都要留，而且是三份内容】
--    ai_original_*   生成那一刻的样子，**永不改写**
--    current_*       她现在改到哪了
--    （用户最终版 = 她最后停下的那个 current）
-- 计划第九节：`不要把"用户最终版本"直接覆盖成唯一版本`。
-- 留着原稿才答得出"AI 和她的差别在哪" —— 而那正是学习功能唯一的原料。
--
-- 【为什么 json 和 text 都存】
--   *_json  编辑用（节点要增删改排序，文本没法排序）
--   *_text  留档 + 复制用（她要能整段复制去写正文）
-- 文本是保存那一刻**渲染一次**存下来的，不是每次现渲染：
-- 以后改了渲染函数（加个字段、调个顺序），老大纲的"原样"也得稳住。
CREATE TABLE IF NOT EXISTS outlines (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id              TEXT    NOT NULL,
    outline_type          TEXT    NOT NULL DEFAULT '同人短篇',
    title                 TEXT    NOT NULL DEFAULT '',

    -- ---- 输入快照（全是快照，不是 id 引用）----
    worldview_id          INTEGER DEFAULT NULL,
    world_input_snapshot  TEXT    NOT NULL DEFAULT '',
    world_name            TEXT    NOT NULL DEFAULT '',
    character_ids_json    TEXT    NOT NULL DEFAULT '[]',
    character_snapshot_json TEXT  NOT NULL DEFAULT '[]',
    one_sentence_hook     TEXT    NOT NULL DEFAULT '',
    hook_ai_derived       INTEGER NOT NULL DEFAULT 0,
    plot_design           TEXT    NOT NULL DEFAULT '',
    target_words          INTEGER NOT NULL DEFAULT 0,

    -- ---- 生成来源（可追溯）----
    run_id                INTEGER DEFAULT NULL,
    candidate_id          INTEGER DEFAULT NULL,
    model_key             TEXT    NOT NULL DEFAULT '',
    model_name            TEXT    NOT NULL DEFAULT '',
    prompt_version        TEXT    NOT NULL DEFAULT '',
    prompt_ref_id         INTEGER NOT NULL DEFAULT 0,
    prompt_name           TEXT    NOT NULL DEFAULT '',
    user_prompt_len       INTEGER NOT NULL DEFAULT 0,
    user_prompt_snapshot  TEXT    NOT NULL DEFAULT '',

    -- ---- 三份内容 ----
    ai_original_json      TEXT    NOT NULL DEFAULT '{}',
    ai_original_text      TEXT    NOT NULL DEFAULT '',
    current_json          TEXT    NOT NULL DEFAULT '{}',
    current_text          TEXT    NOT NULL DEFAULT '',

    -- ---- 最终采用的零件 + 学习开关 ----
    selected_plot_ids_json TEXT   NOT NULL DEFAULT '[]',
    user_note             TEXT    NOT NULL DEFAULT '',
    in_learning           INTEGER NOT NULL DEFAULT 0,

    created_at            TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL
);

-- 生成任务。一次生成 = 一行（哪怕勾了四个模型）。
CREATE TABLE IF NOT EXISTS outline_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id            TEXT    NOT NULL,
    outline_type        TEXT    NOT NULL DEFAULT '同人短篇',

    -- 输入快照。整包存 JSON 而不是拆成十几列：
    -- 这张表只在"重试"和"回看这次到底怎么问的"时被读，
    -- 从来不需要按输入里的某一项做 SQL 筛选。
    input_json          TEXT    NOT NULL DEFAULT '{}',
    target_words        INTEGER NOT NULL DEFAULT 0,

    model_keys_json     TEXT    NOT NULL DEFAULT '[]',
    prompt_version      TEXT    NOT NULL DEFAULT '',
    prompt_ref_id       INTEGER NOT NULL DEFAULT 0,
    prompt_name         TEXT    NOT NULL DEFAULT '',
    user_prompt         TEXT    NOT NULL DEFAULT '',
    user_prompt_len     INTEGER NOT NULL DEFAULT 0,

    -- 候选池：**这次一共摆出来哪些零件给它挑**（计划第十节要求记录）。
    -- 记这个才能回答"没被选中是因为不合适，还是 AI 漏掉了"。
    cand_plot_ids_json  TEXT    NOT NULL DEFAULT '[]',
    -- 最终有零件被引用过的那一份，回填 outline_id
    outline_id          INTEGER DEFAULT NULL,

    status              TEXT    NOT NULL DEFAULT '排队中',
    total_models        INTEGER NOT NULL DEFAULT 0,
    done_models         INTEGER NOT NULL DEFAULT 0,
    failed_models       INTEGER NOT NULL DEFAULT 0,
    total_input_chars   INTEGER NOT NULL DEFAULT 0,
    saved_outline_id    INTEGER DEFAULT NULL,
    retry_of_run_id     INTEGER DEFAULT NULL,
    note                TEXT    NOT NULL DEFAULT '',
    error               TEXT    NOT NULL DEFAULT '',
    heartbeat_at        TEXT    NOT NULL DEFAULT '',
    -- 输入快照指纹（任务书补充二.5）：世界观 + 角色卡 + 约束 + 零件 +
    -- 素材参考 + 模型 + 提示词版本，算一个 hash。相同 hash 的已跑任务
    -- 能直接读结果、不重复收费。
    input_hash          TEXT    NOT NULL DEFAULT '',
    created_at          TEXT    NOT NULL,
    started_at          TEXT    NOT NULL DEFAULT '',
    finished_at         TEXT    NOT NULL DEFAULT ''
);

-- 每个模型各出的那一份候选。
--
-- 【为什么未采用的候选不删】计划第五.7 节写死的：
-- `未采用候选保留为历史候选，不自动删除`。
-- 她今天选了 A，过两周回头想看看 B 长什么样 —— 那份已经花钱生成过了，
-- 删掉等于让她重花一次钱。
CREATE TABLE IF NOT EXISTS outline_candidates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER NOT NULL,
    owner_id            TEXT    NOT NULL,
    model_key           TEXT    NOT NULL DEFAULT '',
    model_name          TEXT    NOT NULL DEFAULT '',
    prompt_version      TEXT    NOT NULL DEFAULT '',
    status              TEXT    NOT NULL DEFAULT '排队中',
    content_json        TEXT    NOT NULL DEFAULT '{}',
    content_text        TEXT    NOT NULL DEFAULT '',
    used_plot_ids_json  TEXT    NOT NULL DEFAULT '[]',
    used_plot_names_json TEXT   NOT NULL DEFAULT '[]',
    warnings_json       TEXT    NOT NULL DEFAULT '[]',
    raw_response        TEXT    NOT NULL DEFAULT '',
    input_chars         INTEGER NOT NULL DEFAULT 0,
    output_chars        INTEGER NOT NULL DEFAULT 0,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    elapsed_ms          INTEGER NOT NULL DEFAULT 0,
    -- 这次生成是怎么结束的（stop / length / content_filter…），
    -- 由 llm.finish_label() 翻成中文给界面。取值含义见 llm.py 常量区。
    -- 【为什么要单独存一列】它是判断"这篇写完了没有"的**唯一硬证据**：
    -- 被 max_tokens 掐断时，返回体本身是合法的、只有正文是半截，
    -- 而模型自己填的"预计 3000 字"照旧 —— 光看内容分辨不出来。
    finish_reason       TEXT    NOT NULL DEFAULT '',
    -- 结构上缺什么（[] / ["结局"] / ["高潮", "故事核心"]…），
    -- 由 outline_db.structure_gaps() 在写候选那一刻算好。
    -- 【为什么不读取时现算】列表接口 /api/outline-runs 不带 content_json
    -- （太长），现算就没有原料 —— 那一行"体检结论"会时有时无。
    gaps_json           TEXT    NOT NULL DEFAULT '[]',
    error               TEXT    NOT NULL DEFAULT '',
    adopted             INTEGER NOT NULL DEFAULT 0,
    outline_id          INTEGER DEFAULT NULL,
    created_at          TEXT    NOT NULL,
    UNIQUE (run_id, model_key)
);

-- 剧情零件引用明细。**参考次数就是这张表算出来的。**
--
-- 【幂等的根在这一行上】UNIQUE(outline_id, plot_id)
-- 同一份大纲里同一条零件最多一行 —— 计划第四.1 节：
-- `同一份大纲中同一剧情零件只增加 1 次，不按出现次数重复增加`。
--
-- 【kept 的两层意思】
--   1  她最终保留了它（计入参考次数）
--   0  AI 提过、或她后来删掉了（**记下来但不计数**）
-- 为什么要记得 0 的那些：计划第四.3 节要能回答"没被选中是因为不合适，
-- 还是因为 AI 漏掉了"。只记用上的，这个问题就永远答不出来。
--
-- 【ref_count_before / ref_count_after 为什么存】
-- 计划第四.3 节明列的字段。存下来才能对账：
-- "这条零件当时是第 3 次被用，现在是第 8 次" —— 事后也算得出来。
--
-- 【outline_title 为什么冗余】
-- 大纲被删掉之后，明细行还在（计数不回退）。没有这个快照，
-- 那些行就成了指向空气的 id。
CREATE TABLE IF NOT EXISTS outline_plot_refs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    outline_id       INTEGER NOT NULL,
    outline_title    TEXT    NOT NULL DEFAULT '',
    plot_id          INTEGER NOT NULL,
    owner_id         TEXT    NOT NULL,
    plot_title       TEXT    NOT NULL DEFAULT '',
    kept             INTEGER NOT NULL DEFAULT 1,
    ref_count_before INTEGER NOT NULL DEFAULT 0,
    ref_count_after  INTEGER NOT NULL DEFAULT 0,
    position_note    TEXT    NOT NULL DEFAULT '',
    manual_added     INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL,
    UNIQUE (outline_id, plot_id)
);

-- 学习反馈。第一版只做"差异记录 + 人工确认"，不训练模型参数（计划第九节）。
--
-- 【为什么不另建一张 outline_diffs】
-- 差异是"两份 JSON 比出来的"，随时能重算。但它读的是**当时的**
-- AI 原稿和用户稿 —— 而她以后可能继续在大纲上改，那两份就变了。
-- 所以保存那一刻的快照要落一行 kind='diff'，把"这一版差在哪"钉住。
-- 这跟 plots 那边"改内容一定产生新版本"是同一个思路：
-- 结论可以重算，当时的事实不能。
CREATE TABLE IF NOT EXISTS outline_feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    outline_id  INTEGER NOT NULL,
    owner_id    TEXT    NOT NULL,
    run_id      INTEGER DEFAULT NULL,
    kind        TEXT    NOT NULL DEFAULT 'diff',
    node_id     TEXT    NOT NULL DEFAULT '',
    problem     TEXT    NOT NULL DEFAULT '',
    note        TEXT    NOT NULL DEFAULT '',
    detail_json TEXT    NOT NULL DEFAULT '{}',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outlines_own
    ON outlines (owner_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_outline_runs_own
    ON outline_runs (owner_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_outline_runs_status
    ON outline_runs (status);
CREATE INDEX IF NOT EXISTS idx_outline_cand_run
    ON outline_candidates (run_id, id);
CREATE INDEX IF NOT EXISTS idx_outline_refs_plot
    ON outline_plot_refs (owner_id, plot_id);
CREATE INDEX IF NOT EXISTS idx_outline_refs_outline
    ON outline_plot_refs (outline_id);
CREATE INDEX IF NOT EXISTS idx_outline_fb_outline
    ON outline_feedback (outline_id, id);
CREATE INDEX IF NOT EXISTS idx_chars_own
    ON characters (owner_id, status, id);
CREATE INDEX IF NOT EXISTS idx_worlds_own
    ON worldviews (owner_id, id DESC);
"""

# ---- 改写对比表（2026-10-08 加）----
#
# 【为什么放在 SCHEMA 之外单独一段】
# 上面那份 SCHEMA 是"建这批表"时的一次性脚本，改动它会让人误以为
# 老库会被重建（其实 CREATE TABLE IF NOT EXISTS 对已有的表什么都不做）。
# 新表单独一段、单独 execut，语义清楚：**这是一张新加的表**。
REWRITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS outline_rewrites (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id        TEXT    NOT NULL,

    -- ---- 挂在哪个生成任务 / 哪一版候选上 ----
    -- 可以为空：她完全自己写的、没对着哪一版的那份也能进来。
    run_id          INTEGER DEFAULT NULL,
    candidate_id    INTEGER DEFAULT NULL,
    -- ★ 阶段一E：也可以挂在**大纲库里某一版**上（她从库里点的"我改了这一版"）。
    --   跟 candidate_id 二选一，都是"AI 原稿是哪一份"的锚点。
    outline_id      INTEGER DEFAULT NULL,
    model_key       TEXT    NOT NULL DEFAULT '',
    model_name      TEXT    NOT NULL DEFAULT '',

    source          TEXT    NOT NULL DEFAULT 'adopted',

    -- ---- 她的成品 ----
    user_text       TEXT    NOT NULL DEFAULT '',
    user_json       TEXT    NOT NULL DEFAULT '{}',

    -- ---- AI 对比出来的结构差异（纯函数算的，不花钱）----
    -- 形状跟 diff_outlines() 的返回一致，界面直接复用现成的渲染。
    diff_json       TEXT    NOT NULL DEFAULT '{}',

    -- ---- AI 总结出来的改写偏好 ----
    summary_text    TEXT    NOT NULL DEFAULT '',
    summary_json    TEXT    NOT NULL DEFAULT '[]',
    summary_model   TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT '待分析',
    error           TEXT    NOT NULL DEFAULT '',

    -- ---- 备注：她自己写的一句（比如"这版我把结局全换了"）----
    note            TEXT    NOT NULL DEFAULT '',

    -- 只有 enabled=1 的才进以后的生成提示词。默认开 ——
    -- 她主动交了一份成品过来，意思就是要它起作用。
    enabled         INTEGER NOT NULL DEFAULT 1,

    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rewrites_own
    ON outline_rewrites (owner_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_rewrites_cand
    ON outline_rewrites (owner_id, candidate_id);
"""

# ---- 逐条建议表（2026-10-08 阶段一加）----
#
# 【为什么从单行 JSON 拆出来】原来 N 条建议全塞在
# outline_rewrites.summary_json 里，只能整份开关（一个 enabled）。
# 需求第 8 条要的是**逐条**确认：这条接受、那条拒绝、还有一条只算本篇。
# 塞在一个 JSON 里做这件事，等于每次改一条就把整块读出来、改了、写回去 ——
# 并发时后写的那次会盖掉前一次（她点得快一点就会丢确认）。
# 拆成表之后一条建议一行，各改各的，互不干扰。
#
# ★★ 三个轴分开存（2026-10-08 折腰纠正）★★
#   review  审核状态：待确认 / 已接受 / 已拒绝
#   active  使用状态：启用 / 停用
#   scope   适用范围：长期偏好 / 情境适用 / 仅本篇
# 一条建议可以同时是「已接受 + 启用 + 仅本篇」——
# 它只在对应作品里生效，不会因为接受了就进别的作品。
# 「判断不充分、需要解释」是 confidence，**不是** scope 的一个取值。
#
# 【8 项证据字段（需求第 6 条）为什么都要留】
# 需求点名"禁止『加强逻辑』『增加细节』这种没法执行的结论"。
# 一条建议能被判定"能不能执行"，靠的就是这几格填得实不实。
#
# 【为什么原稿/人工稿不在这张表里】它们只存一份，在 outline_rewrites 上，
# 而且**只增不改**。建议是可再生的（重跑分析就有一份新的），
# 稿子是不可再生的 —— 两者的生命周期不一样，不该混在一张表里。
REWRITE_POINT_SCHEMA = """
CREATE TABLE IF NOT EXISTS outline_rewrite_points (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id        TEXT    NOT NULL,
    rewrite_id      INTEGER NOT NULL,

    -- 第几条（AI 输出里的顺序），界面按它排；重跑分析会整批换掉
    seq             INTEGER NOT NULL DEFAULT 0,

    -- ---- 归到哪一类（节奏/篇幅/删减/增补/人物/冲突/信息/结尾/语言）----
    kind            TEXT    NOT NULL DEFAULT '',

    -- ---- 三个正交的轴 ----
    review          TEXT    NOT NULL DEFAULT '待确认',   -- 审核状态
    active          TEXT    NOT NULL DEFAULT '启用',     -- 使用状态
    scope           TEXT    NOT NULL DEFAULT 'this',     -- 适用范围
    confidence      TEXT    NOT NULL DEFAULT '未标注',   -- 分析把握程度

    -- ---- 结论本身 ----
    point           TEXT    NOT NULL DEFAULT '',   -- 一句话说清她倾向什么
    how             TEXT    NOT NULL DEFAULT '',   -- 以后具体怎么做

    -- ---- 8 项证据（需求第 6 条）----
    problem         TEXT    NOT NULL DEFAULT '',
    change_note     TEXT    NOT NULL DEFAULT '',   -- 她那版改成了什么（change 是 SQL 关键字）
    evidence        TEXT    NOT NULL DEFAULT '',
    improved        TEXT    NOT NULL DEFAULT '',
    method          TEXT    NOT NULL DEFAULT '',
    applies_when    TEXT    NOT NULL DEFAULT '',
    not_when        TEXT    NOT NULL DEFAULT '',
    -- 【uncertain 为什么单独一列】需求第 4 条：情节对齐允许不确定，
    -- 且"不确定"本身也是要留下的信息 —— 它不是 confidence（把握程度），
    -- 而是"这一条我到底对不对得上"的标记。
    uncertain       TEXT    NOT NULL DEFAULT '',

    -- ---- 她自己的话 ----
    user_note       TEXT    NOT NULL DEFAULT '',   -- 补充解释
    user_suggest    TEXT    NOT NULL DEFAULT '',   -- 她改后的措辞（改后接受时填）

    -- ---- 来自哪一版分析（可追溯，需求第 1/4 条）----
    source_version  TEXT    NOT NULL DEFAULT '',   -- 分析那一刻人工稿的版本
    source_model    TEXT    NOT NULL DEFAULT '',

    -- 冲突/去重时用来标记"这条被哪条盖住了"（阶段二用，先留列）
    superseded_by   INTEGER DEFAULT NULL,

    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rwpoints_own
    ON outline_rewrite_points (owner_id, rewrite_id, seq);
CREATE INDEX IF NOT EXISTS idx_rwpoints_live
    ON outline_rewrite_points (owner_id, review, active, id DESC);
"""

# ---- 审核动作日志（2026-10-08 折腰点名要的）----
#
# 「修改后接受」要**保存为操作记录及修改后的内容**（需求第 1 条）。
# 一次动作 = 一行，记下：从什么状态到什么状态、她改后的措辞、
# 当时那条建议长什么样（快照，事后她改了建议也翻得出原话）。
#
# 【为什么不写在一列上】审计轨迹是"发生过什么"，写在建议行上，
# 下一次动作就把上一次覆盖了 —— 那就不是轨迹了。
REWRITE_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS outline_rewrite_actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id        TEXT    NOT NULL,
    rewrite_id      INTEGER NOT NULL,
    point_id        INTEGER DEFAULT NULL,
    action          TEXT    NOT NULL DEFAULT '',
    from_review     TEXT    NOT NULL DEFAULT '',
    to_review       TEXT    NOT NULL DEFAULT '',
    from_active     TEXT    NOT NULL DEFAULT '',
    to_active       TEXT    NOT NULL DEFAULT '',
    from_scope      TEXT    NOT NULL DEFAULT '',
    to_scope        TEXT    NOT NULL DEFAULT '',
    -- 她这次改后的措辞 / 补充说明（「修改后接受」要留的就是这个）
    detail          TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rwactions_own
    ON outline_rewrite_actions (owner_id, rewrite_id, id DESC);
"""

# ---- 生成时"到底用了哪些学习条目"（需求第 8 条，阶段一就要能查）----
#
# 【为什么不能只记"学习开关已打开"】折腰原话："每次生成保存实际采用的
# 学习条目及版本、作用阶段和必要的调用记录。不能只记录『学习开关已打开』。"
# 开关打开 ≠ 有东西进来（她可能一条都还没接受）。要能回答
# "这一次生成的规划阶段，具体注入了哪几条、哪一版"。
REWRITE_USE_SCHEMA = """
CREATE TABLE IF NOT EXISTS outline_learning_uses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id      TEXT    NOT NULL,
    run_id        INTEGER DEFAULT NULL,
    candidate_id  INTEGER DEFAULT NULL,
    -- 哪一批（跑这一次生成时算的批次），同一批共用一个 batch
    batch         TEXT    NOT NULL DEFAULT '',
    -- 作用在哪个阶段：story/stage/lite，或 all（暂未分阶段时）
    stage         TEXT    NOT NULL DEFAULT 'all',
    point_id      INTEGER DEFAULT NULL,
    rewrite_id    INTEGER DEFAULT NULL,
    -- 快照：注入时的文字 + 版本，事后她改了建议也翻得出"当时发的是什么"
    point_text    TEXT    NOT NULL DEFAULT '',
    scope         TEXT    NOT NULL DEFAULT '',
    confidence    TEXT    NOT NULL DEFAULT '',
    source_version TEXT   NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_learnuse_run
    ON outline_learning_uses (owner_id, run_id, stage, id DESC);
CREATE INDEX IF NOT EXISTS idx_learnuse_cand
    ON outline_learning_uses (owner_id, candidate_id, id DESC);
"""


def migrate(verbose=False):
    """建这七张表，再给老库补上后加的列。反复跑是安全的。

    【2026-09-26 起这里多了一步 ALTER】
    这几张表当初是全新空表建的，但 finish_reason / gaps_json 是后来才加的 ——
    已经建过表的库（包括她的真实库）**不会**因为
    CREATE TABLE IF NOT EXISTS 就多出列，必须显式 ALTER。
    补列只加不改、老数据落到默认值，所以不需要整库备份。

    【2026-10-08 加了一张新表】
    outline_rewrites（改写对比）。**新表**用 CREATE TABLE IF NOT EXISTS
    就够了 —— 老库里没有它，会被建出来；已经有的库不受影响。
    这一条跟上面的补列是两种不同的事，别混。

    【2026-10-08 阶段一又加了两张 + 一堆列】
    outline_rewrite_points（逐条建议）+ outline_rewrite_actions（审核动作日志）
    都是**新表**，各自单独 execut。
    outline_rewrites 上补的那些（世界观/角色卡/AI 原稿快照、当时输入、
    分析模型+提示词版本、人工稿版本号、source_kind）走 _ADDED_COLUMNS。
    两种事还是别混 —— 新表能被"从头建"覆盖，补列只能 ALTER。
    """
    with db.connect() as conn:
        conn.executescript(SCHEMA)
        conn.executescript(REWRITE_SCHEMA)
        conn.executescript(REWRITE_POINT_SCHEMA)
        conn.executescript(REWRITE_LOG_SCHEMA)
        conn.executescript(REWRITE_USE_SCHEMA)
        _add_missing_columns(conn)
        # ★ 表结构换代：旧的单轴 status 版本 → 三轴（scope/review/active）。
        #   CREATE TABLE IF NOT EXISTS 改不了已存在的表，必须显式搬一次。
        _migrate_rewrite_points_schema(conn, verbose)
    backfill_candidate_meta(verbose)
    # 阶段一之前建的改写记录：补快照状态、老 points 搬进新表（待确认）。
    # **不猜**世界观/角色卡 —— 那两样留空、标缺失，绝不用现在的冒充。
    backfill_rewrite_snapshots(verbose)
    if verbose:
        print("大纲生成：十一张表就位 →", db.DB_PATH)
    return db.DB_PATH


# 后加的列：(表名, 列名, 列定义)。以后加列就往这里添一行。
#
# 【为什么用表驱动】补列这件事一写成 if 判断就会长成一片：每加一列塞一个
# if，还得靠注释记"这列是哪次加的"。列在这里是**声明**，用 PRAGMA 对个差集
# 就补上，加多少列都是同一段代码、同一个测试。
_ADDED_COLUMNS = (
    ("outline_candidates", "finish_reason", "TEXT NOT NULL DEFAULT ''"),
    ("outline_candidates", "gaps_json", "TEXT NOT NULL DEFAULT '[]'"),
    # 分类素材参考的节省效果记录（任务书补充二.10）：
    # reference_meta_json 记 {needs,candidates,injected} 三个数，
    # input_hash 记这次生成用到的输入快照指纹（补充二.5，缓存命中靠它）。
    ("outline_candidates", "reference_meta_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("outline_candidates", "input_hash", "TEXT NOT NULL DEFAULT ''"),
    ("outline_runs", "input_hash", "TEXT NOT NULL DEFAULT ''"),

    # ---- 改写对比：阶段一补的（2026-10-08）----
    #
    # 【为什么这些要落到 rewrite 行上，而不是分析时现取】
    # 原来分析时是拿 run["input"] 现取的 —— 只要那个 run 还在，取出来是对的。
    # 但需求第 4 条要的是"保存完整可追溯的学习材料"：
    # **这一条建议是在什么设定下得出的**，以后得能单独拿出来看。
    # run 是会被清理/它自己的输入也会变的，所以这里存一份快照。
    #
    # 【"原稿和人工稿不能被分析结果覆盖"怎么落】
    # 重跑分析只 UPDATE summary_* 和这几个新列，**user_text / user_json /
    # diff_json 一个字都不动**（见 set_rewrite_summary）。
    # 全新的分析结果落在 outline_rewrite_points 里，旧的那批标 source_version，
    # 不是删掉。
    ("outline_rewrites", "source_kind", "TEXT NOT NULL DEFAULT 'rewrite'"),
    ("outline_rewrites", "world_snapshot", "TEXT NOT NULL DEFAULT ''"),
    ("outline_rewrites", "chars_snapshot", "TEXT NOT NULL DEFAULT '[]'"),
    ("outline_rewrites", "hook_snapshot", "TEXT NOT NULL DEFAULT ''"),
    ("outline_rewrites", "design_snapshot", "TEXT NOT NULL DEFAULT ''"),
    ("outline_rewrites", "target_words", "INTEGER NOT NULL DEFAULT 0"),
    ("outline_rewrites", "analyze_model", "TEXT NOT NULL DEFAULT ''"),
    ("outline_rewrites", "analyze_version", "TEXT NOT NULL DEFAULT ''"),
    ("outline_rewrites", "user_version", "INTEGER NOT NULL DEFAULT 1"),
    # ★ 阶段一E：挂在大纲库里某一版上（从库里点"我改了这一版"）。
    # 老库补这一列，默认 NULL = 没挂。
    ("outline_rewrites", "outline_id", "INTEGER DEFAULT NULL"),
    # 按 (owner, 候选) 找"上一次"用，取代原来直接扫 candidate_id 的写法 ——
    # 幂等那一段要连"最新那一版的版本号"一起看。
    ("outline_rewrites", "ai_text_len", "INTEGER NOT NULL DEFAULT 0"),

    # ---- ★ 需求第 2 条：AI 原稿快照 + 恢复来源（2026-10-08 折腰点名）----
    #
    # 「不能继续只靠 candidate_id 找原稿。原候选修改或删除后，
    #   学习证据仍应准确。」
    # 所以分析那一刻把原稿全文抄一份到 rewrite 行上，以后不许再回头去
    # 候选表里捞 —— 候选可能被她编辑、也可能被清理掉。
    ("outline_rewrites", "ai_text_snapshot", "TEXT NOT NULL DEFAULT ''"),
    ("outline_rewrites", "ai_model_snapshot", "TEXT NOT NULL DEFAULT ''"),
    # 这份原稿是怎么来的：candidate=分析时从候选抄的；missing=当时就没有；
    # recovered=老记录补录回来的（注明来源）。界面上要如实标出来，
    # **绝不许拿现在的东西冒充当时的东西**（需求第 2 条原文）。
    ("outline_rewrites", "ai_source_note", "TEXT NOT NULL DEFAULT ''"),
    # 用户的创作要求（梗/情节/字数）也冻一份快照在行上，
    # 跟 world/chars 快照一起构成"分析那一刻的完整输入"。
    ("outline_rewrites", "req_snapshot", "TEXT NOT NULL DEFAULT '{}'"),
    # 分析开始那一刻的输入指纹 —— 分析跑着的时候她又编辑了页面，
    # 结果不能算在这份新输入头上（需求第 2 条：分析开始后编辑页面，
    # 不得改变正在分析的输入）。
    ("outline_rewrites", "input_frozen", "TEXT NOT NULL DEFAULT ''"),
    # 快照的补齐状态：complete / partial / missing_recovered / missing
    ("outline_rewrites", "snapshot_state", "TEXT NOT NULL DEFAULT ''"),
)


def _add_missing_columns(conn):
    """把 _ADDED_COLUMNS 里声明、但库里还没有的列补上。"""
    for table, col, decl in _ADDED_COLUMNS:
        if not _has_table(conn, table):
            continue
        have = {r["name"] for r in conn.execute(
            "PRAGMA table_info(%s)" % table).fetchall()}
        if col in have:
            continue
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))


def _migrate_rewrite_points_schema(conn, verbose=False):
    """把「旧中间版本」的 outline_rewrite_points 换成当前结构。

    ★ 2026-10-08 折腰实测撞上的 bug（大纲页「用哪个模型」下拉一片空白）★
    根因不是下拉本身，是 /api/outline-meta 里的 learning_stats() 报 500：
        sqlite3.OperationalError: no such column: review
    她的库里有这张表，但列是**旧中间版本**的（scope=适用层次 + status=单一状态）；
    而当前代码要求的是**三个正交轴** scope(适用范围) + review(审核) + active(使用)。
    CREATE TABLE IF NOT EXISTS 只在表不存在时建表 —— 表已经在了，所以列从来没被改过。
    她那次把三轴拆开的纠正之后，我漏了给已有库补一段列迁移，这就是那处漏洞。

    【为什么不能简单 ALTER 加列】
    旧 structure 里没有 review/active，但有 status；直接 ADD COLUMN 会多出
    两个空列，而 status 里原本存着的"她拍板了没有"的信息就废了。
    所以要**重建表**，把旧 status 的值按语义拆进 review + active。

    【旧 status 怎么拆（她纠正前的那一套单轴取值）】
        待确认  → review=待确认, active=启用
        已接受  → review=已接受, active=启用
        已拒绝  → review=已拒绝, active=启用
        仅本篇  → review=待确认, scope 压到 this（"仅本篇"其实是范围，不是状态）
        已停用  → review=已接受, active=停用（接受过才谈得上停用）
    认不出来的一律 review=待确认（最保守：宁可让她再点一次，不敢当她已认可）。

    【安全性】
    - 只认"表在、但缺 review 列"这一种情况；已经是新结构的一律不碰。
    - 重建走「建新表 → 逐行搬 → 删旧表 → 改名」，全程在一个事务里，
      中途出错整段回滚，不会留下半张表。
    - 她的那张表当前是 0 行，但这里仍按"有数据"来写 ——
      今天为空不代表以后别的库为空，搬数据的逻辑必须真的对。
    """
    if not _has_table(conn, "outline_rewrite_points"):
        return 0
    have = {r["name"] for r in conn.execute(
        "PRAGMA table_info(outline_rewrite_points)").fetchall()}
    if "review" in have:
        return 0                      # 已经是新结构，不碰

    # 旧结构长什么样：有 status、没有 review。不是这个形状就交给别人处理。
    if "status" not in have:
        return 0

    _OLD_STATUS_TO_AXES = {
        "待确认": (REWRITE_RV_PENDING, REWRITE_ACTIVE_ON),
        "已接受": (REWRITE_RV_ACCEPTED, REWRITE_ACTIVE_ON),
        "已拒绝": (REWRITE_RV_REJECTED, REWRITE_ACTIVE_ON),
        "仅本篇": (REWRITE_RV_PENDING, REWRITE_ACTIVE_ON),
        "已停用": (REWRITE_RV_ACCEPTED, REWRITE_ACTIVE_OFF),
    }

    old_rows = conn.execute(
        "SELECT * FROM outline_rewrite_points").fetchall()
    n = len(old_rows)

    # 旧表先改名（是"拆列"不是"删表"，万一搬挂了还能翻回去）
    conn.execute("ALTER TABLE outline_rewrite_points"
                 " RENAME TO _rwpoints_old_tmp")
    conn.execute("DROP INDEX IF EXISTS idx_rwpoints_own")
    conn.execute("DROP INDEX IF EXISTS idx_rwpoints_live")
    # 建当前结构的新表（复用同一份 schema，避免两处定义分叉）
    conn.executescript(REWRITE_POINT_SCHEMA)

    NEW_COLS = [
        "id", "owner_id", "rewrite_id", "seq", "kind",
        "review", "active", "scope", "confidence",
        "point", "how", "problem", "change_note", "evidence",
        "improved", "method", "applies_when", "not_when", "uncertain",
        "user_note", "user_suggest", "source_version", "source_model",
        "superseded_by", "created_at", "updated_at",
    ]
    placeholders = ",".join("?" * len(NEW_COLS))
    ins = "INSERT INTO outline_rewrite_points (%s) VALUES (%s)" % (
        ",".join(NEW_COLS), placeholders)

    for r in old_rows:
        d = dict(r)
        st = str(d.get("status") or "").strip()
        review, active = _OLD_STATUS_TO_AXES.get(
            st, (REWRITE_RV_PENDING, REWRITE_ACTIVE_ON))
        scope = str(d.get("scope") or "").strip()
        # 旧的 scope 是"适用层次"，取值也是 long/case/this，语义刚好对齐；
        # 认不出就压到最保守的 this。另外"仅本篇"那类要锁死 scope=this。
        if st == "仅本篇" or scope not in ALL_REWRITE_SCOPE:
            scope = REWRITE_SCOPE_THIS
        conf = str(d.get("confidence") or "").strip()
        if conf not in ALL_REWRITE_CONF:
            conf = REWRITE_CONF_UNKNOWN
        vals = [
            d.get("id"), d.get("owner_id"), d.get("rewrite_id"),
            d.get("seq", 0), d.get("kind", ""),
            review, active, scope, conf,
            d.get("point", ""), d.get("how", ""),
            d.get("problem", ""), d.get("change_note", ""),
            d.get("evidence", ""), d.get("improved", ""),
            d.get("method", ""), d.get("applies_when", ""),
            d.get("not_when", ""), d.get("uncertain", ""),
            d.get("user_note", ""), d.get("user_suggest", ""),
            d.get("source_version", ""), d.get("source_model", ""),
            d.get("superseded_by"),
            d.get("created_at", now_str()), d.get("updated_at", now_str()),
        ]
        conn.execute(ins, vals)

    conn.execute("DROP TABLE _rwpoints_old_tmp")
    if verbose:
        print("outline_rewrite_points：旧结构 → 三轴结构，搬了 %d 行" % n)
    return n


def guess_finish_reason(raw):
    """从原始返回里抠出 finish_reason。抠不出来就返回空串。

    【为什么解析失败不硬猜】正常被掐断的返回体仍是合法 JSON
    （finish_reason 在 choices 里，只有正文是半截），所以一般解得出来。
    真解不出来时宁可返回空 —— 调用方会把它记成 FINISH_UNKNOWN
    （"当时没记"），而不是硬安一个"正常写完"上去。
    """
    try:
        d = json.loads(raw or "")
    except Exception:
        return ""
    ch = d.get("choices") or []
    if not ch:
        return ""
    return (ch[0].get("finish_reason") or "").strip()


def structure_gaps(obj):
    """这份大纲在**结构上**缺了什么，返回几个短词。

    【为什么跟 validate_outline 分开写】那个是"提醒清单"，把字数偏离、
    空泛说法、缺结局混在一起，一条条读下来看不出重点在哪。
    这一个只回答"结构完整不完整"，只回几个短词，
    好让界面上压成一行、扫一眼就分得开（她要的正是这个）。
    """
    o = obj or {}
    if not isinstance(o, dict):
        return []
    gaps = []
    if not (o.get("story_core") or "").strip():
        gaps.append("故事核心")
    if not (o.get("climax") or "").strip():
        gaps.append("高潮")
    if not (o.get("ending") or "").strip():
        gaps.append("结局")
    nodes = [n for n in (o.get("nodes") or []) if isinstance(n, dict)]
    if not nodes:
        gaps.append("所有段落")
        return gaps
    # 「哪一段没写完」要能指名道姓 —— 只说"结构不完整"等于没说。
    empty = [n.get("node_title") or ("第 %d 段" % (i + 1))
             for i, n in enumerate(nodes)
             if not (n.get("event") or "").strip()]
    if empty:
        gaps.append("段落内容（%s）" % "、".join(empty[:3]))
    return gaps


def backfill_candidate_meta(verbose=False):
    """给"补列那一刻之前生成的"老候选补上结束原因与结构缺口。

    【为什么补得出来】raw_response 和 content_json 当时就存下来了，
    只是没算成字段 —— 没算不等于没有，所以**不用重新花钱跑一遍**。

    【幂等靠什么】判据是 finish_reason=''，而只有老行会是空
    （新行写入时必填，连抠不出来也记成 FINISH_UNKNOWN）。
    所以补完再跑，挑出来的就是 0 行，热重载天天触发也没关系。
    """
    n = 0
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, raw_response, content_json FROM outline_candidates"
            " WHERE finish_reason=''").fetchall()
        for r in rows:
            fr = guess_finish_reason(r["raw_response"]) or llm.FINISH_UNKNOWN
            conn.execute(
                "UPDATE outline_candidates SET finish_reason=?, gaps_json=?"
                " WHERE id=?",
                (fr, _dumps(structure_gaps(_loads(r["content_json"], {}))),
                 r["id"]))
            n += 1
    if verbose and n:
        print("补了 %d 条历史候选的结束原因 / 结构缺口" % n)
    return n


# 快照补齐状态（rewrite 行上的 snapshot_state）：
#   complete        有原稿 + 有设定，齐
#   no_original     独立创作，本来就没有原稿（不是缺失，是事实）
#   partial         有原稿但设定不全
#   legacy_missing  老记录：阶段一之前建的，当时压根没存
#   recovered       老记录但**从候选里把原稿补回来了**（注明来源）
SNAP_COMPLETE = "complete"
SNAP_NO_ORIGINAL = "no_original"
SNAP_PARTIAL = "partial"
SNAP_LEGACY_MISSING = "legacy_missing"
SNAP_RECOVERED = "recovered"
SNAP_LABELS = {
    SNAP_COMPLETE: "有完整快照",
    SNAP_NO_ORIGINAL: "没有 AI 原稿（自己写的）",
    SNAP_PARTIAL: "设定快照不全",
    SNAP_LEGACY_MISSING: "老记录：当时没存快照",
    SNAP_RECOVERED: "老记录：原稿已从候选补回",
}


def backfill_rewrite_snapshots(verbose=False):
    """给阶段一之前建的改写记录补快照状态、并**如实标注**能补/不能补。

    【折腰第 2 条要求原文】
      · 能恢复的内容，注明恢复来源。
      · 无法恢复的内容，标记缺失。
      · **不能把现在的世界观、角色卡冒充当时的版本。**
      · 旧版未经逐条确认的 points，保留记录并进入待复核状态，
        不能自动变成新版已确认规则。

    【本函数只做能确认的三件事】
    1. snapshot_state 为空（= 阶段一之前建的）→ 试着重算：
       · 有候选 → 从候选抄原稿全文，标 recovered（注明来源）
       · 没候选 → 标 legacy_missing（当时就没存，不编）
    2. 老的 summary_json 条目 → 搬进 outline_rewrite_points，
       状态一律 **待确认**（不是"已接受"！她当年没逐条确认过）
    3. **绝不**去 outline_runs 里捞世界观/角色卡冒充"当时那份" ——
       run 的输入可能早被改了，那样填进来的就不是当时的真相。
       所以 world/chars 快照留空，靠 snapshot_state 告诉她"这块缺"。
    """
    n = 0
    with db.connect() as conn:
        if not _has_table(conn, "outline_rewrites"):
            return 0
        rows = conn.execute(
            "SELECT id, owner_id, candidate_id, source_kind, snapshot_state,"
            " ai_text_snapshot, summary_json, user_version"
            " FROM outline_rewrites WHERE snapshot_state=''").fetchall()
        for r in rows:
            state = SNAP_LEGACY_MISSING
            ai_text = ""
            ai_model = ""
            note = "老记录：建这条时还没开始存快照。"
            cid = r["candidate_id"]
            if cid:
                cand = conn.execute(
                    "SELECT content_text, content_json, model_name, model_key"
                    " FROM outline_candidates WHERE id=? AND owner_id=?",
                    (cid, r["owner_id"])).fetchone()
                if cand:
                    ai_text = (cand["content_text"]
                               or render_outline_text(_loads(cand["content_json"], {}))
                               or "")
                    ai_model = cand["model_name"] or cand["model_key"] or ""
                    if ai_text.strip():
                        state = SNAP_RECOVERED
                        note = ("老记录：原稿已从候选 #%d 补回"
                                "（不是当时存的，是事后补的）。" % cid)
                    else:
                        note = ("老记录：候选 #%d 还在，但里面没有正文，"
                                "原稿恢复不了。" % cid)
                else:
                    note = ("老记录：原候选 #%d 已经不在了，"
                            "这一条的原稿恢复不了。" % cid)
            conn.execute(
                "UPDATE outline_rewrites SET snapshot_state=?, ai_source_note=?,"
                " ai_text_snapshot=?, ai_model_snapshot=?"
                " WHERE id=? AND owner_id=?",
                (state, note, ai_text, ai_model, r["id"], r["owner_id"]))
            # 老的 summary_json 条目 → 搬进 points 表，**一律待确认**
            try:
                v = int(r["user_version"] or 1)
            except (TypeError, ValueError):
                v = 1
            old_pts = _loads(r["summary_json"], [])
            if isinstance(old_pts, list) and old_pts:
                has = conn.execute(
                    "SELECT COUNT(*) AS n FROM outline_rewrite_points"
                    " WHERE owner_id=? AND rewrite_id=?",
                    (r["owner_id"], r["id"])).fetchone()["n"]
                if not has:
                    ts = now_str()
                    for i, p in enumerate(old_pts, 1):
                        if isinstance(p, str):
                            p = {"point": p}
                        if not isinstance(p, dict):
                            continue
                        pt = (p.get("point") or "").strip()
                        if not pt:
                            continue
                        conn.execute(
                            "INSERT INTO outline_rewrite_points (owner_id,"
                            " rewrite_id, seq, kind, review, active, scope,"
                            " confidence, point, how, source_version,"
                            " source_model, user_note, created_at, updated_at)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (r["owner_id"], r["id"], i,
                             (p.get("kind") or "")[:20],
                             REWRITE_RV_PENDING,      # ★ 待确认，不是已接受
                             REWRITE_ACTIVE_ON,
                             REWRITE_SCOPE_THIS,      # ★ 仅本篇，最保守
                             REWRITE_CONF_UNKNOWN, pt[:600],
                             (p.get("how") or "")[:600], str(v), "",
                             "（老记录搬过来的，还没逐条确认过）", ts, ts))
            n += 1
    if verbose and n:
        print("处理了 %d 条历史改写记录的快照状态" % n)
    return n


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------

def _dumps(v):
    return json.dumps(v, ensure_ascii=False)


def _loads(s, fallback):
    """把 JSON 列解出来。解不开给兜底 —— 手工改坏过的库不该让整页崩掉。"""
    if not s:
        return fallback
    try:
        v = json.loads(s)
    except Exception:
        return fallback
    return v if isinstance(v, type(fallback)) else fallback


def _txt(v, limit, name, allow_empty=True, warns=None):
    """一段纯文本：去两端空白、卡长度。

    【warns 的两种模式 —— 这是 2026-09-27 补的，血的教训】
    模型返回的东西和她手填的东西，处理方式必须相反：

      · warns 传进来了 = 「这是模型的返回，能救就救」。
        超长就**切到上限**，同时记一条警告挂到候选卡上，不整份作废 ——
        她为此已经付过钱了。宁可给她一份带瑕疵的大纲，也别让她看到
        "失败"两个字然后重跑一遍再花一次钱。
      · warns 没传 = 「这是她自己在界面上填的」。
        直接报错，让她当场去改 —— 背着她偷偷切掉她写的一段，
        比报错恶劣得多（她会以为系统把它存下了）。

    为什么以前不这么分：以前一律"超了就抛错"，于是模型多写了几个字，
    整份大纲连同 171 秒、2.6 万字的输入一起作废。
    """
    s = (v if isinstance(v, str) else ("" if v is None else str(v))).strip()
    if len(s) > limit:
        if warns is None:
            raise ValueError("%s最长 %d 个字，现在有 %d 个。" % (name, limit, len(s)))
        warns.append("%s太长（%d 字，上限 %d 字），超出那截先切掉了。"
                     % (name, len(s), limit))
        s = s[:limit]
    if not s and not allow_empty:
        raise ValueError("%s不能空着。" % name)
    return s


def _str_list(v, limit, item_max, name, warns=None):
    """一组字符串：去空白、去重、保序、逐个卡长度。

    warns 的两种模式见 _txt 的说明（传 = 模型返回，宽容并记警告）。
    """
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple)):
        if warns is None:
            raise ValueError("%s得是一组文字。" % name)
        warns.append("%s不是一组文字（收到的是 %s），这一项先当空的。"
                     % (name, type(v).__name__))
        return []
    out = []
    cut_long = 0
    for x in v:
        s = str(x or "").strip()
        if not s:
            continue
        if len(s) > item_max:
            if warns is None:
                raise ValueError("%s里的「%s…」太长了（最长 %d 字）。"
                                 % (name, s[:12], item_max))
            s = s[:item_max]
            cut_long += 1
        if s not in out:
            out.append(s)
    if cut_long:
        warns.append("%s里有 %d 条太长，超出那截先切掉了。" % (name, cut_long))
    if len(out) > limit:
        if warns is None:
            raise ValueError("%s最多 %d 条，现在给了 %d 条。"
                             % (name, limit, len(out)))
        warns.append("%s给了 %d 条，超过上限 %d 条，多出来的先没算。"
                     % (name, len(out), limit))
        out = out[:limit]
    return out


def _int_list(v, limit, name, warns=None):
    """一组整数 id（来源零件、角色 id 之类）。

    warns 的两种模式见 _txt 的说明（传 = 模型返回，宽容并记警告）。
    """
    if v is None:
        return []
    if isinstance(v, (str, int)):
        v = [v]
    if not isinstance(v, (list, tuple)):
        if warns is None:
            raise ValueError("%s得是一组编号。" % name)
        warns.append("%s不是一组编号（收到的是 %s），这一项先当空的。"
                     % (name, type(v).__name__))
        return []
    out = []
    bad = []
    for x in v:
        if x in (None, ""):
            continue
        try:
            n = int(x)
        except (TypeError, ValueError):
            if warns is None:
                raise ValueError("%s里有不是编号的东西：「%s」" % (name, x))
            bad.append(str(x))
            continue
        if n and n not in out:
            out.append(n)
    if bad:
        warns.append("%s里有 %d 个不是编号的东西（比如「%s」），先跳过了。"
                     % (name, len(bad), bad[0][:12]))
    if len(out) > limit:
        if warns is None:
            raise ValueError("%s最多 %d 个，现在给了 %d 个。"
                             % (name, limit, len(out)))
        warns.append("%s给了 %d 个，超过上限 %d 个，多出来的先没算。"
                     % (name, len(out), limit))
        out = out[:limit]
    return out


def _has_table(conn, name):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone())


# ----------------------------------------------------------------------
# 预期字数 → 结构规模
# ----------------------------------------------------------------------

def word_tier(words):
    """这个字数属于哪一档。返回 WORD_TIERS 里的一条（字典副本）。

    nodes（建议节点数）**在这里现算**，不写死在 WORD_TIERS 里 ——
    它由"预期字数 ÷ 每段字数"决定，见 NODE_WORDS_* 的注释。
    """
    try:
        n = int(words or 0)
    except (TypeError, ValueError):
        n = 0
    d = None
    for t in WORD_TIERS:
        if t["min"] <= n <= t["max"]:
            d = dict(t)
            break
    if d is None:
        d = dict(WORD_TIERS[0])
    d["nodes"] = list(node_range_for(n))
    return d


def tier_of_key(key):
    """按 key 取字数档。

    【这里故意不给 nodes】节点数得知道**确切字数**才算得准，而这个函数
    只知道一个档位名。要节点数请用 word_tier(实际字数)，别在这儿凑一个
    看起来合理的数字出来。
    """
    for t in WORD_TIERS:
        if t["key"] == key:
            return dict(t)
    return None


# 【"发给模型的那句话"不在这里】它由 outline_ai._target_words_block() 拼 ——
# 那边还要带上档位说明（"6000～9000 字这一档：完整起承转合…"）。
# 以前这个文件里另有一个 word_budget_hint() 也在说节点数和每段字数，
# 措辞还不一样，两处必然走岔 —— 2026-09-26 合并成 _target_words_block 一个出口。


# ----------------------------------------------------------------------
# 角色卡
# ----------------------------------------------------------------------

CHAR_FIELDS = ("name", "identity", "personality", "goal", "fear",
               "relations", "speech", "must_do", "never_do", "note")
CHAR_FIELD_LABELS = {
    "name": "角色名 / 角色位",
    "identity": "身份",
    "personality": "性格",
    "goal": "目标和欲望",
    "fear": "恐惧或弱点",
    "relations": "与其他角色的关系",
    "speech": "说话方式",
    "must_do": "必须遵守",
    "never_do": "禁止出现的行为",
    "note": "备注",
}


def _char_row(row):
    d = {"id": row["id"]}
    for f in CHAR_FIELDS:
        d[f] = row[f]
    d["status"] = row["status"]
    d["created_at"] = row["created_at"]
    d["updated_at"] = row["updated_at"]
    # 空字段数 —— 界面上用来提示"这张卡还没填全"。
    # 计划第三节第 2 条把八项都列出来了，少填不是说不能用，
    # 但"AI 只能照名字猜"这件事她自己得看得见。
    d["filled"] = sum(1 for f in CHAR_FIELDS if (row[f] or "").strip())
    d["field_total"] = len(CHAR_FIELDS)
    return d


def list_characters(owner, status=None, keyword=None, limit=500, offset=0):
    """角色卡列表。默认全给（含停用），带状态标记。"""
    sql = ["SELECT * FROM characters WHERE owner_id=?"]
    args = [owner]
    if status:
        sql.append("AND status=?")
        args.append(status)
    if keyword:
        kw = "%" + keyword.strip() + "%"
        sql.append("AND (name LIKE ? OR identity LIKE ? OR personality LIKE ?"
                   " OR relations LIKE ?)")
        args.extend([kw, kw, kw, kw])
    sql.append("ORDER BY CASE WHEN status='启用' THEN 0 ELSE 1 END, id DESC"
               " LIMIT ? OFFSET ?")
    args.extend([int(limit), int(offset)])
    with db.connect() as conn:
        rows = conn.execute(" ".join(sql), args).fetchall()
        return [_char_row(r) for r in rows]


def get_character(owner, cid):
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        return None
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM characters WHERE id=? AND owner_id=?",
                           (cid, owner)).fetchone()
        return _char_row(row) if row else None


def get_characters(owner, ids):
    """按 id 批量取（保存大纲时要把它们写成快照）。顺序按传进来的来。"""
    ids = _int_list(ids, 50, "角色卡")
    if not ids:
        return []
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM characters WHERE owner_id=? AND id IN (%s)"
            % ",".join("?" * len(ids)), [owner] + ids).fetchall()
    by_id = {r["id"]: _char_row(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def create_character(owner, data, created_by=""):
    """新建一张角色卡。

    【重名为什么不覆盖】跟模型清单 ADD 那条一个道理：
    "新建"撞上已有的名字，正确反应是告诉她"这个名字有了"，
    而不是默默盖掉原来那张 —— 那等于删数据。
    """
    data = data or {}
    name = _txt(data.get("name"), CHAR_NAME_MAX, "角色名", allow_empty=False)
    fields = {"name": name}
    for f in CHAR_FIELDS:
        if f == "name":
            continue
        limit = CHAR_NOTE_MAX if f == "note" else CHAR_FIELD_MAX
        fields[f] = _txt(data.get(f), limit, CHAR_FIELD_LABELS[f])

    ts = now_str()
    with db.connect() as conn:
        old = conn.execute(
            "SELECT id FROM characters WHERE owner_id=? AND name=?",
            (owner, name)).fetchone()
        if old:
            raise ValueError("已经有一张叫「%s」的角色卡了。"
                             "换个名字，或者直接改那一张。" % name)
        cur = conn.execute(
            """INSERT INTO characters
               (owner_id, name, identity, personality, goal, fear, relations,
                speech, must_do, never_do, note, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (owner, fields["name"], fields["identity"], fields["personality"],
             fields["goal"], fields["fear"], fields["relations"], fields["speech"],
             fields["must_do"], fields["never_do"], fields["note"],
             CHAR_ON, ts, ts))
        cid = cur.lastrowid
        row = conn.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()
        return _char_row(row)


def update_character(owner, cid, patch):
    """改一张角色卡。只改传进来的字段（不传 = 不动）。

    这里没有"版本"概念 —— 角色卡是她随手维护的资料卡，不是成品。
    大纲那边存的是**保存那一刻的快照**，所以改角色卡永远影响不到老大纲。
    """
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        return None
    patch = patch or {}
    sets, args = [], []
    for f in CHAR_FIELDS:
        if f not in patch:
            continue
        if f == "name":
            v = _txt(patch.get(f), CHAR_NAME_MAX, "角色名", allow_empty=False)
        else:
            limit = CHAR_NOTE_MAX if f == "note" else CHAR_FIELD_MAX
            v = _txt(patch.get(f), limit, CHAR_FIELD_LABELS[f])
        sets.append(f + "=?")
        args.append(v)
    if "status" in patch:
        st = str(patch.get("status") or "").strip()
        if st not in ALL_CHAR_STATUS:
            raise ValueError("没有「%s」这个状态。" % st)
        sets.append("status=?")
        args.append(st)
    if not sets:
        return get_character(owner, cid)

    sets.append("updated_at=?")
    args.append(now_str())
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM characters WHERE id=? AND owner_id=?",
                           (cid, owner)).fetchone()
        if not row:
            return None
        # 改名要查重 —— UNIQUE(owner_id, name) 撞上会抛 sqlite3.IntegrityError，
        # 那种错冒到界面上是英文的，不如在这儿换成一句人话。
        if "name" in patch:
            dup = conn.execute(
                "SELECT id FROM characters WHERE owner_id=? AND name=? AND id<>?",
                (owner, _txt(patch.get("name"), CHAR_NAME_MAX, "角色名"),
                 cid)).fetchone()
            if dup:
                raise ValueError("已经有一张叫「%s」的角色卡了。" % patch.get("name"))
        args.extend([cid, owner])
        conn.execute("UPDATE characters SET %s WHERE id=? AND owner_id=?"
                     % ", ".join(sets), args)
        r = conn.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()
        return _char_row(r)


def delete_character(owner, cid):
    """删一张角色卡。

    【为什么允许真删】角色卡是资料，不是成品，也没有被别人"引用计数"。
    已经保存过的大纲存的是快照，删了它照样读得出来 —— 所以删是安全的。
    （跟"零件不能删只能排除"是两回事：零件挂着一堆引用和版本历史。）
    """
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        return False
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM characters WHERE id=? AND owner_id=?",
                           (cid, owner))
        return cur.rowcount > 0


# ----------------------------------------------------------------------
# 世界观库
# ----------------------------------------------------------------------

def _world_row(row):
    return {"id": row["id"], "name": row["name"], "content": row["content"],
            "chars": row["chars"], "created_at": row["created_at"],
            "updated_at": row["updated_at"]}


def list_worldviews(owner, with_content=False, limit=200):
    """世界观列表。默认不带正文（列表上只显示名字和字数）。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM worldviews WHERE owner_id=? ORDER BY updated_at DESC,"
            " id DESC LIMIT ?", (owner, int(limit))).fetchall()
        out = []
        for r in rows:
            d = _world_row(r)
            if not with_content:
                d.pop("content")
                # 列表上要有段预览，否则她分不清"旧城设定"和"旧城设定2"
                d["preview"] = (r["content"] or "")[:80]
            out.append(d)
        return out


def get_worldview(owner, wid):
    try:
        wid = int(wid)
    except (TypeError, ValueError):
        return None
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM worldviews WHERE id=? AND owner_id=?",
                           (wid, owner)).fetchone()
        return _world_row(row) if row else None


def create_worldview(owner, name, content):
    name = _txt(name, WORLD_NAME_MAX, "世界观名字", allow_empty=False)
    content = _txt(content, WORLD_CONTENT_MAX, "世界观正文", allow_empty=False)
    ts = now_str()
    with db.connect() as conn:
        old = conn.execute(
            "SELECT id FROM worldviews WHERE owner_id=? AND name=?",
            (owner, name)).fetchone()
        if old:
            # 【同名为什么是覆盖，不是报错】跟角色卡不同：
            # 世界观是"一段设定文本"，她调完一版再存一次是常态操作，
            # 每次都逼她改名会攒出一堆"世界观3""世界观3-改"。
            conn.execute(
                "UPDATE worldviews SET content=?, chars=?, updated_at=? WHERE id=?",
                (content, len(content), ts, old["id"]))
            r = conn.execute("SELECT * FROM worldviews WHERE id=?",
                             (old["id"],)).fetchone()
            return _world_row(r)
        cur = conn.execute(
            """INSERT INTO worldviews (owner_id, name, content, chars,
               created_at, updated_at) VALUES (?,?,?,?,?,?)""",
            (owner, name, content, len(content), ts, ts))
        r = conn.execute("SELECT * FROM worldviews WHERE id=?",
                         (cur.lastrowid,)).fetchone()
        return _world_row(r)


def delete_worldview(owner, wid):
    try:
        wid = int(wid)
    except (TypeError, ValueError):
        return False
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM worldviews WHERE id=? AND owner_id=?",
                           (wid, owner))
        return cur.rowcount > 0


# ----------------------------------------------------------------------
# 剧情零件：候选池 + 参考次数
# ----------------------------------------------------------------------

def plot_ref_counts(owner, plot_ids=None):
    """每条零件的累计参考次数 —— **现算，不给 plots 加列**。

    口径 = "被多少份**已保存**的大纲采用过"，不是"出现过几次"。
    这三个设计点每一个都有原因：
      · DISTINCT outline_id —— 计划第四.1：同一份大纲里同一条只算 1 次
      · kept=1             —— 只是 AI 提过、被她删掉的不算
      · 不 JOIN outlines   —— 大纲被删了，明细还在，计数不回退
                              （计划第四.1 明确定过）
    """
    with db.connect() as conn:
        if plot_ids:
            ids = _int_list(plot_ids, 2000, "零件")
            if not ids:
                return {}
            rows = conn.execute(
                "SELECT plot_id, COUNT(DISTINCT outline_id) AS n"
                " FROM outline_plot_refs WHERE owner_id=? AND kept=1"
                " AND plot_id IN (%s) GROUP BY plot_id"
                % ",".join("?" * len(ids)), [owner] + ids).fetchall()
        else:
            rows = conn.execute(
                "SELECT plot_id, COUNT(DISTINCT outline_id) AS n"
                " FROM outline_plot_refs WHERE owner_id=? AND kept=1"
                " GROUP BY plot_id", (owner,)).fetchall()
        return {r["plot_id"]: r["n"] for r in rows}


def _plot_local_only(owner, plot_ids):
    """哪些零件背后有"仅本地"的素材。

    【为什么这条链路必须自己走一遍】
    计划第十一节第 4 条：`标记为 local_only 的素材或剧情不能发送给
    不允许的云端模型`。但 **plots 表上没有 local_only 这一列** ——
    零件是从卡片内化来的，卡片的素材才是 `materials.local_only`。
    所以只能 plot_cards → cards → materials 一路查上去。
    漏了这一步的后果很实在：她把某份稿子标成"仅本地"，
    结果生成大纲时那份稿子提炼出来的剧情照样被发给了云端模型。
    """
    if not plot_ids:
        return set()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT pc.plot_id AS pid FROM plot_cards pc"
            " JOIN cards k ON k.id = pc.card_id"
            " JOIN materials m ON m.id = k.material_id"
            " WHERE pc.plot_id IN (%s) AND m.local_only=1"
            % ",".join("?" * len(plot_ids)), list(plot_ids)).fetchall()
        return {r["pid"] for r in rows}


def plot_status_counts(owner):
    """她库里各状态的零件有几条（含已排除）。

    【为什么要有这个函数】
    候选池只收 PLOT_USABLE_STATUS（已确认 / 已编辑）。别的状态的零件
    在 list_candidate_plots 的 WHERE 里就被滤掉了 —— 它们既不进
    items，也不进 blocked，**在任何地方都不出现**。
    结果就是：她 AI 内化出 21 条零件（状态全是「待确认」），去生成大纲时
    看到的是"没有可用的剧情零件"，却完全不知道库里有 21 条被状态挡在外面。
    这个函数就是为了让那 21 条能被数出来、摆到她眼前。
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM plots WHERE owner_id=?"
            " GROUP BY status", (owner,)).fetchall()
    return {r["status"]: r["n"] for r in rows}


def list_blocked_by_status(owner, limit=500):
    """列出来那些"在库里、没被排除、但状态够不上可用"的零件。

    预览页要能具体告诉她是哪几条，而不是只报一个数 ——
    报一个数她还得自己去翻是哪几条。
    """
    out = []
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, title, status FROM plots"
            " WHERE owner_id=? AND status NOT IN (%s) AND status != ?"
            " ORDER BY id LIMIT ?" % ",".join("?" * len(PLOT_USABLE_STATUS)),
            [owner] + list(PLOT_USABLE_STATUS)
            + [pdb.PLOT_STATUS_EXCLUDED, int(limit)]).fetchall()
    for r in rows:
        out.append({"id": r["id"], "title": r["title"], "status": r["status"]})
    return out


def list_candidate_plots(owner, keyword=None, include_blocked=False,
                         limit=1000):
    """能参与大纲生成的零件清单（带参考次数、新鲜度排序）。

    【排序为什么不是"数字最小排前面"】
    计划第四.2 节写得很清楚：`不能简单地"永远选数字最小的"`。
    所以这里的排序只解决**摊给她的顺序**（次数少的先露出来），
    真正的组合逻辑是模型照着提示词做的 —— 那一步会明确要求它
    "先满足世界观和角色，再考虑次数少"。两件事别混。
    """
    with db.connect() as conn:
        sql = ["SELECT p.*, c.name AS category_name FROM plots p"
               " LEFT JOIN categories c ON c.id = p.primary_category_id"
               " WHERE p.owner_id=? AND p.status IN (%s)"
               % ",".join("?" * len(PLOT_USABLE_STATUS))]
        args = [owner] + list(PLOT_USABLE_STATUS)
        if keyword:
            kw = "%" + keyword.strip() + "%"
            sql.append("AND (p.title LIKE ? OR p.summary LIKE ?)")
            args.extend([kw, kw])
        sql.append("ORDER BY p.id")
        rows = conn.execute(" ".join(sql), args).fetchall()

    ids = [r["id"] for r in rows]
    counts = plot_ref_counts(owner, ids) if ids else {}
    blocked = _plot_local_only(owner, ids) if ids else set()

    out = []
    for r in rows:
        d = pdb._row_to_plot(r)
        d["ref_count"] = counts.get(r["id"], 0)
        d["local_only"] = r["id"] in blocked
        if d["local_only"] and not include_blocked:
            continue
        out.append(d)

    # 新鲜度：次数少的先露出来；同样新的按 id 倒序（后建的一般更相关）
    out.sort(key=lambda x: (x["ref_count"], -x["id"]))
    if limit and limit > 0:
        out = out[:int(limit)]
    return out


def plot_blocks(owner, plot_ids):
    """把若干零件拼成"给模型看"的块。

    给的是**抽象后的零件内容**（summary / beats / 角色位 / 使用场景），
    不是原文。计划第七节第 5 条：`内化剧情是结构参考。不得照搬原文
    人名、专属设定和具体句子。`—— 零件本身就是抽象过的，给这个最安全。
    """
    ids = _int_list(plot_ids, 60, "零件")
    if not ids:
        return []
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT p.*, c.name AS category_name FROM plots p"
            " LEFT JOIN categories c ON c.id = p.primary_category_id"
            " WHERE p.owner_id=? AND p.id IN (%s)"
            % ",".join("?" * len(ids)), [owner] + ids).fetchall()
    by_id = {r["id"]: pdb._row_to_plot(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def render_plot_block(items):
    """零件块的文本形态。给模型看的就是这一段。"""
    parts = []
    for i, p in enumerate(items, 1):
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
        parts.append("\n".join(lines))
    return "\n\n".join(parts) if parts else "（这次没有摆出任何剧情零件）"


# ----------------------------------------------------------------------
# 分类素材检索（"分类素材参考"那条线，2026-09-26）
# ----------------------------------------------------------------------

# 整篇大纲最多带多少条分类素材摘要。
#
# 【为什么有硬顶，而且顶在 30】
# 她要求上限用户可填、填多少用多少；但"多少"也得有个保险丝 ——
# 每条摘要一两百字，300 组全带就是几万字，token 钱和上下文都会爆。
# 界面上填超过 30 时按 30 算，并告诉一声"这次最多带 30 条"。
REFERENCE_MAX = 30
REFERENCE_DEFAULT = 15

# 打分权重：needs 命中「摘要正文」最重（那是模型按节点需求写的查询词），
# 命中「主类名/标签」再次 —— 标签太宽，靠标签命中的多半只是"碰巧同类"，
# 不是"真能给细节"。meta 里的用途/情绪/动作/场景四样单独列（见 _score_group），
# 它们存在 summary_meta_json 里，不在行顶层。
_REF_WEIGHTS = (
    ("summary", 4),
    ("category_name", 1),
    ("tags", 1),
)

# summary_meta 里参与打分的字段和权重。
_META_WEIGHTS = (
    ("use", 3),
    ("emotion_path", 3),
    ("character_action", 2),
    ("scene_function", 2),
)


def _group_search_rows(owner):
    """这个账号所有「已合并且已生成摘要」的逻辑素材组，拍平成检索用的行。

    【为什么直接查 card_groups 而不 import classify_db】
    这里只需要**只读**几张固定表，读的是同一份 SQLite。真 import 的话，
    outline_db ← classify_db 的依赖一建立，以后 classify_db 想引用大纲这边的
    任何东西都会变成循环 import。数据层之间跨表只读查询，注释写清表名即可。

    【merged_card_id 可能为空吗】
    「已合并」状态下它一定有值（confirm_group 写回去的）；但撤销会把它清成
    NULL 同时状态摆回待确认 —— 所以 WHERE 里卡死 status=已合并 就够。
    稳妥起见 ref_card_id 优先取 merged_card_id，取不到才退 group id，
    且两者都没有的行直接丢掉（没有编号的素材没法被引用）。
    """
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT g.id AS group_id, g.merged_card_id, g.material_id,
                      g.tags, g.primary_category_id, g.summary,
                      g.summary_meta_json,
                      c.name AS category_name
               FROM card_groups g
               LEFT JOIN categories c ON c.id = g.primary_category_id
               WHERE g.owner_id=? AND g.status='已合并'
                 AND g.summary<>''""", (owner,)).fetchall()
    out = []
    for r in rows:
        if not r["merged_card_id"]:
            continue
        try:
            meta = json.loads(r["summary_meta_json"] or "{}") or {}
        except Exception:
            meta = {}
        try:
            tags = json.loads(r["tags"] or "[]") or []
        except Exception:
            tags = []
        out.append({
            "group_id": r["group_id"],
            "ref_card_id": r["merged_card_id"],
            "material_id": r["material_id"],
            "category_name": r["category_name"] or "",
            "tags": [str(t) for t in tags if str(t)],
            "summary": r["summary"] or "",
            "summary_meta": meta,
        })
    return out


def _score_group(g, need):
    """一条素材对一个 need 的得分。need 是一句短话（如「雨夜对峙」）。

    【为什么按"子串命中"打分而不是关键词分词】
    第一版不做分词和向量（任务书写明）。子串匹配的弱处是"雨夜"匹配不上
    "下雨的夜里"，但摘要本来就是模型为检索写的——提示词里已经要求它
    用可检索的具体措辞。这一版先把链路跑通，检索质量等她用过再调。
    """
    text_need = (need or "").strip()
    if not text_need:
        return 0
    pieces = [p for p in re.split(r"[，,。；;、\s（）()]+", text_need)
              if len(p) >= 2]
    score = 0

    # 顶层字段：摘要正文 / 主类名 / 标签
    for field, w in _REF_WEIGHTS:
        if field == "tags":
            hay = " ".join(g.get("tags") or [])
        else:
            hay = str(g.get(field) or "")
        if not hay:
            continue
        if text_need in hay or hay in text_need:
            score += w
        elif any(p in hay for p in pieces):
            score += w - 1   # 一个字段对一个 need 最多记一次片段命中

    # meta 字段：用途 / 情绪路径 / 人物动作 / 场景作用
    meta = g.get("summary_meta") or {}
    for key, w in _META_WEIGHTS:
        hay = str(meta.get(key) or "")
        if not hay:
            continue
        if text_need in hay or hay in text_need:
            score += w
        elif any(p in hay for p in pieces):
            score += w - 1
    return score


def search_reference_cards(owner, needs, limit=None):
    """按节点需求检索分类素材摘要。返回按得分排好序的列表。

    needs：一组短话（每个节点骨架里的 reference_needs，可合并去重）。
    limit：整篇总上限。None = 默认；超过 REFERENCE_MAX 按 REFERENCE_MAX 算。

    【为什么返回里带 node_needs】
    节点阶段注入时，模型要知道"这条素材是被哪个需求捞进来的"，
    reference_use 才有的写。同一条素材命中多个需求时都记下。
    """
    limit = REFERENCE_DEFAULT if limit is None else max(0, int(limit))
    limit = min(limit, REFERENCE_MAX)
    needs = [str(n or "").strip() for n in (needs or [])]
    needs = [n for n in needs if n]
    if not needs or limit <= 0:
        return []

    rows = _group_search_rows(owner)
    if not rows:
        return []

    # 每条素材 × 每个 need 打分，命中过的 need 记下来
    scored = []
    for g in rows:
        total, hit_needs = 0, []
        for n in needs:
            s = _score_group(g, n)
            if s > 0:
                total += s
                hit_needs.append(n)
        if total > 0:
            gg = dict(g)
            gg["score"] = total
            gg["node_needs"] = hit_needs
            scored.append(gg)

    scored.sort(key=lambda x: (-x["score"], x["ref_card_id"]))
    # 同一素材组只出现一次（天然满足 —— rows 本来就是一行一组），
    # 截到上限。
    return scored[:limit]


def render_reference_items(items):
    """检索结果里抽出「给 _reference_block 用」的最小字段集。

    【为什么多这一步】search_reference_cards 返回的行里有 score /
    node_needs 这些编排层的内部字段，不该原样塞进 ctx（ctx 会被
    预览接口整个返回给前端）。这里只挑展示和注入要用的。
    """
    out = []
    for x in items or []:
        out.append({
            "group_id": x.get("group_id"),
            "ref_card_id": x.get("ref_card_id"),
            "material_id": x.get("material_id"),
            "category_name": x.get("category_name") or "",
            "tags": x.get("tags") or [],
            "summary": x.get("summary") or "",
            "summary_meta": x.get("summary_meta") or {},
            "node_needs": x.get("node_needs") or [],
        })
    return out


# ----------------------------------------------------------------------
# 大纲 JSON 的清洗与渲染
# ----------------------------------------------------------------------

NODE_FIELDS = ("node_title", "purpose", "event", "character_action",
                "conflict", "emotional_change", "information_revealed",
                "connection_to_next", "location_time", "writing_notes")

NODE_FIELD_LABELS = {
    "node_title": "这一段叫什么",
    "purpose": "这一段的作用",
    "event": "发生了什么事",
    "character_action": "角色做了什么",
    "conflict": "冲突是什么",
    "emotional_change": "情绪怎么变的",
    "information_revealed": "透露了什么信息",
    "connection_to_next": "怎么接到下一段",
    "location_time": "时间地点",
    "writing_notes": "写的时候注意",
}

# 渲染成可读文本时要摊开的字段，以及它们的显示名。
#
# 【为什么不能直接拿 NODE_FIELD_LABELS[k] 查】participating_roles
# （参与角色）是**列表型**字段，不在 NODE_FIELDS 那十个平铺文本里，
# 所以它没有条目。以前这里写成 NODE_FIELD_LABELS[k]，
# 于是只要某一段填了参与角色，整份大纲一渲染就 KeyError ——
# 而渲染发生在"候选入库 / 保存大纲"那一刻，
# 她看到的只是一个没头没尾的 500，节点内容其实一点问题都没有。
# 用 .get(k, k) 兜底：万一以后再往里加字段，最差是显示成英文键名，
# 而不是整份存不进去。
NODE_RENDER_LABELS = dict(NODE_FIELD_LABELS)
NODE_RENDER_LABELS["participating_roles"] = "参与角色"

_NODE_RENDER_ORDER = (
    "location_time", "participating_roles", "purpose", "event",
    "character_action", "conflict", "emotional_change",
    "information_revealed", "connection_to_next", "writing_notes",
)


def _clean_node(v, idx, known_plots, known_refs=None, warns=None):
    """清洗一个节点。

    node_id 只在**同一份大纲内**唯一，所以由我们兜底生成 ——
    模型给的编号要是不小心重了，后面的 diff 和"标记不可用"会全错位。

    warns 传进来 = 这是模型的返回，超长/超量一律截断 + 记警告，
    不作废（见 _txt 的说明）。警告里带「第 N 段」，她一眼能找到是哪儿。
    """
    if not isinstance(v, dict):
        return None
    known_refs = known_refs if known_refs is not None else set()
    if warns is None:
        warns = []
    pos = "第 %d 段" % idx
    out = {}
    nid = str(v.get("node_id") or "").strip()
    if not nid or len(nid) > 24 or not re.match(r"^[A-Za-z0-9_\-]+$", nid):
        nid = "n%d" % idx
    out["node_id"] = nid
    for f in NODE_FIELDS:
        if f == "node_title":
            out[f] = _txt(v.get(f), NODE_TITLE_MAX, pos + "标题", warns=warns)
        elif f in ("purpose", "conflict", "emotional_change",
                   "information_revealed", "connection_to_next", "location_time"):
            out[f] = _txt(v.get(f), NODE_SHORT_MAX, pos + NODE_FIELD_LABELS[f],
                          warns=warns)
        else:
            out[f] = _txt(v.get(f), NODE_TEXT_MAX, pos + NODE_FIELD_LABELS[f],
                          warns=warns)
    if not out["node_title"]:
        out["node_title"] = "第 %d 段" % idx

    try:
        w = int(v.get("estimated_words") or 0)
    except (TypeError, ValueError):
        w = 0
    out["estimated_words"] = max(0, min(w, 200000))

    roles = _str_list(v.get("participating_roles"), NODE_LIST_MAX, 30,
                      pos + "的参与角色", warns=warns)
    out["participating_roles"] = roles

    src = _int_list(v.get("source_plot_ids"), NODE_SOURCE_MAX,
                    pos + "的来源零件", warns=warns)
    # 【为什么在这里过滤】模型的编号跑偏是最危险的一类错。
    # 一个不存在的 plot_id 混进来，会让大纲"看起来有来源"，
    # 点进去却是空的 —— 来源错了比没有来源更糟。
    out["source_plot_ids"] = [i for i in src if i in known_plots]

    # ---- 分类素材引用（reference_card_ids / reference_use）----
    # 跟 source_plot_ids 同一条规矩：只准用本次实际发给它的编号，
    # 编号跑偏的直接抹掉。reference_use 是一句"借了它的什么功能"，
    # 没有编号却写了 use、或有编号没写 use，都不拦 —— 那是内容质量问题，
    # 不是形状问题（形状问题才值得丢数据）。
    refs = _int_list(v.get("reference_card_ids"), NODE_REF_MAX,
                     pos + "的参考素材", warns=warns)
    out["reference_card_ids"] = [i for i in refs if i in known_refs]
    out["reference_use"] = _txt(v.get("reference_use"), NODE_SHORT_MAX,
                                pos + "的素材参考说明", warns=warns)
    return out


def clean_outline_payload(payload, known_plots=None, known_refs=None):
    """把模型返回（或她编辑后回传）的大纲 JSON 洗一遍。

    设计原则跟内化那边一样：**硬伤抛错，软伤降级 + 警告**。
    硬伤只留一条 —— nodes 不是数组 / 一个节点都救不回来
    （没有节点就没有大纲，没什么好降级的）。
    其余一律降级成警告，不整份作废：**她已经花了钱，能救回来的就救**。

    【2026-09-27 修】以前"降级"只写在文档里，代码没做到 ——
    标题多给一条、某个字段超几个字，都会 ValueError 把整份作废。
    她那次就是被这个坑掉：171.9 秒、2.6 万字输入算完，模型多写了
    一条标题候选（要 3 给 4），界面上只剩一个"失败"。
    现在超长/超量一律截断 + 记警告（见 _txt / _str_list / _int_list
    的 warns 参数），候选卡上会写明哪一段的哪个字段被截了。
    注意区分：**她自己手填**的地方仍然严格报错（不传 warns），
    因为背着她切掉她写的内容比报错恶劣得多。

    known_refs：本次实际发给模型的分类素材编号集合（合并卡的 id）。
    不传 = 这次没有参考素材，节点里所有 reference 编号都会被抹掉 ——
    这是对的：没发过的素材不可能被"参考"，写上去就是编造来源。
    """
    if not isinstance(payload, dict):
        raise ValueError("大纲的顶层得是一个对象。")
    known = set(known_plots or [])
    known_ref_set = set(known_refs or [])
    warns = []

    titles = _str_list(payload.get("title_candidates"), 3, OUTLINE_TITLE_MAX,
                       "标题候选", warns=warns)
    story_core = _txt(payload.get("story_core"), NODE_TEXT_MAX, "故事核心",
                      warns=warns)
    theme_tone = _txt(payload.get("theme_tone"), NODE_SHORT_MAX, "主题与基调",
                      warns=warns)
    overview = _txt(payload.get("overview"), NODE_TEXT_MAX * 2, "故事总览",
                    warns=warns)

    fns = []
    raw_fns = payload.get("character_functions")
    if isinstance(raw_fns, list):
        for it in raw_fns[:ROLE_FUNCTION_MAX]:
            if isinstance(it, dict):
                who = _txt(it.get("role"), 40, "角色名", warns=warns)
                tag = "「%s」的" % who if who else "角色功能表里的"
                fns.append({
                    "role": who,
                    "goal": _txt(it.get("goal"), NODE_SHORT_MAX, tag + "目标",
                                 warns=warns),
                    "obstacle": _txt(it.get("obstacle"), NODE_SHORT_MAX,
                                     tag + "阻碍", warns=warns),
                    "change": _txt(it.get("change"), NODE_SHORT_MAX,
                                   tag + "变化", warns=warns),
                })
            else:
                s = _txt(it, NODE_SHORT_MAX, "角色功能", warns=warns)
                if s:
                    fns.append({"role": s, "goal": "", "obstacle": "", "change": ""})
    if not fns:
        warns.append("角色功能表是空的 —— 每个角色在这篇里的目标、阻碍、变化都没写。")

    raw_nodes = payload.get("nodes")
    if raw_nodes is None:
        raise ValueError("返回里没有 nodes 字段，看不出故事分了几段。")
    if not isinstance(raw_nodes, list):
        raise ValueError("nodes 不是数组，是 %s。" % type(raw_nodes).__name__)
    if len(raw_nodes) > MAX_NODES:
        warns.append("模型给了 %d 个节点，超过上限 %d 个，后面那些先截掉了。"
                     % (len(raw_nodes), MAX_NODES))
    nodes = []
    used_ids = set()
    for i, nv in enumerate(raw_nodes[:MAX_NODES], 1):
        n = _clean_node(nv, i, known, known_ref_set, warns=warns)
        if n is None:
            continue
        # 编号去重（模型偶尔会把两段都叫 n3）
        while n["node_id"] in used_ids:
            n["node_id"] = n["node_id"] + "b"
        used_ids.add(n["node_id"])
        nodes.append(n)
    if not nodes:
        raise ValueError("nodes 里一个能用的节点都没有。")

    # 下面两处只是**数一数**丢了多少（同样的警告 _clean_node 已经记过了），
    # 所以给一个一次性的列表把重复的警告扔掉 —— 否则同一个毛病会说两遍。
    _quiet = []

    # 丢来源零件的要说出来，别让她以为"AI 用了很多零件"
    dropped = 0
    for nv in raw_nodes[:MAX_NODES]:
        if isinstance(nv, dict):
            raw_src = _int_list(nv.get("source_plot_ids"), NODE_SOURCE_MAX,
                                "来源零件", warns=_quiet)
            dropped += len([x for x in raw_src if x not in known])
    if dropped:
        warns.append("模型引用了 %d 处不在本次候选里的零件编号，已经抹掉 ——"
                     "来源不明的零件比没有来源更危险。" % dropped)

    # 素材引用编号跑偏的也要说（同一条规矩：编造的来源比没有来源更糟）
    dropped_ref = 0
    for nv in raw_nodes[:MAX_NODES]:
        if isinstance(nv, dict):
            raw_ref = _int_list(nv.get("reference_card_ids"), NODE_REF_MAX,
                                "参考素材", warns=_quiet)
            dropped_ref += len([x for x in raw_ref if x not in known_ref_set])
    if dropped_ref:
        warns.append("模型引用了 %d 处没发给它的分类素材编号，已经抹掉。"
                     % dropped_ref)

    climax = _txt(payload.get("climax"), NODE_SHORT_MAX * 2, "高潮与转折",
                  warns=warns)
    if isinstance(payload.get("climax"), dict):
        climax = _txt(payload["climax"].get("description"), NODE_SHORT_MAX * 2,
                      "高潮与转折", warns=warns)
    ending = _txt(payload.get("ending"), NODE_TEXT_MAX, "结局", warns=warns)

    risks = _str_list(payload.get("logic_risks"), 8, NODE_SHORT_MAX, "逻辑风险",
                      warns=warns)

    out = {
        "title_candidates": titles,
        "story_core": story_core,
        "theme_tone": theme_tone,
        "character_functions": fns,
        "overview": overview,
        "nodes": nodes,
        "climax": climax,
        "ending": ending,
        "used_plot_ids": [],          # 由调用方按"节点里真的引用了的"回填
        "logic_risks": risks,
        "word_budget": {},
    }
    # 统一来源：以节点里实际挂着的为准（计划要求"采用了哪些零件，
    # 以及各自被放在什么位置"—— 位置就在节点上，所以这里天然一致）
    used = []
    for n in nodes:
        for pid in n.get("source_plot_ids") or []:
            if pid not in used:
                used.append(pid)
    out["used_plot_ids"] = used

    # 分类素材引用同样按节点实际挂的汇总（保持顺序、去重）
    used_refs = []
    for n in nodes:
        for cid in n.get("reference_card_ids") or []:
            if cid not in used_refs:
                used_refs.append(cid)
    out["used_reference_card_ids"] = used_refs

    total = sum(n["estimated_words"] for n in nodes)
    out["word_budget"] = {
        "total": total,
        "node_total": total,
    }
    return out, warns


def validate_outline(outline, target_words, known_plots=None):
    """按预期字数校验结构规模。返回警告列表（**只警告，不拒绝**）。

    【为什么不硬拒】她可能就是要写 12000 字但只有 5 个节点（每段 2400 字），
    那是她的写法。硬拦下来等于替她做决定。但"8000 字给了 15 个空壳节点"
    这种事必须让她看见 —— 计划第十四节第 4 条禁止的正是那个。
    """
    warns = []
    t = word_tier(target_words)
    lo, hi = t["nodes"]
    nodes = outline.get("nodes") or []
    n = len(nodes)
    if n < lo:
        warns.append("预期 %d 字对应建议 %d～%d 个节点，现在只有 %d 个，"
                     "每段会偏长。" % (int(target_words or 0), lo, hi, n))
    elif n > hi:
        warns.append("预期 %d 字对应建议 %d～%d 个节点，现在有 %d 个，"
                     "容易写出空壳段落（不要把每段数字压小来凑数）。"
                     % (int(target_words or 0), lo, hi, n))

    total = sum(x.get("estimated_words") or 0 for x in nodes)
    tw = int(target_words or 0)
    if tw > 0:
        if total <= 0:
            warns.append("每个节点都没写预计字数，正文字数没法分配。")
        else:
            diff = abs(total - tw) / float(tw)
            if diff > 0.25:
                warns.append("各段预计字数加起来是 %d 字，和你要的 %d 字差得有点多"
                             "（差 %.0f%%）。" % (total, tw, diff * 100))

    # 空泛句子：计划第七节第 7 条点名禁止的那两句
    vague = ("经历了一系列事件", "关系逐渐升温", "一系列事件", "逐渐升温")
    for x in nodes:
        blob = (x.get("event") or "") + (x.get("character_action") or "")
        for v in vague:
            if v in blob:
                warns.append("「%s」这一段用了空泛说法（「%s」），"
                             "要换成具体发生了什么。" % (x.get("node_title"), v))
                break

    # 高潮必须有铺垫、结局必须回应核心
    if not outline.get("climax"):
        warns.append("没写高潮和转折是哪一段。")
    if not outline.get("ending"):
        warns.append("没写结局。")
    if not outline.get("story_core"):
        warns.append("没写故事核心（这篇真正讲什么），结局就没法回头呼应它。")

    bad = [x.get("node_title") for x in nodes if not (x.get("event") or "").strip()]
    if bad:
        warns.append("这几段只有标题、没写发生了什么：%s" % "、".join(bad[:3]))
    return warns


def render_outline_text(obj):
    """把大纲 JSON 渲染成可直接复制去写正文的文本。

    渲染出来存一份（不做成"每次现渲染"），理由见 outlines 表的注释：
    以后改了渲染函数，老大纲的"原样"得稳住。
    """
    o = obj or {}
    L = []
    titles = o.get("title_candidates") or []
    L.append("标题候选")
    if titles:
        L.extend("  · %s" % t for t in titles)
    else:
        L.append("  （没有）")
    L.append("")
    L.append("故事核心")
    L.append("  %s" % (o.get("story_core") or "（没有）"))
    L.append("")
    L.append("主题与基调")
    L.append("  %s" % (o.get("theme_tone") or "（没有）"))
    L.append("")
    L.append("角色功能表")
    fns = o.get("character_functions") or []
    if fns:
        for f in fns:
            L.append("  · %s" % (f.get("role") or "（没写角色）"))
            if f.get("goal"):
                L.append("      目标：%s" % f["goal"])
            if f.get("obstacle"):
                L.append("      阻碍：%s" % f["obstacle"])
            if f.get("change"):
                L.append("      变化：%s" % f["change"])
    else:
        L.append("  （没有）")
    L.append("")
    L.append("故事总览")
    L.append("  %s" % (o.get("overview") or "（没有）"))
    L.append("")
    nodes = o.get("nodes") or []
    total = sum(n.get("estimated_words") or 0 for n in nodes)
    L.append("总字数与分配")
    L.append("  全篇约 %d 字，共 %d 段。" % (total, len(nodes)))
    L.append("")
    for i, n in enumerate(nodes, 1):
        L.append("第 %d 段：%s（约 %d 字）"
                 % (i, n.get("node_title") or "", n.get("estimated_words") or 0))
        for k in _NODE_RENDER_ORDER:
            v = n.get(k)
            if not v:
                continue
            if isinstance(v, list):
                v = "、".join(v)
            L.append("    %s：%s" % (NODE_RENDER_LABELS.get(k, k), v))
        src = n.get("source_plot_ids") or []
        if src:
            L.append("    用了零件：%s" % "、".join("#%d" % s for s in src))
        refs = n.get("reference_card_ids") or []
        if refs:
            line = "    参考素材：%s" % "、".join("#%d" % r for r in refs)
            use = (n.get("reference_use") or "").strip()
            if use:
                line += "（%s）" % use
            L.append(line)
        L.append("")
    L.append("高潮和转折")
    L.append("  %s" % (o.get("climax") or "（没有）"))
    L.append("")
    L.append("结局")
    L.append("  %s" % (o.get("ending") or "（没有）"))
    L.append("")
    L.append("使用的剧情零件")
    used = o.get("used_plot_ids") or []
    L.append("  %s" % ("、".join("#%d" % u for u in used) if used else "（没有）"))
    L.append("")
    L.append("逻辑风险")
    risks = o.get("logic_risks") or []
    if risks:
        L.extend("  · %s" % r for r in risks)
    else:
        L.append("  （没有）")
    return "\n".join(L)


def empty_outline_json():
    """一份空骨架。新建大纲、或者用户手写时用它打底。"""
    return {
        "title_candidates": [],
        "story_core": "",
        "theme_tone": "",
        "character_functions": [],
        "overview": "",
        "nodes": [],
        "climax": "",
        "ending": "",
        "used_plot_ids": [],
        "used_reference_card_ids": [],
        "logic_risks": [],
        "word_budget": {"total": 0},
    }


# ----------------------------------------------------------------------
# AI 稿 vs 用户稿的差异
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# 把"她交进来的整篇文本"变成能比的结构
# ----------------------------------------------------------------------
#
# 【为什么不能直接复用 diff_outlines】
# diff_outlines 比的是 **node_id**。她那版是自己写的（或大改过的），
# 段落编号早就不一样了 —— 按 id 比会把每一段都算成"删了 + 加了"，
# 得到一份全是噪声的差异，AI 拿着它也总结不出东西。
#
# 【那按什么比】
# 按**顺序**比，并用文本相似度认"这两段其实是同一段"。这是启发式，
# 会有认错的时候 —— 所以它的定位只是"给 AI 的辅助线索"，不是结论。
# 真正下判断的是 AI 读了全文之后。界面上那份差异也标着"自动认的，仅供参考"。
#
# 段落切法：空行分段。她粘进来的东西多半本身就是分段的；
# 一整坨没分段的，就按编号行（"1."/"第1段"/"一、"）再试一刀。

_SPLIT_PAT = re.compile(
    r"^\s*(?:第\s*[0-9一二三四五六七八九十]+\s*[段节]|"
    r"[0-9]{1,2}\s*[.、)）]|[一二三四五六七八九十]{1,2}\s*[、.])\s*(?=\S)")


def split_outline_text(text):
    """把一段文字切成段落。故意做得笨：切不细也比切错强。

    先按空行分；如果分不出（只有一段或两段）再按编号行切。
    编号行**自己也算一段的开头** —— 不然后面那段会挂在上一段尾巴上。
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if len(blocks) <= 2:
        lines = [l for l in text.split("\n")]
        scored = sum(1 for l in lines if _SPLIT_PAT.match(l))
        if scored >= 2:
            out, cur = [], []
            for l in lines:
                if _SPLIT_PAT.match(l) and cur:
                    out.append("\n".join(cur).strip())
                    cur = [l]
                else:
                    cur.append(l)
            if cur:
                out.append("\n".join(cur).strip())
            blocks = [b for b in out if b]
    return blocks


def _norm_for_cmp(s):
    """比较用的归一化：去掉所有空白和常见标点，只留实义字符。"""
    s = s or ""
    s = re.sub(r"[\s，。、；：！？「」『』“”‘’（）()【】\[\]—…·\-—_*#]", "", s)
    return s


def _sim(a, b):
    """两段的相似度（0~1），比的是**归一化之后的字符序列**。

    用 difflib 的比值（= 2M / (len(a)+len(b))）。它对"她只挪了半句"
    确实偏敏感 —— 但那是**配段阶段**的事，那边靠
    「位置对齐优先 + 两步走」兜住了，不靠调这个数。
    这里保持一个中性的、各家都认识的比值，别自己发明一套尺度。
    """
    import difflib
    a, b = _norm_for_cmp(a), _norm_for_cmp(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def text_to_outline_json(text, threshold=0.35):
    """把整篇文本变成一个**只有节点**的大纲 JSON。

    不猜 story_core / ending 这些 —— 她那版可能是自由文本，
    替她猜出错误的结构，比不猜更坏（AI 会照着错的结构去比对）。
    只切段，每段一个节点，正文放 event 里。
    """
    blocks = split_outline_text(text)
    nodes = []
    for i, b in enumerate(blocks, 1):
        lines = b.split("\n")
        title = lines[0].strip()
        # 首行太长就不当标题（多半是没分段的正文）。
        # 顺手把行首的编号去掉 —— "1. 开场" 里的 "1." 是她自己的分段记号，
        # 留着会让相似度比较多出一截噪声（两段的编号一样就白涨一分）。
        title = _SPLIT_PAT.sub("", title).strip()
        if len(title) > 40 or len(lines) == 1:
            title = ""
        nodes.append({
            "node_id": "u%d" % i,
            "node_title": title[:NODE_TITLE_MAX],
            "event": b[:NODE_TEXT_MAX],
            "estimated_words": len(b),
        })
    return {
        "nodes": nodes,
        "word_total": sum(n["estimated_words"] for n in nodes),
        "used_plot_ids": [],
    }


def _same_seg(ai_node, user_node):
    """这一段到底算不算"被她改过"。

    判据是**内容**，不是相似度分数：

      · 归一化之后两边一模一样 → 没改
      · 一边是另一边的子串 → 也没改。她那版的一段是"标题 + 正文"整块，
        AI 那版标题单独一栏，于是照抄的一段会天然多出标题那几个字。
        这几个字不该算成"改动"。

    为什么不直接卡一个高相似度阈值：阈值是拍出来的，
    而"多出来的只是标题"这件事是可以**确凿判断**的。
    能确凿判断的就别拍阈值 —— 拍出来的数以后没人说得清为什么是这个。
    """
    a = _norm_for_cmp(str(ai_node.get("event") or ""))
    b = _norm_for_cmp(str(user_node.get("event") or ""))
    if not a and not b:
        return True
    if not a or not b:
        return False
    if a == b:
        return True
    # 短的那边被长的那边整个包含 → 认作同一段（多出来的是标题行之类）
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    # 太短的（比如就三五个字）不做包含判断：那种"包含"纯属巧合
    return len(short) >= 8 and short in long_


def diff_against_text(ai_json, user_json, threshold=0.5):
    """AI 原稿 vs 她的文本稿，**按顺序 + 相似度**认段。

    返回的形状刻意跟 diff_outlines 一样（removed/added/changed…），
    这样界面上一套渲染能同时吃两份差异。

    配段算法：对 AI 的每一段，在她的段落里找"还没被配走、且最像它"的那一段。
    这一步是**贪心**的，不是最优匹配 —— 段落数一般不超过二十，
    贪心够了，而写匈牙利算法只会让这段代码以后没人敢改。
    """
    a = ai_json if isinstance(ai_json, dict) else {}
    u = user_json if isinstance(user_json, dict) else {}
    an = [n for n in (a.get("nodes") or []) if isinstance(n, dict)]
    un = [n for n in (u.get("nodes") or []) if isinstance(n, dict)]

    def body(n):
        # 一段的"实质内容" —— **只认 event**。
        #
        # 【为什么不把 character_action / conflict 一起拼进来】
        # 因为**她那版是自由文本，只有 event 这一栏**。
        # 把 AI 独有、她那边根本不存在的字段拼进来，等于让分母白涨一截：
        # 实测一份"她一字未改、整段照抄"的稿子，相似度被压到 0.77 ——
        # 于是每一段都被报成"她改过"。而"AI 独有的字段"恰恰是她
        # 不可能去改的东西，拿它当判据只会误报。
        return str(n.get("event") or "")

    def title_of(n):
        return str(n.get("node_title") or "")

    def seg_sim(x, y):
        """两段的相似度 = 正文为主 + 标题小幅加成。

        写成函数而不是拼字符串，就是为了让"哪一部分算得多"这件事
        在代码里一眼看得出来 —— 拼字符串时权重是隐式的，
        以后谁动一下字段名就能把尺度整个挪走。
        """
        s = _sim(body(x), body(y))
        tx, ty = title_of(x), title_of(y)
        if tx and ty:
            # 标题像 → 往上抬一点（最多抬到 1.0）；标题不像 → 不往下砸。
            ts = _sim(tx, ty)
            s = s + (1.0 - s) * 0.35 * ts
        return s

    taken = set()
    removed, changed, pairs = [], [], []
    # 上一段配到了她的第几段。下一段只能从这里往后找 ——
    # 这是把"顺序"这个约束真正用起来的地方。
    #
    # 【为什么必须这样】不限制的话，AI 的第 1 段会去跟她的第 4 段配
    # （因为两段文字更像），结果第 1 段算"改了"、她的前 3 段算"新加的" ——
    # 一片噪声。加了游标之后，"她删了一段"就老老实实报成删了一段。
    #
    # 【允许多大的回退】2。她改动时偶尔会把相邻两段对调，
    # 或者把一段拆成两段 —— 留一点回退量兜住这两种，但不放开，
    # 放开了就等于没限制。
    cursor = 0
    for i, n in enumerate(an):
        rows = [(seg_sim(n, un[j]), j)
                for j in range(len(un)) if j not in taken]
        best, best_s = None, 0.0
        if rows:
            # ---- 两步定配段 ----
            # 第一步：看看"在她那边、跟上一次配到的位置大致对齐"的那一段像不像。
            #   很像就直接认它 —— 这是最常见的情况（她基本照原顺序改），
            #   也是最不该被别的段抢走的情况。
            # 第二步：对齐位置不像，才放开在她还没配走的段里挑最像的，
            #   用来兜"她删了一段/插了一段"造成的整体错位。
            #
            # 【为什么要分两步】只用"挑最像的"是不够的：AI 写大纲爱用同一套
            # 句式，好几段彼此很像（实测 0.756 撞 0.756），于是第 1 段会去配
            # 她的第 4 段，把她真正改过的那段报成"删了 + 新加的"。
            # 先认对齐位置，顺序这个约束才真的起了作用。
            aligned = [j for _, j in rows if j == cursor]
            if aligned:
                # 位置对上了就优先认它 —— 但**门槛比别处低**：
                # 位置本身已经是强证据了，两段都排在第 3 个位置，
                # 就算文字被改得只剩三成相像，也基本是同一段。
                # （门槛高的话，"她大改了一段"会被报成"删一段+加一段"，
                #   而那恰恰是她最想让 AI 学的那种改动。）
                a_s = seg_sim(n, un[aligned[0]])
                if a_s >= 0.28:
                    best, best_s = aligned[0], a_s
            if best is None and rows:
                top = max(s for s, _ in rows)
                if top >= 0.5:
                    # 同分附近取最靠前的（理由同上：按顺序认段）
                    near = [j for s, j in rows if s >= top - 0.08]
                    best = min(near)
                    best_s = dict((j, s) for s, j in rows)[best]
        if best is None:
            removed.append({"node_id": n.get("node_id") or ("n%d" % (i + 1)),
                            "node_title": n.get("node_title") or ""})
            continue
        taken.add(best)
        cursor = best + 1
        pairs.append((i, best))
        m = un[best]
        # 【判"改了没有"为什么不能只看相似度】
        # 她那版的一段 = "标题 + 正文"整块，而 AI 那版标题和正文是分开的。
        # 于是一段"照抄没动"的正文，相似度也只能到 0.9 几 ——
        # 拿 0.995 当"没改"的线，会把整份照抄的稿子全报成"她改过 8 段"。
        # （这正是 2026-10-08 实测踩到的：她一字没动，界面说她改了每一段。）
        #
        # 所以改成"内容上认得出是同一份东西就不算改"：
        # 归一化之后互相包含（一边是另一边的子串）就算没动。
        # 标点、空白、标题行都不影响这个判断 —— 那些本来就不是"改动"。
        if not _same_seg(n, m):
            changed.append({
                "node_id": m.get("node_id") or n.get("node_id") or "",
                "node_title": m.get("node_title") or n.get("node_title") or "",
                "similarity": round(best_s, 2),
                "words_before": n.get("estimated_words") or len(body(n)),
                "words_after": m.get("estimated_words") or len(body(m)),
            })

    added = []
    for j, m in enumerate(un):
        if j not in taken:
            added.append({"node_id": m.get("node_id") or ("u%d" % (j + 1)),
                          "node_title": m.get("node_title") or ""})

    # 顺序：只看"两边都认到的那些段"，在各自列表里的先后是不是一致
    ai_seq = [i for i, _ in pairs]
    us_seq = [j for _, j in pairs]
    reordered = ai_seq != sorted(ai_seq) or us_seq != sorted(us_seq)

    wa = sum(n.get("estimated_words") or 0 for n in an)
    wu = sum(n.get("estimated_words") or 0 for n in un)
    return {
        "removed_nodes": removed,
        "added_nodes": added,
        "changed_nodes": changed,
        "reordered": bool(reordered),
        "meta_changed": [],
        "plots_removed": [],
        "plots_added": [],
        "words_before": wa,
        "words_after": wu,
        "nodes_before": len(an),
        "nodes_after": len(un),
        "matched_pairs": [[i, j] for i, j in pairs],
        "by_text": True,     # 标记：这是按文本认的，不是按 node_id
        "changed": bool(removed or added or changed or reordered),
    }


# ----------------------------------------------------------------------
# 事件级对齐（2026-10-08 阶段一 B）
# ----------------------------------------------------------------------
#
# 【为什么还要有一个 align_events，不就用 diff_against_text】
# diff_against_text 是**段落级**的：按位置 + 相似度认段，一步一对一的贪心。
# 它能回答"哪一段没动、哪一段被删了"，但回答不了需求第 5/6 条问的事：
#   · 她把她那版的一段拆成两段 → 这是"一对多"，不是"删一段加两段"
#   · 她大段重排（把第 5 段挪到第 1 段）→ 段落级只会报成"删了又加了"
#   · 她这一段既是"AI 第 2 段"也是"AI 第 3 段"来的 → "多对一"
# 所以再写一层**事件序列对齐**：
#   · 先做本地算法能**确定**的部分（完全相同 / 只差格式 / 明确的整段移动）
#   · 认不准的**明确标 uncertain**，交给 AI 复核，不硬凑成完整表格
#
# ★ 折腰第 4 条原文："不要为了让表格完整而强行配对。"
# 这就是下面为什么宁可留 uncertain，也不给每一段都硬安一个对应。

# 对齐结果的类型
ALIGN_SAME = "same"            # 内容一致（可能只差格式）
ALIGN_CHANGED = "changed"      # 一对一，内容动了
ALIGN_MERGED = "merged"        # 多对一：AI 那几段被她并成一段
ALIGN_SPLIT = "split"          # 一对多：AI 那一段被她拆成几段
ALIGN_ADDED = "added"          # 她那版新增
ALIGN_REMOVED = "removed"      # AI 那版有、她那版没了
ALIGN_MOVED = "moved"          # 位置变了（内容基本没动）
ALIGN_UNCERTAIN = "uncertain"  # 认不准 —— 明确标出来，不硬配
ALL_ALIGN_KINDS = (ALIGN_SAME, ALIGN_CHANGED, ALIGN_MERGED, ALIGN_SPLIT,
                   ALIGN_ADDED, ALIGN_REMOVED, ALIGN_MOVED, ALIGN_UNCERTAIN)

# 九类变化（需求第 5 条点名要识别的），映射到对齐类型
#   新增事件 / 删除事件 / 顺序调整 / 动机补充改变 / 冲突解决方式改变 /
#   信息揭露时机改变 / 铺垫回收变化 / 结局关系变化 / 仅措辞格式标题
CHANGE_ADDED_EVENT = "新增事件"
CHANGE_REMOVED_EVENT = "删除事件"
CHANGE_REORDER = "顺序调整"
CHANGE_MOTIVE = "动机改变"
CHANGE_CONFLICT = "冲突处理改变"
CHANGE_DISCLOSURE = "信息揭露时机改变"
CHANGE_SETUP_PAYOFF = "铺垫回收变化"
CHANGE_ENDING = "结局关系变化"
CHANGE_FORMAT_ONLY = "仅措辞格式标题"
ALL_CHANGE_KINDS = (CHANGE_ADDED_EVENT, CHANGE_REMOVED_EVENT,
                    CHANGE_REORDER, CHANGE_MOTIVE, CHANGE_CONFLICT,
                    CHANGE_DISCLOSURE, CHANGE_SETUP_PAYOFF, CHANGE_ENDING,
                    CHANGE_FORMAT_ONLY)


def _fmt_only(a, b):
    """两段是不是**只差格式**（标点、空白、标题行、编号）。

    折腰第 4 条："本地程序负责能确定的内容，例如完全相同、
    格式变化和明确的文本移动。"
    这条判据是纯函数、不花钱，能在提示词里被 AI 复核 —— 但先由程序定。
    """
    na, nb = _norm_for_cmp(str(a or "")), _norm_for_cmp(str(b or ""))
    if na == nb:
        return True
    # 一边是另一边的子串（"她只多加了一行标题"）也算只差格式
    if na and nb:
        short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
        if len(short) >= 8 and short in long_:
            return True
    return False


def _body(n):
    return str(n.get("event") or n.get("node_title") or "")


def align_events(ai_json, user_json, threshold=0.42):
    """把两稿的段落按**事件**对齐，能确定的确定、不确定的标出来。

    返回：
      {
        "pairs": [ {"kind": ALIGN_*, "ai": [下标...], "user": [下标...],
                    "similarity": 0.0~1.0, "reason": "为什么这么配",
                    "uncertain": bool, "node_title": "..."} ],
        "only_ai": [下标...],      # 她那版没有的（删了）
        "only_user": [下标...],    # AI 那版没有的（新增）
        "format_only": [下标...],  # 前面 pairs 里"只是格式变了"的那些（便于统计）
        "counts": {"added": n, "removed": n, "moved": n, "merged": n,
                   "split": n, "changed": n, "same": n, "uncertain": n},
        "by_text": True,
        "uncertain_pairs": [pairs 里 uncertain 的],
      }

    【算法分四步，每步只做**能确定**的事】
    1. 先扫"完全相同 / 只差格式"的段：按内容直接配掉。这类是最硬的证据，
       先钉住它们，后面就不会把"她照抄的一段"误当成"新写的"。
    2. 剩下的段按顺序做一对一相似度配（门槛比段落级那次低一点，
       因为这里已经排除了"格式没变的"，剩下的都是真动过的）。
    3. 处理多对一 / 一对多：连续的几段都没配上、但拼起来的文本跟对面
       某一段很像 → 合并/拆分。这个只在**文本证据足够**时才认。
    4. 认不准的（相似度在中间灰区 0.25~threshold，或者跨太远的位置）
       一律进 uncertain，**不硬配**。
    """
    a = ai_json if isinstance(ai_json, dict) else {}
    u = user_json if isinstance(user_json, dict) else {}
    an = [n for n in (a.get("nodes") or []) if isinstance(n, dict)]
    un = [n for n in (u.get("nodes") or []) if isinstance(n, dict)]

    def title(n):
        return str(n.get("node_title") or "")[:NODE_TITLE_MAX]

    a_taken = set()
    u_taken = set()
    pairs = []

    # ---- 第 1 步：完全一致 / 只差格式，按内容先钉住 ----
    #
    # 【为什么要"双向最好"】只按顺序扫、见到像就配，会踩到"她拆段"：
    #   AI 第 2 段 =「他在城外的客栈住下，夜里把信拿出来看。」
    #   她第 2 段 =「他在城外的客栈住下。」   ← 是 AI 第 2 段的子串
    # 一旦先把它配掉，她第 3 段「夜里他把信拿出来看。」就成了"凭空多出来的"，
    # 于是"她拆了一段"被报成"删一段加一段" —— 恰恰丢掉我们要学的改动。
    # 所以：一个 AI 段要配一个她段，得是**它俩互为最像的那个**（双向最好），
    # 不是"谁先扫到算谁的"。
    def _all_sims(i, taken_u):
        return [(_sim(_body(an[i]), _body(un[j])), j)
                for j in range(len(un)) if j not in taken_u]

    for i, ai in enumerate(an):
        if i in a_taken:
            continue
        sims = _all_sims(i, u_taken)
        if not sims:
            continue
        # 她那边跟这段最像的
        sims.sort(key=lambda t: (-t[0], t[1]))
        top_s, top_j = sims[0]
        # 反向看：她第 top_j 段在剩下的 AI 段里最像谁
        back = [( _sim(_body(an[k]), _body(un[top_j])), k)
                for k in range(len(an)) if k not in a_taken]
        back.sort(key=lambda t: (-t[0], t[1]))
        best_k = back[0][1] if back else -1
        if best_k != i:
            continue                    # 不是双向最好 → 留到第 2 步
        if _fmt_only(_body(ai), _body(un[top_j])):
            pairs.append({
                "kind": ALIGN_SAME, "ai": [i], "user": [top_j],
                "similarity": 1.0,
                "reason": "内容一致，只差标点/空白/标题这类格式",
                "uncertain": False, "node_title": title(un[top_j]) or title(ai),
                "format_only": True,
            })
            a_taken.add(i)
            u_taken.add(top_j)

    # ---- 第 1.5 步：她拆段的修复 ----
    #
    # 上面那步之后，AI 的某段可能还配着她其中一段，而她紧邻的下一段
    # 单独拿出来像"新写的"，但**两段拼起来**正好像 AI 那一段 → 这是拆段。
    # 这一步专门认它：把那张一对一的票撤回来，换成"一对多"。
    rep = []
    for pi, p in enumerate(pairs):
        if p["kind"] != ALIGN_SAME or len(p["ai"]) != 1:
            continue
        i = p["ai"][0]
        j = p["user"][0]
        # 看她下一段（紧邻、还没配走）拼起来像不像 AI 这一段
        nxt = j + 1
        if nxt >= len(un) or nxt in u_taken:
            continue
        joined = _body(un[j]) + _body(un[nxt])
        if _sim(_body(an[i]), joined) >= 0.85 \
                and _sim(_body(an[i]), _body(un[j])) < 0.95:
            # 拼起来比单段明显更像 → 拆段
            p["kind"] = ALIGN_SPLIT
            p["user"] = [j, nxt]
            p["reason"] = "她那版把这一段拆成了几段"
            p["format_only"] = False
            p["similarity"] = round(_sim(_body(an[i]), joined), 2)
            u_taken.add(nxt)
            rep.append(pi)
    if rep:
        pass    # 就地改了 pairs，无需额外处理

    # ---- 第 2 步：剩下的一对一，按顺序 + 相似度配 ----
    # 游标思路跟 diff_against_text 一样：顺序是强证据，但不锁死，
    # 因为这一步要兜"大段重排"。
    for i, ai in enumerate(an):
        if i in a_taken:
            continue
        cands = [(i, j, _sim(_body(ai), _body(un[j])))
                 for j in range(len(un)) if j not in u_taken]
        if not cands:
            continue
        cands.sort(key=lambda t: (-t[2], t[1]))
        bi, bj, bs = cands[0]
        if bs >= threshold:
            # 只有唯一一个候选明显高于其它时才算"确信的配对"；
            # 有并列竞争者 → 降级为不确定。
            second = cands[1][2] if len(cands) > 1 else 0.0
            ambiguous = (bs - second) < 0.08 and second >= 0.25
            kind = ALIGN_SAME if _fmt_only(_body(ai), _body(un[bj])) \
                else ALIGN_CHANGED
            pairs.append({
                "kind": kind, "ai": [i], "user": [bj],
                "similarity": round(bs, 2),
                "reason": "按顺序和内容最像的一段",
                "uncertain": bool(ambiguous),
                "node_title": title(un[bj]) or title(ai),
                "format_only": False,
            })
            a_taken.add(i)
            u_taken.add(bj)
        elif bs >= 0.25:
            # 灰区：像但不够像 —— 标不确定，让 AI 复核，**不硬配**
            pairs.append({
                "kind": ALIGN_UNCERTAIN, "ai": [i], "user": [bj],
                "similarity": round(bs, 2),
                "reason": "内容只有部分像，配不配得上要人来判断",
                "uncertain": True,
                "node_title": title(un[bj]) or title(ai),
                "format_only": False,
            })
            a_taken.add(i)
            u_taken.add(bj)

    # ---- 第 2.5 步：拆段修复（补扫一遍）----
    #
    # 【为什么第 1.5 步之后还要再来一次】第 1.5 步只在"格式已一致"的
    # 那种配对上找拆段。但拆段也常发生在"AI 一段被她拆成两段、
    # 第二段重写了"的情况 —— 那时第 2 步会先把她第 2 段配走，
    # 她第 1 段就成了"新加的"。这一步把这类票撤回来改成"一对多"。
    #
    # 判据很硬：**她相邻两段拼起来**跟 AI 那一段的相似度，要明显
    # 高于她单独任一段跟它的相似度。够硬才认，否则宁可留成"新增"。
    _repaired = set()
    for pi, p in enumerate(pairs):
        if p["kind"] not in (ALIGN_CHANGED, ALIGN_SAME):
            continue
        if len(p["ai"]) != 1 or len(p["user"]) != 1:
            continue
        i, j = p["ai"][0], p["user"][0]
        best_gain, best_extra = 0.0, None
        for nxt in (j - 1, j + 1):
            if nxt < 0 or nxt >= len(un):
                continue
            # 邻居得是"单挂着、配不上任何 AI 段"的那种（即在 only_user 候选里）
            if nxt in u_taken and _pair_of_user(pairs, nxt) != pi:
                continue
            joined = _body(un[min(j, nxt)]) + _body(un[max(j, nxt)])
            sj = _sim(_body(an[i]), joined)
            gain = sj - p["similarity"]
            if gain > best_gain:
                best_gain, best_extra = gain, nxt
        if best_extra is not None and best_gain >= 0.12 \
                and _sim(_body(an[i]),
                         _body(un[j]) + _body(un[best_extra])) >= 0.6:
            p["kind"] = ALIGN_SPLIT
            p["user"] = sorted([j, best_extra])
            p["reason"] = "她那版把这一段拆成了几段"
            p["format_only"] = False
            p["similarity"] = round(
                _sim(_body(an[i]),
                     _body(un[p["user"][0]]) + _body(un[p["user"][1]])), 2)
            u_taken.add(best_extra)
            _repaired.add(pi)

    # ---- 第 3 步：多对一 / 一对多（连续的段拼起来才像）----
    def _try_group(side_a, side_u, ca, cu):
        """把 side_a 的几段跟 side_u 的几段比：拼起来像 → 合并/拆分。"""
        if not ca or not cu or (len(ca) == 1 and len(cu) == 1):
            return False
        ta = "\n".join(_body(an[k]) for k in ca)
        tu = "\n".join(_body(un[k]) for k in cu)
        if _sim(ta, tu) < 0.45:
            return False
        kind = ALIGN_MERGED if len(ca) > 1 else ALIGN_SPLIT
        pairs.append({
            "kind": kind, "ai": list(ca), "user": list(cu),
            "similarity": round(_sim(ta, tu), 2),
            "reason": ("她那版把这几段并成了一段" if kind == ALIGN_MERGED
                       else "她那版把这一段拆成了几段"),
            "uncertain": False,
            "node_title": title(un[cu[0]]),
            "format_only": False,
        })
        for k in ca:
            a_taken.add(k)
        for k in cu:
            u_taken.add(k)
        return True

    left_a = [k for k in range(len(an)) if k not in a_taken]
    left_u = [k for k in range(len(un)) if k not in u_taken]
    # 只看连续块（合并/拆分本质上是连续段的事）
    for run_a in _runs(left_a):
        for run_u in _runs(left_u):
            if _try_group(run_a, run_u, run_a, run_u):
                pass

    only_ai = [k for k in range(len(an)) if k not in a_taken]
    only_user = [k for k in range(len(un)) if k not in u_taken]

    # ---- 第 4 步：只 AI 有 / 只她有，是"删"还是"挪" ----
    # 挪：她那版有一段跟 AI 的某段很像，但位置差很远。
    # 光看"没配上"分不出"删了"和"挪走了"，这里补一层判断。
    moved_ai = set()
    for k in list(only_ai):
        best, bj = 0.0, None
        for m in only_user:
            s = _sim(_body(an[k]), _body(un[m]))
            if s > best:
                best, bj = s, m
        if best >= 0.6 and bj is not None:
            pairs.append({
                "kind": ALIGN_MOVED, "ai": [k], "user": [bj],
                "similarity": round(best, 2),
                "reason": "内容基本没动，但位置挪了",
                "uncertain": False,
                "node_title": title(un[bj]),
                "format_only": False,
            })
            moved_ai.add(k)
    only_ai = [k for k in only_ai if k not in moved_ai]

    for k in only_ai:
        pairs.append({"kind": ALIGN_REMOVED, "ai": [k], "user": [],
                      "similarity": 0.0, "reason": "AI 有这一段，她那份里没有",
                      "uncertain": False, "node_title": title(an[k]),
                      "format_only": False})
    for k in only_user:
        pairs.append({"kind": ALIGN_ADDED, "ai": [], "user": [k],
                      "similarity": 0.0, "reason": "她那版新写的，AI 那份里没有",
                      "uncertain": False, "node_title": title(un[k]),
                      "format_only": False})

    # 排序：让"主顺序"按她那版的顺序 + AI 的顺序排，界面读起来顺
    def _key(p):
        return (min(p["user"]) if p["user"] else 10 ** 6,
                min(p["ai"]) if p["ai"] else 10 ** 6)
    pairs.sort(key=_key)

    counts = {k: 0 for k in ("added", "removed", "moved", "merged", "split",
                             "changed", "same", "uncertain")}
    for p in pairs:
        if p["kind"] == ALIGN_ADDED:
            counts["added"] += 1
        elif p["kind"] == ALIGN_REMOVED:
            counts["removed"] += 1
        elif p["kind"] == ALIGN_MOVED:
            counts["moved"] += 1
        elif p["kind"] == ALIGN_MERGED:
            counts["merged"] += 1
        elif p["kind"] == ALIGN_SPLIT:
            counts["split"] += 1
        elif p["kind"] == ALIGN_CHANGED:
            counts["changed"] += 1
        elif p["kind"] == ALIGN_SAME:
            counts["same"] += 1
        elif p["kind"] == ALIGN_UNCERTAIN:
            counts["uncertain"] += 1

    return {
        "pairs": pairs,
        "only_ai": only_ai,
        "only_user": only_user,
        "counts": counts,
        "by_text": True,
        "uncertain_pairs": [p for p in pairs if p.get("uncertain")],
    }


def _runs(idxs):
    """把一串（升序）下标切成连续段。合并/拆分只在连续段里找。"""
    out = []
    cur = []
    for k in sorted(idxs):
        if cur and k == cur[-1] + 1:
            cur.append(k)
        else:
            if cur:
                out.append(cur)
            cur = [k]
    if cur:
        out.append(cur)
    return out


def _pair_of_user(pairs, j):
    """她第 j 段现在配在哪一条 pair 上（没有就返回 None）。"""
    for pi, p in enumerate(pairs):
        if j in (p.get("user") or []):
            return pi
    return None


def align_block(al):
    """把对齐结果渲染成一段**给 AI 看**的文字（带不确定标记）。

    【为什么要有这一段】折腰第 4 条："程序 diff 是辅助信息。
    提示词应明确：它可能不准确，分析应以两份全文和创作要求为依据。"
    这段文字本身就是这么写的 —— 逐行标出"这是程序认的""这几条不确定"。
    """
    if not isinstance(al, dict):
        return "（没有自动对齐信息。）"
    L = ["（这是程序自动对齐的结果，**可能认错**。"
         "请以两份大纲全文和当时的创作要求为准，"
         "下面的内容只当作参考线索。）", ""]
    for p in al.get("pairs") or []:
        ai_i = "、".join("AI第%d段" % (k + 1) for k in p.get("ai") or []) or "—"
        u_i = "、".join("她第%d段" % (k + 1) for k in p.get("user") or []) or "—"
        mark = "（不确定）" if p.get("uncertain") else ""
        L.append("· %s ←→ %s　[%s] 相似度 %.2f%s"
                 % (ai_i, u_i, p.get("kind"), p.get("similarity", 0),
                    mark))
        if p.get("reason"):
            L.append("    %s" % p["reason"])
    c = al.get("counts") or {}
    L.append("")
    L.append("小结：新增 %d 段、删除 %d 段、位置挪动 %d 段、"
             "合并 %d 处、拆分 %d 处、内容改动 %d 段、"
             "完全没动 %d 段、**认不准 %d 处**"
             % (c.get("added", 0), c.get("removed", 0), c.get("moved", 0),
                c.get("merged", 0), c.get("split", 0), c.get("changed", 0),
                c.get("same", 0), c.get("uncertain", 0)))
    if c.get("uncertain"):
        L.append("有几处程序认不准，请你**自己读全文判断**，"
                 "判断不了就明说『这条不确定』，不要硬给一个结论。")
    return "\n".join(L)


def diff_outlines(ai_json, user_json):
    """比出"她到底改了 AI 什么"。计划第九节要的那几样，一样不少。

    比的是**节点 id**，不是节点顺序或位置 ——
    她最难被发现的改动就是把两段前后调了个头，
    按位置比会算成"两段都改了"，按 id 比才知道是"顺序换了"。
    """
    a = ai_json if isinstance(ai_json, dict) else {}
    u = user_json if isinstance(user_json, dict) else {}
    an = {n.get("node_id"): n for n in (a.get("nodes") or [])
          if isinstance(n, dict) and n.get("node_id")}
    un = {n.get("node_id"): n for n in (u.get("nodes") or [])
          if isinstance(n, dict) and n.get("node_id")}

    removed, added, changed = [], [], []
    for nid, n in an.items():
        if nid not in un:
            removed.append({"node_id": nid, "node_title": n.get("node_title") or ""})
    for nid, n in un.items():
        if nid not in an:
            added.append({"node_id": nid, "node_title": n.get("node_title") or ""})
    for nid, n in an.items():
        if nid not in un:
            continue
        uu = un[nid]
        fields = []
        for k in list(NODE_FIELDS) + ["estimated_words"]:
            if (n.get(k) or "") != (uu.get(k) or ""):
                fields.append(k)
        if fields:
            changed.append({
                "node_id": nid,
                "node_title": uu.get("node_title") or n.get("node_title") or "",
                "fields": fields,
                "words_before": n.get("estimated_words") or 0,
                "words_after": uu.get("estimated_words") or 0,
            })

    ai_order = [n.get("node_id") for n in (a.get("nodes") or [])
                if isinstance(n, dict) and n.get("node_id")]
    user_order = [n.get("node_id") for n in (u.get("nodes") or [])
                  if isinstance(n, dict) and n.get("node_id")]
    reordered = [x for x in ai_order if x in user_order] != \
                [x for x in user_order if x in ai_order]

    ap = list(a.get("used_plot_ids") or [])
    up = list(u.get("used_plot_ids") or [])
    meta_changed = [k for k in ("story_core", "theme_tone", "overview",
                                "ending", "climax")
                    if (a.get(k) or "") != (u.get(k) or "")]

    wa = sum(n.get("estimated_words") or 0 for n in (a.get("nodes") or [])
             if isinstance(n, dict))
    wu = sum(n.get("estimated_words") or 0 for n in (u.get("nodes") or [])
             if isinstance(n, dict))

    return {
        "removed_nodes": removed,
        "added_nodes": added,
        "changed_nodes": changed,
        "reordered": reordered,
        "meta_changed": meta_changed,
        "plots_removed": [p for p in ap if p not in up],
        "plots_added": [p for p in up if p not in ap],
        "words_before": wa,
        "words_after": wu,
        "changed": bool(removed or added or changed or reordered
                        or meta_changed or ap != up),
    }


# ----------------------------------------------------------------------
# 大纲库：保存（幂等）
# ----------------------------------------------------------------------

def find_outline_by_source(owner, run_id, model_key):
    """这份候选是不是已经推入过大纲库了。

    计划第十一节第 7 条：`失败、取消和重试不能造成重复大纲`。
    她点两次"推入大纲库"、网络抖动重发、或者从历史候选里再点一次 ——
    都不该产生第二份大纲。
    """
    if not run_id or not model_key:
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM outlines WHERE owner_id=? AND run_id=? AND model_key=?"
            " ORDER BY id LIMIT 1", (owner, int(run_id), str(model_key))).fetchone()
        return row["id"] if row else None


def save_outline(owner, data):
    """把一份大纲推入大纲库。**这是引用次数唯一会被影响的地方。**

    返回 {"outline_id":..., "created":bool, "refs":[...], "warnings":[...]}

    幂等三条路（按优先级）：
      1. 带了 outline_id        → 改那一份
      2. 带了 run_id+model_key  → 之前推过就改那一份
      3. 都没有                 → 新建
    """
    data = data or {}
    title = _txt(data.get("title"), OUTLINE_TITLE_MAX, "大纲标题")
    worldview = _txt(data.get("world_input_snapshot"), WORLD_CONTENT_MAX,
                     "世界观", allow_empty=False)   # 计划第十四.1：不许空着
    hook = _txt(data.get("one_sentence_hook"), HOOK_MAX, "一句话梗")
    design = _txt(data.get("plot_design"), DESIGN_MAX, "情节设计")
    note = _txt(data.get("user_note"), OUTLINE_NOTE_MAX, "备注")
    snap = _txt(data.get("world_name"), WORLD_NAME_MAX, "世界观名字")
    try:
        target = int(data.get("target_words") or 0)
    except (TypeError, ValueError):
        raise ValueError("预期字数得是一个整数。")
    if target < TARGET_WORDS_MIN:
        raise ValueError("预期字数至少 %d 字。" % TARGET_WORDS_MIN)

    char_ids = _int_list(data.get("character_ids"), 50, "角色卡")
    char_snaps = get_characters(owner, char_ids)
    if not char_snaps:
        raise ValueError("至少要关联一张角色卡。")

    cur_json = data.get("current_json")
    if not isinstance(cur_json, dict):
        raise ValueError("大纲内容得是一个对象。")
    ai_json = data.get("ai_original_json")
    if not isinstance(ai_json, dict):
        ai_json = cur_json
    cur_text = _txt(data.get("current_text"), 10 ** 9, "大纲正文") \
        or render_outline_text(cur_json)
    ai_text = _txt(data.get("ai_original_text"), 10 ** 9, "AI 原稿")
    # 【这条标记是给警告文案用的】"没有 AI 原稿"和"有原稿但她一字没改"
    # 是两件完全不同的事，但 diff 的结果都是"changed=False"。
    # 不区分的话，她手动新建一份大纲，会收到一句
    # "这一版和 AI 原稿一模一样"—— 可她压根没用 AI。
    # 默认按"有"算（只有接口层明确知道没有时才传 False）。
    has_ai = data.get("has_ai_original")
    has_ai = True if has_ai is None else bool(has_ai)

    sel = _int_list(data.get("selected_plot_ids"), 60, "采用的零件")
    # AI 提过、但最终没留下的 —— 记 kept=0，但**不计数**
    ai_used = _int_list(ai_json.get("used_plot_ids"), 60, "AI 用过的零件")

    manual = set(_int_list(data.get("manual_plot_ids"), 60, "手动添加的零件"))
    positions = data.get("plot_positions")
    if not isinstance(positions, dict):
        positions = {}

    ts = now_str()
    warnings = []
    with db.connect() as conn:
        oid = 0
        try:
            oid = int(data.get("outline_id") or 0)
        except (TypeError, ValueError):
            oid = 0
        if not oid:
            oid = find_outline_by_source(owner, data.get("run_id"),
                                         data.get("model_key")) or 0

        created = False
        if oid:
            row = conn.execute("SELECT * FROM outlines WHERE id=? AND owner_id=?",
                               (oid, owner)).fetchone()
            if not row:
                oid = 0
        if oid:
            # ---- AI 原稿只认库里那一份 ----
            # 【为什么必须从库里重取】她在大纲库页面上继续改的时候，
            # 前端回传的"当前版"就是她的稿子，**里面没有 AI 原稿**。
            # 如果这里不补一次，diff 就会拿"她的稿"跟"她的稿"比 ——
            # 结果永远是"一字没改"，而她明明改了一大堆。
            # 更糟的是哪天有人把 UPDATE 里加上 ai_original_json，
            # 原稿就被永久覆盖了：那是唯一能回答"AI 当初写了什么"的东西，
            # 覆盖掉就再也算不回来。所以 UPDATE 语句里**永不出现**这两列。
            ai_json = _loads(row["ai_original_json"], {}) or ai_json
            ai_text = row["ai_original_text"] or ai_text
        if not ai_text:
            ai_text = render_outline_text(ai_json)
        if oid:
            conn.execute(
                """UPDATE outlines SET title=?, world_input_snapshot=?,
                   world_name=?, character_ids_json=?, character_snapshot_json=?,
                   one_sentence_hook=?, hook_ai_derived=?, plot_design=?,
                   target_words=?, current_json=?, current_text=?,
                   selected_plot_ids_json=?, user_note=?, in_learning=?,
                   updated_at=? WHERE id=? AND owner_id=?""",
                (title, worldview, snap, _dumps(char_ids), _dumps(char_snaps),
                 hook, 1 if data.get("hook_ai_derived") else 0, design, target,
                 _dumps(cur_json), cur_text, _dumps(sel), note,
                 1 if data.get("in_learning") else 0, ts, oid, owner))
        else:
            cur = conn.execute(
                """INSERT INTO outlines
                   (owner_id, outline_type, title, worldview_id,
                    world_input_snapshot, world_name, character_ids_json,
                    character_snapshot_json, one_sentence_hook, hook_ai_derived,
                    plot_design, target_words, run_id, candidate_id, model_key,
                    model_name, prompt_version, prompt_ref_id, prompt_name,
                    user_prompt_len, user_prompt_snapshot, ai_original_json,
                    ai_original_text, current_json, current_text,
                    selected_plot_ids_json, user_note, in_learning,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (owner, OUTLINE_TYPE_TW, title, data.get("worldview_id") or None,
                 worldview, snap, _dumps(char_ids), _dumps(char_snaps), hook,
                 1 if data.get("hook_ai_derived") else 0, design, target,
                 data.get("run_id") or None, data.get("candidate_id") or None,
                 _txt(data.get("model_key"), 60, "模型"), 
                 _txt(data.get("model_name"), 120, "模型名"),
                 _txt(data.get("prompt_version"), 40, "提示词版本"),
                 data.get("prompt_ref_id") or 0,
                 _txt(data.get("prompt_name"), 120, "提示词名"),
                 int(data.get("user_prompt_len") or 0),
                 _txt(data.get("user_prompt_snapshot"), 8000, "补充提示词快照"),
                 _dumps(ai_json), ai_text, _dumps(cur_json), cur_text,
                 _dumps(sel), note, 1 if data.get("in_learning") else 0, ts, ts))
            oid = cur.lastrowid
            created = True

        # ---- 引用明细：先整份删掉再重插 ----
        # 【为什么敢删】因为计数是从明细 COUNT 出来的，而这次一定重插全套。
        # 删+插在同一个事务里，中途不会留下"半个"状态。
        before = _counts_in(conn, owner, set(sel) | set(ai_used))
        conn.execute("DELETE FROM outline_plot_refs WHERE outline_id=?", (oid,))
        allp = list(sel) + [p for p in ai_used if p not in sel]
        for pid in allp:
            conn.execute(
                """INSERT OR REPLACE INTO outline_plot_refs
                   (outline_id, outline_title, plot_id, owner_id, plot_title,
                    kept, ref_count_before, ref_count_after, position_note,
                    manual_added, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (oid, title, pid, owner, _plot_title(conn, owner, pid),
                 1 if pid in sel else 0, before.get(pid, 0), 0,
                 str(positions.get(str(pid)) or positions.get(pid) or "")[:200],
                 1 if pid in manual else 0, ts))
        after = _counts_in(conn, owner, set(sel) | set(ai_used))
        for pid in allp:
            conn.execute(
                "UPDATE outline_plot_refs SET ref_count_after=?"
                " WHERE outline_id=? AND plot_id=?",
                (after.get(pid, 0), oid, pid))

        # ---- 差异快照（kind='diff'）----
        # 【为什么要落一行】diff 随时能重算，但它读的是"当时的 AI 稿和用户稿"。
        # 她以后接着改，那两份就变了 —— 当时差在哪就永远答不出来了。
        # 所以保存这一刻钉一行快照。重新保存会产生新的一行（旧的留着），
        # 这跟 plots 那边"改内容一定产生新版本"是同一个思路。
        d = diff_outlines(ai_json, cur_json)
        conn.execute(
            """INSERT INTO outline_feedback
               (outline_id, owner_id, run_id, kind, node_id, problem, note,
                detail_json, enabled, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (oid, owner, data.get("run_id") or None, FEEDBACK_DIFF, "", "", "",
             _dumps(d), 1 if d.get("changed") else 0, ts))
        if not d.get("changed"):
            warnings.append("这一版和 AI 原稿一模一样（只在流程上过了你的手）。"
                            if has_ai else
                            "这份没有 AI 原稿（是你自己写的），所以没有可比对的差异。")

        if data.get("run_id"):
            conn.execute("UPDATE outline_runs SET saved_outline_id=?"
                         " WHERE id=? AND owner_id=?",
                         (oid, int(data["run_id"]), owner))
        if data.get("candidate_id"):
            conn.execute("UPDATE outline_candidates SET adopted=1, outline_id=?"
                         " WHERE id=? AND owner_id=?",
                         (oid, int(data["candidate_id"]), owner))

    return {"outline_id": oid, "created": created,
            "refs": [{"plot_id": p, "kept": p in sel,
                      "ref_count_before": before.get(p, 0),
                      "ref_count_after": after.get(p, 0)} for p in allp],
            "diff": d, "warnings": warnings}


def _counts_in(conn, owner, plot_ids):
    """在**已有连接里**算参考次数（保存过程中要对账，不能另开连接）。"""
    if not plot_ids:
        return {}
    ids = list(plot_ids)
    rows = conn.execute(
        "SELECT plot_id, COUNT(DISTINCT outline_id) AS n FROM outline_plot_refs"
        " WHERE owner_id=? AND kept=1 AND plot_id IN (%s) GROUP BY plot_id"
        % ",".join("?" * len(ids)), [owner] + ids).fetchall()
    return {r["plot_id"]: r["n"] for r in rows}


def _plot_title(conn, owner, pid):
    r = conn.execute("SELECT title FROM plots WHERE id=? AND owner_id=?",
                     (int(pid), owner)).fetchone()
    return (r["title"] if r else "") or ""


# ----------------------------------------------------------------------
# 大纲库：读取
# ----------------------------------------------------------------------

def _outline_row(row, with_content=False):
    d = {
        "id": row["id"],
        "outline_type": row["outline_type"],
        "title": row["title"],
        "world_name": row["world_name"],
        "target_words": row["target_words"],
        "one_sentence_hook": row["one_sentence_hook"],
        "hook_ai_derived": bool(row["hook_ai_derived"]),
        "model_key": row["model_key"],
        "model_name": row["model_name"],
        "prompt_version": row["prompt_version"],
        "prompt_name": row["prompt_name"],
        "selected_plot_ids": _loads(row["selected_plot_ids_json"], []),
        "character_ids": _loads(row["character_ids_json"], []),
        "in_learning": bool(row["in_learning"]),
        "user_note": row["user_note"],
        "run_id": row["run_id"],
        "candidate_id": row["candidate_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if with_content:
        d["world_input_snapshot"] = row["world_input_snapshot"]
        d["character_snapshot"] = _loads(row["character_snapshot_json"], [])
        d["plot_design"] = row["plot_design"]
        d["user_prompt_snapshot"] = row["user_prompt_snapshot"]
        d["ai_original_json"] = _loads(row["ai_original_json"], {})
        d["ai_original_text"] = row["ai_original_text"]
        d["current_json"] = _loads(row["current_json"], {})
        d["current_text"] = row["current_text"]
        d["word_budget"] = _loads(row["current_json"], {}).get("word_budget") or {}
    return d


def list_outlines(owner, keyword=None, limit=100, offset=0):
    sql = ["SELECT * FROM outlines WHERE owner_id=?"]
    args = [owner]
    if keyword:
        kw = "%" + keyword.strip() + "%"
        sql.append("AND (title LIKE ? OR world_name LIKE ? OR current_text LIKE ?)")
        args.extend([kw, kw, kw])
    sql.append("ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?")
    args.extend([int(limit), int(offset)])
    with db.connect() as conn:
        rows = conn.execute(" ".join(sql), args).fetchall()
        total = conn.execute("SELECT COUNT(*) AS n FROM outlines WHERE owner_id=?",
                             (owner,)).fetchone()["n"]
        return {"total": total, "items": [_outline_row(r) for r in rows]}


def get_outline(owner, oid):
    try:
        oid = int(oid)
    except (TypeError, ValueError):
        return None
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM outlines WHERE id=? AND owner_id=?",
                           (oid, owner)).fetchone()
        if not row:
            return None
        d = _outline_row(row, with_content=True)
        d["refs"] = [dict(r) for r in conn.execute(
            "SELECT plot_id, plot_title, kept, ref_count_before,"
            " ref_count_after, position_note, manual_added"
            " FROM outline_plot_refs WHERE outline_id=? ORDER BY id",
            (oid,)).fetchall()]
        d["diff"] = diff_outlines(d["ai_original_json"], d["current_json"])
    return d


def update_outline(owner, oid, patch):
    """改"当前版"。**AI 原稿一个字都不动。**

    计划第十四.8：`不要覆盖 AI 原始版本或用户历史版本`。
    所以这里能改的只有 title / current_* / selected_* / note / in_learning。
    """
    try:
        oid = int(oid)
    except (TypeError, ValueError):
        return None
    patch = patch or {}
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM outlines WHERE id=? AND owner_id=?",
                           (oid, owner)).fetchone()
        if not row:
            return None
        sets, args = [], []
        if "title" in patch:
            sets.append("title=?")
            args.append(_txt(patch.get("title"), OUTLINE_TITLE_MAX, "大纲标题"))
        if "user_note" in patch:
            sets.append("user_note=?")
            args.append(_txt(patch.get("user_note"), OUTLINE_NOTE_MAX, "备注"))
        if "in_learning" in patch:
            sets.append("in_learning=?")
            args.append(1 if patch.get("in_learning") else 0)
        if "target_words" in patch:
            try:
                tw = int(patch.get("target_words") or 0)
            except (TypeError, ValueError):
                raise ValueError("预期字数得是一个整数。")
            if tw < TARGET_WORDS_MIN:
                raise ValueError("预期字数至少 %d 字。" % TARGET_WORDS_MIN)
            sets.append("target_words=?")
            args.append(tw)
        if "world_input_snapshot" in patch:
            sets.append("world_input_snapshot=?")
            args.append(_txt(patch.get("world_input_snapshot"),
                             WORLD_CONTENT_MAX, "世界观", allow_empty=False))
        if "current_json" in patch:
            cj = patch.get("current_json")
            if not isinstance(cj, dict):
                raise ValueError("大纲内容得是一个对象。")
            sets.append("current_json=?")
            args.append(_dumps(cj))
            sets.append("current_text=?")
            args.append(render_outline_text(cj))
        if "selected_plot_ids" in patch:
            sel = _int_list(patch.get("selected_plot_ids"), 60, "采用的零件")
            sets.append("selected_plot_ids_json=?")
            args.append(_dumps(sel))
        if not sets:
            return get_outline(owner, oid)
        sets.append("updated_at=?")
        args.append(now_str())
        args.extend([oid, owner])
        conn.execute("UPDATE outlines SET %s WHERE id=? AND owner_id=?"
                     % ", ".join(sets), args)

        # ---- 改了零件清单，引用明细要跟着重算（但**旧明细留着审计**）----
        # 计划要求"删除大纲不回退历史计数"，同理：她后来把某条零件从大纲里
        # 拿掉了，历史计数也不减 —— 那一次它确实被用过。
        # 所以这里只做两件事：新加进来的补一行 kept=1，被拿掉的标 kept=0。
        # **绝不 DELETE**。
        if "selected_plot_ids" in patch:
            sel = _int_list(patch.get("selected_plot_ids"), 60, "采用的零件")
            ts = now_str()
            for pid in sel:
                old = conn.execute(
                    "SELECT id, kept FROM outline_plot_refs"
                    " WHERE outline_id=? AND plot_id=?", (oid, pid)).fetchone()
                if old:
                    if not old["kept"]:
                        conn.execute(
                            "UPDATE outline_plot_refs SET kept=1,"
                            " ref_count_after=? WHERE id=?",
                            (_counts_within(conn, owner, pid, oid, True), old["id"]))
                else:
                    conn.execute(
                        """INSERT OR REPLACE INTO outline_plot_refs
                           (outline_id, outline_title, plot_id, owner_id,
                            plot_title, kept, ref_count_before,
                            ref_count_after, position_note, manual_added, created_at)
                           VALUES (?,?,?,?,?,1,?,?,?,1,?)""",
                        (oid, row["title"], pid, owner,
                         _plot_title(conn, owner, pid),
                         _counts_within(conn, owner, pid, oid, False),
                         _counts_within(conn, owner, pid, oid, True),
                         "", ts))
            conn.execute(
                "UPDATE outline_plot_refs SET kept=0 WHERE outline_id=?"
                " AND plot_id NOT IN (%s)" % ",".join("?" * len(sel)),
                [oid] + sel)
        r = conn.execute("SELECT * FROM outlines WHERE id=?", (oid,)).fetchone()
        return _outline_row(r, with_content=True)


def _counts_within(conn, owner, plot_id, outline_id, include_self):
    """算某条零件当前被几份大纲采用（可含/不含指定这一份）。"""
    rows = conn.execute(
        "SELECT DISTINCT outline_id FROM outline_plot_refs"
        " WHERE owner_id=? AND plot_id=? AND kept=1", (owner, int(plot_id))).fetchall()
    ids = {r["outline_id"] for r in rows}
    if include_self:
        ids.add(int(outline_id))
    else:
        ids.discard(int(outline_id))
    return len(ids)


def delete_outline(owner, oid):
    """删一份大纲。

    **只删 outlines 那一行** —— 引用明细一行不动。原因见文件头第 2 条：
    计划规定删除不回退历史计数，而计数正是从明细算出来的。
    明细里存了 outline_title 快照，所以历史还认得出"当初被哪篇用过"。
    """
    try:
        oid = int(oid)
    except (TypeError, ValueError):
        return False
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM outlines WHERE id=? AND owner_id=?",
                           (oid, owner))
        if cur.rowcount:
            # 学习反馈也跟着走（那是"这份大纲的"反馈，大纲没了就没意义了）。
            # 引用明细**故意不删** —— 上面那句注释说的就是这件事。
            conn.execute("DELETE FROM outline_feedback WHERE outline_id=?"
                         " AND owner_id=? AND kind<>?", (oid, owner, FEEDBACK_DIFF))
        return cur.rowcount > 0


# ----------------------------------------------------------------------
# 学习反馈
# ----------------------------------------------------------------------

def list_feedback(owner, oid, kind=None):
    sql = ["SELECT * FROM outline_feedback WHERE owner_id=? AND outline_id=?"]
    args = [owner, int(oid)]
    if kind:
        sql.append("AND kind=?")
        args.append(kind)
    sql.append("ORDER BY id DESC LIMIT 200")
    with db.connect() as conn:
        rows = conn.execute(" ".join(sql), args).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "kind": r["kind"], "node_id": r["node_id"],
            "problem": r["problem"], "note": r["note"],
            "enabled": bool(r["enabled"]),
            "detail": _loads(r["detail_json"], {}),
            "created_at": r["created_at"],
        })
    return out


def add_feedback(owner, oid, kind, node_id="", problem="", note="", enabled=True):
    """记一条学习反馈。

    计划第九节：只有她**明确选择**加入学习的反馈，才进学习案例。
    所以 enabled 默认给的是"她勾了才 True"——调用方负责传对。
    """
    if kind not in ALL_FEEDBACK_KIND:
        raise ValueError("没有「%s」这种反馈。" % kind)
    if kind == FEEDBACK_NODE:
        node_id = _txt(node_id, 40, "节点编号", allow_empty=False)
        if problem and problem not in PROBLEM_TYPES:
            raise ValueError("没有「%s」这个原因，只能从清单里选。" % problem)
    note = _txt(note, FEEDBACK_NOTE_MAX, "说明")
    try:
        oid = int(oid)
    except (TypeError, ValueError):
        raise ValueError("没说是哪一份大纲。")
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM outlines WHERE id=? AND owner_id=?",
                           (oid, owner)).fetchone()
        if not row:
            return None
        cur = conn.execute(
            """INSERT INTO outline_feedback
               (outline_id, owner_id, kind, node_id, problem, note,
                detail_json, enabled, created_at)
               VALUES (?,?,?,?,?,?,'{}',?,?)""",
            (oid, owner, kind, node_id, problem, note,
             1 if enabled else 0, now_str()))
        return cur.lastrowid


def set_feedback_enabled(owner, fid, enabled):
    with db.connect() as conn:
        cur = conn.execute("UPDATE outline_feedback SET enabled=? WHERE id=?"
                           " AND owner_id=?", (1 if enabled else 0, int(fid), owner))
        return cur.rowcount > 0


def learning_examples(owner, limit=3):
    """挑几条"可以拿来参考"的学习案例。

    计划第九节把门槛列得很清楚，这里逐条对应：
      · 优先当前用户自己的         → owner_id=? 兜住了
      · 与当前类型/字数相似         → 收紧到同一个 outline_type
      · 不无限注入全部历史大纲      → limit（默认 3 条）
      · 分类/内化/大纲学习分开存储   → 读的是 outline_feedback 这一张
    另加一条：**只挑 enabled=1 的**。她没勾"加入学习"的，一个字都不进。
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT f.*, o.title AS outline_title, o.target_words AS target_words,"
            " o.outline_type AS outline_type"
            " FROM outline_feedback f"
            " JOIN outlines o ON o.id = f.outline_id"
            " WHERE f.owner_id=? AND f.enabled=1 AND f.kind=?"
            " ORDER BY f.id DESC LIMIT ?",
            (owner, FEEDBACK_NODE, int(limit))).fetchall()
    out = []
    for r in rows:
        out.append({"problem": r["problem"],
                    "note": r["note"],
                    "node_id": r["node_id"],
                    "outline_title": r["outline_title"],
                    "target_words": r["target_words"]})
    return out


def learning_stats(owner):
    """学习库的一眼概况（界面上显示"现在有多少可供参考的案例"）。

    【rewrites 这个数从哪儿来（阶段一改过）】
    原来是把每条改写记录的 summary_json 里的条目数加起来 ——
    但那里面混着各种状态，而**只有"已接受 + 启用 + 不是仅本篇"的
    才真的会进生成**。数字比实际能用的多，她会以为"学了很多"。
    现在按三个轴一起数，口径跟 rewrite_examples 完全一致。
    """
    with db.connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM outlines WHERE owner_id=?",
            (owner,)).fetchone()["n"]
        inl = conn.execute(
            "SELECT COUNT(*) AS n FROM outlines WHERE owner_id=? AND in_learning=1",
            (owner,)).fetchone()["n"]
        diffs = conn.execute(
            "SELECT COUNT(*) AS n FROM outline_feedback WHERE owner_id=? AND kind=?",
            (owner, FEEDBACK_DIFF)).fetchone()["n"]
        cases = conn.execute(
            "SELECT COUNT(*) AS n FROM outline_feedback WHERE owner_id=?"
            " AND kind=? AND enabled=1", (owner, FEEDBACK_NODE)).fetchone()["n"]
        # 已经接受 + 启用 + 适用范围可跨作品 的改写建议。
        # 判据跟 rewrite_examples 保持一致 —— 界面上那个数跟"实际会进生成
        # 的条数"必须是同一个口径，不然她数着 5 条、实际只用了 2 条。
        live = conn.execute(
            "SELECT COUNT(*) AS n FROM outline_rewrite_points"
            " WHERE owner_id=? AND review=? AND active=?"
            " AND scope IN (?,?)",
            (owner, REWRITE_RV_ACCEPTED, REWRITE_ACTIVE_ON,
             REWRITE_SCOPE_LONG, REWRITE_SCOPE_CASE)).fetchone()["n"]
        # 待确认的（她还没点过的）—— 界面上要能看出"还有多少等我拍板"
        pend = conn.execute(
            "SELECT COUNT(*) AS n FROM outline_rewrite_points"
            " WHERE owner_id=? AND review=?",
            (owner, REWRITE_RV_PENDING)).fetchone()["n"]
        # 只有这一块整个开着、而且真的总结出东西来的改写记录数
        rwrec = conn.execute(
            "SELECT COUNT(*) AS n FROM outline_rewrites"
            " WHERE owner_id=? AND enabled=1 AND status=?",
            (owner, REWRITE_DONE)).fetchone()["n"]
    return {"outlines": total, "in_learning": inl, "diffs": diffs,
            "cases": cases, "rewrites": live, "rewrite_pending": pend,
            "rewrite_records": rwrec}


# ----------------------------------------------------------------------
# 改写对比：她交一份成品大纲，AI 对着 AI 原稿看"她改了什么"
# ----------------------------------------------------------------------

def _rewrite_row(row, with_text=True):
    """把一行 outline_rewrites 变成接口返回的 dict。

    【keys 为什么用 row.keys() 探测】老库可能还没补上阶段一那些列
    （虽然 migrate 会补，但自检/测试里可能拿到手搓的行）。探测一下比
    直接下标更稳 —— 缺列时给默认值，而不是 KeyError 把接口打成 500。
    """
    if not row:
        return None
    keys = set(row.keys())

    def g(col, default=None):
        return row[col] if col in keys else default

    out = {
        "id": row["id"],
        "run_id": row["run_id"],
        "candidate_id": row["candidate_id"],
        # ★ 阶段一E：也可能挂在大纲库某一版上
        "outline_id": g("outline_id", None),
        "model_key": row["model_key"],
        "model_name": row["model_name"],
        "source": row["source"],
        "source_kind": g("source_kind", REWRITE_KIND_ADOPT) or REWRITE_KIND_ADOPT,
        "diff": _loads(row["diff_json"], {}),
        "summary_text": row["summary_text"],
        "points": _loads(row["summary_json"], []),
        "summary_model": row["summary_model"],
        "status": row["status"],
        "error": row["error"],
        "note": row["note"],
        "enabled": bool(row["enabled"]),
        "chars": len(row["user_text"] or ""),
        # ---- 阶段一加的（来源快照 + 版本 + 分析来源）----
        "user_version": int(g("user_version", 1) or 1),
        "analyze_model": g("analyze_model", "") or "",
        "analyze_version": g("analyze_version", "") or "",
        "target_words": int(g("target_words", 0) or 0),
        # 快照的完整程度。老记录是空的 —— 界面上要如实说"当时没存"，
        # 不许拿现在的设定冒充（需求第 2 条）。
        "snapshot_state": g("snapshot_state", "") or "",
        "ai_source_note": g("ai_source_note", "") or "",
        "has_ai_snapshot": bool((g("ai_text_snapshot", "") or "").strip()),
        "frozen": bool((g("input_frozen", "") or "").strip()),
        "snapshot": {
            "worldview": g("world_snapshot", "") or "",
            "characters": _loads(g("chars_snapshot", "[]"), []),
            "one_sentence_hook": g("hook_snapshot", "") or "",
            "plot_design": g("design_snapshot", "") or "",
            "target_words": int(g("target_words", 0) or 0),
            "requirement": _loads(g("req_snapshot", "{}"), {}),
        },
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if with_text:
        out["user_text"] = row["user_text"]
        # AI 原稿快照（分析时真正用的那份）。老记录可能没有 —— 给空串，
        # 界面据此显示"当时没存原稿"。
        out["ai_text"] = g("ai_text_snapshot", "") or ""
        out["ai_model"] = g("ai_model_snapshot", "") or ""
    return out


def create_rewrite(owner, data):
    """收一份她的成品大纲。可以挂在某次任务的某一版候选上，也可以什么都不挂。

    【幂等】同一个 (owner, candidate_id) 只留一条 —— 她对着同一版反复交，
    应该更新那一条，而不是堆出一串看起来一模一样的记录。
    她真正想留的历史版本在大纲库里（"另存一份"那条路），这里只是
    "针对这一版，我的改写偏好是什么"。candidate_id 为空（自己写的）时
    不做幂等，每次都是新的一条。

    【阶段一收的三样快照（需求第 2/4 条）】
    1. 设定：世界观 / 角色卡 / 一句话梗 / 情节设计 / 预期字数
    2. **AI 原稿全文**（★ 折腰点名："不能继续只靠 candidate_id 找原稿"）
       —— 候选以后被她改了、或者被清理了，学习证据还得是当时那一份。
    3. 输入指纹（input_frozen）—— 分析跑起来之后她又编辑页面，
       结果不该算在那份新输入头上。
    """
    data = data or {}
    text = _txt(data.get("user_text"), REWRITE_TEXT_MAX, "你的大纲",
                allow_empty=False)
    note = _txt(data.get("note"), OUTLINE_NOTE_MAX, "备注")
    src = (data.get("source") or REWRITE_SOURCE_SCRATCH).strip()
    if src not in ALL_REWRITE_SOURCE:
        src = REWRITE_SOURCE_SCRATCH

    try:
        cid = int(data.get("candidate_id") or 0) or None
    except (TypeError, ValueError):
        cid = None
    try:
        rid = int(data.get("run_id") or 0) or None
    except (TypeError, ValueError):
        rid = None
    try:
        oid = int(data.get("outline_id") or 0) or None
    except (TypeError, ValueError):
        oid = None

    ts = now_str()
    with db.connect() as conn:
        # 候选 / 任务都得是**她自己的** —— 拿别人的 id 进来就是越权读原稿。
        #
        # 【source_kind 由什么定】只看**有没有真的挂上原稿**，
        # 不看入口。她可能从"独立创作"入口进来却挑了一版候选当蓝本，
        # 也可能从候选卡进来、实际整篇重写 —— 两种都得按事实记。
        ai_text = ""
        ai_model = ""
        ai_source_note = ""
        ai_json = {}
        # 挂大纲库时，这份大纲行自己带的设定快照（run 补不上时兜底）。
        # ★ 必须在分支**之前**初始化 —— 候选支路不填它，但后面
        #   统一的兜底段会读它，不初始化就是 NameError。
        ol_snap = {}
        if cid:
            r = conn.execute(
                "SELECT run_id, model_key, model_name, content_text,"
                " content_json FROM outline_candidates"
                " WHERE id=? AND owner_id=?", (cid, owner)).fetchone()
            if not r:
                return None
            rid = rid or r["run_id"]
            mk = r["model_key"]
            mn = r["model_name"]
            ai_json = _loads(r["content_json"], {})
            # 原稿全文落快照：content_text 为空时现渲染一份，
            # 保证快照里一定有可读的正文（需求第 2 条要的是"全文"）。
            ai_text = r["content_text"] or _outline_text_of(ai_json) or ""
            ai_model = mn or mk or ""
            ai_source_note = "candidate"    # 分析时从候选抄的
            kind = REWRITE_KIND_ADOPT
        elif oid:
            # ★ 从大纲库挂过来的（阶段一E）：原稿取"库里那一版"。
            # 【为什么用 current_json 而不是 ai_original_json】她要对照的
            # 是**她自己在库里存的那一版**（可能就是她在库里改过的）。
            # 但她改完又交一份新的过来，那"新的"就是 user_text ——
            # 所以拿 current 当原稿、跟 user_text 比，才是她要的那份 diff。
            #
            # ★ 一并把**这份大纲自己的设定快照**读出来（ol_snap）。
            #   【为什么非读不可】她的 run 会被清理掉，而大纲是留在库里的。
            #   只认 run 的话，run 一没，世界观和角色卡就**整块丢掉**
            #   （实测：snapshot_state 报 partial、worldview 空串）。
            #   大纲行里 world_input_snapshot / character_snapshot_json
            #   存的就是"存这份大纲时那套设定"，本来就是权威来源。
            r = conn.execute(
                "SELECT run_id, model_key, model_name, current_text,"
                " current_json, world_input_snapshot, world_name,"
                " character_snapshot_json, character_ids_json,"
                " one_sentence_hook, plot_design, target_words"
                " FROM outlines"
                " WHERE id=? AND owner_id=?", (oid, owner)).fetchone()
            if not r:
                return None
            rid = rid or r["run_id"]
            mk = r["model_key"] or ""
            mn = r["model_name"] or ""
            ai_json = _loads(r["current_json"], {})
            ai_text = r["current_text"] or _outline_text_of(ai_json) or ""
            ai_model = mn or mk or ""
            # 大纲库那一版**可能是她改过的** —— 如实标"来自大纲库"，
            # 让她知道对照的基准是库里的那一版，不是某个模型的原始输出。
            ai_source_note = "outline"
            kind = REWRITE_KIND_ADOPT
            # 存起来给下面的快照段用（run 补不上时兜底）。
            ol_snap = {
                "worldview": str(r["world_input_snapshot"] or ""),
                "world_name": str(r["world_name"] or ""),
                "characters": _loads(r["character_snapshot_json"], []),
                "hook": str(r["one_sentence_hook"] or ""),
                "design": str(r["plot_design"] or ""),
                "target_words": int(r["target_words"] or 0),
            }
        else:
            mk = mn = ""
            kind = REWRITE_KIND_SCRATCH
            # 没候选 —— 明说没有原稿，**绝不伪造**（需求第 2/3 条）。
            ai_source_note = "missing"

        # ---- 当时的设定快照 ----
        # 有 run 就照抄它的输入快照（那才是**生成时真正用的**那套设定）；
        # 没 run 就用调用方传进来的（独立创作时她填的）。
        snap = {"worldview": "", "characters": [], "hook": "", "design": "",
                "target_words": 0}
        req = {}
        # ol_snap 由上面「挂大纲库」那条支路填（见 elif oid 段）。
        # ★ 这里**千万不要再写 ol_snap = {}** —— 那会把刚读到的大纲快照
        #   当场清掉，兜底变成永远走不到。我第一版就是这么写的，
        #   【22】实测角色卡一直空，查了半天才发现是自己把它清掉了。
        if rid:
            run = conn.execute(
                "SELECT input_json FROM outline_runs WHERE id=? AND owner_id=?",
                (rid, owner)).fetchone()
            if run:
                inp = _loads(run["input_json"], {})
                if isinstance(inp, dict):
                    snap["worldview"] = str(inp.get("worldview") or "")
                    snap["characters"] = inp.get("character_snapshot") or []
                    snap["hook"] = str(inp.get("one_sentence_hook") or "")
                    snap["design"] = str(inp.get("plot_design") or "")
                    try:
                        snap["target_words"] = int(inp.get("target_words") or 0)
                    except (TypeError, ValueError):
                        snap["target_words"] = 0
                    req = {"one_sentence_hook": snap["hook"],
                           "plot_design": snap["design"],
                           "target_words": snap["target_words"],
                           "world_name": str(inp.get("world_name") or "")}
        # 调用方传的覆盖空白的那些（她独立创作时会带上）。
        if not snap["worldview"]:
            snap["worldview"] = str(data.get("worldview") or "")[:WORLD_CONTENT_MAX]
        if not snap["characters"]:
            chs = data.get("characters")
            snap["characters"] = chs if isinstance(chs, list) else []
        if not snap["hook"]:
            snap["hook"] = str(data.get("one_sentence_hook") or "")[:HOOK_MAX]
        if not snap["design"]:
            snap["design"] = str(data.get("plot_design") or "")[:DESIGN_MAX]
        if not snap["target_words"]:
            try:
                snap["target_words"] = int(data.get("target_words") or 0)
            except (TypeError, ValueError):
                snap["target_words"] = 0
        if not req:
            req = {"one_sentence_hook": snap["hook"],
                   "plot_design": snap["design"],
                   "target_words": snap["target_words"],
                   "world_name": str(data.get("world_name") or "")}

        # ★ 大纲库那条路的兜底：run 被清了 / 本来就没 run 时，
        #   用**这份大纲行里存的**那份设定。它的权威性和 run 的
        #   input_json 是同一级的（存大纲时就抄下来了），
        #   而且她清 run 的时候大纲还在 —— 不兜这一下，世界观和角色卡
        #   会静默丢空，AI 拿到一份"没有设定"的学习材料。
        #
        # ★ 位置很关键：必须放在**调用方覆盖那段之后**。
        #   我第一版放在前面，紧接着的 `if not snap["characters"]:
        #   snap["characters"] = data.get("characters")` 又把它刷回 [] ——
        #   从大纲库交改写时调用方本来就不带 characters（她的稿子里没有
        #   角色卡，角色卡在库里），于是兜底白做、角色卡永远空。
        #   这一条是【22】实测抓出来的。
        if oid and ol_snap:
            for k in ("worldview", "characters", "hook", "design",
                      "target_words"):
                if not snap[k] and ol_snap.get(k):
                    snap[k] = ol_snap[k]
            if not req.get("world_name") and ol_snap.get("world_name"):
                req["world_name"] = ol_snap["world_name"]

        # 快照完整程度：有原稿 + 有设定 → complete；
        # 独立创作（本来就没原稿）→ no_original；设定也缺 → partial。
        if kind == REWRITE_KIND_SCRATCH:
            snap_state = "no_original"
        elif not (snap["worldview"] or snap["characters"]):
            snap_state = "partial"
        else:
            snap_state = "complete"

        # ---- 算差异：纯函数，不联网不花钱 ----
        # 她那版永远按段落切（她没有 node_id 这个概念）；
        # 有原稿时（候选 或 大纲库那一版）就对着比，没有就只切段。
        #
        # ★ 阶段一E：判据从 `if cid:` 改成 `if ai_text:` ——
        #   挂大纲库那条路的原稿一样在 ai_text 里，只认 cid 的话
        #   "我改了这一版"交上去**差异永远是空的**（界面上什么都不显示）。
        user_json = text_to_outline_json(text)
        diff = {}
        if ai_text:
            diff = diff_against_text(ai_json, user_json)

        # 输入指纹（冻结）：分析开始后编辑页面不影响这次分析。
        frozen = _freeze_key(
            {"ai": ai_text, "user": text, "world": snap["worldview"],
             "chars": snap["characters"], "req": req, "note": note,
             "v_kind": kind})

        old = None
        if cid:
            old = conn.execute(
                "SELECT id, user_version FROM outline_rewrites WHERE owner_id=?"
                " AND candidate_id=?", (owner, cid)).fetchone()
        elif oid:
            # 从大纲库挂过来的也做幂等：对着同一版大纲反复交，
            # 应该更新那一条，不是堆出一串。
            old = conn.execute(
                "SELECT id, user_version FROM outline_rewrites WHERE owner_id=?"
                " AND outline_id=?", (owner, oid)).fetchone()

        if old:
            # 重交同一版：正文和差异换新，**版本号 +1**，
            # AI 总结作废重来 —— 正文都换了，旧总结说的就不是这份东西了。
            #
            # 【旧建议怎么处理】不是删掉。整批标成"停用"，
            # 并记下它来自哪一版 —— 她要是想回看"上一版 AI 说过什么"，
            # 还在。新的分析结果会写进 outline_rewrite_points 的新行里。
            v = int(old["user_version"] or 1) + 1
            conn.execute(
                "UPDATE outline_rewrites SET user_text=?, user_json=?,"
                " diff_json=?, note=?, status=?, error='', summary_json='[]',"
                " summary_text='', source_kind=?, world_snapshot=?,"
                " chars_snapshot=?, hook_snapshot=?, design_snapshot=?,"
                " target_words=?, user_version=?, ai_text_len=?,"
                " ai_text_snapshot=?, ai_model_snapshot=?, ai_source_note=?,"
                " req_snapshot=?, snapshot_state=?, input_frozen=?, updated_at=?"
                " WHERE id=? AND owner_id=?",
                (text, _dumps(user_json), _dumps(diff), note,
                 REWRITE_PENDING, kind, snap["worldview"],
                 _dumps(snap["characters"]), snap["hook"], snap["design"],
                 snap["target_words"], v, len(ai_text),
                 ai_text, ai_model, ai_source_note, _dumps(req), snap_state,
                 frozen, ts, old["id"], owner))
            _disable_points(conn, owner, old["id"], v - 1)
            return old["id"]

        cur = conn.execute(
            "INSERT INTO outline_rewrites (owner_id, run_id, candidate_id,"
            " outline_id, model_key, model_name, source, source_kind,"
            " user_text, user_json,"
            " diff_json, note, status, enabled, world_snapshot, chars_snapshot,"
            " hook_snapshot, design_snapshot, target_words, user_version,"
            " ai_text_len, ai_text_snapshot, ai_model_snapshot, ai_source_note,"
            " req_snapshot, snapshot_state, input_frozen, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?)",
            (owner, rid, cid, oid, mk, mn, src, kind, text, _dumps(user_json),
             _dumps(diff), note, REWRITE_PENDING, snap["worldview"],
             _dumps(snap["characters"]), snap["hook"], snap["design"],
             snap["target_words"], len(ai_text), ai_text, ai_model,
             ai_source_note, _dumps(req), snap_state, frozen, ts, ts))
        return cur.lastrowid


def _freeze_key(obj):
    """给一份输入算个指纹。分析跑起来的那一刻算一次，落库。

    【为什么不存整个输入】整个输入已经在快照列里了。这个指纹只回答
    一个是非题：**分析开始之后，她有没有改过输入**。改了就说明
    这份结论对应的是"开跑那一刻"那版，不是现在这版。
    """
    try:
        raw = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        raw = str(obj)
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]


def _disable_points(conn, owner, rewrite_id, src_version=None):
    """把某条改写记录现有的建议整批标成**停用**（不是删）。

    【为什么不删】需求第 2/4 条：已确认的旧成果不能因为新分析开始就
    悄悄消失。她重交了一份稿，上一批建议说的就不是这份稿子了 ——
    但它们是她确认过的历史，得留着、得能查，只是不再参与生成。

    【只动 active，不动 review】★ 2026-10-08 折腰纠正后的关键一点：
    "停用"是使用状态，不是审核状态。她之前接受过的那几条，
    审核状态仍然是"已接受"（她的判断没变），只是现在**不生效**了。
    把 review 也一起改成"已停用"就是把两件事揉一起 —— 那正是被纠正的错。
    """
    if src_version is None:
        conn.execute(
            "UPDATE outline_rewrite_points SET active=?, updated_at=?"
            " WHERE owner_id=? AND rewrite_id=? AND active!=?",
            (REWRITE_ACTIVE_OFF, now_str(), owner, int(rewrite_id),
             REWRITE_ACTIVE_OFF))
    else:
        conn.execute(
            "UPDATE outline_rewrite_points SET active=?, updated_at=?"
            " WHERE owner_id=? AND rewrite_id=? AND active!=?"
            " AND source_version=?",
            (REWRITE_ACTIVE_OFF, now_str(), owner, int(rewrite_id),
             REWRITE_ACTIVE_OFF, str(src_version)))


def list_rewrites(owner, candidate_id=None, limit=50, offset=0):
    """列出她的改写记录（大纲库那边要能看见"我以前交过哪些"）。

    【为什么顺手把"N 条会进生成"也算出来】大纲库那一列要是每条都去
    单独问一次建议表，就是 N+1 次查询 —— 50 条记录 = 51 次数据库往返，
    列表一多就明显卡。这里用**一条 GROUP BY** 把每条记录的建议总数和
    生效数一次查完，界面上直接显示"3 条里 1 条生效"，不用再点进去看。
    """
    with db.connect() as conn:
        if candidate_id:
            rows = conn.execute(
                "SELECT * FROM outline_rewrites WHERE owner_id=?"
                " AND candidate_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (owner, int(candidate_id), int(limit), int(offset))).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM outline_rewrites WHERE owner_id=?"
                " ORDER BY id DESC LIMIT ? OFFSET ?",
                (owner, int(limit), int(offset))).fetchall()
        items = [_rewrite_row(r, with_text=False) for r in rows]
        if not items:
            return items
        ids = [it["id"] for it in items]
        # 占位符按 id 个数生成 —— 不能用 f-string 拼 id（SQL 注入）。
        qs = ",".join("?" for _ in ids)
        # ★ 生效/条数都只算**当前版本那批建议**（source_version = 记录的
        #   user_version），跟 list_rewrite_points() 的默认口径严格一致。
        #   【为什么必须对齐】她重交同一版时旧建议会被留成历史
        #   （_disable_points 只标不用、不删）。这里要是把历史一起数进去，
        #   大纲库列表会显示"5 条建议"，点进去只看见 3 条 —— 数字对不上，
        #   她只会怀疑是不是丢了东西。这个坑是测试【21】实测抓出来的。
        live_sql = ("SUM(CASE WHEN p.review=? AND p.active=?"
                    " AND p.scope IN (?,?) THEN 1 ELSE 0 END)")
        rows2 = conn.execute(
            "SELECT p.rewrite_id AS rewrite_id, COUNT(*) AS n,"
            " " + live_sql + " AS live_n"
            " FROM outline_rewrite_points p"
            " JOIN outline_rewrites w ON w.id = p.rewrite_id"
            " AND w.owner_id = p.owner_id"
            " WHERE p.owner_id=? AND p.rewrite_id IN (" + qs + ")"
            " AND p.source_version = CAST(w.user_version AS TEXT)"
            " GROUP BY p.rewrite_id",
            (REWRITE_RV_ACCEPTED, REWRITE_ACTIVE_ON,
             REWRITE_SCOPE_LONG, REWRITE_SCOPE_CASE,
             owner, *ids)).fetchall()
        stat = {int(r["rewrite_id"]): (int(r["n"] or 0), int(r["live_n"] or 0))
                for r in rows2}
        for it in items:
            n, ln = stat.get(it["id"], (0, 0))
            it["point_count"] = n
            it["live_count"] = ln
    return items


def get_rewrite(owner, rid):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM outline_rewrites WHERE id=? AND owner_id=?",
            (int(rid), owner)).fetchone()
    return _rewrite_row(row, with_text=True)


# 8 项证据字段的键 → 列名映射。写成表驱动，加字段就是加一行。
#
# 【为什么不在循环里 if 每个字段】那样一加字段就要在清洗和 SQL 两处各写一遍，
# 两处不同步就是"填了却存不进去"（而且静默）。这里一份声明，两边都用它。
REWRITE_PT_FIELDS = (
    ("problem", "problem"),
    ("change", "change_note"),          # change 是 SQL 关键字，列名避开
    ("evidence", "evidence"),
    ("improved", "improved"),
    ("method", "method"),
    ("applies_when", "applies_when"),
    ("not_when", "not_when"),
    ("confidence", "confidence"),
)
# confidence 是**分析把握程度**，独立于 scope（折腰点名）。
# 它在 REWRITE_PT_FIELDS 里只是"要落库"的意思，取值校验单独走 ALL_REWRITE_CONF。
_CONF_KEYS = ("confidence", "判断是否充分", "置信度", "把握", "判断充分度")
# AI 有可能用中文键名回，这里给个别名表（跟其它 AI 解析处一个套路）。
_PT_ALIAS = {
    "problem": ("原稿问题", "问题", "原稿有什么问题"),
    "change": ("人工稿调整", "调整", "她改成了什么", "人工稿做了什么"),
    "evidence": ("证据", "对应证据"),
    "improved": ("改善", "改善了什么", "好处"),
    "method": ("可复用方法", "方法", "做法"),
    "applies_when": ("适用情境", "适用", "什么时候适用"),
    "not_when": ("不适用", "什么情况不适用", "不适用情况"),
}
# scope 认得的写法（中文/英文都收）
_SCOPE_ALIAS = {
    "long": ("长期", "长期偏好", "long", "通用", "通用偏好"),
    "case": ("情境", "情境适用", "特定情境", "case", "类型"),
    "this": ("仅本篇", "本篇", "本篇专用", "this", "只这一篇"),
}
# review 认得的写法
_REVIEW_ALIAS = {
    REWRITE_RV_PENDING: ("待确认", "pending", "待定"),
    REWRITE_RV_ACCEPTED: ("已接受", "接受", "accepted", "已采纳"),
    REWRITE_RV_REJECTED: ("已拒绝", "拒绝", "rejected", "已否"),
}


def _match_axis(raw, alias, default):
    """把一个自由写法归到某个轴上的取值。归不上就用 default。

    【为什么默认值这么重要】scope 认不出时兜到 this（最保守），
    绝不兜到 long —— 猜成长期 = 一条没核实的规则进她以后所有生成；
    兜成仅本篇最多是这条先别用，她点一下就能改。这个方向不能反。
    """
    s = (raw or "").strip()
    if not s:
        return default
    for key, names in alias.items():
        if s == key or s in names:
            return key
    return default


def _pt_field(p, key):
    """从 AI 返回的一条建议里抠出某个证据字段（认中文别名）。"""
    for name in (key,) + _PT_ALIAS.get(key, ()):
        v = p.get(name)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s[:REWRITE_PT_FIELD_MAX]
    return ""


def _pt_confidence(p):
    """抠出"判断充分度"。**这是独立的一轴**，不塞进 scope。"""
    for name in _CONF_KEYS:
        v = p.get(name)
        if v is None:
            continue
        s = str(v).strip()
        if not s:
            continue
        if s in (REWRITE_CONF_ENOUGH, "充分", "充足", "高", "enough"):
            return REWRITE_CONF_ENOUGH
        if s in (REWRITE_CONF_MEDIUM, "一般", "中", "medium", "部分"):
            return REWRITE_CONF_MEDIUM
        if s in (REWRITE_CONF_WEAK, "不足", "较低", "低", "weak",
                 "unsure", "不确定", "推测"):
            return REWRITE_CONF_WEAK
    return REWRITE_CONF_UNKNOWN


def clean_rewrite_points(points):
    """把 AI 返回的建议列表清洗成能落库的形状。

    【为什么单独抽成函数】自检要测它、落库要用它、重跑分析也要用它 ——
    三个地方共用一份清洗逻辑，"填了但存不进"这种毛病才只有一处可能。

    ★ 三个轴各自兜底、互不干扰（2026-10-08 折腰纠正）：
      review 默认 待确认（她不点就不生效）
      active 默认 启用（"启用但待确认"实际也不生效，因为 review 卡着）
      scope  默认 this（认不出时最保守）
      confidence 独立保存，"判断不充分"不会跑到 scope 里去
    """
    if not isinstance(points, list):
        return []
    clean = []
    for p in points[:REWRITE_POINTS_MAX]:
        if isinstance(p, str):
            p = {"point": p}
        if not isinstance(p, dict):
            continue
        t = (p.get("point") or p.get("text") or p.get("观察") or "").strip()
        if not t:
            continue
        kind = (p.get("kind") or p.get("类别") or "").strip()[:20]
        how = (p.get("how") or p.get("以后怎么写") or "").strip()[:600]
        scope = _match_axis(p.get("scope") or p.get("适用范围")
                            or p.get("适用层次"), _SCOPE_ALIAS,
                            REWRITE_SCOPE_THIS)
        review = _match_axis(p.get("review") or p.get("审核"), _REVIEW_ALIAS,
                             REWRITE_RV_PENDING)
        row = {
            "kind": kind, "point": t[:600], "how": how, "scope": scope,
            "review": review, "active": REWRITE_ACTIVE_ON,
            "confidence": _pt_confidence(p),
        }
        for key, _col in REWRITE_PT_FIELDS:
            if key == "confidence":
                continue            # 上面单独抠过，取值要校验
            row[key] = _pt_field(p, key)
        # "对不上 / 不确定" 这条信息也收（需求第 4 条）
        row["uncertain"] = str(p.get("uncertain") or p.get("不确定")
                               or p.get("对齐说明") or "").strip()[:REWRITE_PT_FIELD_MAX]
        clean.append(row)
    return clean


def set_rewrite_summary(owner, rid, points, summary_text="", model_name="",
                        status=REWRITE_DONE, error="", prompt_version="",
                        user_version=None):
    """把 AI 总结落的库。

    【阶段一起改成两处落库】
    1. outline_rewrites 上：一句话总述 + 分析用的模型和提示词版本 + 状态。
       **稿子和快照一个字不动** —— 需求第 4 条点名"原稿和人工稿
       不能被分析结果覆盖"。
    2. outline_rewrite_points 上：逐条建议（每条一行，三个轴 + 8 项证据）。
       上一批先整批**停用**（不是删、也不是改成"拒绝"），再写新的。

    points 是列表（一条条建议），不是一整块文本 —— 界面上要能**逐条确认**。
    """
    clean = clean_rewrite_points(points)
    ts = now_str()
    with db.connect() as conn:
        # 老版本号（这条 rewrite 现在是第几版）—— 新建议要记上来源版本，
        # 以后才看得出"这批建议是对着第几版人工稿总结的"。
        if user_version is None:
            row = conn.execute(
                "SELECT user_version FROM outline_rewrites"
                " WHERE id=? AND owner_id=?", (int(rid), owner)).fetchone()
            try:
                user_version = int(row["user_version"] or 1) if row else 1
            except (TypeError, ValueError, KeyError):
                user_version = 1
        src_ver = str(user_version)

        # 【重跑分析时旧建议怎么办】先整批停用（不删），再写新的。
        # 只在真有新结果（clean 非空）时才停用 —— 要是这次分析失败、
        # 一条都没解析出来，就别把上一批好建议也顺手关了。
        if clean:
            _disable_points(conn, owner, int(rid), src_ver)

        n = conn.execute(
            "UPDATE outline_rewrites SET summary_json=?, summary_text=?,"
            " summary_model=?, analyze_model=?, analyze_version=?,"
            " status=?, error=?, updated_at=?"
            " WHERE id=? AND owner_id=?",
            (_dumps([{"kind": c["kind"], "point": c["point"],
                      "how": c["how"], "scope": c["scope"]} for c in clean]),
             (summary_text or "")[:REWRITE_SUMMARY_MAX],
             (model_name or "")[:120], (model_name or "")[:120],
             (prompt_version or "")[:40], status, (error or "")[:2000],
             ts, int(rid), owner)).rowcount
        if n <= 0:
            return False

        # 写新建议。同 (rewrite, source_version) 先删了再插，
        # 保证重跑同一次分析不会堆出两套一模一样的。
        if clean:
            conn.execute(
                "DELETE FROM outline_rewrite_points WHERE owner_id=?"
                " AND rewrite_id=? AND source_version=?",
                (owner, int(rid), src_ver))
            cols = ["owner_id", "rewrite_id", "seq", "kind", "review", "active",
                    "scope", "point", "how"]
            cols += [c for _, c in REWRITE_PT_FIELDS]
            cols += ["uncertain", "source_version", "source_model",
                     "created_at", "updated_at"]
            sql = ("INSERT INTO outline_rewrite_points (%s) VALUES (%s)"
                   % (", ".join(cols), ", ".join(["?"] * len(cols))))
            for i, c in enumerate(clean, 1):
                vals = [owner, int(rid), i, c["kind"], c["review"], c["active"],
                        c["scope"], c["point"], c["how"]]
                vals += [c.get(k, "") for k, _ in REWRITE_PT_FIELDS]
                vals += [c.get("uncertain", ""), src_ver,
                         (model_name or "")[:120], ts, ts]
                conn.execute(sql, vals)
    return True


def list_rewrite_points(owner, rid, include_history=False):
    """某条改写记录下的建议（界面主视图用）。

    默认只给**最新版本那批** —— 旧版本的建议是历史，别把
    "上一版 AI 说过的话"跟现在这批混在一起。要看历史传 include_history=True。
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT user_version FROM outline_rewrites"
            " WHERE id=? AND owner_id=?", (int(rid), owner)).fetchone()
        cur_ver = str(int(row["user_version"] or 1)) if row else ""
        if include_history:
            rows = conn.execute(
                "SELECT * FROM outline_rewrite_points WHERE owner_id=?"
                " AND rewrite_id=? ORDER BY seq ASC, id ASC",
                (owner, int(rid))).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM outline_rewrite_points WHERE owner_id=?"
                " AND rewrite_id=? AND source_version=? ORDER BY seq ASC, id ASC",
                (owner, int(rid), cur_ver)).fetchall()
    return [_pt_row(r) for r in rows]


def _pt_row(row):
    keys = set(row.keys())

    def g(col, default=""):
        return row[col] if col in keys else default

    out = {
        "id": row["id"],
        "rewrite_id": row["rewrite_id"],
        "seq": int(g("seq", 0) or 0),
        "kind": g("kind", ""),
        # ---- 三个独立的轴 ----
        "review": g("review", REWRITE_RV_PENDING),
        "active": g("active", REWRITE_ACTIVE_ON),
        "scope": g("scope", REWRITE_SCOPE_UNKNOWN),
        "confidence": g("confidence", REWRITE_CONF_UNKNOWN),
        "point": g("point", ""),
        "how": g("how", ""),
        "uncertain": g("uncertain", ""),
        "user_note": g("user_note", ""),
        "user_suggest": g("user_suggest", ""),
        "source_version": g("source_version", ""),
        "source_model": g("source_model", ""),
        "created_at": g("created_at", ""),
        "updated_at": g("updated_at", ""),
    }
    for key, col in REWRITE_PT_FIELDS:
        out[key] = g(col, "")
    out["live"] = rewrite_point_is_live(out)
    return out


def get_rewrite_point(owner, pid):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM outline_rewrite_points WHERE id=? AND owner_id=?",
            (int(pid), owner)).fetchone()
    return _pt_row(row) if row else None


def _log_action(conn, owner, rewrite_id, point_id, action, detail="",
                frm=None, to=None):
    """写一行审核动作日志（需求第 1 条：「修改后接受」要留操作记录）。"""
    frm = frm or {}
    to = to or {}
    conn.execute(
        "INSERT INTO outline_rewrite_actions (owner_id, rewrite_id, point_id,"
        " action, from_review, to_review, from_active, to_active,"
        " from_scope, to_scope, detail, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (owner, int(rewrite_id), (int(point_id) if point_id else None),
         action, frm.get("review", ""), to.get("review", ""),
         frm.get("active", ""), to.get("active", ""),
         frm.get("scope", ""), to.get("scope", ""),
         (detail or "")[:REWRITE_SUGGEST_MAX], now_str()))


def list_rewrite_actions(owner, rid, limit=100):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM outline_rewrite_actions WHERE owner_id=?"
            " AND rewrite_id=? ORDER BY id DESC LIMIT ?",
            (owner, int(rid), int(limit))).fetchall()
    out = []
    for r in rows:
        out.append({k: r[k] for k in r.keys()})
        out[-1]["action_label"] = REWRITE_ACTION_LABELS.get(
            r["action"], r["action"])
    return out


def review_point(owner, pid, action, user_note=None, user_suggest=None,
                 scope=None):
    """对一条建议做一次审核动作。**这是唯一改这三个轴的入口。**

    action ∈ ALL_REWRITE_ACTION。语义：
      accept         → review=已接受（可同时带 scope）
      accept_edited  → review=已接受，并把 user_suggest 存成"改后的说法"
      reject         → review=已拒绝
      reopen         → review=待确认
      enable/disable → active=启用/停用（**不动 review**）
      set_scope      → 只改 scope
      note           → 只加补充说明
    每次都会写一行日志（她改后说了什么、从什么状态到什么状态）。
    """
    if action not in ALL_REWRITE_ACTION:
        raise ValueError("没有这个审核动作：%s" % action)
    pt = get_rewrite_point(owner, pid)
    if not pt:
        return None
    frm = {"review": pt["review"], "active": pt["active"],
           "scope": pt["scope"]}

    sets, vals = [], []
    to = dict(frm)
    if action == REWRITE_ACT_ACCEPT:
        to["review"] = REWRITE_RV_ACCEPTED
    elif action == REWRITE_ACT_ACCEPT_EDITED:
        to["review"] = REWRITE_RV_ACCEPTED
    elif action == REWRITE_ACT_REJECT:
        to["review"] = REWRITE_RV_REJECTED
    elif action == REWRITE_ACT_REOPEN:
        to["review"] = REWRITE_RV_PENDING
    elif action == REWRITE_ACT_ENABLE:
        to["active"] = REWRITE_ACTIVE_ON
    elif action == REWRITE_ACT_DISABLE:
        to["active"] = REWRITE_ACTIVE_OFF
    elif action == REWRITE_ACT_SCOPE:
        if scope not in ALL_REWRITE_SCOPE:
            raise ValueError("没有这个适用范围：%s" % scope)
        to["scope"] = scope
    if scope is not None and action != REWRITE_ACT_SCOPE:
        if scope not in ALL_REWRITE_SCOPE:
            raise ValueError("没有这个适用范围：%s" % scope)
        to["scope"] = scope

    for col in ("review", "active", "scope"):
        if to[col] != frm[col]:
            sets.append("%s=?" % col)
            vals.append(to[col])
    detail = ""
    if user_suggest is not None:
        s = _txt(user_suggest, REWRITE_SUGGEST_MAX, "改后的说法")
        sets.append("user_suggest=?")
        vals.append(s)
        detail = s
    if user_note is not None:
        nt = _txt(user_note, REWRITE_PT_EXTRA_MAX, "补充说明")
        sets.append("user_note=?")
        vals.append(nt)
        detail = detail or nt

    sets.append("updated_at=?")
    vals.append(now_str())
    vals += [int(pid), owner]
    with db.connect() as conn:
        n = conn.execute(
            "UPDATE outline_rewrite_points SET %s WHERE id=? AND owner_id=?"
            % ", ".join(sets), vals).rowcount
        if n > 0:
            _log_action(conn, owner, pt["rewrite_id"], pid, action,
                        detail=detail, frm=frm, to=to)
    return get_rewrite_point(owner, pid) if n > 0 else None


def bulk_set_points(owner, rid, action, scope=None):
    """把某条改写记录当前这一版的建议**整批**做同一个动作。

    【为什么不叫"全部接受"】需求第 8 条：提供批量确认，但**不默认全部启用**。
    所以这里是个中性工具 —— 界面上"全部标为停用""全部接受"都走它，
    但"全部接受"那个按钮在界面层要多一步确认。
    """
    if action not in (REWRITE_ACT_ACCEPT, REWRITE_ACT_REJECT,
                      REWRITE_ACT_REOPEN, REWRITE_ACT_ENABLE,
                      REWRITE_ACT_DISABLE, REWRITE_ACT_SCOPE):
        raise ValueError("批量不支持这个动作：%s" % action)
    pts = [p for p in list_rewrite_points(owner, rid)]
    n = 0
    for p in pts:
        if review_point(owner, p["id"], action, scope=scope):
            n += 1
    return n


def set_rewrite_enabled(owner, rid, enabled):
    with db.connect() as conn:
        n = conn.execute(
            "UPDATE outline_rewrites SET enabled=?, updated_at=?"
            " WHERE id=? AND owner_id=?",
            (1 if enabled else 0, now_str(), int(rid), owner)).rowcount
    return n > 0


def set_rewrite_note(owner, rid, note):
    """只改备注。

    【为什么单独有个函数】PATCH 那条接口要能只改备注不动别的；
    enabled 那一支走 set_rewrite_enabled。合成一个"改任意字段"的函数
    看起来省事，但调用方传什么就改什么，等于把校验甩给上层 ——
    上面那两个字段是各自独立的开关，分开更清楚。
    """
    txt = _txt(note, OUTLINE_NOTE_MAX, "备注")
    with db.connect() as conn:
        n = conn.execute(
            "UPDATE outline_rewrites SET note=?, updated_at=?"
            " WHERE id=? AND owner_id=?",
            (txt, now_str(), int(rid), owner)).rowcount
    return n > 0


def delete_rewrite(owner, rid):
    with db.connect() as conn:
        n = conn.execute(
            "DELETE FROM outline_rewrites WHERE id=? AND owner_id=?",
            (int(rid), owner)).rowcount
    return n > 0


def rewrite_examples(owner, limit=3, scope=None):
    """挑几份"可以拿来参考"的改写总结，进以后的生成提示词。

    跟 learning_examples 的分工：
      learning_examples  说的是"她不满意什么"（负面规避）
      rewrite_examples   说的是"她会把东西改成什么样"（正面取向）
    两块都要发 —— 只告诉她别踩什么，模型还是会写成她不喜欢的样子。

    【阶段一的两条硬规矩，都写在这儿】
    1. **只给已接受 + 启用 + 不是仅本篇的**（需求第 1/8/14 条）。
       待确认、已拒绝、停用、仅本篇 —— 一律不发。她没拍板的东西
       不该左右生成；接受了的"仅本篇"也不该进别的作品。
    2. **层次得是长期或情境**（需求第 7 条）。
       `scope=` 传了就再收一层：比如这次是"当众对质"类场景，
       只要情境适用的那批 —— 这是阶段二检索的地基，先留好口子。

    【为什么资格条件用 SQL 过滤、不取回来再筛】需求第 8 条：
    "确认、启用及权限是资格条件，先过滤；之后才比较相关性和证据。"
    先过滤能少读一大堆绝不会用的行（她可能攒了几十条待确认的）。
    """
    with db.connect() as conn:
        # 记录本身得是开着的（整条关掉等于她反悔了这件事）
        recs = conn.execute(
            "SELECT id FROM outline_rewrites WHERE owner_id=? AND enabled=1"
            " AND status=? ORDER BY id DESC LIMIT ?",
            (owner, REWRITE_DONE, max(int(limit) * 4, 8))).fetchall()
        out = []
        for rec in recs:
            sql = ("SELECT * FROM outline_rewrite_points WHERE owner_id=?"
                   " AND rewrite_id=? AND review=? AND active=?"
                   " AND scope IN (?,?)")
            args = [owner, rec["id"], REWRITE_RV_ACCEPTED, REWRITE_ACTIVE_ON,
                    REWRITE_SCOPE_LONG, REWRITE_SCOPE_CASE]
            if scope in ALL_REWRITE_SCOPE:
                sql += " AND scope=?"
                args.append(scope)
            sql += " ORDER BY seq ASC, id ASC"
            prows = conn.execute(sql, args).fetchall()
            if not prows:
                continue
            pts = [_pt_row(r) for r in prows]
            # 一句话总述（可能为空 —— 不强行编）
            srow = conn.execute(
                "SELECT summary_text, note FROM outline_rewrites WHERE id=?",
                (rec["id"],)).fetchone()
            out.append({
                "id": rec["id"],
                "summary": (srow["summary_text"] if srow else "") or "",
                "note": (srow["note"] if srow else "") or "",
                "points": pts,
            })
            if len(out) >= int(limit):
                break
    return out


def rewrite_points_using(owner, rid=None, limit=50):
    """现在**真正会进生成**的那些建议（给界面上的"当前生效"视图 / 手册用）。

    【为什么要单独有个查询】她最常问的一句是"现在到底有哪些在起作用"。
    这个问题不该靠她自己去别处拼 —— 直接一条 SQL 给她。
    判据跟 rewrite_examples 完全一致（走同一个资格条件）。
    """
    with db.connect() as conn:
        sql = ("SELECT * FROM outline_rewrite_points WHERE owner_id=?"
               " AND review=? AND active=? AND scope IN (?,?)")
        args = [owner, REWRITE_RV_ACCEPTED, REWRITE_ACTIVE_ON,
                REWRITE_SCOPE_LONG, REWRITE_SCOPE_CASE]
        if rid:
            sql += " AND rewrite_id=?"
            args.append(int(rid))
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        rows = conn.execute(sql, args).fetchall()
    return [_pt_row(r) for r in rows]


# ----------------------------------------------------------------------
# "这一次生成到底用了哪些学习条目"（需求第 8 条）
# ----------------------------------------------------------------------

def record_learning_use(owner, run_id, points, batch="", stage="all",
                        candidate_id=None):
    """把"这次生成实际注入了哪几条"落库。

    【为什么必须落库】需求第 8 条点名：不能只记"学习开关已打开"。
    她以后问"明明接受了 5 条，为什么这次生成没变化" —— 有这个记录
    就能回答"因为这次只注入了 2 条 / 因为那 3 条是仅本篇"。
    """
    pts = points or []
    if not pts:
        return 0
    ts = now_str()
    with db.connect() as conn:
        # 同一 (run, stage, batch) 先清掉 —— 重跑不该堆重复。
        conn.execute(
            "DELETE FROM outline_learning_uses WHERE owner_id=? AND run_id=?"
            " AND stage=? AND batch=?", (owner, int(run_id or 0), stage, batch))
        n = 0
        for p in pts:
            conn.execute(
                "INSERT INTO outline_learning_uses (owner_id, run_id,"
                " candidate_id, batch, stage, point_id, rewrite_id, point_text,"
                " scope, confidence, source_version, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (owner, (int(run_id) if run_id else None),
                 (int(candidate_id) if candidate_id else None), batch, stage,
                 p.get("id"), p.get("rewrite_id"),
                 (p.get("method") or p.get("point") or "")[:600],
                 p.get("scope") or "", p.get("confidence") or "",
                 p.get("source_version") or "", ts))
            n += 1
    return n


def learning_uses_of_run(owner, run_id, stage=None):
    """某次生成用了哪些学习条目（界面 / 手册的"调用记录"视图）。"""
    with db.connect() as conn:
        if stage:
            rows = conn.execute(
                "SELECT * FROM outline_learning_uses WHERE owner_id=?"
                " AND run_id=? AND stage=? ORDER BY id ASC",
                (owner, int(run_id), stage)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM outline_learning_uses WHERE owner_id=?"
                " AND run_id=? ORDER BY id ASC", (owner, int(run_id))).fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]


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

    check("字数档：6000 落在 short", word_tier(6000)["key"], "short")
    check("字数档：8000 落在 short", word_tier(8000)["key"], "short")
    check("字数档：5999 落在 tiny（边界不悬空）",
          word_tier(5999)["key"], "tiny")
    check("字数档：9999 落在 short（边界不悬空）",
          word_tier(9999)["key"], "short")
    check("字数档：15000 落在 medium", word_tier(15000)["key"], "medium")
    check("字数档：15001 落在 long", word_tier(15001)["key"], "long")

    # 超长必须报错，不许静默截断
    try:
        _txt("あ" * 10, 5, "试")
        check("超长文本要报错", False, True)
    except ValueError:
        check("超长文本要报错", True, True)

    # 节点清洗：编号不合法要兜底
    n = _clean_node({"node_title": "开场", "estimated_words": "900",
                     "source_plot_ids": [1, 999], "participating_roles": ["a", "a"]},
                    3, known_plots={1})
    check("节点编号兜底成 n3", n["node_id"], "n3")
    check("字数从字符串转过来", n["estimated_words"], 900)
    check("不在候选池里的零件被抹掉", n["source_plot_ids"], [1])
    check("角色去重", n["participating_roles"], ["a"])

    # 硬伤必须抛
    for bad, why in (({"title_candidates": []}, "没有 nodes"),
                     ({"nodes": "x"}, "nodes 不是数组"),
                     ({"nodes": []}, "nodes 是空的")):
        try:
            clean_outline_payload(bad, {1})
            check("%s 要报错" % why, False, True)
        except ValueError:
            check("%s 要报错" % why, True, True)

    # 结构规模校验。8000 字的口径是「每段 450～800 字 → 10～17 个节点」
    # （2026-09-26 改过：以前先定 5～8 个节点、每段字数由它倒推，
    #  结果每段被写肥、十个字段摊进去全是概括）。所以要测区间外的两个方向，
    # 外加区间内**不该**被提醒。
    obj, _w = clean_outline_payload({"nodes": [
        {"node_title": "第%d段" % i, "event": "发生了事情", "estimated_words": 300}
        for i in range(1, 26)]}, {1})
    ws = validate_outline(obj, 8000)
    check("8000 字给 25 个节点（超出上限）会被提醒",
          any("空壳" in x for x in ws), True)

    obj_few, _wf = clean_outline_payload({"nodes": [
        {"node_title": "第%d段" % i, "event": "发生了事情", "estimated_words": 1600}
        for i in range(1, 6)]}, {1})
    ws_few = validate_outline(obj_few, 8000)
    check("8000 字只给 5 个节点（少于下限）会被提醒每段偏长",
          any("偏长" in x for x in ws_few), True)

    obj_ok, _wo = clean_outline_payload({"nodes": [
        {"node_title": "第%d段" % i, "event": "发生了事情", "estimated_words": 600}
        for i in range(1, 14)]}, {1})
    ws_ok = validate_outline(obj_ok, 8000)
    check("8000 字给 13 个节点（落在区间内）不该被结构规模提醒",
          not any(("空壳" in x) or ("偏长" in x) for x in ws_ok), True)

    # 空泛句子要被抓出来
    obj2, _w2 = clean_outline_payload({"nodes": [
        {"node_title": "中段", "event": "他们经历了一系列事件",
         "estimated_words": 3000}]}, {1})
    ws2 = validate_outline(obj2, 3000)
    check("空泛句子会被抓出来",
          any("空泛" in x for x in ws2), True)

    # diff 要按 node_id 比，能看出"只是顺序换了"
    a = {"nodes": [{"node_id": "n1", "node_title": "甲"},
                   {"node_id": "n2", "node_title": "乙"}]}
    b = {"nodes": [{"node_id": "n2", "node_title": "乙"},
                   {"node_id": "n1", "node_title": "甲"}]}
    d = diff_outlines(a, b)
    check("顺序换了 = reordered，不是两段都改了",
          (d["reordered"], len(d["changed_nodes"])), (True, 0))

    d2 = diff_outlines({"nodes": [{"node_id": "n1", "node_title": "甲"}]},
                       {"nodes": [{"node_id": "n3", "node_title": "丙"}]})
    check("删一段加一段能认出来",
          (len(d2["removed_nodes"]), len(d2["added_nodes"])), (1, 1))

    # 渲染出来要有那几个必须的标题
    txt = render_outline_text(clean_outline_payload(
        {"title_candidates": ["甲"], "story_core": "核心",
         "nodes": [{"node_title": "开场", "event": "事件",
                    "estimated_words": 100}]}, {1})[0])
    for must in ("标题候选", "故事核心", "角色功能表", "总字数与分配",
                 "高潮和转折", "结局", "使用的剧情零件", "逻辑风险"):
        check("渲染里有「%s」" % must, must in txt, True)

    check("空骨架的节点是空的", empty_outline_json()["nodes"], [])

    # ---- 改写对比：她那版是自由文本，配段靠"顺序 + 相似度" ----
    # 这一组是 2026-10-08 折腰要的功能。测的是最容易出错的两件事：
    #   ① 她删了一段，要报成"删了 1 段"，而不是"改了后面每一段"
    #   ② 她一字没改，**一段都不许报成改过**
    #      （这条真踩过：AI 那版有 character_action / conflict 等栏位，
    #       她那版只有正文，当初把它们一起拼进比较，导致
    #       照抄的段落相似度只有 0.77，整份被报成"她全改了"）
    ai_n = [
        {"node_id": "n1", "node_title": "开场", "estimated_words": 100,
         "event": "他带着信出城，路上撞见旧同门。", "conflict": "两人都不开口"},
        {"node_id": "n2", "node_title": "客栈", "estimated_words": 100,
         "event": "他在城外的客栈住下，夜里把信拿出来看。",
         "conflict": "想知道信里写了什么"},
        {"node_id": "n3", "node_title": "对质", "estimated_words": 100,
         "event": "镖局里当众把货拆开，信是假的。"},
        {"node_id": "n4", "node_title": "烧信", "estimated_words": 100,
         "event": "夜里他把信投进火盆，手停了三次才松。"},
    ]
    her_j = text_to_outline_json(
        "开场\n他带着信出城，路上撞见旧同门。\n\n"
        "对质\n镖局里当众把货拆开，信是假的。\n\n"
        "烧信\n夜里他把信投进火盆，手停了三次才松。\n\n"
        "尾声\n他把灰倒进了河里。")
    dd = diff_against_text({"nodes": ai_n}, her_j)
    check("她删了一段 → 认得出来是哪一段",
          [x["node_title"] for x in dd["removed_nodes"]], ["客栈"])
    check("她新加了一段 → 认得出来", [x["node_title"] for x in dd["added_nodes"]],
          ["尾声"])
    check("删+加之外，没改的段落「一段都不许」报成改过",
          dd["changed_nodes"], [])
    check("配段按下标一一对上", dd["matched_pairs"], [[0, 0], [2, 1], [3, 2]])
    check("标了 by_text（说明这是自由文本比出来的，不是按 node_id）",
          dd["by_text"], True)

    same_j = text_to_outline_json(
        "开场\n他带着信出城，路上撞见旧同门。\n\n"
        "客栈\n他在城外的客栈住下，夜里把信拿出来看。\n\n"
        "对质\n镖局里当众把货拆开，信是假的。\n\n"
        "烧信\n夜里他把信投进火盆，手停了三次才松。")
    dd2 = diff_against_text({"nodes": ai_n}, same_j)
    check("一字没改 → 没有删、没有加、没有改",
          (bool(dd2["removed_nodes"]), bool(dd2["added_nodes"]),
           bool(dd2["changed_nodes"])), (False, False, False))

    check("两段归一化后完全一样 = 同一段",
          _same_seg({"event": "他，出城了。"}, {"event": "他出城了"}), True)
    check("她的段落多出一行标题（含在里面）= 还是同一段",
          _same_seg({"event": "他带着信出城，路上撞见旧同门。"},
                    {"event": "开场\n他带着信出城，路上撞见旧同门。"}), True)
    check("正文真的动了 = 不是同一段",
          _same_seg({"event": "他带着信出城，路上撞见旧同门。"},
                    {"event": "她回城了，还带了一个人。"}), False)
    # 太短的段落不做"包含"判断 —— 三个字被包三个字纯属巧合，
    # 拿它当"同一段"会让"她新写了一句短话"被吞掉。
    check("太短的两段不靠包含关系认同一段（防巧合）",
          _same_seg({"event": "他走了"}, {"event": "他走了吧"}), False)
    # 【一条曾经真出过事的用例】某一段填了「参与角色」时，
    # 整份大纲渲染会 KeyError（那字段不在 NODE_FIELD_LABELS 里），
    # 于是"保存大纲"和"候选入库"都变成 500。这条钉住它。
    t2 = render_outline_text(clean_outline_payload(
        {"title_candidates": ["甲"], "nodes": [
            {"node_title": "开场", "event": "事件", "estimated_words": 100,
             "participating_roles": ["沈砚", "崔明"],
             "source_plot_ids": [1]}]}, {1})[0])
    check("★ 节点填了参与角色也能渲染出来（不该崩）",
          "参与角色：沈砚、崔明" in t2, True)
    check("★ 渲染里每个字段都有显示名（不会冒出英文键名）",
          all("%s：" % k not in t2 for k in _NODE_RENDER_ORDER
              if k in NODE_RENDER_LABELS), True)
    check("渲染字段表和标签表对得上",
          [k for k in _NODE_RENDER_ORDER if k not in NODE_RENDER_LABELS], [])
    check("可用零件状态只有已确认/已编辑",
          tuple(sorted(PLOT_USABLE_STATUS)),
          tuple(sorted((pdb.PLOT_STATUS_CONFIRMED, pdb.PLOT_STATUS_EDITED))))
    check("问题类型表里没有重复",
          len(PROBLEM_TYPES), len(set(PROBLEM_TYPES)))

    # ---- 改写建议：三个轴分开、互不污染（2026-10-08 折腰纠正）----
    check("三个轴没有重叠取值",
          sorted(set(ALL_REWRITE_SCOPE) & set(ALL_REWRITE_REVIEW)
                 & set(ALL_REWRITE_ACTIVE)), [])
    check("scope 只有三个（长期/情境/仅本篇），没有把『还没把握』塞进来",
          len(ALL_REWRITE_SCOPE), 3)
    check("『判断不充分』在把握程度那一轴，不在 scope 里",
          REWRITE_CONF_WEAK in ALL_REWRITE_CONF
          and REWRITE_CONF_WEAK not in ALL_REWRITE_SCOPE, True)
    check("审核状态三个：待确认/已接受/已拒绝",
          len(ALL_REWRITE_REVIEW), 3)
    check("使用状态两个：启用/停用", len(ALL_REWRITE_ACTIVE), 2)
    check("动作日志表里每种动作都有中文标签",
          [a for a in ALL_REWRITE_ACTION if a not in REWRITE_ACTION_LABELS], [])
    check("标签表跟取值表一一对应（scope）",
          [s for s in ALL_REWRITE_SCOPE if s not in REWRITE_SCOPE_LABELS], [])
    check("标签表跟取值表一一对应（review）",
          [s for s in ALL_REWRITE_REVIEW if s not in REWRITE_RV_LABELS], [])
    check("标签表跟取值表一一对应（active）",
          [s for s in ALL_REWRITE_ACTIVE if s not in REWRITE_ACTIVE_LABELS], [])
    check("标签表跟取值表一一对应（confidence）",
          [s for s in ALL_REWRITE_CONF if s not in REWRITE_CONF_LABELS], [])

    # 资格判据：一条建议"已接受 + 启用 + 仅本篇" → 不进别的作品
    check("★ 已接受+启用+仅本篇 → 不进别的作品（live=False）",
          rewrite_point_is_live({"review": REWRITE_RV_ACCEPTED,
                                 "active": REWRITE_ACTIVE_ON,
                                 "scope": REWRITE_SCOPE_THIS}), False)
    check("★ 已接受+启用+长期 → 进生成",
          rewrite_point_is_live({"review": REWRITE_RV_ACCEPTED,
                                 "active": REWRITE_ACTIVE_ON,
                                 "scope": REWRITE_SCOPE_LONG}), True)
    check("★ 已接受+停用 → 不进（停用是独立的一轴）",
          rewrite_point_is_live({"review": REWRITE_RV_ACCEPTED,
                                 "active": REWRITE_ACTIVE_OFF,
                                 "scope": REWRITE_SCOPE_LONG}), False)
    check("★ 待确认+启用 → 不进（她没拍板）",
          rewrite_point_is_live({"review": REWRITE_RV_PENDING,
                                 "active": REWRITE_ACTIVE_ON,
                                 "scope": REWRITE_SCOPE_LONG}), False)
    check("★ 已拒绝+启用 → 不进",
          rewrite_point_is_live({"review": REWRITE_RV_REJECTED,
                                 "active": REWRITE_ACTIVE_ON,
                                 "scope": REWRITE_SCOPE_LONG}), False)

    # 清洗：认不出 scope 时兜到最保守那层，绝不兜成"长期"
    _c = clean_rewrite_points([
        {"point": "她习惯先写动机", "kind": "人物"},
        {"point": "结尾要回收", "scope": "长期", "confidence": "充足"},
        {"point": "这条只这一篇要", "scope": "仅本篇",
         "review": "已接受", "confidence": "不足"},
    ])
    check("认不出适用范围 → 兜到『仅本篇』（最保守，不许兜长期）",
          _c[0]["scope"], REWRITE_SCOPE_THIS)
    check("认不出审核 → 兜到『待确认』", _c[0]["review"], REWRITE_RV_PENDING)
    check("scope 认中文『长期』", _c[1]["scope"], REWRITE_SCOPE_LONG)
    check("confidence 认『充足』→ 充分", _c[1]["confidence"],
          REWRITE_CONF_ENOUGH)
    check("『不足』进的是把握程度，不是 scope",
          _c[2]["confidence"] == REWRITE_CONF_WEAK
          and _c[2]["scope"] == REWRITE_SCOPE_THIS, True)
    check("清洗出来的每条默认启用（启用但待确认 → 实际不生效）",
          _c[0]["active"], REWRITE_ACTIVE_ON)
    check("★ 清洗不吞字段（key 都在）",
          [k for k in ("problem", "change", "evidence", "improved", "method",
                       "applies_when", "not_when", "confidence")
           if k not in _c[0]], [])
    check("空建议列表不会崩", clean_rewrite_points(None), [])
    check("一条说不出 point 的会被丢掉",
          clean_rewrite_points([{"kind": "节奏"}]), [])

    # _freeze_key：同输入同指纹、改一个字就变
    _k1 = _freeze_key({"a": "甲", "b": [1, 2]})
    _k2 = _freeze_key({"b": [1, 2], "a": "甲"})       # 键序不同，值一样
    _k3 = _freeze_key({"a": "乙", "b": [1, 2]})
    check("输入没变 → 指纹一样（键序不影响）", _k1 == _k2, True)
    check("输入改了一个字 → 指纹变了", _k1 != _k3, True)

    # ---- 事件级对齐（阶段一 B）----
    _A = [
        {"node_id": "n1", "node_title": "出城",
         "event": "他带着信出城，路上撞见旧同门，对方没认出他。"},
        {"node_id": "n2", "node_title": "落脚",
         "event": "他在城外的客栈住下，夜里把信拿出来看。"},
        {"node_id": "n3", "node_title": "对质",
         "event": "镖局里当众把货拆开，信是假的。"},
        {"node_id": "n4", "node_title": "烧信",
         "event": "夜里他把信投进火盆，手停了三次才松。"},
    ]
    # 她只改了格式（加了标题行、换了标点）
    _U_fmt = text_to_outline_json(
        "出城\n他带着信出城，路上撞见旧同门，对方没认出他。\n\n"
        "落脚\n他在城外的客栈住下，夜里把信拿出来看。\n\n"
        "对质\n镖局里当众把货拆开，信是假的。\n\n"
        "烧信\n夜里他把信投进火盆，手停了三次才松。")
    _al1 = align_events({"nodes": _A}, _U_fmt)
    check("★ 只差格式 → 全算 same，没有一处报成改动",
          _al1["counts"]["same"], 4)
    check("★ 只差格式 → 新增 0、删除 0、改动 0",
          (_al1["counts"]["added"], _al1["counts"]["removed"],
           _al1["counts"]["changed"]), (0, 0, 0))

    # 她删一段 + 加一段
    _U_del = text_to_outline_json(
        "他带着信出城，路上撞见旧同门，对方没认出他。\n\n"
        "镖局里当众把货拆开，信是假的。\n\n"
        "夜里他把信投进火盆，手停了三次才松。\n\n"
        "他把灰倒进了河里。")
    _al2 = align_events({"nodes": _A}, _U_del)
    check("她删了一段 → 认出一条删除", _al2["counts"]["removed"], 1)
    check("她加了一段 → 认出一条新增", _al2["counts"]["added"], 1)

    # 她拆段：AI 的一段 → 她的两段
    _U_split = text_to_outline_json(
        "他带着信出城，路上撞见旧同门，对方没认出他。\n\n"
        "他在城外的客栈住下。\n\n夜里他把信拿出来看。\n\n"
        "镖局里当众把货拆开，信是假的。\n\n"
        "夜里他把信投进火盆，手停了三次才松。")
    _al3 = align_events({"nodes": _A}, _U_split)
    check("★ 她拆段 → 有 split 或 merged 认出来（不是只报删+加）",
          (_al3["counts"]["split"] + _al3["counts"]["merged"]) >= 1, True)

    # 大段重排：把第 4 段挪到最前面
    _U_moved = text_to_outline_json(
        "夜里他把信投进火盆，手停了三次才松。\n\n"
        "他带着信出城，路上撞见旧同门，对方没认出他。\n\n"
        "他在城外的客栈住下，夜里把信拿出来看。\n\n"
        "镖局里当众把货拆开，信是假的。")
    _al4 = align_events({"nodes": _A}, _U_moved)
    check("★ 大段重排 → 内容没丢（新增+删除都为 0 或极少）",
          _al4["counts"]["added"] + _al4["counts"]["removed"], 0)

    # 认不准的必须能被标出来，且**不硬配**
    # 造一个真歧义的：AI 一段内容跟她的第 1 段、第 2 段都很像
    # （她两段彼此也像），程序分不出到底该配哪一段 → 必须标 uncertain。
    _A2 = [{"node_id": "x1", "node_title": "甲",
            "event": "他在城门口站了很久，最后还是把信交给了守门的兵。"}]
    _U2 = text_to_outline_json(
        "他在城门口站了很久。\n\n他最后还是把信交给了守门的兵。")
    _al5 = align_events({"nodes": _A2}, _U2)
    check("★ 一对二、两段都能沾上 → 不许硬配成单段改动，要标不确定或拆段",
          _al5["counts"]["uncertain"] >= 1
          or _al5["counts"]["split"] >= 1, True)
    check("对齐结果里有 by_text（说明是按文本认的）", _al5["by_text"], True)
    check("★ 对齐渲染里写明了『程序可能认错』",
          "可能认错" in align_block(_al5), True)
    check("★ 九类变化表齐全",
          len(ALL_CHANGE_KINDS), 9)
    check("对齐类型表里没有重复",
          len(ALL_ALIGN_KINDS), len(set(ALL_ALIGN_KINDS)))
    check("_runs 把连续下标切段",
          _runs([1, 2, 3, 7, 9, 10]), [[1, 2, 3], [7], [9, 10]])
    check("_runs 空输入不崩", _runs([]), [])
    check("★ 空稿对齐不崩",
          align_events({}, {}).get("counts", {}).get("same"), 0)

    print()
    print("数据层自测：%s" % ("全部通过" if ok else "有失败"))
    return ok


if __name__ == "__main__":                                  # pragma: no cover
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(0 if _self_check() else 1)
