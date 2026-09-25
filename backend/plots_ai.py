# -*- coding: utf-8 -*-
"""
墨阁 · 剧情内化（AI 那一半：把素材卡片抽象成可迁移的剧情零件）
========================================================
这个文件管一件事：**「点一下 AI 内化」之后发生的全部事情**。

三个文件的分工（跟分类那条线长得一样）

    segmentation.py    纯算法。切段。不碰数据库。
    plots_db.py        数据层。零件 / 来源 / 版本 怎么存、怎么取。**不碰 AI。**
    plots_ai.py        编排层。什么时候、把哪些卡片、交给哪个模型、判完怎么落库。

为什么 AI 那半要单独一个文件

    plots_db 只管"零件长什么样、怎么存"，它一个字都不该知道有模型这回事。
    AI 这部分将来一定会换（换模型、换提示词、加多模型比较、加学习库），
    换的时候只动这一个文件，第 1 步那三张表和接口一个字都不用改。

========================================================
七条不能破的规矩（改代码之前先读这段）
========================================================

一、不碰原文
    卡片正文只从 materials.content 按偏移现算。
    这个文件一个字符都不写 content，也**不接受模型返回的正文**。

二、AI 提的不能冒充人工确认
    自动落库的零件一律 status='待确认' + source='ai'。
    「已确认」只能由人点出来 —— plots_db.create_plot 那边还有第二道硬拦。

三、不覆盖人工的成果
    AI 只会**新建**零件，绝不改任何已有的零件、也不改它的版本。
    已经人工确认或人工编辑过的零件，重跑多少次都不会被动到。

四、来源只认程序算出来的，不认模型说的
    来源关系由 card_id 保存。模型返回的 used_card_ids 只是"它说自己用了哪些"，
    必须**逐条校验它们都在本批输入里** —— 校验不过整批作废，
    宁可下次重来，也不让错位的来源混进库里（来源错了比没有来源更糟）。

五、一个文件同时只能有一个任务在跑
    否则点两下会生成两个任务，两批结果互相覆盖，还看不出是谁写的。

六、失败要留痕，不能悄悄过去
    单批失败 → plot_items.error_message + plot_runs.note
    界面上必须能看到"哪一批、为什么失败"。

七、不阻塞页面
    任务在后台线程里跑，HTTP 请求立刻返回。进度靠前端轮询拿。

========================================================
跟"自动分类"那条线的三处**故意不一样**（别照着那边改）
========================================================

1. 批次小得多。分类一批 25 条，内化一批 12 条，首批只发 3 条。
   原因：内化的**输出**长得多 —— 一条候选就是几百字（摘要+七个点位+理由），
   一批 25 张卡可能产出十几个候选，输出动辄上万 token，很容易被截断。
   首批 3 条是为了让进度条尽早"动起来"（跟分类同一个理由）。

2. 状态值存**中文**（"排队中"/"进行中"…），不跟 classification_runs 一样存英文。
   对齐 plots.status 和 cards.status 的规矩：这些状态她可能会自己翻库看。

3. 模板里的 {card_blocks} 是**模板槽位**，正文走模板进 system 消息。
   分类那边是"模板只管怎么问、正文现拼放 user 消息"。
   内化改成槽位，是因为她的提示词明确把 {card_blocks} 定义成模板的一部分，
   而且把它列进了必需槽位 —— 模板里删了它就退回通用版，正文不会静默丢。
   （为了防"正文里恰好有花括号被当成槽位"，替换用单遍扫描，见 _fill_slots。）
"""

import json
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime

try:
    from backend import db, segmentation as sg
    from backend import plots_db as plots
    from backend import classification as cls
    from backend import classify_db as cdb
    from backend import llm
except ImportError:                                   # pragma: no cover
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from backend import db, segmentation as sg
    from backend import plots_db as plots
    from backend import classification as cls
    from backend import classify_db as cdb
    from backend import llm


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------
# 一、常量（全是唯一定义处，别在别处再写一遍）
# ----------------------------------------------------------------------

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 真提示词的落点。prompts/ 有两道锁（.gitignore 第 112 行 + 同步白名单里没有它），
# 所以她调好的那份永远不会被同步到公开仓库。
PROMPT_FILE = "infuse.txt"

# 提示词版本。做成动态的（见 prompt_version()）：
#   v1          用了 prompts/infuse.txt（她调的那版）
#   v1-generic  退回了代码里的通用模板
# 为什么不能写死一个号：她改完文件重跑两次要能比出"是不是同一版"。
PROMPT_VERSION = "v1"
PROMPT_VERSION_GENERIC = "v1-generic"

# 模板里必须有这六个槽位。缺任何一个 → 退回通用模板并把警告写进任务备注。
# 为什么缺一个就整份退回，而不是"缺的那个填空"：
#   缺 {card_blocks} 就等于把正文全丢了，模型只能瞎编；
#   缺 {categories} 它就自己发明类名。
#   这两种都是**静默丢内容**，最难查。宁可退回通用版。
REQUIRED_SLOTS = ("categories", "plot_types", "usage_hints",
                  "card_blocks", "learning_examples", "user_prompt")

# 补充提示词复用分类那条线存的同一张表（user_prompts），靠 kind 区分用途。
# 好处：上限校验、超长报错、界面那套交互全部现成，不用再写一遍。
USER_PROMPT_KIND_INFUSE = "infuse"
USER_PROMPT_MAX = cls.USER_PROMPT_MAX          # 5000

# ---- 任务状态（存中文）----
RUN_QUEUED = "排队中"
RUN_RUNNING = "进行中"
RUN_COMPLETED = "已完成"
RUN_PARTIAL = "部分失败"
RUN_FAILED = "失败"
RUN_CANCELLED = "已取消"
ALL_RUN_STATUS = (RUN_QUEUED, RUN_RUNNING, RUN_COMPLETED,
                  RUN_PARTIAL, RUN_FAILED, RUN_CANCELLED)
RUN_ACTIVE = (RUN_QUEUED, RUN_RUNNING)         # 这两种状态下不许再开新任务

# ---- 逐条卡片的处理状态 ----
ITEM_PENDING = "待处理"
ITEM_RUNNING = "处理中"
ITEM_DONE = "已处理"
ITEM_FAILED = "失败"
ITEM_SKIPPED = "已跳过"
# "还没轮到的"两种。卡片内化状态那边的「排队中」认的就是这两个
# （plots_db.card_infuse_states 2a）。别在那边再抄一遍字符串。
ITEM_ACTIVE = (ITEM_PENDING, ITEM_RUNNING)

# ---- 模型返回的三种结果（她提示词里定的，照抄不翻译）----
RESULT_GENERATE = "generate"
RESULT_UNSURE = "unsure"
RESULT_UNSUITABLE = "unsuitable"
RESULT_FAILED = "failed"                       # 程序加的：这一批压根没跑成
ALL_RESULT = (RESULT_GENERATE, RESULT_UNSURE, RESULT_UNSUITABLE, RESULT_FAILED)
RESULT_LABELS = {
    RESULT_GENERATE: "可以生成零件",
    RESULT_UNSURE: "拿不准",
    RESULT_UNSUITABLE: "不适合内化",
    RESULT_FAILED: "没跑成",
}

# ---- 一张卡在这一批里的去向 ----
CARD_USED = "已内化"          # 被某个候选当作来源用了
CARD_UNUSED = "看过没用"      # 模型看了，但没用它组零件

# ---- 批次 ----
# 见文件头第 1 条：内化输出长，批次要比分类小得多。
FIRST_BATCH_SIZE = 3
BATCH_SIZE = 12

# 置信度分界线。前端 const LOW_CONF 必须跟它一致（否则"没把握"标记两边对不上）。
CONFIDENCE_LOW = 0.70

# 一次任务最多送多少张卡（安全阀，不是产品限制）。
# 663 张卡按 12 一张批是 56 批，跑一两个小时、花的钱也不少。
# 界面上会先告诉她"这次要送 N 张"，她也可以先跑前 N 张试水。
MAX_CARDS_PER_RUN = 2000


# ----------------------------------------------------------------------
# 二、建表
#
# 四张表，全是**纯新增空表**：没有 ALTER、不碰任何已有表。
# 所以 migrate() 任何时候跑都安全，也不需要备份（跟第 1 步同一个判断）。
#
# 【例外：plot_runs.user_prompt】这一列是后补的，见 migrate() 里那段补列。
# 它不在这句话的"没有 ALTER"范围内 —— 加列是幂等的、不动已有数据，
# 但仍然要在老库上真跑一次 ALTER。
# ----------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS plot_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id            TEXT    NOT NULL,
    material_id         INTEGER DEFAULT NULL,
    material_title      TEXT    NOT NULL DEFAULT '',
    source_collection   TEXT    NOT NULL DEFAULT '',
    run_type            TEXT    NOT NULL DEFAULT 'batch',
    input_card_count    INTEGER NOT NULL DEFAULT 0,
    skip_infused        INTEGER NOT NULL DEFAULT 1,
    limit_count         INTEGER NOT NULL DEFAULT 0,
    model_key           TEXT    NOT NULL DEFAULT '',
    model_name          TEXT    NOT NULL DEFAULT '',
    prompt_version      TEXT    NOT NULL DEFAULT '',
    template_source     TEXT    NOT NULL DEFAULT '',
    user_prompt_len     INTEGER NOT NULL DEFAULT 0,
    user_prompt         TEXT    NOT NULL DEFAULT '',
    -- 这段补充提示词是从「提示词库」的哪一条来的（自由文本时全是 0/空）。
    -- 【为什么必须记】她跑完一轮会去改提示词再跑第二轮，回头必须答得出
    -- "第一轮到底用的是哪条"。光存正文只能看出"写了什么"，
    -- 看不出"它是哪条、是不是别人公开出来的那条"。
    prompt_ref_id       INTEGER NOT NULL DEFAULT 0,
    prompt_name         TEXT    NOT NULL DEFAULT '',
    prompt_owner        TEXT    NOT NULL DEFAULT '',
    category_set_version TEXT   NOT NULL DEFAULT '',
    status              TEXT    NOT NULL DEFAULT '排队中',
    total_batches       INTEGER NOT NULL DEFAULT 0,
    done_batches        INTEGER NOT NULL DEFAULT 0,
    total_items         INTEGER NOT NULL DEFAULT 0,
    done_items          INTEGER NOT NULL DEFAULT 0,
    failed_items        INTEGER NOT NULL DEFAULT 0,
    candidate_count     INTEGER NOT NULL DEFAULT 0,
    created_plot_count  INTEGER NOT NULL DEFAULT 0,
    total_input_chars   INTEGER NOT NULL DEFAULT 0,
    retry_of_run_id     INTEGER DEFAULT NULL,
    note                TEXT    NOT NULL DEFAULT '',
    error               TEXT    NOT NULL DEFAULT '',
    heartbeat_at        TEXT    NOT NULL DEFAULT '',
    created_at          TEXT    NOT NULL,
    started_at          TEXT    NOT NULL DEFAULT '',
    finished_at         TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS plot_run_cards (
    run_id      INTEGER NOT NULL,
    card_id     INTEGER NOT NULL,
    input_order INTEGER NOT NULL DEFAULT 0,
    batch_no    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    PRIMARY KEY (run_id, card_id)
);

CREATE TABLE IF NOT EXISTS plot_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL,
    card_id       INTEGER NOT NULL,
    batch_no      INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT '待处理',
    outcome       TEXT    NOT NULL DEFAULT '',
    reason        TEXT    NOT NULL DEFAULT '',
    candidate_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT    NOT NULL DEFAULT '',
    retry_count   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    UNIQUE (run_id, card_id)
);

