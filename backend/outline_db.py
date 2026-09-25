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
from datetime import datetime

from backend import db
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
    {"key": "tiny",   "min": 0,     "max": 5999,  "nodes": (3, 5),
     "label": "5000 字上下", "hint": "单一核心冲突，角色少，结局收束快"},
    {"key": "short",  "min": 6000,  "max": 9999,  "nodes": (5, 8),
     "label": "6000～9000 字", "hint": "完整起承转合，至少一次明显转折"},
    {"key": "medium", "min": 10000, "max": 15000, "nodes": (7, 12),
     "label": "10000～15000 字", "hint": "冲突和关系变化更充分，可以有副冲突"},
    {"key": "long",   "min": 15001, "max": 10 ** 9, "nodes": (10, 14),
     "label": "15000 字以上", "hint": "已接近中短篇，动手前建议再确认一次"},
)

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


def migrate(verbose=False):
    """建这七张表。反复跑是安全的（全是 IF NOT EXISTS）。

    【这一轮为什么不需要整库备份】
    七张全是新增的空表，一个 ALTER 都没有、一行已有数据都不碰。
    所以幂等执行即可 —— 热重载触发多少次都一样。
    """
    with db.connect() as conn:
        conn.executescript(SCHEMA)
    if verbose:
        print("大纲生成：七张表就位 →", db.DB_PATH)
    return db.DB_PATH


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


def _txt(v, limit, name, allow_empty=True):
    """一段纯文本：去两端空白、卡长度。超了直接报错，不静默截断。"""
    s = (v if isinstance(v, str) else ("" if v is None else str(v))).strip()
    if len(s) > limit:
        raise ValueError("%s最长 %d 个字，现在有 %d 个。" % (name, limit, len(s)))
    if not s and not allow_empty:
        raise ValueError("%s不能空着。" % name)
    return s


def _str_list(v, limit, item_max, name):
    """一组字符串：去空白、去重、保序、逐个卡长度。"""
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple)):
        raise ValueError("%s得是一组文字。" % name)
    out = []
    for x in v:
        s = str(x or "").strip()
        if not s:
            continue
        if len(s) > item_max:
            raise ValueError("%s里的「%s…」太长了（最长 %d 字）。"
                             % (name, s[:12], item_max))
        if s not in out:
            out.append(s)
    if len(out) > limit:
        raise ValueError("%s最多 %d 条，现在给了 %d 条。" % (name, limit, len(out)))
    return out


def _int_list(v, limit, name):
    """一组整数 id（来源零件、角色 id 之类）。非法值直接报错。"""
    if v is None:
        return []
    if isinstance(v, (str, int)):
        v = [v]
    if not isinstance(v, (list, tuple)):
        raise ValueError("%s得是一组编号。" % name)
    out = []
    for x in v:
        if x in (None, ""):
            continue
        try:
            n = int(x)
        except (TypeError, ValueError):
            raise ValueError("%s里有不是编号的东西：「%s」" % (name, x))
        if n and n not in out:
            out.append(n)
    if len(out) > limit:
        raise ValueError("%s最多 %d 个，现在给了 %d 个。" % (name, limit, len(out)))
    return out


def _has_table(conn, name):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone())


# ----------------------------------------------------------------------
# 预期字数 → 结构规模
# ----------------------------------------------------------------------

def word_tier(words):
    """这个字数属于哪一档。返回 WORD_TIERS 里的一条（字典副本）。"""
    try:
        n = int(words or 0)
    except (TypeError, ValueError):
        n = 0
    for t in WORD_TIERS:
        if t["min"] <= n <= t["max"]:
            return dict(t)
    return dict(WORD_TIERS[0])


def tier_of_key(key):
    for t in WORD_TIERS:
        if t["key"] == key:
            return dict(t)
    return None


def word_budget_hint(words):
    """给一句"这一段大概该写多长"的提示（均匀分配，仅供参考）。"""
    t = word_tier(words)
    try:
        n = int(words or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return ""
    lo, hi = t["nodes"]
    # 每段字数就给个粗略区间：按"最多几段"算下限、按"最少几段"算上限，
    # 这只是给她一个手感；真正的分配是模型按情节轻重写、她再调的。
    return "建议 %d～%d 个情节节点，每个节点大约 %d～%d 字。" % (
        lo, hi, n // max(1, hi), n // max(1, lo))


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


def _clean_node(v, idx, known_plots):
    """清洗一个节点。

    node_id 只在**同一份大纲内**唯一，所以由我们兜底生成 ——
    模型给的编号要是不小心重了，后面的 diff 和"标记不可用"会全错位。
    """
    if not isinstance(v, dict):
        return None
    out = {}
    nid = str(v.get("node_id") or "").strip()
    if not nid or len(nid) > 24 or not re.match(r"^[A-Za-z0-9_\-]+$", nid):
        nid = "n%d" % idx
    out["node_id"] = nid
    for f in NODE_FIELDS:
        if f == "node_title":
            out[f] = _txt(v.get(f), NODE_TITLE_MAX, "节点标题")
        elif f in ("purpose", "conflict", "emotional_change",
                   "information_revealed", "connection_to_next", "location_time"):
            out[f] = _txt(v.get(f), NODE_SHORT_MAX, NODE_FIELD_LABELS[f])
        else:
            out[f] = _txt(v.get(f), NODE_TEXT_MAX, NODE_FIELD_LABELS[f])
    if not out["node_title"]:
        out["node_title"] = "第 %d 段" % idx

    try:
        w = int(v.get("estimated_words") or 0)
    except (TypeError, ValueError):
        w = 0
    out["estimated_words"] = max(0, min(w, 200000))

    roles = _str_list(v.get("participating_roles"), NODE_LIST_MAX, 30,
                      "参与角色")
    out["participating_roles"] = roles

    src = _int_list(v.get("source_plot_ids"), NODE_SOURCE_MAX, "来源零件")
    # 【为什么在这里过滤】模型的编号跑偏是最危险的一类错。
    # 一个不存在的 plot_id 混进来，会让大纲"看起来有来源"，
    # 点进去却是空的 —— 来源错了比没有来源更糟。
    out["source_plot_ids"] = [i for i in src if i in known_plots]
    return out


def clean_outline_payload(payload, known_plots=None):
    """把模型返回（或她编辑后回传）的大纲 JSON 洗一遍。

    设计原则跟内化那边一样：**硬伤抛错，软伤降级 + 警告**。
    硬伤只留一条 —— nodes 不是数组（没有节点就没有大纲，没什么好降级的）。
    其余（缺标题、字数离谱、字段超长）都降级成警告，不整份作废：
    她已经花了钱，能救回来的就救。
    """
    if not isinstance(payload, dict):
        raise ValueError("大纲的顶层得是一个对象。")
    known = set(known_plots or [])
    warns = []

    titles = _str_list(payload.get("title_candidates"), 3, OUTLINE_TITLE_MAX,
                       "标题候选")
    story_core = _txt(payload.get("story_core"), NODE_TEXT_MAX, "故事核心")
    theme_tone = _txt(payload.get("theme_tone"), NODE_SHORT_MAX, "主题与基调")
    overview = _txt(payload.get("overview"), NODE_TEXT_MAX * 2, "故事总览")

    fns = []
    raw_fns = payload.get("character_functions")
    if isinstance(raw_fns, list):
        for it in raw_fns[:ROLE_FUNCTION_MAX]:
            if isinstance(it, dict):
                fns.append({
                    "role": _txt(it.get("role"), 40, "角色名"),
                    "goal": _txt(it.get("goal"), NODE_SHORT_MAX, "目标"),
                    "obstacle": _txt(it.get("obstacle"), NODE_SHORT_MAX, "阻碍"),
                    "change": _txt(it.get("change"), NODE_SHORT_MAX, "变化"),
                })
            else:
                s = _txt(it, NODE_SHORT_MAX, "角色功能")
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
        n = _clean_node(nv, i, known)
        if n is None:
            continue
        # 编号去重（模型偶尔会把两段都叫 n3）
        while n["node_id"] in used_ids:
            n["node_id"] = n["node_id"] + "b"
        used_ids.add(n["node_id"])
        nodes.append(n)
    if not nodes:
        raise ValueError("nodes 里一个能用的节点都没有。")

    # 丢来源零件的要说出来，别让她以为"AI 用了很多零件"
    dropped = 0
    for nv in raw_nodes[:MAX_NODES]:
        if isinstance(nv, dict):
            raw_src = _int_list(nv.get("source_plot_ids"), NODE_SOURCE_MAX, "来源零件")
            dropped += len([x for x in raw_src if x not in known])
    if dropped:
        warns.append("模型引用了 %d 处不在本次候选里的零件编号，已经抹掉 ——"
                     "来源不明的零件比没有来源更危险。" % dropped)

    climax = _txt(payload.get("climax"), NODE_SHORT_MAX * 2, "高潮与转折")
    if isinstance(payload.get("climax"), dict):
        climax = _txt(payload["climax"].get("description"), NODE_SHORT_MAX * 2,
                      "高潮与转折")
    ending = _txt(payload.get("ending"), NODE_TEXT_MAX, "结局")

    risks = _str_list(payload.get("logic_risks"), 8, NODE_SHORT_MAX, "逻辑风险")

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
        "logic_risks": [],
        "word_budget": {"total": 0},
    }


# ----------------------------------------------------------------------
# AI 稿 vs 用户稿的差异
# ----------------------------------------------------------------------

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
    """学习库的一眼概况（界面上显示"现在有多少可供参考的案例"）。"""
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
    return {"outlines": total, "in_learning": inl, "diffs": diffs,
            "cases": cases}


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

    # 结构规模校验：8000 字给 12 个节点要警告
    obj, _w = clean_outline_payload({"nodes": [
        {"node_title": "第%d段" % i, "event": "发生了事情", "estimated_words": 600}
        for i in range(1, 13)]}, {1})
    ws = validate_outline(obj, 8000)
    check("8000 字 12 个节点会被提醒",
          any("空壳" in x for x in ws), True)

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