CREATE TABLE IF NOT EXISTS plot_candidates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER NOT NULL,
    owner_id            TEXT    NOT NULL,
    batch_no            INTEGER NOT NULL DEFAULT 0,
    model_key           TEXT    NOT NULL DEFAULT '',
    model_name          TEXT    NOT NULL DEFAULT '',
    prompt_version      TEXT    NOT NULL DEFAULT '',
    candidate_group_key TEXT    NOT NULL DEFAULT '',
    result              TEXT    NOT NULL DEFAULT '',
    title               TEXT    NOT NULL DEFAULT '',
    summary             TEXT    NOT NULL DEFAULT '',
    plot_type           TEXT    NOT NULL DEFAULT '',
    primary_category    TEXT    NOT NULL DEFAULT '',
    usage_hint_json     TEXT    NOT NULL DEFAULT '[]',
    tags_json           TEXT    NOT NULL DEFAULT '[]',
    beats_json          TEXT    NOT NULL DEFAULT '{}',
    role_slots_json     TEXT    NOT NULL DEFAULT '[]',
    used_card_ids_json  TEXT    NOT NULL DEFAULT '[]',
    transferable_core   TEXT    NOT NULL DEFAULT '',
    variations_json     TEXT    NOT NULL DEFAULT '[]',
    reason              TEXT    NOT NULL DEFAULT '',
    confidence          REAL    DEFAULT NULL,
    unsure_points_json  TEXT    NOT NULL DEFAULT '[]',
    raw_response        TEXT    NOT NULL DEFAULT '',
    adopted             INTEGER NOT NULL DEFAULT 0,
    plot_id             INTEGER DEFAULT NULL,
    adopt_error         TEXT    NOT NULL DEFAULT '',
    created_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plot_runs_owner
    ON plot_runs (owner_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_plot_runs_material
    ON plot_runs (owner_id, material_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_plot_runs_status
    ON plot_runs (status);
CREATE INDEX IF NOT EXISTS idx_plot_run_cards_run
    ON plot_run_cards (run_id, input_order);
CREATE INDEX IF NOT EXISTS idx_plot_items_run
    ON plot_items (run_id, status);
CREATE INDEX IF NOT EXISTS idx_plot_cand_run
    ON plot_candidates (run_id, id);
CREATE INDEX IF NOT EXISTS idx_plot_cand_group
    ON plot_candidates (candidate_group_key);
"""


def _has_table(conn, name):
    r = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                     (name,)).fetchone()
    return r is not None


def migrate(verbose=False):
    """建这四张表，并补上后加的列。反复跑是安全的。

    【为什么不备份】跟第 1 步同一个判断：这四张全是**新增的空表**，
    不碰任何已有表，所以不存在"改错了回不去"的风险。
    那一段补列（user_prompt）是 ALTER TABLE ADD COLUMN，同样是幂等的、
    不动已有数据 —— 只有"改已写死的约束"才需要整容迁移那一套，
    动手前必须先 shutil.copy2 备份整个库。
    """
    with db.connect() as conn:
        conn.executescript(SCHEMA)

    # ---- 补列：老库上的 plot_runs 没有 user_prompt ----
    # 【为什么必须补，不能只写进 SCHEMA】SCHEMA 是 CREATE TABLE IF NOT
    # EXISTS —— 表已经在了就整段跳过，新加的列**永远不会生效**。
    # 少了这一列，跑任务时用的哪句补充提示词就存不下来：重跑、重试
    # 都只能拿"她现在写的那句"，第一轮怎么问的永远答不出来。
    # 加列前先查一遍列名，不然第二次启动会报 duplicate column name。
    with db.connect() as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(plot_runs)")]
        if cols and "user_prompt" not in cols:
            conn.execute("ALTER TABLE plot_runs "
                         "ADD COLUMN user_prompt TEXT NOT NULL DEFAULT ''")
            if verbose:
                print("  plot_runs.user_prompt  已补上")
        # ---- 补列：提示词库引用（哪一条 / 叫什么 / 谁的）----
        # 老库上这三列都没有。少了它们，跑完之后只看得见正文，
        # 看不见"这条是从库里哪条来的"，而正文是会被改的、名字不会。
        for col, ddl in (
                ("prompt_ref_id", "prompt_ref_id INTEGER NOT NULL DEFAULT 0"),
                ("prompt_name", "prompt_name TEXT NOT NULL DEFAULT ''"),
                ("prompt_owner", "prompt_owner TEXT NOT NULL DEFAULT ''")):
            if cols and col not in cols:
                conn.execute("ALTER TABLE plot_runs ADD COLUMN " + ddl)
                if verbose:
                    print("  plot_runs.%s  已补上" % col)

    if verbose:
        with db.connect() as conn:
            for t in ("plot_runs", "plot_run_cards", "plot_items",
                      "plot_candidates"):
                n = conn.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
                print("  %-18s %d 行" % (t, n))
    return True


# ----------------------------------------------------------------------
# 三、模板装载
# ----------------------------------------------------------------------

# 公开版用的通用模板。
#
# 【为什么必须有它】prompts/infuse.txt 里带着她的真实原稿和角色名，
# 永远不进公开仓库。别人 clone 公开版之后，这个文件是**不存在**的 ——
# 靠下面这份通用模板照样能跑起来。
# 里面的例子全部是中性内容（不来自任何真实作品），所以可以公开。
GENERIC_INFUSE_PROMPT = """你是"墨阁"的剧情内化助手。

你的任务不是摘抄原文，也不是评价文笔，而是把用户已经选中的素材卡片，
提炼成可迁移、可复用的"剧情零件"。

剧情零件的含义是：把具体人物、具体作品、具体地点和具体道具替换掉以后，
仍然成立的一种事件、关系推进、冲突结构、误会结构、情绪转折或剧情钩子。

总原则：

1. 原文是证据，不是需要改写的底稿。
2. 只使用输入卡片里确实提供的信息，不凭空补充原文没有的事实。
3. 抽象时可以把具体姓名替换成角色位，但不能改变事件因果。
4. 不要把华丽描写简单改写成空泛的"发生了一些事情"。
5. 一条卡片可以没有可内化剧情；多条卡片可以共同组成一个零件；
   一批卡片也可以产生多个不同零件。
6. 每个零件必须明确列出实际使用的 card_id。
7. 只是人物描写、环境描写、金句、静态设定的，应返回 unsuitable，
   不要硬抽象。
8. "纯对白"不是绝对排除条件。对白推动了关系、冲突、误会或决定，就可以生成零件。
9. 任何不确定都必须诚实返回 unsure，并说明缺少什么信息。

------------------------------------------------------------
判断流程
------------------------------------------------------------

第一步：列出你实际使用的 card_id。只允许使用本次输入里的编号。

第二步：判断素材里有没有"变化"。
至少要有一种：事件变化、关系变化、冲突变化、情绪变化、信息变化、目标变化。

第三步：判断迁移性。把姓名和作品设定换成角色位之后，这个结构还立不立得住。

第四步：判断能不能服务大纲（初遇 / 升温 / 拉扯 / 冲突升级 / 误会 /
真相揭露 / 危机处理 / 情绪转折 / 结尾反转 这一类位置）。

第五步：决定结果，只能是三种之一：
- generate     可以生成一个或多个剧情零件
- unsure       有潜在价值，但信息不足或边界不清，暂不生成正式零件
- unsuitable   更适合留在素材分类库，不适合剧情内化

------------------------------------------------------------
分类和标签规则
------------------------------------------------------------

主分类只能从下面这些里选一个 primary_category：

{categories}

剧情类型从下面这些里选一个 plot_type：

{plot_types}

使用场景可以从下面这些里选一个或多个 usage_hint：

{usage_hints}

如果来源卡片有多个主类，优先按零件本身的主要用途选，不要机械复制。
实在判断不了就填 null，并在 reason 里说明，不要为了凑一个分类编理由。

------------------------------------------------------------
输出格式
------------------------------------------------------------

只输出**一个合法的 JSON 对象**，不要 Markdown 代码围栏，不要任何解释文字。

顶层结构：

{"items": [], "input_assessment": {}, "warnings": []}

items 里每个元素代表一个候选剧情零件：

- result             generate / unsure / unsuitable
- used_card_ids      实际使用的 card_id 数组
- title              20 字以内、方便扫读的标题
- summary            一段完整、可迁移、可直接供大纲使用的剧情描述
- primary_category   一个主分类名称；不确定时为 null
- plot_type          一个剧情类型；不确定时为 null
- usage_hint         一个或多个使用场景
- tags               少量副标签
- role_slots         角色位置数组，例如"主动者""被保护者""误判者"
- beats              固定结构对象，见下
- transferable_core  换掉角色和场景后仍然不能丢掉的核心机制
- optional_variations 可以替换的变量或变体
- reason             为什么判定为可生成、待定或不适合
- confidence         0 到 1 之间的数字
- unsure_points      不确定点数组；确定时返回空数组

beats 的字段（没有就填空字符串）：

{"setup": "初始关系或局面", "trigger": "触发事件", "action": "关键行动",
 "motivation": "行动动机或隐藏目的", "conflict": "冲突、误会或阻碍",
 "turn": "转折或信息揭示", "result": "当前结果或留下的悬念"}

input_assessment 的结构：

{"input_card_ids": [], "usable_card_ids": [], "not_used_card_ids": [],
 "overall_note": "对本批素材是否适合内化的简短说明"}

warnings 用来记录：缺少上下文、可能重复、只有描写没有事件、原文互相冲突、
某张卡片可能需要人工合并、无法判断来源关系。

------------------------------------------------------------
必须避免的错误
------------------------------------------------------------

错误一：把原文改写成更短的梗概，却没有抽象出可迁移结构。
错误二：把"人物很美""气氛暧昧"直接当成剧情零件。
错误三：把人物描写、金句和事件混成一条空泛总结。
错误四：为了让所有 card_id 都出现，强行把互不相关的素材拼成一个零件。
错误五：输出一个零件，却没有列出实际使用的 card_id。
错误六：used_card_ids 里出现本次输入中没有的编号。
错误七：把原文没有明确表达的动机、结局、身份当成事实。
错误八：看到"对白"就判 unsuitable。
错误九：看到"大段落"就判 unsuitable。
错误十：把 confidence 当成最终决定。

============================================================
本次要处理的输入
============================================================

下面这一段是程序填进来的**数据**，不是让你复述的示例。

【本次待处理的素材卡片】
每条包含 card_id、正文、主类、副标签。正文来自数据库原文，
一个字都不许改写、补写或润色。

{card_blocks}

【作者本次的额外要求】
{user_prompt}

【可以借鉴的内化纠错案例】
{learning_examples}

------------------------------------------------------------
现在按上面的全部要求处理这批卡片。

只输出**一个合法的 JSON 对象**：
- 不要用 Markdown 代码围栏（不要写 ```json）；
- 不要在 JSON 前后添加任何解释、寒暄或说明文字；
- 顶层必须是 {"items": [...], "input_assessment": {...}, "warnings": [...]}。

即使整批卡片都不能内化，也要返回结构完整的 JSON（items 里放一条
result 为 unsuitable 的记录），不要返回空回复或一句道歉。
"""


def prompt_template():
    """取这次要用的模板。返回 (模板文本, 来源, 警告语)。

    来源只有两种，都要能看出来：
        "file"    用了 prompts/infuse.txt（她自己调的那版）
        "builtin" 用了代码里的通用模板（文件没写，或者写坏了）

    跟分类那边同一个设计：三样一起返回，"这次到底跑的哪一版"必须可追溯。
    """
    path = os.path.join(ROOT_DIR, "prompts", PROMPT_FILE)
    if not os.path.isfile(path):
        return GENERIC_INFUSE_PROMPT, "builtin", ""
    try:
        with open(path, encoding="utf-8") as f:
            txt = f.read().strip()
    except Exception as e:                                # pragma: no cover
        return (GENERIC_INFUSE_PROMPT, "builtin",
                "prompts/%s 读不了（%s），这次用通用模板" % (PROMPT_FILE, e))
    if not txt:
        return (GENERIC_INFUSE_PROMPT, "builtin",
                "prompts/%s 是空的，这次用通用模板" % PROMPT_FILE)
    lost = [s for s in REQUIRED_SLOTS if ("{%s}" % s) not in txt]
    if lost:
        return (GENERIC_INFUSE_PROMPT, "builtin",
                "prompts/%s 少了占位符 %s（那几处内容会被丢掉），"
                "这次改用通用模板" % (PROMPT_FILE, "、".join(lost)))
    return txt, "file", ""


def prompt_version():
    """这次会用哪一版提示词（任务记录里要存它）。"""
    _, src, _ = prompt_template()
    return PROMPT_VERSION if src == "file" else PROMPT_VERSION_GENERIC


# 只认这六个槽位名。单遍扫描替换 —— 见下面 _fill_slots 的注释。
_SLOT_RE = re.compile(r"\{(" + "|".join(REQUIRED_SLOTS) + r")\}")


def _fill_slots(tpl, **slots):
    """把 {xxx} 换成实际内容。**单遍扫描**，不是逐个 replace。

    【为什么不能像分类那边那样逐个 .replace】
    内化的 {card_blocks} 里装的是**她小说原文**。原文里出现一个花括号
    完全不奇怪（写个代码片段、写个算式都可能）。如果先替换了
    {card_blocks}，再去 replace {user_prompt}，那么原文里恰好写了
    "{user_prompt}" 这几个字的地方会被**再替换一次** ——
    结果是把别人的补充要求塞进了正文中间，而且谁也不会发现。

    【为什么也不能用 str.format】
    模板正文里有 JSON 示例（{"items": []}），大括号会跟 format 语法打架。

    单遍正则同时躲开这两个坑：只认自己定义的槽位名，每个位置只替换一次，
    替换进去的内容（正文、补充提示词）永远不会被二次解释。
    """
    def _sub(m):
        return slots.get(m.group(1), m.group(0))
    return _SLOT_RE.sub(_sub, tpl)


# ----------------------------------------------------------------------
# 四、补充提示词（复用分类那条线的存储，换一个 kind）
# ----------------------------------------------------------------------

def get_user_prompt(owner):
    """读她给内化写的补充提示词。没写过就是空串。"""
    return cls.get_user_prompt(owner, kind=USER_PROMPT_KIND_INFUSE)


def set_user_prompt(owner, content):
    """存她给内化写的补充提示词。超长直接拒绝，不悄悄截断。

    直接把分类那边那套拿过来用 —— 上限、报错话术、行为全都一致，
    她在这个框和那个框里踩的是同一套规则，不用学两遍。
    """
    return cls.set_user_prompt(owner, content, kind=USER_PROMPT_KIND_INFUSE)


# ----------------------------------------------------------------------
# 五、拼提示词
# ----------------------------------------------------------------------

def _categories_block(cats):
    """主类清单（名字 + 判据）。判据用她写在库里的原文，不改写。"""
    out = []
    for i, c in enumerate(cats, 1):
        t = "%d. %s" % (i, c.get("name") or "")
        d = (c.get("description") or "").strip()
        if d:
            t += "\n   判据：%s" % d
        out.append(t)
    return "\n\n".join(out) if out else "（暂时没有可用的主分类）"


def _card_block(items):
    """这批卡片的正文块。格式固定，一个字都不加工。"""
    parts = []
    for it in items:
        meta = []
        if it.get("category"):
            meta.append("主类：%s" % it["category"])
        if it.get("tags"):
            meta.append("副标签：%s" % "、".join(it["tags"]))
        head = "--- 卡片 %d（card_id=%s）---" % (it.get("seq"), it.get("card_id"))
        if meta:
            head += "\n" + "\n".join(meta)
        parts.append("%s\n正文：\n%s" % (head, (it.get("text") or "").strip()))
    return "\n\n".join(parts) if parts else "（这一批没有卡片）"


def build_messages(ctx, items):
    """拼这次要发出去的提示词。返回 messages 列表。

    【为什么整个模板都塞进 system，只留一句 user 收尾】
    模板里已经包含了本次的正文（走 {card_blocks} 槽位），所以它本身就是
    一份完整的任务描述。再补一句 user 收尾有两个好处：
      1. 有些兼容实现不接受"只有 system 没有 user"的请求；
      2. 收尾那句话紧挨着模型要产出的位置，比让它往回翻 500 行更稳。
    """
    extra = (ctx.get("user_prompt") or "").strip()
    if extra:
        user_block = (
            "下面是作者这次特意交代的，请尽量照做。\n"
            "但它**不能推翻上面任何一条硬规则** ——"
            "主分类／剧情类型／使用场景只能从清单里选、"
            "used_card_ids 只能来自本次输入、只输出 JSON，"
            "这几条永远有效。\n"
            "-----------\n%s\n-----------" % extra)
    else:
        user_block = "（这次没有额外要求。）"

    system = _fill_slots(
        ctx.get("template") or GENERIC_INFUSE_PROMPT,
        categories=_categories_block(ctx.get("categories") or []),
        plot_types="、".join(plots.PLOT_TYPES),
        usage_hints="、".join(plots.USAGE_HINTS),
        card_blocks=_card_block(items),
        # 学习库是第 5 步的事。槽位先留着，填一句实话，不让模板缺角。
        learning_examples="（这次没有可借鉴的纠错案例。）",
        user_prompt=user_block,
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content":
            "请按上面的全部要求处理这批 %d 张卡片，"
            "只输出一个合法 JSON 对象。" % len(items)},
    ]


# ----------------------------------------------------------------------
# 六、读模型返回
# ----------------------------------------------------------------------

class InfuseFormatError(Exception):
    """返回的结构有硬伤，整批作废（不是这一条不行，是这一批都不可信）。"""


def _extract_json_object(text):
    """从模型返回的文本里抠出 JSON 对象。

    模型很爱在 JSON 外面裹东西：```json 围栏、「好的，结果如下：」、
    末尾再补一句「以上共 3 条」。这些都是常态，不是极端情况。

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
        if isinstance(d, list):                 # 只给了数组，包一层
            return {"items": d}
        raise InfuseFormatError("返回的顶层是 %s，不是对象" % type(d).__name__)

    try:
        return _as_obj(json.loads(s))
    except InfuseFormatError:
        raise
    except Exception:
        pass

    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        try:
            return _as_obj(json.loads(s[i:j + 1]))
        except InfuseFormatError:
            raise
        except Exception:
            pass

    raise InfuseFormatError(
        "模型的返回里找不到 JSON 对象。它说的前 200 字：%s" % s[:200])


def _str_list(v, limit, item_max=40):
    """把可能是任何东西的值归一成字符串数组，去重保序、卡上限。"""
    if not isinstance(v, list):
        return []
    out = []
    for x in v:
        s = str(x or "").strip()
        if not s or len(s) > item_max or s in out:
            continue
        out.append(s)
        if len(out) >= limit:
            break
    return out


def _clean_beats(v):
    """只留认识的七个点位，其余丢掉（丢的是模型编的字段，不是她的数据）。"""
    if not isinstance(v, dict):
        return {}
    out = {}
    for k in plots.BEAT_KEYS:
        s = str(v.get(k, "") or "").strip()
        if s:
            out[k] = s[:plots.SUMMARY_MAX]
    return out


def _validate_payload(payload, batch_ids, cat_names):
    """校验模型返回。返回 (候选列表, 警告列表)。

    【硬伤 vs 软伤 —— 这条分界是整个校验的设计核心】

    硬伤 → 抛 InfuseFormatError，**整批作废**：
        · 顶层没有 items、或 items 不是数组
        · result 不是三种之一
        · used_card_ids 不是数组
        · used_card_ids 里出现本批之外的 card_id  ← 最关键的一条
      为什么整批而不是只丢那一条：一旦有编号跑偏，我们无法知道
      "看起来对的那几条"是不是也一起错位了（比如整体串了一位）。
      来源错了比没有来源更糟 —— 零件会挂到别人的素材上。
      宁可这批下次重来，也不要让来源不明的结果混进库里。

    软伤 → 逐条降级，其余照常：
        · 说能生成却没给标题   → 降级成 unsure
        · 剧情类型/主类名不认识 → 清掉那个字段 + 警告（不丢零件本身）
        · 使用场景/角色位超量   → 截断 + 警告
        · confidence 不是 0~1 的数 → 置空（不编一个数出来）
    """
    items = payload.get("items")
    if items is None:
        raise InfuseFormatError("返回里没有 items 字段")
    if not isinstance(items, list):
        raise InfuseFormatError("items 不是数组，是 %s" % type(items).__name__)

    batch = set(batch_ids)
    cat_set = set(cat_names or [])
    out, warns = [], []

    for idx, raw in enumerate(items, 1):
        if not isinstance(raw, dict):
            warns.append("第 %d 条不是对象，已跳过" % idx)
            continue

        result = raw.get("result")
        if result not in (RESULT_GENERATE, RESULT_UNSURE, RESULT_UNSUITABLE):
            raise InfuseFormatError(
                "第 %d 条的 result 是「%s」，只能是 generate / unsure / unsuitable"
                % (idx, result))

        used = raw.get("used_card_ids")
        if used is None:
            used = []
        if not isinstance(used, list):
            raise InfuseFormatError("第 %d 条的 used_card_ids 不是数组" % idx)

        norm = []
        for x in used:
            try:
                xi = int(x)
            except (TypeError, ValueError):
                raise InfuseFormatError(
                    "第 %d 条的 used_card_ids 里有不是编号的值：%r" % (idx, x))
            if xi not in batch:
                raise InfuseFormatError(
                    "第 %d 条引用了本批之外的卡片 card_id=%s。"
                    "整批作废，避免来源错位混进库里。" % (idx, xi))
            if xi not in norm:
                norm.append(xi)

        # ---- 下面都是软伤，逐条处理 ----
        title = str(raw.get("title") or "").strip()
        summary = str(raw.get("summary") or "").strip()

        cat = raw.get("primary_category")
        cat = cat.strip() if isinstance(cat, str) else ""
        if cat and cat_set and cat not in cat_set:
            warns.append("第 %d 条给了个不认识的主分类「%s」，这一格已清空" % (idx, cat))
            cat = ""

        ptype = raw.get("plot_type")
        ptype = ptype.strip() if isinstance(ptype, str) else ""
        if ptype and ptype not in plots.PLOT_TYPES:
            warns.append("第 %d 条给了个不认识的剧情类型「%s」，这一格已清空" % (idx, ptype))
            ptype = ""

        hints = _str_list(raw.get("usage_hint"), plots.USAGE_HINTS_MAX)
        bad_hints = [h for h in hints if h not in plots.USAGE_HINTS]
        if bad_hints:
            warns.append("第 %d 条的使用场景里有不认识的：%s" % (idx, "、".join(bad_hints)))
            hints = [h for h in hints if h in plots.USAGE_HINTS]

        conf = raw.get("confidence")
        if isinstance(conf, bool) or not isinstance(conf, (int, float)):
            conf = None
        else:
            conf = float(conf)
            if conf < 0 or conf > 1:
                warns.append("第 %d 条的 confidence 是 %s，超出 0~1，已置空" % (idx, conf))
                conf = None

        item = {
            "result": result,
            "used_card_ids": norm,
            "title": title[:plots.TITLE_MAX],
            "summary": summary[:plots.SUMMARY_MAX],
            "primary_category": cat,
            "plot_type": ptype,
            "usage_hint": hints,
            "tags": _str_list(raw.get("tags"), 8, 20),
            "role_slots": _str_list(raw.get("role_slots"), plots.ROLE_SLOTS_MAX),
            "beats": _clean_beats(raw.get("beats")),
            "transferable_core": str(raw.get("transferable_core") or "").strip()[:plots.SUMMARY_MAX],
            "optional_variations": _str_list(raw.get("optional_variations"), 8, 80),
            "reason": str(raw.get("reason") or "").strip()[:plots.SUMMARY_MAX],
            "confidence": conf,
            "unsure_points": _str_list(raw.get("unsure_points"), 8, 80),
        }

        # 说能生成、却没给标题 —— 落不成零件（标题是硬要求）。
        # 降级成"拿不准"而不是丢掉，她要能在候选里看到"模型说这里有个零件但没说清"。
        if item["result"] == RESULT_GENERATE and not item["title"]:
            item["result"] = RESULT_UNSURE
            item["reason"] = (item["reason"] + "（模型判了可生成，却没给标题，按「拿不准」处理）").strip()
            warns.append("第 %d 条说可以生成，但标题是空的，已降级为「拿不准」" % idx)

        out.append(item)

    return out, warns


def _ask(cfg, messages):
    """问一次模型。json_mode 先开着试，报 400 就关掉再来一次。

    直接用分类那边那份实现 —— 它踩过的坑（部分兼容实现不认
    response_format）这边一模一样，没必要再踩一遍。
    """
    return cls._ask(cfg, messages)


def call_model(cfg, ctx, items):
    """调一次模型，返回 (payload, raw_text)。

    payload 是校验过的结构化结果（还没有落库）。
    """
    messages = build_messages(ctx, items)
    raw = _ask(cfg, messages)
    if not (raw or "").strip():
        raise InfuseFormatError("模型返回了空内容（可能是被截断或触发了内容策略）")
    payload = _extract_json_object(raw)
    cands, warns = _validate_payload(
        payload, [it["card_id"] for it in items], ctx.get("cat_names") or [])
    payload["items"] = cands
    payload["_warns"] = warns
    return payload, raw


# ----------------------------------------------------------------------
# 七、选目标卡片
# ----------------------------------------------------------------------

# 不送出去的卡片状态。
#   已排除  —— 她明确不要了，送出去就是浪费钱
#   暂不用  —— 她现在不用，也不该拿去生成零件
# 其余（待确认 / 已确认 / 已内化 / 分类失败）都送 —— 内化看的是正文，
# 不是分类结果，一张没分好类的卡照样可能有很好的剧情。
SKIP_STATUS = (sg.STATUS_EXCLUDED, sg.STATUS_UNUSED)

# 「已经跑过了」的卡片状态（默认会被跳过的那几种）。
#
# 【为什么"看过没用"也算跑过】
# 这是从实际用法倒推的：一份六百多张卡的长篇，第一轮跑完大概是
# "两百张出了零件 + 其余全部看过没用"。要是只跳"有零件的"，
# 再点一次内化就会把那些"看过没用"的**原样重送一遍** —— 花一遍钱，
# 一张新零件都不会多。
# 所以「跳过已经跑过的卡」按"这张卡花过钱了"来判，不按"有没有出零件"。
# 她真想换个要求（改补充提示词）重跑那 463 张，把勾去掉就是 ——
# 那个勾存在的唯一意义就是"别重复花钱"。
#
# 没跑过的不算：还没轮到 / 分类失败 / 上一轮跑失败 —— 这些本来就该重送。
CARD_DONE_STATES = (plots.CARD_INFUSE_HAS_PLOT,
                    plots.CARD_INFUSE_UNSUITABLE,
                    plots.CARD_INFUSE_REVIEW)


def _is_done(cid, by_state):
    return by_state.get(cid) in CARD_DONE_STATES


def target_preview(owner, material_id, skip_infused=True, limit=0):
    """看一眼这份素材会送出去多少张卡。一个字都不写库。"""
    with db.connect() as conn:
        m = conn.execute(
            "SELECT id, title, source_collection, local_only FROM materials"
            " WHERE id=? AND owner_id=?", (material_id, owner)).fetchone()
        if not m:
            return {"ok": False, "reason": "no_material",
                    "message": "没有这份素材，或者它不属于你"}

        rows = conn.execute(
            "SELECT id FROM cards WHERE owner_id=? AND material_id=?"
            " AND status NOT IN (%s) ORDER BY start_offset, id"
            % ",".join("?" * len(SKIP_STATUS)),
            [owner, material_id] + list(SKIP_STATUS)).fetchall()
        all_ids = [r["id"] for r in rows]

    by_state = plots.card_infuse_states(owner, all_ids) if all_ids else {}
    n_plot = sum(1 for cid in all_ids
                 if by_state.get(cid) == plots.CARD_INFUSE_HAS_PLOT)
    n_unsuitable = sum(1 for cid in all_ids
                       if by_state.get(cid) == plots.CARD_INFUSE_UNSUITABLE)
    n_review = sum(1 for cid in all_ids
                   if by_state.get(cid) == plots.CARD_INFUSE_REVIEW)

    picked = [cid for cid in all_ids
              if not (skip_infused and _is_done(cid, by_state))]
    # 数量要分得清清楚楚，否则弹层上那行字会自相矛盾
    # （"已经跑过 0 张，跳过 3 张"这种东西她一眼就看出来不对）：
    #   all_card_count  这份素材下能送的卡（已排除 / 暂不用 的本来就不算）
    #   done_before     其中已经跑过的（含"出过零件"和"看过没用"）
    #   skip_by_done    因为勾了"跳过跑过的"而砍掉的
    #   picked_count    砍完之后、还没砍 limit 的数量
    #   cut_by_limit    因为"只跑前 N 张"而砍掉的
    #   will_send       最后真正送出去的  ← 弹层上最显眼的那个数
    done_before = n_plot + n_unsuitable + n_review
    skip_by_done = len(all_ids) - len(picked)
    picked_count = len(picked)
    if limit and limit > 0:
        picked = picked[:limit]

    return {
        "ok": True,
        "material_id": material_id,
        "material_title": m["title"] or "",
        "source_collection": m["source_collection"] or "",
        "local_only": bool(m["local_only"]),
        "all_card_count": len(all_ids),
        "done_before": done_before,
        "has_plot_count": n_plot,
        "not_suitable_count": n_unsuitable,
        "needs_review_count": n_review,
        "skip_by_done": skip_by_done,
        "picked_count": picked_count,
        "cut_by_limit": picked_count - len(picked),
        "will_send": len(picked),
        "skip_infused": bool(skip_infused),
        "limit": limit or 0,
    }


def _target_cards(owner, material_id, skip_infused=True, limit=0):
    """真正要送的卡片 id 列表（列好顺序）。

    "跳过跑过的"口径跟 target_preview 必须**完全一致** ——
    两处用的是同一个 _is_done()。不一致的下场是弹层上说"要送 51 张"、
    点下去实际送了 514 张，她按那个数字估的钱全错。
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM cards WHERE owner_id=? AND material_id=?"
            " AND status NOT IN (%s) ORDER BY start_offset, id"
            % ",".join("?" * len(SKIP_STATUS)),
            [owner, material_id] + list(SKIP_STATUS)).fetchall()
        ids = [r["id"] for r in rows]

    if skip_infused and ids:
        st = plots.card_infuse_states(owner, ids)
        ids = [c for c in ids if not _is_done(c, st)]

    if limit and limit > 0:
        ids = ids[:limit]
    return ids[:MAX_CARDS_PER_RUN]


# ----------------------------------------------------------------------
# 八、发起任务
# ----------------------------------------------------------------------

_create_lock = threading.Lock()


def create_run(owner, material_id, model_key=None, user_prompt=None,
               skip_infused=True, limit=0, background=True,
               retry_of_run_id=None, card_ids=None, prompt_id=None):
    """建一个内化任务。返回 (结果字典, run_id)。

    model_key     用哪个模型；不传就用清单里第一个填了 Key 的
    user_prompt   她这次写的补充提示词；None = 用她存的那份
    skip_infused  跳过已经生成过零件的卡片（默认跳，避免重复出零件）
    limit         只跑前 N 张（0 = 不限）。663 张的稿子先跑 20 张试水用得上。
    background    True 起后台线程立刻返回（接口用）；
                  False 当场跑完再返回（测试和命令行用）
    card_ids      只跑这几张（重试时用）
    prompt_id     从「提示词库」里挑的那条（可以是我自己的，也可以是
                  别人公开出来的那些）。见下。

    【prompt_id 和 user_prompt 的关系】
      两个都传时 **prompt_id 说了算**，user_prompt 被忽略。
      理由是"她刚从快捷选项里挑了一条"是个明确得多的意图，
      而输入框里可能还留着上一次敲的半句话。

    【prompt_id 最要紧的一条：别人的内容不许外泄】
      这条提示词可能是**别人公开出来的**。公开的含义是"她可以拿去用"，
      **不是**"她能看见里面写了什么"。所以这里解析出来的 content
      只落在 user_prompt 这个字段里直接进库（任务自己要用），
      返回值里只带名字、不带内容。

    【为什么默认后台】任务书第十五节明确禁止"把任务执行放在 HTTP 请求里
    长时间阻塞页面"。663 张卡按 12 张一批是 56 批，每批等模型回话
    几十秒 —— 放在请求里，页面直接死在那儿。
    """
    # ---- 先把"方法 + 模型"定下来，错在这里当场报 ----
    try:
        cfg = cls.pick_model(model_key)
    except ValueError as e:
        return {"ok": False, "reason": "no_model", "message": str(e)}, None
    model_key = cfg.get("key") or ""
    model_name = cfg.get("label") or cfg.get("model") or model_key

    # ---- 提示词：库里的某一条优先于自由文本 ----
    # 引用信息（哪条 / 叫什么 / 谁的）单独记一份，用来回答
    # "第一轮到底用的哪条" —— 正文会被她改，名字不会。
    ref_id, ref_name, ref_owner = 0, "", ""
    if prompt_id:
        try:
            ref_id, ref_name, lib_content, ref_owner, _mine = \
                cls.resolve_prompt_for_use(owner, prompt_id,
                                           kind=USER_PROMPT_KIND_INFUSE)
        except ValueError as e:
            return {"ok": False, "reason": "bad_prompt",
                    "message": str(e)}, None
        user_prompt = lib_content

    # ---- 补充提示词：**存快照**，不存"她当前写着什么" ----
    # 她跑完一轮会去改这句再跑第二轮，回头必须还能答出"第一轮到底怎么问的"。
    if prompt_id:
        pass          # 上面已经从库里取了内容，别再被 get_user_prompt 覆盖
    elif user_prompt is None:
        user_prompt = get_user_prompt(owner)
    user_prompt = (user_prompt or "").strip()
    if len(user_prompt) > USER_PROMPT_MAX:
        return {"ok": False, "reason": "prompt_too_long",
                "message": "补充提示词最长 %d 字，现在 %d 字，超了 %d 字。"
                           % (USER_PROMPT_MAX, len(user_prompt),
                              len(user_prompt) - USER_PROMPT_MAX)}, None

    with _create_lock:
        with db.connect() as conn:
            m = conn.execute(
                "SELECT id, title, source_collection FROM materials"
                " WHERE id=? AND owner_id=?", (material_id, owner)).fetchone()
            if not m:
                return {"ok": False, "reason": "no_material",
                        "message": "没有这份素材，或者它不属于你"}, None

            # 规矩五：同一个文件不许同时跑两个任务
            row = conn.execute(
                "SELECT id FROM plot_runs WHERE owner_id=? AND material_id=?"
                " AND status IN (%s) LIMIT 1" % ",".join("?" * len(RUN_ACTIVE)),
                [owner, material_id] + list(RUN_ACTIVE)).fetchone()
            if row:
                return {"ok": False, "reason": "already_running", "run_id": row["id"],
                        "message": "这个文件已经有一个内化任务在跑了（任务 #%d），"
                                   "等它结束或者先取消它。" % row["id"]}, row["id"]

        if card_ids:
            todo = list(card_ids)
        else:
            todo = _target_cards(owner, material_id, skip_infused, limit)

        if not todo:
            # 两种"一张都没有"，分开说清楚 —— 否则她只会看到一句没信息量的失败
            pv = target_preview(owner, material_id, skip_infused, limit)
            if pv.get("ok") and pv.get("done_before"):
                msg = ("这份素材的 %d 张卡都跑过了，没有要新内化的（其中 %d 张"
                       "已经产出零件、%d 张 AI 看过没用、%d 张待定）。"
                       "想再跑一遍就关掉「跳过已经跑过的卡」。"
                       % (pv["done_before"], pv["has_plot_count"],
                          pv["not_suitable_count"], pv["needs_review_count"]))
            else:
                msg = ("这份素材一张能送的卡都没有。先确认它切过卡了 ——"
                       "切分入口在【总素材库】每份文件的「切分」按钮上。")
            return {"ok": False, "reason": "nothing_to_do", "message": msg}, None

        tpl, src, warn = prompt_template()
        ts = now_str()
        with db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO plot_runs (owner_id, material_id, material_title,"
                " source_collection, run_type, input_card_count, skip_infused,"
                " limit_count, model_key, model_name, prompt_version,"
                " template_source, user_prompt_len, user_prompt,"
                " prompt_ref_id, prompt_name, prompt_owner, status,"
                " total_items, retry_of_run_id, error, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (owner, material_id, m["title"] or "", m["source_collection"] or "",
                 "batch", len(todo), 1 if skip_infused else 0, int(limit or 0),
                 model_key, model_name, prompt_version(), src, len(user_prompt),
                 user_prompt, int(ref_id or 0), ref_name or "", ref_owner or "",
                 RUN_QUEUED, len(todo),
                 retry_of_run_id, warn, ts))
            run_id = cur.lastrowid

            for i, cid in enumerate(todo, 1):
                conn.execute(
                    "INSERT OR IGNORE INTO plot_run_cards"
                    " (run_id, card_id, input_order, batch_no, created_at)"
                    " VALUES (?,?,?,?,?)", (run_id, cid, i, 0, ts))
                conn.execute(
                    "INSERT OR IGNORE INTO plot_items"
                    " (run_id, card_id, batch_no, status, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (run_id, cid, 0, ITEM_PENDING, ts, ts))

    if background:
        th = threading.Thread(
            target=_run_worker, args=(run_id, owner),
            name="plot-infuse-%d" % run_id, daemon=True)
        th.start()
        return {"ok": True, "run_id": run_id, "queued": len(todo),
                "model_name": model_name, "reason": "",
                "prompt_version": prompt_version()}, run_id

    _run_worker(run_id, owner)
    return {"ok": True, "run_id": run_id, "queued": len(todo),
            "model_name": model_name, "reason": "",
            "prompt_version": prompt_version()}, run_id


# ----------------------------------------------------------------------
# 九、跑任务
# ----------------------------------------------------------------------

def _set_run(conn, run_id, **fields):
    if not fields:
        return
    keys = list(fields)
    conn.execute("UPDATE plot_runs SET %s WHERE id=?"
                 % ", ".join("%s=?" % k for k in keys),
                 [fields[k] for k in keys] + [run_id])


def _cancelled(run_id):
    """外面有没有人点了取消。

    【为什么每批都重新查一次库，而不是用一个内存标志】
    取消请求是**另一个线程**（HTTP 请求线程）发来的。内存标志要加锁、
    要保证可见性，还得处理"任务刚好在这一刻结束"的竞态。
    直接查库最省心，代价是每批一次 SELECT —— 相对于等模型那几十秒，可以忽略。
    """
    with db.connect() as conn:
        r = conn.execute("SELECT status FROM plot_runs WHERE id=?",
                         (run_id,)).fetchone()
        if not r:
            return True
        return r["status"] == RUN_CANCELLED


def reap_orphan_runs(reason="服务重启了"):
    """把上一个进程留下的"还在跑"的任务收尾。返回收尾了几条。

    【为什么必须有这一步】后台任务是跑在进程内的线程里的。进程一没
    （关服务、热重载、断电），线程也就没了 —— 但任务表里那几条仍然写着
    「进行中」。后果是界面上永远显示进度条、再也不动，而她没有任何办法
    知道它其实早就死了。

    【为什么把没轮到的条目记成"失败"】这样点「重试」正好只补这些没跑的，
    **已经跑过的那几十批不会重花钱**。接上真模型之后，那是真金白银。
    跟分类那边 reap_orphan_runs 同一个设计。

    调用时机：服务启动时（见 main.py 的 lifespan）。
    """
    ts = now_str()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, done_items, total_items FROM plot_runs"
            " WHERE status IN (%s)" % ",".join("?" * len(RUN_ACTIVE)),
            list(RUN_ACTIVE)).fetchall()
        for r in rows:
            cur = conn.execute(
                "UPDATE plot_items SET status=?, error_message=?, updated_at=?"
                " WHERE run_id=? AND status=?",
                (ITEM_FAILED, reason + "，这一条还没轮到", ts, r["id"],
                 ITEM_PENDING))
            _set_run(conn, r["id"], status=RUN_FAILED,
                     error="%s，这个任务中断了（已经跑好的部分都留着）。" % reason,
                     note="点「重试」会从断的地方接着跑，已经跑过的 %s 张不会重花钱。"
                          % (r["done_items"] or 0),
                     finished_at=ts, heartbeat_at=ts)
            print("[内化任务] 任务 #%s 是上次留下的（%s/%s 张），已标成中断；"
                  "其中 %s 条没轮到的记成待重试"
                  % (r["id"], r["done_items"], r["total_items"], cur.rowcount))
    return len(rows)


def _run_worker(run_id, owner):
    """后台线程真正干活的地方。

    这个函数里**不允许**抛异常出去 —— 抛出去线程就死了，
    任务会永远停在「进行中」。所有异常都在这里接住、写进任务里。
    """
    try:
        _execute(run_id, owner)
    except Exception as e:                                # pragma: no cover
        tb = traceback.format_exc()
        try:
            with db.connect() as conn:
                _set_run(conn, run_id, status=RUN_FAILED,
                         error="任务异常：%s" % e, finished_at=now_str())
        except Exception:
            pass
        print("[内化任务] 任务 #%s 崩了：%s\n%s" % (run_id, e, tb))


def _batches(ids):
    """第一批故意小一点，让界面尽早"动"起来。

    跟分类那边同一个理由：663 张按 12 一批是 56 批，每批等模型回话
    几十秒。点完「开始」盯着一个不动的 0% 等一分钟，看起来就跟卡死一样。
    这个误会真的发生过。
    """
    if not ids:
        return
    yield ids[:FIRST_BATCH_SIZE]
    for s in range(FIRST_BATCH_SIZE, len(ids), BATCH_SIZE):
        yield ids[s:s + BATCH_SIZE]


def _execute(run_id, owner):
    # ---- 用哪个模型 / 哪版提示词，**以任务记录为准** ----
    # 为什么不信任外面传进来的参数：任务记录是唯一真相（重试出来的任务
    # 是从老记录复制的），而参数链要经过 create_run → 后台线程 →
    # _run_worker 三层。中间任何一环传丢，都会静默跑成另一版提示词。
    # 那种错最难发现，因为结果看起来"也像那么回事"。
    with db.connect() as conn:
        run = conn.execute("SELECT * FROM plot_runs WHERE id=?",
                           (run_id,)).fetchone()
        if not run:
            return
        material_id = run["material_id"]
        model_key = run["model_key"] or ""

    try:
        cfg = cls.pick_model(model_key)
    except ValueError as e:
        with db.connect() as conn:
            _set_run(conn, run_id, status=RUN_FAILED, error=str(e),
                     finished_at=now_str())
        return

    # ---- 模板在任务开头解析一次就固定下来 ----
    # 为什么不在每批里现读文件：跑的过程中她改了 prompts/infuse.txt 的话，
    # 同一份素材会前后用两版提示词判 —— 结果里混着两套判断依据，而她看不出来。
    tpl, src, warn = prompt_template()
    # 【找的是 cdb 不是 cls】主类清单（含她自建的私有类）住在 classify_db，
    # classification 那边只有跑分类流程的那套。写成 cls.list_categories
    # 不会在 import 时报错，会等到第一个任务真跑起来才炸 —— 而且炸在
    # 后台线程里，界面上只看到"任务失败"，得翻服务端日志才知道原因。
    cats = cdb.list_categories(owner)
    cat_names = [c["name"] for c in cats]
    cat_id_by_name = {c["name"]: c["id"] for c in cats}

    with db.connect() as conn:
        card_ids = [r["card_id"] for r in conn.execute(
            "SELECT card_id FROM plot_run_cards WHERE run_id=?"
            " ORDER BY input_order", (run_id,)).fetchall()]
        _set_run(conn, run_id, status=RUN_RUNNING, started_at=now_str(),
                 heartbeat_at=now_str(), error=warn or "")

    ctx = {
        "template": tpl,
        "prompt_src": src,
        "user_prompt": run["user_prompt"] if "user_prompt" in run.keys() else "",
        "categories": cats,
        "cat_names": cat_names,
        "cat_id_by_name": cat_id_by_name,
        "model_key": model_key,
        "model_name": run["model_name"] or model_key,
        "prompt_version": run["prompt_version"] or prompt_version(),
    }
    # 补充提示词：**读任务上的快照**，不读"她现在写着什么"。
    # 她跑完一轮会去改这句再跑第二轮，回头必须还能答出"第一轮到底怎么问的"。
    # 而且任务跑的过程中她要是顺手改了那个框，同一份素材会前后用两句不同的
    # 要求判 —— 结果里混着两套依据，她看不出来。
    # 老任务（补 user_prompt 那一列之前建的）存的是空串，就用空串：
    # 宁可这一栏空着，也不要在重跑时偷偷换成另一句话。
    ctx["user_prompt"] = (run["user_prompt"] or "") if "user_prompt" in run.keys() else ""

    batches = list(_batches(card_ids))
    total_batches = len(batches)
    with db.connect() as conn:
        _set_run(conn, run_id, total_batches=total_batches)

    done = failed = 0
    cand_total = plot_total = 0
    chars_total = 0
    error_samples = []
    warn_samples = []
    cancelled = False

    for batch_no, batch_ids in enumerate(batches, 1):
        if _cancelled(run_id):
            cancelled = True
            break

        # ---- 取正文：只在这一刻从 materials.content 按偏移现算 ----
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT k.id, k.start_offset, k.end_offset,"
                " k.primary_category_id, c.name AS cat_name"
                " FROM cards k LEFT JOIN categories c"
                " ON c.id = k.primary_category_id"
                " WHERE k.owner_id=? AND k.id IN (%s)"
                % ",".join("?" * len(batch_ids)),
                [owner] + batch_ids).fetchall()
            by_id = {r["id"]: r for r in rows}
            content = ""
            if rows:
                mr = conn.execute("SELECT content FROM materials WHERE id=?",
                                  (material_id,)).fetchone()
                content = (mr["content"] if mr else "") or ""
            # 副标签要 JOIN sub_tags 才拿得到名字（card_tags 里只存 id）。
            # 写法照抄 classify_db._card_tags，别自己另发明一套。
            tags_by_card = {}
            try:
                for tr in conn.execute(
                        "SELECT ct.card_id, t.name FROM card_tags ct"
                        " JOIN sub_tags t ON t.id = ct.sub_tag_id"
                        " WHERE ct.card_id IN (%s) ORDER BY t.name"
                        % ",".join("?" * len(batch_ids)), batch_ids).fetchall():
                    tags_by_card.setdefault(tr["card_id"], []).append(tr["name"])
            except Exception as e:               # pragma: no cover
                # 副标签只是给模型多一点上下文。取不到就少一行，
                # 绝不能因为读副标签失败就让整批卡片跑不成 —— 那是本末倒置。
                print("[内化任务] 副标签读取失败（不影响内化）：%s" % e)
                tags_by_card = {}

            items = []
            for i, cid in enumerate(batch_ids, 1):
                r = by_id.get(cid)
                if r is None:
                    continue
                items.append({
                    "card_id": cid,
                    "seq": i,
                    "text": content[r["start_offset"]:r["end_offset"]],
                    "category": r["cat_name"] or "",
                    "tags": tags_by_card.get(cid) or [],
                })

        if not items:
            continue

        chars_total += sum(len(it["text"]) for it in items)

        ts = now_str()
        with db.connect() as conn:
            for it in items:
                conn.execute(
                    "UPDATE plot_items SET status=?, batch_no=?, updated_at=?"
                    " WHERE run_id=? AND card_id=?",
                    (ITEM_RUNNING, batch_no, ts, run_id, it["card_id"]))
            _set_run(conn, run_id, heartbeat_at=ts)

        try:
            payload, raw = call_model(cfg, ctx, items)
            err = ""
        except InfuseFormatError as e:
            payload, raw, err = None, "", "返回格式不合规：%s" % e
        except Exception as e:
            payload, raw, err = None, "", "调模型出错：%s" % e

        ts = now_str()
        if err:
            failed += len(items)
            if len(error_samples) < 3:
                error_samples.append(err)
            with db.connect() as conn:
                for it in items:
                    conn.execute(
                        "UPDATE plot_items SET status=?, error_message=?,"
                        " retry_count=retry_count+1, updated_at=?"
                        " WHERE run_id=? AND card_id=?",
                        (ITEM_FAILED, err, ts, run_id, it["card_id"]))
            with db.connect() as conn:
                _set_run(conn, run_id, done_batches=batch_no,
                         done_items=done, failed_items=failed, heartbeat_at=ts)
            continue

        warns = payload.get("_warns") or []
        if warns and len(warn_samples) < 5:
            warn_samples.extend(warns[:5 - len(warn_samples)])

        cands, adopted = _write_batch(owner, run_id, batch_no, cfg, ctx,
                                      items, payload, raw)
        cand_total += cands
        plot_total += adopted

        # ---- 逐张卡记去向 ----
        assessment = payload.get("input_assessment") or {}
        not_used = set()
        if isinstance(assessment, dict):
            for x in (assessment.get("not_used_card_ids") or []):
                try:
                    not_used.add(int(x))
                except (TypeError, ValueError):
                    pass
        used_any = set()
        for c in payload.get("items") or []:
            used_any.update(c.get("used_card_ids") or [])

        with db.connect() as conn:
            for it in items:
                cid = it["card_id"]
                n_here = sum(1 for c in (payload.get("items") or [])
                             if cid in (c.get("used_card_ids") or []))
                if cid in used_any:
                    outcome, st = CARD_USED, ITEM_DONE
                    done += 1
                elif cid in not_used:
                    outcome, st = CARD_UNUSED, ITEM_DONE
                    done += 1
                else:
                    # 模型既没说用了、也没列进 not_used。归到"看过没用"并留个记号，
                    # 不假装它"已内化" —— 那会让界面上多出一批假的已内化标记。
                    outcome, st = CARD_UNUSED, ITEM_DONE
                    done += 1
                conn.execute(
                    "UPDATE plot_items SET status=?, outcome=?, reason=?,"
                    " candidate_count=?, updated_at=?"
                    " WHERE run_id=? AND card_id=?",
                    (st, outcome, "", n_here, ts, run_id, cid))
            _set_run(conn, run_id, done_batches=batch_no, done_items=done,
                     failed_items=failed, candidate_count=cand_total,
                     created_plot_count=plot_total, total_input_chars=chars_total,
                     heartbeat_at=ts)

    # ---- 收尾 ----
    ts = now_str()
    if cancelled:
        status = RUN_CANCELLED
    elif failed == 0:
        status = RUN_COMPLETED
    elif done == 0:
        status = RUN_FAILED
    else:
        status = RUN_PARTIAL

    notes = []
    if warn_samples:
        notes.append("有几处小问题（不影响落库）：" + "；".join(warn_samples[:5]))
    if error_samples:
        notes.append("失败原因：%s" % error_samples[0])
    if ctx["prompt_src"] == "builtin" and warn:
        notes.append(warn)
    if status == RUN_PARTIAL:
        notes.append("点「重试」只会补失败的那 %d 张，跑好的不会重花钱。" % failed)

    with db.connect() as conn:
        _set_run(conn, run_id, status=status,
                 done_items=done, failed_items=failed,
                 candidate_count=cand_total, created_plot_count=plot_total,
                 total_input_chars=chars_total,
                 note="\n".join(notes), finished_at=ts, heartbeat_at=ts)


def _write_batch(owner, run_id, batch_no, cfg, ctx, items, payload, raw):
    """把一批的候选写进库，并把 result=generate 的落成正式零件。

    返回 (候选条数, 落成零件的条数)。

    【为什么 generate 就直接落库，而不是先在候选池等她挑】
    这是她定的（2026-09-25）：AI 提的直接出现在【剧情内化库】里，
    状态是「待确认」+ 标「AI 提的」+ 带置信度和理由，她逐条确认／改／排除。
    任务书要的"AI 候选不会自动成为人工确认结果"由**状态**保证，
    不是由"隔一层候选池"保证 —— 状态是"待确认"就绝不会被当成她认过的。

    候选记录照样存全份（含模型的原始返回），所以第 3 步做多模型比较、
    或者以后想溯源"这条到底是哪个模型哪一版提示词提的"，都拿得到。
    """
    cands = payload.get("items") or []
    group_key = "run%d-batch%d" % (run_id, batch_no)
    ts = now_str()
    adopted = 0

    for c in cands:
        used = c.get("used_card_ids") or []
        with db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO plot_candidates (run_id, owner_id, batch_no,"
                " model_key, model_name, prompt_version, candidate_group_key,"
                " result, title, summary, plot_type, primary_category,"
                " usage_hint_json, tags_json, beats_json, role_slots_json,"
                " used_card_ids_json, transferable_core, variations_json,"
                " reason, confidence, unsure_points_json, raw_response,"
                " created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, owner, batch_no,
                 cfg.get("key") or "", cfg.get("label") or cfg.get("model") or "",
                 ctx.get("prompt_version") or "", group_key,
                 c.get("result") or "", c.get("title") or "",
                 c.get("summary") or "", c.get("plot_type") or "",
                 c.get("primary_category") or "",
                 json.dumps(c.get("usage_hint") or [], ensure_ascii=False),
                 json.dumps(c.get("tags") or [], ensure_ascii=False),
                 json.dumps(c.get("beats") or {}, ensure_ascii=False),
                 json.dumps(c.get("role_slots") or [], ensure_ascii=False),
                 json.dumps(used, ensure_ascii=False),
                 c.get("transferable_core") or "",
                 json.dumps(c.get("optional_variations") or [], ensure_ascii=False),
                 c.get("reason") or "", c.get("confidence"),
                 json.dumps(c.get("unsure_points") or [], ensure_ascii=False),
                 (raw or "")[:200000], ts))
            cand_id = cur.lastrowid

        if c.get("result") != RESULT_GENERATE or not used:
            continue

        pid, adopt_err = _adopt_candidate(owner, c, ctx)
        with db.connect() as conn:
            if pid:
                conn.execute("UPDATE plot_candidates SET adopted=1, plot_id=?"
                             " WHERE id=?", (pid, cand_id))
                adopted += 1
            elif adopt_err:
                conn.execute("UPDATE plot_candidates SET adopt_error=?"
                             " WHERE id=?", (adopt_err[:500], cand_id))

    return len(cands), adopted


def _adopt_candidate(owner, c, ctx):
    """把一个 generate 候选落成正式零件。返回 (plot_id, 错误语)。

    主分类的解析顺序（这条是"按原素材标签分类"的落点）：
      1. 模型判的 primary_category —— 它按零件的用途判的，最贴
      2. 模型没判出来 → 退回**来源卡片的主分类**
         （她要的"按原素材标签分类就在这里兜底"）
      3. 都没有 → 未分类（None），照样落库，不因为没分类就不给她看

    绝不能因为"分类没判出来"就把零件丢了 —— 零件本身才是她要的东西。
    """
    cat_id = None
    cat_name = (c.get("primary_category") or "").strip()
    cat_id_by_name = ctx.get("cat_id_by_name") or {}
    if cat_name and cat_name in cat_id_by_name:
        cat_id = cat_id_by_name[cat_name]

    if cat_id is None:
        used = c.get("used_card_ids") or []
        if used:
            with db.connect() as conn:
                ids = [r["id"] for r in conn.execute(
                    "SELECT DISTINCT primary_category_id AS id FROM cards"
                    " WHERE owner_id=? AND id IN (%s)"
                    " AND primary_category_id IS NOT NULL"
                    % ",".join("?" * len(used)), [owner] + used).fetchall()]
            visible = set(cat_id_by_name.values())
            ids = [i for i in ids if i in visible]
            if ids:
                # 多张来源卡主类不一致时，取 id 最小的那个系统类
                # （排序稳定，重跑结果不会飘）。真要精确归属她自己会改。
                cat_id = sorted(ids)[0]

    try:
        p = plots.create_plot(
            owner,
            title=c.get("title") or "",
            summary=c.get("summary") or "",
            plot_type=c.get("plot_type") or "",
            usage_hints=c.get("usage_hint") or [],
            beats=c.get("beats") or {},
            role_slots=c.get("role_slots") or [],
            category_id=cat_id,
            source=plots.PLOT_SOURCE_AI,
            status=plots.PLOT_STATUS_AI_SUGGESTED,
            card_ids=c.get("used_card_ids") or [],
            ai_reason=c.get("reason") or "",
            ai_confidence=c.get("confidence"),
            change_note="AI 内化自动生成（模型 %s）" % (ctx.get("model_name") or ""),
            created_by=owner,
        )
        return p["id"], ""
    except Exception as e:
        return 0, "落库失败：%s" % e


# ----------------------------------------------------------------------
# 十、读任务 / 取消 / 重试
# ----------------------------------------------------------------------

def _parse_ts(s):
    """解析库里那种 '2026-09-25 18:45:50' 的时间串。"""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s, fmt)
        except ValueError:
            continue
    return None


def _run_dict(row):
    d = dict(row)

    # ---- 补充提示词的正文要不要下发 ----
    #
    # 【这条规矩是跟分类那边学的，别改回去】
    #   内化现在也能从「提示词库」挑一条来跑，而库里可能有**别人公开出来的**条目。
    #   公开的含义是"她可以拿去用"，**不是**"她能看见里面写了什么"。
    #   正文一旦下发到浏览器，就等于公开了 —— 所以引用别人的条目跑出来的任务，
    #   正文一律抹成空、只留字数；界面上显示"用了《XXX》那条（别人的，内容不公开）"。
    #
    #   不是别人的（自己写的 / 自由文本敲的）原样留着 ——
    #   那是她自己的字，留着有用：可以拿历史任务对照"上一轮我到底怎么要求的"。
    ref = d.get("prompt_ref_id") or 0
    ref_owner = d.get("prompt_owner") or ""
    mine = (not ref_owner) or (ref_owner == (d.get("owner_id") or ""))
    if ref and not mine:
        d["user_prompt"] = ""
    d["user_prompt_hidden"] = bool(ref and not mine)
    # 归属标记（__u<数字>）→ 给人看的名字。查不到就原样返回，绝不编。
    d["prompt_owner_label"] = cls._owner_label(ref_owner) if ref_owner else ""

    d["status_label"] = d.get("status") or ""
    d["active"] = d.get("status") in RUN_ACTIVE

    total = d.get("total_items") or 0
    d["progress"] = 0.0 if not total else round(
        min(1.0, ((d.get("done_items") or 0) + (d.get("failed_items") or 0))
            / float(total)), 4)
    # percent 用**批次**算，progress 用**张数**算。两个都留着：
    # "已经跑到第几批"回答的是"还要多久"，"已经处理多少张"回答的是"进度条该画到哪"。
    d["percent"] = (round(100.0 * (d.get("done_batches") or 0)
                          / (d.get("total_batches") or 1), 1)
                    if d.get("total_batches") else 0.0)

    # 心跳：从"最后动过"到现在过了几秒。**服务端算好秒数传出去**，
    # 别让界面自己拿浏览器时钟做减法 —— 她那台机器时间要是偏了几分钟，
    # 就会看到"最后更新 -320 秒前"这种荒唐的数。
    d["stale_seconds"] = None
    if d.get("heartbeat_at"):
        t0 = _parse_ts(d["heartbeat_at"])
        t1 = _parse_ts(d["finished_at"]) or datetime.now()
        if t0:
            d["stale_seconds"] = max(0, int((t1 - t0).total_seconds()))
    return d


def get_run(run_id, owner):
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM plot_runs WHERE id=? AND owner_id=?",
                         (run_id, owner)).fetchone()
    return _run_dict(r) if r else None


def list_runs(owner, material_id=None, limit=20):
    sql = "SELECT * FROM plot_runs WHERE owner_id=?"
    args = [owner]
    if material_id:
        sql += " AND material_id=?"
        args.append(material_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit or 20))
    with db.connect() as conn:
        return [_run_dict(r) for r in conn.execute(sql, args).fetchall()]


def list_items(run_id, owner, status=None, outcome=None, limit=1000, offset=0):
    sql = ("SELECT i.* FROM plot_items i JOIN plot_runs r ON r.id = i.run_id"
           " WHERE i.run_id=? AND r.owner_id=?")
    args = [run_id, owner]
    if status:
        sql += " AND i.status=?"
        args.append(status)
    if outcome:
        sql += " AND i.outcome=?"
        args.append(outcome)
    sql += " ORDER BY i.batch_no, i.card_id LIMIT ? OFFSET ?"
    args += [int(limit or 1000), int(offset or 0)]
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def list_candidates(run_id, owner, result=None, limit=200):
    sql = "SELECT * FROM plot_candidates WHERE run_id=? AND owner_id=?"
    args = [run_id, owner]
    if result:
        sql += " AND result=?"
        args.append(result)
    sql += " ORDER BY id LIMIT ?"
    args.append(int(limit or 200))
    with db.connect() as conn:
        out = []
        for r in conn.execute(sql, args).fetchall():
            d = dict(r)
            for k, j in (("usage_hint_json", "usage_hint"),
                         ("tags_json", "tags"), ("beats_json", "beats"),
                         ("role_slots_json", "role_slots"),
                         ("used_card_ids_json", "used_card_ids"),
                         ("variations_json", "optional_variations"),
                         ("unsure_points_json", "unsure_points")):
                try:
                    d[j] = json.loads(d.pop(k) or "[]")
                except Exception:                        # pragma: no cover
                    d.pop(k, None)
                    d[j] = [] if j != "beats" else {}
            d["result_label"] = RESULT_LABELS.get(d.get("result"), d.get("result"))
            d["low_confidence"] = (d.get("confidence") is not None
                                   and d["confidence"] < CONFIDENCE_LOW)
            out.append(d)
        return out


def states_for_owner(owner):
    """总素材库里每一份素材的内化状态 —— 一次全给齐。

    【为什么单开一个，不让前端拉 /api/infuse-runs 自己分组】
    15 份素材就是 15 次请求；而且"跑过几张 / 还有几张没跑过"这件事
    要从卡片状态现算，前端根本算不出来（它只有任务记录，不知道卡片）。
    跟分类那条线一样：多一个接口，坏了也只坏这一个状态条。
    """
    with db.connect() as conn:
        mats = conn.execute(
            "SELECT id, title, source_collection FROM materials"
            " WHERE owner_id=? ORDER BY id", (owner,)).fetchall()
        if not mats:
            return {"items": {}, "running_count": 0}
        rows = conn.execute(
            "SELECT material_id, id FROM cards WHERE owner_id=?"
            " AND status NOT IN (%s) ORDER BY material_id, start_offset, id"
            % ",".join("?" * len(SKIP_STATUS)),
            [owner] + list(SKIP_STATUS)).fetchall()
        runs = conn.execute(
            "SELECT * FROM plot_runs WHERE owner_id=? ORDER BY id DESC",
            (owner,)).fetchall()

    by_mat = {}
    for r in rows:
        by_mat.setdefault(r["material_id"], []).append(r["id"])
    # 全部卡片一次算完 —— 她的库才 793 张，一次 JOIN 比 15 次便宜。
    st = plots.card_infuse_states(owner) if rows else {}

    latest = {}
    for r in runs:
        latest.setdefault(r["material_id"], r)      # 已经按 id DESC，头一条最新

    items, running = {}, 0
    for m in mats:
        mid = m["id"]
        ids = by_mat.get(mid) or []
        n_plot = sum(1 for c in ids
                     if st.get(c) == plots.CARD_INFUSE_HAS_PLOT)
        n_done = sum(1 for c in ids if _is_done(c, st))
        r = latest.get(mid)
        run = _run_dict(r) if r is not None else None
        state = _run_state(r)
        if state == "running":
            running += 1
        items[str(mid)] = {
            "material_id": mid,
            "title": m["title"] or "",
            "source_collection": m["source_collection"] or "",
            "all_card_count": len(ids),
            "done_before": n_done,
            "has_plot_count": n_plot,
            "fresh_count": len(ids) - n_done,     # 还有几张没跑过
            "state": state,
            "run": run,
        }
    return {"items": items, "running_count": running}


def _run_state(r):
    """任务记录 → 界面上认的那几个状态词。

    没跑过是 "never"；排队中/进行中合成 "running"（对她来说都是"正在跑"）。
    """
    if r is None:
        return "never"
    s = r["status"] if not isinstance(r, dict) else r.get("status")
    if s in RUN_ACTIVE:
        return "running"
    return {
        RUN_COMPLETED: "done",
        RUN_PARTIAL: "partial",
        RUN_FAILED: "failed",
        RUN_CANCELLED: "cancelled",
    }.get(s, "never")


def cancel_run(run_id, owner):
    with db.connect() as conn:
        r = conn.execute("SELECT status FROM plot_runs WHERE id=? AND owner_id=?",
                         (run_id, owner)).fetchone()
        if not r:
            raise ValueError("没有这个任务，或者它不属于你")
        if r["status"] not in RUN_ACTIVE:
            raise ValueError("这个任务已经结束了（%s），不用取消。" % r["status"])
        _set_run(conn, run_id, status=RUN_CANCELLED, finished_at=now_str())
    return True


def retry_run(run_id, owner, background=True):
    """重试失败的那些卡。返回 (结果字典, 新任务 id)。

    【只补"没轮到的"和"失败的"】
    已经跑好的那几十批绝不重跑 —— 那是真金白银。
    这也正是 reap_orphan_runs 要把没轮到的记成"失败"的原因。
    """
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM plot_runs WHERE id=? AND owner_id=?",
                         (run_id, owner)).fetchone()
        if not r:
            raise ValueError("没有这个任务，或者它不属于你")
        if r["status"] in RUN_ACTIVE:
            raise ValueError("这个任务还在跑，先等它结束或取消它。")
        todo = [x["card_id"] for x in conn.execute(
            "SELECT card_id FROM plot_items WHERE run_id=? AND status<>?"
            " ORDER BY batch_no, card_id", (run_id, ITEM_DONE)).fetchall()]
        material_id = r["material_id"]
        model_key = r["model_key"]

    if not todo:
        return {"ok": False, "reason": "nothing_to_retry",
                "message": "这个任务没有失败的条目，不用重试。"}, None

    # 补充提示词**沿用原任务那份快照**，不是她现在存着的那句。
    # 理由跟"沿用同一个模型"一样：中途换掉的话，同一份素材里一半是按
    # 上次的要求判的、一半按新的判的，她看不出这个区别。
    # 她真想换要求，那是新的一轮 —— 重新点【AI 内化】，不是点重试。
    #
    # 【为什么引用信息要另外拷，而不是把 prompt_id 再传一遍】
    #   传 prompt_id 会让服务端**重新去库里取一次内容** ——
    #   那条提示词要是被她改过、或者已经删了，重试就变成了"用新要求跑旧任务"，
    #   甚至直接报错跑不起来。这里要的是"重演原任务"，所以正文用快照，
    #   引用信息也只是**照抄一份标签**，不产生任何新的取数行为。
    res, new_id = create_run(owner, material_id, model_key=model_key,
                             user_prompt=r["user_prompt"] or "",
                             skip_infused=False, limit=0,
                             background=background,
                             retry_of_run_id=run_id, card_ids=todo)
    if new_id and r["prompt_ref_id"]:
        with db.connect() as conn:
            conn.execute(
                "UPDATE plot_runs SET prompt_ref_id=?, prompt_name=?,"
                " prompt_owner=? WHERE id=?",
                (r["prompt_ref_id"], r["prompt_name"] or "",
                 r["prompt_owner"] or "", new_id))
    return res, new_id


# ----------------------------------------------------------------------
# 十一、自测（python backend/plots_ai.py 直接跑）
# ----------------------------------------------------------------------

def _code_only(src):
    """把注释和字符串字面量**挖成空格**，只留能执行的那部分。

    给自测里的"跨模块引用名字对不对"用的正则扫描打底。
    不挖空的话，注释和文档字符串里那句"写成 cls.list_categories 会炸"
    本身就会被当成一处引用 —— 每写一句解释就多一条假警告。

    挖成空格而不是删掉：保持列位置不变，多行文档字符串也不会把后续行错位。
    tokenize 万一解析不了（源码有问题），退回原文：宁可多报几条，
    也不要因为怕误报而漏掉真错。
    """
    import io as _io
    import tokenize as _tk

    out = [list(ln) for ln in src.splitlines(keepends=True)]

    def blank(srow, scol, erow, ecol):
        for row in range(srow, erow + 1):
            i = row - 1
            if i < 0 or i >= len(out):
                continue
            a = scol if row == srow else 0
            b = ecol if row == erow else len(out[i])
            for c in range(a, min(b, len(out[i]))):
                if out[i][c] not in "\r\n":
                    out[i][c] = " "

    try:
        for tok in _tk.generate_tokens(_io.StringIO(src).readline):
            if tok.type in (_tk.COMMENT, _tk.STRING):
                blank(tok.start[0], tok.start[1], tok.end[0], tok.end[1])
    except Exception:                                     # pragma: no cover
        return src
    return "".join("".join(x) for x in out)


def _self_check():
    import shutil
    import tempfile

    ok = fail = 0

    def check(name, got, want=True):
        nonlocal ok, fail
        good = (got == want)
        print("  %s %s" % ("OK  " if good else "FAIL", name))
        if not good:
            print("       实际 %r / 期望 %r" % (got, want))
        if good:
            ok += 1
        else:
            fail += 1

    tmp = tempfile.mkdtemp(prefix="moge_plotsai_self_")
    old = os.environ.get("MOGE_DATA_DIR")
    os.environ["MOGE_DATA_DIR"] = tmp
    try:
        # 模块级常量已经按老路径算过，这里重新指一次。
        # 【这道保险不能省】下面有一段会**写**库（存补充提示词）。
        # 万一 DB_PATH 没指过去，写的就是她的真实库。
        db.DATA_DIR = tmp
        db.DB_PATH = os.path.join(tmp, "moge.db")
        if tmp not in db.DB_PATH:
            print("!! 库路径没指到临时目录，为保险直接停下")
            return False

        db.init_db()
        plots.migrate()
        migrate()
        # user_prompts / ai_judgements 这些表归 classify_db 管，
        # 按 main.py 启动时的真实顺序把它们也建起来，
        # 否则下面那段"存补充提示词"的测试会报 no such table。
        cdb.migrate()
        cls.migrate()

        print("\n[1] 建表")
        with db.connect() as conn:
            for t in ("plot_runs", "plot_run_cards", "plot_items",
                      "plot_candidates"):
                check("表 %s 建好了" % t, _has_table(conn, t))

        print("\n[2] 模板里的六个槽位")
        tpl, src, warn = prompt_template()
        check("通用模板六个槽位齐全",
              all(("{%s}" % s) in tpl for s in REQUIRED_SLOTS))
        # prompts/infuse.txt 在真实项目里存在，但自测跑在项目目录下，
        # 读到的会是真文件 —— 两种都要能过：槽位齐全即可。
        check("拿到的模板六个槽位齐全",
              all(("{%s}" % s) in tpl for s in REQUIRED_SLOTS))
        check("来源是 file 或 builtin", src in ("file", "builtin"))

        print("\n[3] 单遍扫描替换（正文里的花括号不许被二次替换）")
        t = _fill_slots("A{categories}B{user_prompt}C{card_blocks}D",
                        categories="类", user_prompt="要求",
                        card_blocks='正文里写了 {user_prompt} 这四个字')
        check("正文里的 {user_prompt} 原样保留",
              "{user_prompt}" in t.split("C")[1].split("D")[0])
        # 真正的槽位都换掉了，全文只剩下正文里那一个字面量。
        # 注意不能断言"全文没有 {user_prompt}" —— 正文里那个**就该留着**，
        # 把它替换掉才是 bug（会把别人的补充要求塞进正文中间）。
        check("真正的槽位都换掉了，只剩正文里那个字面量",
              "{categories}" not in t and "{card_blocks}" not in t
              and t.count("{user_prompt}") == 1
              and t.startswith("A类B要求C"))

        print("\n[4] 抠 JSON")
        check("带围栏也能抠出来",
              _extract_json_object('```json\n{"items": []}\n```')["items"] == [])
        check("前面有废话也能抠出来",
              _extract_json_object('好的，结果如下：\n{"items": [1]}')["items"] == [1])
        check("只给数组时包一层",
              _extract_json_object('[{"a": 1}]')["items"] == [{"a": 1}])
        try:
            _extract_json_object("我什么都不会说")
            check("找不到 JSON 要报错", False)
        except InfuseFormatError:
            check("找不到 JSON 要报错", True)

        print("\n[5] 校验：硬伤整批拒收")
        cat_names = ["情节", "神态"]
        for bad, why in (
                ({"nope": 1}, "顶层没有 items"),
                ({"items": "abc"}, "items 不是数组"),
                ({"items": [{"result": "maybe"}]}, "result 不认识"),
                ({"items": [{"result": "generate",
                             "used_card_ids": "1"}]}, "used_card_ids 不是数组"),
                ({"items": [{"result": "generate",
                             "used_card_ids": [999]}]}, "引用了本批之外的卡")):
            try:
                _validate_payload(bad, [1, 2], cat_names)
                check("整批拒收：%s" % why, False)
            except InfuseFormatError:
                check("整批拒收：%s" % why, True)

        print("\n[6] 校验：软伤逐条降级")
        cands, warns = _validate_payload({"items": [
            {"result": "generate", "used_card_ids": [1, 1, 2],
             "title": "", "summary": "x"},
            {"result": "unsuitable", "used_card_ids": [2],
             "primary_category": "不存在的类", "plot_type": "不存在的型",
             "confidence": 3.5,
             "beats": {"setup": "有", "乱写的键": "丢"},
             "usage_hint": ["初遇", "没这个场景", "初遇"]},
        ]}, [1, 2], cat_names)
        check("used_card_ids 去重了", cands[0]["used_card_ids"] == [1, 2])
        check("没标题的 generate 降级成 unsure",
              cands[0]["result"] == RESULT_UNSURE)
        check("不认识的主类被清空", cands[1]["primary_category"] == "")
        check("不认识的剧情类型被清空", cands[1]["plot_type"] == "")
        check("confidence 超范围置空", cands[1]["confidence"] is None)
        check("beats 只留认识的键", set(cands[1]["beats"]) == {"setup"})
        check("不认识的使用场景被剔除",
              cands[1]["usage_hint"] == ["初遇"])
        check("软伤有警告留下来", len(warns) >= 3, True)

        print("\n[7] 补充提示词超长要拒绝")
        try:
            set_user_prompt("__u1", "字" * (USER_PROMPT_MAX + 1))
            check("超长要报错", False)
        except ValueError:
            check("超长要报错", True)
        set_user_prompt("__u1", "重点提炼拉扯")
        check("存进去能读回来", get_user_prompt("__u1") == "重点提炼拉扯")
        set_user_prompt("__u1", "")
        check("清空后是空串", get_user_prompt("__u1") == "")

        # ---- [8] 跨模块引用的名字不许拼错 --------------------------------
        # 这一条是被真事逼出来的：_execute 里写成 cls.list_categories(owner)，
        # 而主类清单住在 classify_db。**import 那一刻不报错**（属性是运行时
        # 才取的），一直到第一个任务真跑起来才在**后台线程**里炸 ——
        # 界面上只显示"任务失败"，得翻服务端日志才知道是拼错了模块。
        # 所以在这里把两个文件的跨模块前缀静态扫一遍，挡在提交之前。
        #
        # 【必须先把注释和字符串挖空】这是拿正则扫源码的，而注释里天天
        # 出现 "cls.list_categories" 这种话（就在这一段的上面一句里）。
        # 不挖空的话，每一句解释性注释都变成一条假警告 —— 而假警告多了，
        # 真警告就没人看了。
        print("\n[8] 跨模块引用的名字都存在（import 时抓不到的那种错）")
        # plots_db 反过来要 plots_ai 的状态词，这层依赖是"反的"，
        # 没法靠 import 检查，只能扫源码。
        scans = [("plots_ai.py", {"cls": cls, "cdb": cdb, "plots": plots,
                                 "sg": sg, "db": db}),
                 ("plots_db.py", {"pai": sys.modules[__name__], "db": db})]
        bad = []
        for fname, mods_ in scans:
            with open(os.path.join(ROOT_DIR, "backend", fname),
                      encoding="utf-8") as f:
                src = _code_only(f.read())
            for prefix, mod in mods_.items():
                for name in sorted(set(re.findall(
                        r"\b%s\.([A-Za-z_]\w*)" % prefix, src))):
                    if not hasattr(mod, name):
                        bad.append("%s 里的 %s.%s" % (fname, prefix, name))
        if bad:
            print("       对不上的引用：%s" % "；".join(bad))
        check("跨模块引用的属性全存在", not bad, True)
        check("★ 挖注释这一步真的在起作用（不然上面那条是假绿）",
              "cls" not in _code_only("x = 1  # cls.foo 在注释里\n"))

        # ---- [9] 提示词按用途分档 + 别人的正文不下发 ----------------------
        # 这两件事隔着一个功能犯过错，放在一起挡：
        #   ① 内化要用自己的提示词库（kind='infuse'），跟分类那档互不串门。
        #      串了不报错 —— 只会让她在做内化时挑到一条讲"怎么判主类"的话，
        #      白跑一轮钱才发现模型答非所问。
        #   ② 挑"别人公开出来的"那条来跑时，正文**只能进任务、不能进浏览器**。
        #      "公开"= 她能拿去用，≠ 她能看到里面写了什么。
        print("\n[9] 提示词分档 + 别人的正文不下发")
        cls.create_library_prompt("__u1", "分类话术", "按外貌气质判",
                                  kind=cls.PROMPT_KIND_CLASSIFY)
        cls.create_library_prompt("__u1", "内化话术", "只提炼拉扯",
                                  kind=cls.PROMPT_KIND_INFUSE)
        names = lambda k: [x["name"] for x in
                           cls.list_my_library_prompts("__u1", k)]
        check("★ 分类那档只看得到分类的",
              names(cls.PROMPT_KIND_CLASSIFY), ["分类话术"])
        check("★ 内化那档只看得到内化的",
              names(cls.PROMPT_KIND_INFUSE), ["内化话术"])
        # 默认值必须还是 classify —— 老前端不传 kind，不能因为这次改动就换档
        check("不传 kind 时默认还是分类那档（老前端不能坏）",
              [x["name"] for x in cls.list_my_library_prompts("__u1")],
              ["分类话术"])
        # 显式传 None 也要兜成默认档。空列表长得像"我存的东西丢了"，
        # 其实一条没少 —— 这种静默失败最难查，所以专门钉一条。
        check("★ 显式传 None 兜成默认档，不许静默返回空列表",
              names(None), ["分类话术"])
        try:
            cls.check_prompt_kind("outline")
            check("不认识的用途要报错（不许静默退默认档）", False)
        except ValueError:
            check("不认识的用途要报错（不许静默退默认档）", True)

        # 改一条：不能因为"取的时候用了默认 kind"而取不到。
        # 这个坑是真踩过的 —— update_library_prompt 里写
        # get_library_prompt(owner, pid)（默认 kind='classify'），
        # 于是内化那条永远返回 None，表现是"点了保存没反应"。
        pid_inf = cls.list_my_library_prompts(
            "__u1", cls.PROMPT_KIND_INFUSE)[0]["id"]
        upd = cls.update_library_prompt("__u1", pid_inf, {"name": "内化话术v2"})
        check("★ 改内化那条能改到（按默认 kind 取会返回 None）", bool(upd))
        check("改完名字生效", (upd or {}).get("name"), "内化话术v2")
        check("★ 改完还在内化那档，没串到分类档去",
              names(cls.PROMPT_KIND_INFUSE), ["内化话术v2"])

        # 别人的公开条目：能用，但内容不给前端
        cls.create_library_prompt("__u2", "别人的内化话术", "别人的私房话",
                                  visibility=cls.VIS_PUBLIC,
                                  kind=cls.PROMPT_KIND_INFUSE)
        pub = cls.list_public_library_prompts("__u1", cls.PROMPT_KIND_INFUSE)
        check("别人的公开条目在内化档看得到", len(pub), 1)
        check("★ 公开条目只给名字、不给正文", "content" in pub[0], False)
        check("但给了字数（她要靠这个判断值不值得用）",
              pub[0]["content_length"] > 0, True)

        _pid, _nm, body, _ow, _mine = cls.resolve_prompt_for_use(
            "__u1", pub[0]["id"], kind=cls.PROMPT_KIND_INFUSE)
        check("服务端取得到别人的正文（任务要用）", body, "别人的私房话")
        # 把"服务端取到的那份正文"塞进一条任务行，看下发给前端时会不会漏
        row = {"owner_id": "__u1", "prompt_ref_id": pub[0]["id"],
               "prompt_owner": "__u2", "prompt_name": _nm,
               "user_prompt": body, "status": RUN_QUEUED,
               "total_items": 0, "total_batches": 0,
               "heartbeat_at": "", "finished_at": ""}
        d = _run_dict(row)
        check("★ 发给前端的任务里，别人的正文被抹成空", d["user_prompt"], "")
        check("★ 但要标出来「这是别人的条目」", d["user_prompt_hidden"], True)
        check("名字照给（好让她知道这轮用的是哪条）",
              d["prompt_name"], "别人的内化话术")
        check("归属照给（好让她知道是谁写的）",
              d["prompt_owner_label"], "__u2")
        # 自己写的那条 → 正文照留
        row2 = dict(row, prompt_owner="__u1")
        check("自己的条目正文照留", _run_dict(row2)["user_prompt"], body)
        check("自己的条目不标「内容不公开」",
              _run_dict(row2)["user_prompt_hidden"], False)
        # 根本没引用库里任何一条（自由文本敲的）→ 正文照留
        row3 = dict(row, prompt_ref_id=0, prompt_owner="")
        check("自由文本敲的正文照留", _run_dict(row3)["user_prompt"], body)
        check("自由文本不标「内容不公开」",
              _run_dict(row3)["user_prompt_hidden"], False)
    finally:
        if old is None:
            os.environ.pop("MOGE_DATA_DIR", None)
        else:
            os.environ["MOGE_DATA_DIR"] = old
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (ok, fail))
    print("=" * 56)
    return fail == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if _self_check() else 1)
