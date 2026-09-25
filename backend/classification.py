# -*- coding: utf-8 -*-
"""
墨阁 · 自动分类任务（编排层）
========================================================
这个文件管一件事：**「点一下自动分类」之后发生的全部事情**。

三个文件的分工

    segmentation.py    纯算法。把正文切成段。不碰数据库，不知道卡片是什么。
    classify_db.py     数据层。卡片 / 切分 / 变更记录 怎么存、怎么取。
    classification.py  编排层。**什么时候、把哪些卡片、交给谁去判、判完怎么写回去。**

为什么这一层单独一个文件

    这一层将来一定会换：现在是"占位规则"，以后换成真的大模型，
    再以后可能换第二家、加提示词版本管理、加并发限制。
    换的时候只动这一个文件，切分和卡片那两块一个字都不用改。

========================================================
七条不能破的规矩（改代码之前先读这段）
========================================================

一、不碰原文
    正文只从 materials.content 按偏移现算。这一层一个字符都不写 content。

二、AI 的建议不能冒充人工确认
    分类结果一律写成「待确认」+ source='ai'。
    「已确认」只能由人点出来 —— 除了这里不写，classify_db.update_cards
    里还有第二道硬拦（校验不过的卡片不许标已确认）。

三、不覆盖人工的成果
    只处理"主类是空的、或者主类本来就是 AI 给的"卡片。
    已经有主类的卡片（来源继承的、人手点的）一律跳过，并在结果里如实报数。
    状态是「已确认」的卡片根本不进候选集。

四、AI 自己不会排除卡片
    占位规则觉得某一行"像表格残留、建议排除"时，只把这条判断写进理由里，
    绝不把卡片状态改成「已排除」。排除是人的动作。

五、一个文件同时只能有一个任务在跑
    否则点两下会生成两个任务，两批结果互相覆盖，还看不出是谁写的。

六、失败要留痕，不能悄悄过去
    单条卡片失败 → classification_items.error
    整批任务失败 → classification_runs.error
    界面上必须能看到"哪一条、为什么失败"。

七、不阻塞页面
    任务在后台线程里跑，HTTP 请求立刻返回。进度靠前端轮询拿。

========================================================
当前进度（读到这段说明你在看第一版）
========================================================

    已做：任务表、任务生命周期（排队/进行/完成/部分完成/失败/取消/重试）、
          可插拔分类器接口、服务端建议校验、占位规则分类器。
    未做：真的大模型调用（见 AI_CLASSIFIER 那段注释）、人工纠错与学习样本、
          总素材库的完整状态展示。

    所以界面上要明确写"当前用的是占位规则、没有联网"，
    不能让人以为这是 AI 判的 —— 这是诚信问题，不是文案问题。
"""

import json
import os
import re
import threading
import traceback
from datetime import datetime

try:
    from backend import db, segmentation as sg
    from backend import classify_db as cls
    from backend import llm
except ImportError:                                   # pragma: no cover
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from backend import db, segmentation as sg
    from backend import classify_db as cls
    from backend import llm


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------
# 一、版本号与常量
#
# 为什么每次任务都要记版本号：
#     三个月后你看到一批卡片觉得"AI 判得不对"，第一个要问的问题是
#     "当时用的是哪个提示词、哪套分类、哪个模型"。
#     不记这三个号，那一批结果就永远是笔糊涂账，只能全部重来。
# ----------------------------------------------------------------------

# 提示词版本。
#   v0 = 占位规则时期（它其实不用提示词，留个号是为了历史记录能对上）
#   v1 = 接入真模型的第一版提示词
# 为什么每次任务都要记它，见本段开头那段话 ——
# 三个月后你看到一批卡觉得"AI 判得不对"，得能回答"当时用的是哪版提示词"。
PROMPT_VERSION = "v1"
PROMPT_VERSION_PLACEHOLDER = "v0"
MODEL_VERSION_PLACEHOLDER = "placeholder-rules-v1"

# 一次任务每批处理多少条卡片。
#
# 为什么是 25：token 预算和"出错代价"之间的平衡。
#   - 太小：11 类判据要跟着每一批发一遍，白花钱还慢
#   - 太大：一批里只要有一条返回格式坏掉，整批都得重来
#          （见 _execute 里"整批拒收"那段），代价跟着变大
#   25 条 × 平均 87 字 ≈ 2200 字正文，加上判据约 3500 字，一次请求很轻。
BATCH_SIZE = 25

# 第一批只发这么多条（理由写在 _execute 里）。
FIRST_BATCH_SIZE = 6

# 置信度低于它、或者分类器自己说"拿不准"，就算「需要人工处理」。
# 注意：这个阈值不改变卡片状态（都还是「待确认」），
#       它只是给界面提供"先看这几条"的筛选依据。
LOW_CONFIDENCE = 0.70

# 任务状态（一张任务表的生命周期）
RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_COMPLETED = "completed"
RUN_PARTIAL = "completed_with_errors"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
ALL_RUN_STATUS = [RUN_QUEUED, RUN_RUNNING, RUN_COMPLETED, RUN_PARTIAL,
                  RUN_FAILED, RUN_CANCELLED]

RUN_STATUS_TEXT = {
    RUN_QUEUED: "排队中",
    RUN_RUNNING: "分类中",
    RUN_COMPLETED: "已完成",
    RUN_PARTIAL: "部分完成",
    RUN_FAILED: "失败",
    RUN_CANCELLED: "已取消",
}

RUN_ACTIVE = (RUN_QUEUED, RUN_RUNNING)    # 这两种状态下不许再开新任务

# 任务里单条卡片的处理状态
ITEM_PENDING = "pending"
ITEM_DONE = "done"
ITEM_FAILED = "failed"
ALL_ITEM_STATUS = [ITEM_PENDING, ITEM_DONE, ITEM_FAILED]

# 哪些状态的卡片会被"自动分类"处理：
#   待确认   —— 常规情况
#   分类失败 —— 上次试过但失败了，重试要能再捞起来
TARGET_STATUS = (sg.STATUS_PENDING, sg.STATUS_FAILED)

# 进程内的一把锁。
# 作用：把"查有没有在跑的任务"和"插入新任务"这两步合成一个原子动作。
# 只用进程内锁够不够？够 —— 这是本机自用版，只有一个服务进程。
# 将来如果多进程部署，要改成数据库唯一索引来兜。
_create_lock = threading.Lock()


# ----------------------------------------------------------------------
# 二、建表
# ----------------------------------------------------------------------

SCHEMA = """
-- 一次自动分类任务。一个文件可以有多次任务（重新分类就是新的一次）。
CREATE TABLE IF NOT EXISTS classification_runs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id             TEXT    NOT NULL,
    material_id          INTEGER NOT NULL,
    -- 这一次分类，用的是哪一次切分的结果。
    -- 为什么要有它：切分和分类是两件事，一个文件可以先后被切好几次。
    -- 不记下来的话，以后看到一批卡片无法回答"它们是从哪版切片来的"。
    segment_run_id       INTEGER DEFAULT NULL,
    -- 重试出来的任务，指回它重试的那一次
    retry_of_run_id      INTEGER DEFAULT NULL,
    status               TEXT    NOT NULL DEFAULT 'queued',
    rule                 TEXT    NOT NULL DEFAULT '',
    rule_version         TEXT    NOT NULL DEFAULT '',
    norm_version         TEXT    NOT NULL DEFAULT '',
    category_set_version TEXT    NOT NULL DEFAULT '',
    prompt_version       TEXT    NOT NULL DEFAULT '',
    model_version        TEXT    NOT NULL DEFAULT '',
    -- 这次用的是哪种方法：placeholder（本地规则）/ llm（大模型）
    classifier           TEXT    NOT NULL DEFAULT '',
    -- 用的是哪个模型：qwen-plus / deepseek-chat / glm-4-flash …（规则时为空）
    -- 【为什么单独存一列】她要比"哪个模型分得准"。同一份素材跑两次，
    -- 按这两列分组就能把两次结果并排看，不用靠脑子记。
    model_key            TEXT    NOT NULL DEFAULT '',
    total_items          INTEGER NOT NULL DEFAULT 0,
    done_items           INTEGER NOT NULL DEFAULT 0,
    failed_items         INTEGER NOT NULL DEFAULT 0,
    created_cards        INTEGER NOT NULL DEFAULT 0,
    skipped_items        INTEGER NOT NULL DEFAULT 0,
    cancel_requested     INTEGER NOT NULL DEFAULT 0,
    error                TEXT    NOT NULL DEFAULT '',
    note                 TEXT    NOT NULL DEFAULT '',
    created_at           TEXT    NOT NULL,
    started_at           TEXT    NOT NULL DEFAULT '',
    finished_at          TEXT    NOT NULL DEFAULT ''
);

-- 任务里每一条卡片的处理情况。
-- 为什么不把这些字段直接写在 cards 上：
--   同一张卡会被分类很多次（重新分类 + 重试）。
--   写在 cards 上只剩"最后一次"，中间那次为什么失败就永远查不到了。
CREATE TABLE IF NOT EXISTS classification_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL,
    card_id     INTEGER NOT NULL,
    seq         INTEGER NOT NULL DEFAULT 0,
    status      TEXT    NOT NULL DEFAULT 'pending',
    error       TEXT    NOT NULL DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    UNIQUE (run_id, card_id)
);

-- 人工纠错样本（"AI 学习"的原料）。
--
-- 刻意**不加** UNIQUE(owner_id, card_id)：
--   同一张卡片被人改了两次，应该留下两条记录 ——
--   第二条是"她后来又改了主意"，这本身就是有用的信息。
--   而且停用（enabled=0）是逐条停用的，压成一条就没法只停用其中一次。
--   将来取 few-shot 时按 card_id 取最新一条且 enabled=1 的那条即可。
--
-- 正文不在这里重复存：靠 card_id 回 cards 找 material_id + 偏移现算。
-- example_text_hash 只存指纹，用来判断"这条样本的原文后来有没有被改过"。
CREATE TABLE IF NOT EXISTS learning_feedback (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id              TEXT    NOT NULL,
    card_id               INTEGER NOT NULL,
    classification_run_id INTEGER DEFAULT NULL,
    ai_judgement_id       INTEGER DEFAULT NULL,
    ai_category           TEXT    NOT NULL DEFAULT '',
    ai_tags               TEXT    NOT NULL DEFAULT '[]',
    ai_reason             TEXT    NOT NULL DEFAULT '',
    ai_confidence         REAL    DEFAULT NULL,
    final_category        TEXT    NOT NULL DEFAULT '',
    final_tags            TEXT    NOT NULL DEFAULT '[]',
    correction_reason     TEXT    NOT NULL DEFAULT '',
    example_text_hash     TEXT    NOT NULL DEFAULT '',
    enabled               INTEGER NOT NULL DEFAULT 1,
    created_at            TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL
);

-- 「补充提示词」：作者自己在界面上写的那几句。
--
-- 为什么不塞进 tasks / 配置项，而单独一张表：
--   它要跟着**账号**走（换个账号就是另一套口味），
--   而且长度有上限、要能单独改，混在别的地方迟早互相踩。
-- 为什么按 (owner_id, kind) 做主键：
--   以后肯定不止分类这一处要写提示词（内化、大纲生成都要）。
--   加新用途就是加一个 kind，不用再建表。
CREATE TABLE IF NOT EXISTS user_prompts (
    owner_id   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    content    TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (owner_id, kind)
);

-- 「提示词库」：她攒下来的成句提示词。能反复用，也能选择公开给别人用。
--
-- 【本文件最反直觉的一条约定，改代码前先读懂】
--   公开 ≠ 内容可见。
--   公开 = **别人可以拿它去跑分类，但永远看不到里面写了什么**。
--   所以两个方向要分开守：
--     · 读内容 —— 只有 owner 本人（get_library_prompt / list_my_library_prompts）
--     · 让别人"能用" —— 服务端按 id 自己取内容、直接塞进任务，**不经过前端**
--   硬保证：所有"给别人看"的出口（list_public_library_prompts /
--   _lib_dict(row, with_content=False) / resolve_prompt_for_use 的返回值）
--   一律**不带 content**。将来加新接口要返回 content 时，
--   先问自己一句"凭什么让别人看见她写的东西"。
--
-- 为什么软删（active=0）而不是 DELETE：
--   任务记录里存着 prompt_ref_id，物理删掉之后翻历史只剩一个查不到名字的数字。
--
-- 为什么 kind 跟 user_prompts 一样按用途分：
--   以后内化、大纲生成也会有自己的提示词库。
--   加新用途就是加一个 kind，不用再建表。
CREATE TABLE IF NOT EXISTS prompt_library (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'classify',
    name       TEXT NOT NULL,
    content    TEXT NOT NULL,
    usage_note TEXT NOT NULL DEFAULT '',
    summary    TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'private',
    use_count  INTEGER NOT NULL DEFAULT 0,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    UNIQUE (owner_id, kind, name)
);
CREATE INDEX IF NOT EXISTS idx_plib_own ON prompt_library(owner_id, kind, active);
CREATE INDEX IF NOT EXISTS idx_plib_pub ON prompt_library(visibility, kind, active);
CREATE INDEX IF NOT EXISTS idx_crun_owner ON classification_runs(owner_id, id);
CREATE INDEX IF NOT EXISTS idx_crun_mat   ON classification_runs(material_id, status);
CREATE INDEX IF NOT EXISTS idx_citem_run  ON classification_items(run_id);
CREATE INDEX IF NOT EXISTS idx_citem_card ON classification_items(card_id);
CREATE INDEX IF NOT EXISTS idx_lfeed_own  ON learning_feedback(owner_id, enabled);
CREATE INDEX IF NOT EXISTS idx_lfeed_card ON learning_feedback(card_id);
"""


def _columns(conn, table):
    """看一张表现在有哪些列。用来判断"这列加过了吗"。"""
    return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}


def migrate(verbose=False):
    """建表 + 给 ai_judgements 补两列。反复调用是安全的。

    必须**在 classify_db.migrate() 之后**调用 ——
    因为它要给 ai_judgements 补列，而那张表是 classify_db 建的。
    main.py 的 lifespan 里就是按这个顺序调的。
    """
    report = {"tables": [], "added_columns": []}
    db.init_db()

    with db.connect() as conn:
        # 记一下建表前有哪些表，用来回答"这次新建了哪几张"
        before = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

        conn.executescript(SCHEMA)

        after = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        report["tables"] = sorted(after - before)

        # ---- ai_judgements 补两列 ----
        #
        # 这张表上一版就建好了，字段基本够用，缺的只有这两列：
        #   owner_id              —— 多用户隔离必须有的（不然查"我的"判断记录要绕一圈）
        #   classification_run_id —— 这条判断属于哪一次任务
        #
        # 为什么不干脆重建表：表可以重建，但里面的历史数据不行。
        # 而且任务书写得很清楚「已有同名表先读结构、采用迁移、不重复创建」。
        cols = _columns(conn, "ai_judgements")
        if "owner_id" not in cols:
            conn.execute("ALTER TABLE ai_judgements "
                         "ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''")
            report["added_columns"].append("ai_judgements.owner_id")
        if "classification_run_id" not in cols:
            conn.execute("ALTER TABLE ai_judgements "
                         "ADD COLUMN classification_run_id INTEGER DEFAULT NULL")
            report["added_columns"].append("ai_judgements.classification_run_id")

        # ---- classification_runs 补两列（接模型之后才需要的）----
        #
        # 老任务记录里这两列是空的，那正表示"那时候还没有模型这回事"，
        # 不需要回填。补列而不是重建表：重建会把历史任务记录弄丢，
        # 而那些记录正是"这批卡片是谁分出来的"的唯一凭据。
        rcols = _columns(conn, "classification_runs")
        if "classifier" not in rcols:
            conn.execute("ALTER TABLE classification_runs "
                         "ADD COLUMN classifier TEXT NOT NULL DEFAULT ''")
            report["added_columns"].append("classification_runs.classifier")
        if "model_key" not in rcols:
            conn.execute("ALTER TABLE classification_runs "
                         "ADD COLUMN model_key TEXT NOT NULL DEFAULT ''")
            report["added_columns"].append("classification_runs.model_key")
        # 心跳：每跑完一批就写一次当前时间。
        #
        # 【为什么非要这一列】
        #   后台线程是"跑起来了但卡住了"还是"其实已经死了"，
        #   光看 done_items 分不出来 —— 两种情况都是数字不动。
        #   有了心跳，界面就能说「最后更新 3 秒前」，
        #   她一眼能分清"在慢慢跑"和"真死了"。
        if "heartbeat_at" not in rcols:
            conn.execute("ALTER TABLE classification_runs "
                         "ADD COLUMN heartbeat_at TEXT NOT NULL DEFAULT ''")
            report["added_columns"].append("classification_runs.heartbeat_at")
        # 这次用的是哪份补充提示词（存快照，不存引用）。
        #
        # 【为什么存快照】
        #   她跑完一轮觉得不准，会去改那句补充提示词再跑第二轮。
        #   如果这里只存"用没用补充提示词"，回头就再也答不出
        #   "第一轮到底是怎么问的" —— 而那正是她对比两轮时的关键变量。
        if "user_prompt" not in rcols:
            conn.execute("ALTER TABLE classification_runs "
                         "ADD COLUMN user_prompt TEXT NOT NULL DEFAULT ''")
            report["added_columns"].append("classification_runs.user_prompt")
        # 提示词模板是哪一份：file（她调的那版）/ builtin（通用版）。
        if "prompt_source" not in rcols:
            conn.execute("ALTER TABLE classification_runs "
                         "ADD COLUMN prompt_source TEXT NOT NULL DEFAULT ''")
            report["added_columns"].append("classification_runs.prompt_source")
        # 这次用的是「提示词库」里的哪一条（0 = 没用库里的，走自由文本或空）。
        #
        # 【为什么既存 id 又存名字】
        #   id 是给程序用的（"这个任务用的是哪条"），
        #   name 是给人看的（任务记录里直接显示「祛AI味」而不是「#7」）。
        #   名字存快照：那条提示词后来被改名或删掉，历史记录照样看得懂，
        #   不会变成一个查不到名字的数字。
        if "prompt_ref_id" not in rcols:
            conn.execute("ALTER TABLE classification_runs "
                         "ADD COLUMN prompt_ref_id INTEGER NOT NULL DEFAULT 0")
            report["added_columns"].append("classification_runs.prompt_ref_id")
        if "prompt_name" not in rcols:
            conn.execute("ALTER TABLE classification_runs "
                         "ADD COLUMN prompt_name TEXT NOT NULL DEFAULT ''")
            report["added_columns"].append("classification_runs.prompt_name")
        # 这条提示词**是谁的**（空 = 没用库里的，是自由文本）。
        #
        # 【为什么非得单独存这一个字段：这是"公开但私密"的防漏点】
        #   假如乙用甲公开出来的那条提示词跑了一次分类，
        #   那么乙这条任务记录里的 user_prompt 就是**甲写的内容**。
        #   而 /api/classification-runs 是会把任务原样返回的 ——
        #   乙一翻历史就看到了甲的私货，她的"内容永远私密"当场作废。
        #   存下 owner，_run_dict 才能在序列化时把这种内容抹掉（见那里的注释）。
        if "prompt_owner" not in rcols:
            conn.execute("ALTER TABLE classification_runs "
                         "ADD COLUMN prompt_owner TEXT NOT NULL DEFAULT ''")
            report["added_columns"].append("classification_runs.prompt_owner")

    if verbose:
        print("[分类任务] 新建表：%s" % (report["tables"] or "无"))
        print("[分类任务] 补列：%s" % (report["added_columns"] or "无"))
    return report


# ======================================================================
# 三、分类器：接口 + 占位实现 + 真实现的位置
# ======================================================================
#
# 这里是一道"插拔口"。
# 上层（任务执行器）只认识下面这个约定，不关心对面是人写的规则还是大模型：
#
#     classify_batch(items, ctx) -> [建议, 建议, ...]
#
#     items  [{"card_id": 123, "seq": 4, "text": "原文片段"}, ...]
#     ctx    {"material_title": "文件名", "categories": ["人物描写", ...]}
#     建议   {"card_id": 123, "primary_category": "对话台词" 或 None,
#             "tags": ["拉扯"], "reason": "为什么这么判",
#             "confidence": 0.0~1.0 或 None, "unsure": False,
#             "suggest_exclude": False}
#
# 注意建议里**没有正文**。真模型返回里如果带了正文，服务端校验会整批拒收 ——
# 理由见 _validate_suggestions。

def placeholder_rule_judge(text, seq):
    """占位规则：**这不是 AI**，只是把整条链路先跑通用的。

    为什么留着规则而不是直接接模型：
        接模型要解决 API Key、费用、超时、限流、脱敏、结果格式校验一大堆事。
        如果链路本身（任务表、进度、取消、重试、写回、界面）还没验证过，
        出问题时你分不清是"链路坏了"还是"模型判得不对"。先用规则跑通链路，
        换模型时就只剩"把这一类换掉"，问题范围小得多。
        另外它还是"没配 Key 时的兜底"和测试里的固定假数据源。

    【它现在的边界 —— 2026-09-24 换分类体系时重定过】

    原来这套规则是按旧的九类（人物描写 / 场景氛围 / 语言表达 /
    世界观设定 …）写的。换到
    「外貌 · 神态 · 梗 · 暧昧拉扯 · 心理 · 搞笑情节 · 对话台词 · 情节」
    之后，旧规则产出的类名**一个都对不上**，于是整批卡片全被判成
    「分类失败」—— 功能看起来像坏了，其实是规则没跟着换。

    重定范围时**没有硬凑满八类**，只留它真判得准的几条：

        对话台词   形式上就有成对引号，不需要读懂意思
        外貌       有静态词（生得 / 平日 / 向来）+ 五官衣着词
        神态       表情、身体动作、生理反应 —— 这些词是有限的
        心理       内省动词（心想 / 暗自 / 本该 / 后悔 …）
        情节       事件动词（杀 / 逃 / 追 / 打 / 救 …）

    剩下三类**故意判不出来**，这是诚实的，不是没做完：

        梗         「为了一碟醋包一盘饺子」那种要读懂了才算得出的，词典里没有
        暧昧拉扯   两个人之间"有没有那点意思"，只按词面判等于瞎判
        搞笑情节   好不好笑是感受，规则判不了

    这三类正是要交给大模型的东西。她最终要的是"判得准"，
    而凑出来的类名比空白更有害 —— 空白她会自己看一眼，
    错的类名会披着「AI 建议」的外衣混过去。
    """
    t = (text or "").strip()
    if not t:
        return {"primary_category": None, "tags": [], "reason": "空行，没有内容",
                "confidence": None, "unsure": True}

    n = len(t)

    # ---- 判据 1：表格 / 导入残留 / 章节标题 ----
    # 这些行是导入时混进来的结构信息，不是素材。
    # 只提"建议排除"，绝不自己把卡片标成已排除（排除是人的动作）。
    if (t.startswith("工作表") or t.startswith("Sheet") or
            re.match(r"^【?\s*工作表", t)):
        return {"primary_category": None, "tags": [],
                "reason": "像表格导入时带出来的工作表标记，不是素材内容，建议排除",
                "confidence": 0.8, "unsure": False, "suggest_exclude": True}
    if re.match(r"^[\d\s.,，、%％:：/\-年月日号]+$", t) and re.search(r"\d", t):
        return {"primary_category": None, "tags": [],
                "reason": "整行只有数字和标点，像表格里的数值行，不是素材内容，建议排除",
                "confidence": 0.8, "unsure": False, "suggest_exclude": True}
    # 章节标题也归"建议排除"。
    # 旧版把它塞进「场景氛围」是为了"留个分界参考"，但大类换成她的八类之后
    # 没有环境类可塞了。而且她自己在总素材库里就是把这些行标成「已排除」的
    # （33 张 human 已排除里大部分是书名和章节标题）—— 建议排除才对得上她的做法。
    if re.match(r"^第\s*[0-9一二三四五六七八九十百零〇两]+\s*[章节回卷篇]", t):
        return {"primary_category": None, "tags": [],
                "reason": "这是章节标题行，本身不承载素材内容，建议排除",
                "confidence": 0.7, "unsure": False, "suggest_exclude": True}

    # ---- 判据 2：对白 ----
    # 判断依据不是"出现了某个字"，而是"这一行在形式上是一句有人说的话"：
    #   有成对的引号，且引号里的内容占了这行的相当一部分。
    #
    # 比例定在 0.25、且引号内至少 4 个字：
    #   小说划线笔记里的对白常常是「短对白 + 长动作」的形式
    #   （「你来了。」他把杯子搁在桌上。），比例卡太高会把它们漏掉。
    #   下限 4 个字是为了挡住"他说「嗯」"这种只是夹了个词的叙述句。
    quotes = re.findall(r"[「『“\"]([^」』”\"]{1,})[」』”\"]", t)
    inside = sum(len(q) for q in quotes)
    if quotes and inside >= max(4, n * 0.25):
        tags = ["好词好句"] if n >= 30 else []
        return {"primary_category": "对话台词", "tags": tags,
                "reason": "这一行有成对引号且引号里的内容占了大半，"
                          "形式上是人物在说话，可以直接拆出来当台词用",
                "confidence": 0.85, "unsure": False}

    # ---- 判据 3：心理（内省）----
    if re.search(r"心里|心想|暗自|觉得|明白|知道|本该|后悔|恨|怕|空|发酸|"
                 r"说不清|不敢想|忍住", t) and not quotes:
        neg = bool(re.search(r"难|疼|痛|冷|空|怕|恨|悔|碎|死", t))
        return {"primary_category": "心理",
                "tags": ["难过"] if neg else [],
                "reason": "写的是人物内部的活动 —— 他意识到什么、说服自己什么、"
                          "压住什么，外面看不见，属于心理层",
                "confidence": 0.72, "unsure": False}

    # ---- 判据 4：外貌（静态、长期的特征）----
    #
    # 为什么要求"必须有一个静态词"（生得 / 长得 / 平日 / 向来 …）：
    #   只按外貌词（袖、衣、眼、发）判会把「他把玉佩塞回袖子里」也吞进来 ——
    #   那一句是动作，不是长相。加了个静态词作门槛，误伤少很多。
    # 为什么排在「神态」前面：
    #   「他生得一双极冷的眼睛，平日很少笑」里有"笑"，
    #   如果神态先判，这句会被算成表情 —— 但它写的是这个人一贯的样子。
    if re.search(r"生得|长得|一副|素来|向来|一向|平日|整日|看上去|"
                 r"容貌|眉眼|身量|身形|穿着", t) and \
            re.search(r"眼|眉|唇|发|鬓|脸|手|指|身|衣|袍|靴|袖|腰", t):
        return {"primary_category": "外貌", "tags": [],
                "reason": "这几句在交代人物「是个什么样的人」（长相、衣着、一贯的习惯），"
                          "是静态、长期的信息，不是他此刻在做什么",
                "confidence": 0.70, "unsure": False}

    # ---- 判据 5：神态（外面看得见的身体）----
    if re.search(r"捏|握|抬|垂|转|站|走|坐|跪|退|靠|皱|抿|挑眉|笑|叹|抖|颤|"
                 r"摔|推|拉|抱|掐|拍|脸色|指尖|喉结|呼吸|心跳|后背|冷汗|"
                 r"侧过|低下头|别开|闭上", t):
        return {"primary_category": "神态", "tags": [],
                "reason": "这一行落在人物的表情、身体动作或身体反应上，"
                          "是能从外面看见的一层",
                "confidence": 0.75, "unsure": False}

    # ---- 判据 6：情节（发生了事件）----
    if re.search(r"杀|逃|追|打|拔刀|血|死|救|拦|闯|绑|撕破|翻脸|动手|埋伏|"
                 r"咬牙|摔门|出门|转身走", t):
        return {"primary_category": "情节", "tags": ["适合冲突"],
                "reason": "这一行在推进事件（冲突、阻拦、逃离之类），"
                          "事件本身能独立成立，不依赖某两个人的关系",
                "confidence": 0.70, "unsure": False}

    # ---- 判不出来就承认 ----
    # 这条比"随便给个类"重要得多。占位规则故意不做兜底猜测：
    # 猜错的主类会被当成"AI 的意见"，比"没有意见"更难发现。
    return {"primary_category": None, "tags": [],
            "reason": "本地占位规则看不出这一条属于哪一类，"
                      "不做猜测，留给人工判断",
            "confidence": None, "unsure": True}


class PlaceholderClassifier(object):
    """占位分类器。名字常量在 MODEL_VERSION_PLACEHOLDER。"""

    name = "placeholder"
    # label 是给界面看的。name 是注册表的键（代码里认它），label 是人读的，
    # 两者分开 —— 界面上不要把 "placeholder" 这种内部名字露给用户。
    label = "本地占位规则"
    model_version = MODEL_VERSION_PLACEHOLDER
    # 它压根不用提示词，留个号是为了历史记录能对上（老记录里都是 v0）。
    prompt_version = PROMPT_VERSION_PLACEHOLDER

    def classify_batch(self, items, ctx):
        out = []
        for it in items:
            r = placeholder_rule_judge(it.get("text") or "", it.get("seq") or 0)
            r["card_id"] = it.get("card_id")
            r.setdefault("suggest_exclude", False)
            out.append(r)
        return out


# ----------------------------------------------------------------------
# 六之二、真·大模型分类器
#
# 【先说清一件事：「用哪个分类器」和「用哪个模型」是两回事】
#
#     分类器 = 方法     （本地规则 / 大模型）
#     模型   = 具体哪家 （qwen-plus / deepseek-chat / glm-4-flash …）
#
# 两个都记进任务表，才能回答"这批结果到底是谁给的"。
# 她要比"哪个模型分得准"，靠的就是把两次任务的这两列并排看。
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# 提示词模板：调好的那版**不在代码里**
#
# 【为什么代码里只留一个通用版】
#   调顺的那版提示词是墨阁的核心资产，不参与开源。
#   所以代码里只放一份**框架级的通用模板**（下面那个常量），
#   真正在用的那份写进 prompts/classify.txt ——
#   prompts/ 在 .gitignore 里，她怎么调都进不了公开仓库。
#   公开仓库只放 prompts/example.classify.txt（占位示例，教人怎么填）。
#
# 【模板里能用哪些占位符】
#   {categories}  主类清单（名称 + 判据 + 这类常用的副标签）
#   {sub_tags}    可以挂的副标签
#   {user_prompt} 作者自己在界面上写的补充要求（没写就是空串）
#
#   注意模板只负责"怎么问"这一段（system 消息）。
#   正文和条数是每次现拼的（user 消息：请判断下面 N 条素材 + 正文），
#   不放进模板 —— 那是机械拼接，换模板的人不该有机会把它写丢。
#
# 【为什么要校验占位符】
#   她会直接手改这个文件。少写一个 {categories}，模型就收不到类目清单，
#   只能照字面猜；它照样会一本正经地返回结果，而报错里一个字都不会提
#   "是你模板少了东西"。所以必须当场发现、退回通用模板，
#   并且把这件事写进任务备注。
# ----------------------------------------------------------------------

PROMPT_FILE = "classify.txt"

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 模板里必须有这几个占位符，少一个就不能用。
REQUIRED_SLOTS = ("{categories}", "{sub_tags}")

PROMPT_VERSION_GENERIC = "v1-generic"

# 框架级通用模板：能跑通、判得出类别，但没有针对某个人的素材口味调过。
# 公开仓库里就是这一份。
GENERIC_CLASSIFY_PROMPT = """\
你是一位中文小说素材的分类助手。用户是写小说的作者，\
她把收集到的素材切成了一条条片段，需要你判断每一条属于哪一类。

【只能从下面这些主类里选，不许自己发明新的】

{categories}

【可以挂的副标签】
{sub_tags}
一条可以挂好几个副标签，也可以一个都不挂。\
副标签只是「这类常用到」，不是规定 —— 你觉得贴切就能用。

【怎么判】
- 把整条读完再判，不要只看开头几个字
- 两个类拿不准的时候，重点看判据里「和 XX 的分界」那一句
- **真的判不出来就把主类填 null，不要猜。**\
猜错比留空更糟：它会冒充成一条「AI 的建议」混进素材库，作者不容易发现。
- 理由要具体：说清这条里的哪些字让你这么判。\
不要写「描写生动」「很有画面感」这种空话。
{user_prompt}
【输出格式】
只输出一个 JSON 数组，不要任何别的话，不要 markdown 代码块。
数组里每个元素长这样：
{"card_id": 12, "primary_category": "外貌", "tags": ["直接描写"], \
"reason": "一句话理由", "confidence": 0.8}
字段说明：
- card_id：**原样照抄**我给每条标的编号，不许自己重新编号
- primary_category：主类名；判不出就填 null
- tags：副标签数组（可以是空数组 []）
- reason：一句话理由
- confidence：0 到 1 的小数，你觉得有多大把握
"""


def prompt_template():
    """取这次要用的模板。返回 (模板文本, 来源, 警告语)。

    来源只有两种，都要能看出来：
        "file"    用了 prompts/classify.txt（她自己调的那版）
        "builtin" 用了代码里的通用模板（文件没写，或者写坏了）

    为什么返回三样而不是直接给模板：
        "现在到底跑的哪一版"必须能被追溯 —— 她改完文件要重跑对比准不准，
        如果两次用的都是同一版而她不记得，对比就没意义了。
    """
    path = os.path.join(ROOT_DIR, "prompts", PROMPT_FILE)
    if not os.path.isfile(path):
        return GENERIC_CLASSIFY_PROMPT, "builtin", ""
    try:
        with open(path, encoding="utf-8") as f:
            txt = f.read().strip()
    except Exception as e:                               # pragma: no cover
        return (GENERIC_CLASSIFY_PROMPT, "builtin",
                "prompts/%s 读不了（%s），这次用通用模板" % (PROMPT_FILE, e))
    if not txt:
        return (GENERIC_CLASSIFY_PROMPT, "builtin",
                "prompts/%s 是空的，这次用通用模板" % PROMPT_FILE)
    lost = [s for s in REQUIRED_SLOTS if s not in txt]
    if lost:
        # 拼错的模板比没模板更危险：它会静默把正文丢掉。
        return (GENERIC_CLASSIFY_PROMPT, "builtin",
                "prompts/%s 少了占位符 %s（那几处内容会被丢掉），"
                "这次改用通用模板" % (PROMPT_FILE, "、".join(lost)))
    return txt, "file", ""


def _fill_slots(tpl, **slots):
    """把 {xxx} 换成实际内容。

    为什么不用 str.format：模板正文里有 JSON 示例（{"card_id": 12}），
    大括号会跟 format 的语法打架，得逐个转义写成 {{ }} ——
    让她手改的时候看见一堆双大括号，必然改错。
    直接替换就没这问题：模板里长什么样，文件里就长什么样。
    """
    out = tpl
    for k, v in slots.items():
        out = out.replace("{" + k + "}", v)
    return out


# ----------------------------------------------------------------------
# 补充提示词：作者自己在界面上写的那几句
# ----------------------------------------------------------------------

# 上限 5000 字。为什么要有上限、而且卡得比较紧：
#   这段字是**每批都要重发一遍**的。630 张卡按 25 条一批是 26 批，
#   写 5000 字就等于每跑一次多发 13 万字 —— 费用和延迟都是成倍涨，
#   而她多半只是想补一两句判据（"带引号的短句优先算对话台词"）。
#   5000 字足够写十几条这种补充，再长就该去改判据，而不是堆在提示词里。
USER_PROMPT_MAX = 5000

USER_PROMPT_KIND_CLASSIFY = "classify"


def get_user_prompt(owner, kind=USER_PROMPT_KIND_CLASSIFY):
    """读她写的补充提示词。没写过就是空串。"""
    kind = _kind_of(kind)
    with db.connect() as conn:
        r = conn.execute("SELECT content FROM user_prompts"
                         " WHERE owner_id=? AND kind=?", (owner, kind)).fetchone()
    return (r["content"] if r else "") or ""


def set_user_prompt(owner, content, kind=USER_PROMPT_KIND_CLASSIFY):
    """存她写的补充提示词。超长直接拒绝，不悄悄截断。

    为什么截断不行：截到一半的提示词会变成一句没头没尾的话，
    模型照样会照做，而她以为自己写的那整段都发出去了。
    宁可当场报错让她自己删。
    """
    content = content or ""
    if not isinstance(content, str):
        raise ValueError("提示词得是文字")
    kind = _kind_of(kind)
    content = content.strip()
    if len(content) > USER_PROMPT_MAX:
        raise ValueError(
            "补充提示词最长 %d 字，你现在写了 %d 字，超了 %d 字。"
            % (USER_PROMPT_MAX, len(content), len(content) - USER_PROMPT_MAX))
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO user_prompts (owner_id, kind, content, updated_at)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(owner_id, kind) DO UPDATE SET"
            " content=excluded.content, updated_at=excluded.updated_at",
            (owner, kind, content, now_str()))
    return content


# ----------------------------------------------------------------------
# 提示词库
#
# 【再强调一遍全项目最反直觉的约定】
#   visibility='public' 的意思是「别人可以拿它去用」，
#   **不是**「别人可以拿去看内容」。
#   所以这个模块里只有两个"出口"允许带 content：
#     · list_my_library_prompts(owner)      —— 我自己的
#     · get_library_prompt(owner, pid)      —— 我自己的
#   其余出口（公开清单、任务详情、任何给别人的地方）一律不带。
# ----------------------------------------------------------------------

PROMPT_NAME_MAX = 30        # 名称，够写「江南（建议搭配全能风格食用）」
PROMPT_USAGE_MAX = 50       # 使用方法，一句话
PROMPT_SUMMARY_MAX = 6000   # 介绍，可以写长一点（她那边原产品也是 6000）
PROMPT_CONTENT_MAX = USER_PROMPT_MAX   # 正文跟"补充提示词"同一个上限，理由见上文

VIS_PRIVATE = "private"
VIS_PUBLIC = "public"
VISIBILITIES = (VIS_PRIVATE, VIS_PUBLIC)

PROMPT_KIND_CLASSIFY = USER_PROMPT_KIND_CLASSIFY
# 剧情内化自己的那一档。
#
# 【为什么不是新开一张表】
#   user_prompts / prompt_library 建表时就都带了 kind 列，注释里写得很清楚：
#   「以后内化、大纲生成也会有自己的提示词库，加新用途就是加一个 kind」。
#   内化要的「存多条 + 列表里挑一条用」跟分类是同一件事，
#   差别只在"发给哪个模型、喂什么料"。分开建表 = 两套增删改查要各修一遍，
#   改一边忘一边就是静默故障。
#
# 【两档之间必须互不串门】
#   分类的提示词讲的是"怎么判主类"，内化的讲的是"怎么抽象剧情"。
#   串了不会报错，只会让她挑到一条完全不对的话去跑，白花钱。
#   所以凡是按 kind 取的地方**一律显式传 kind**，不靠默认值。
PROMPT_KIND_INFUSE = "infuse"

# 大纲生成自己的那一档。
#
# 【为什么大纲也要有自己的一档，而不是共用内化那一档】
#   内化的提示词讲的是"怎么把一张卡抽象成一条零件"，
#   大纲的讲的是"怎么把零件串成一篇能直接动笔的细纲"。
#   这是两个完全不同的问题，同一句话在两边都不成立。
#   共用一档的下场很具体：她给大纲调好一句话，去跑内化时
#   在列表里也看得见它 —— 挑错了不会报错，只会白花一笔钱。
PROMPT_KIND_OUTLINE = "outline"

PROMPT_KINDS = (PROMPT_KIND_CLASSIFY, PROMPT_KIND_INFUSE, PROMPT_KIND_OUTLINE)


def check_prompt_kind(kind):
    """把接口传进来的 kind 收进白名单。

    为什么不让它自由取值：kind 是查询条件，写错一个字不会报错，
    只会**安静地返回空列表** —— 她看到"我存的提示词都没了"，
    而数据其实好端端躺在另一档里。这类 bug 最难查，所以在入口就拦掉。
    """
    k = (kind or PROMPT_KIND_CLASSIFY).strip()
    if k not in PROMPT_KINDS:
        raise ValueError("不认识的提示词用途：%s（只能是 %s）"
                         % (kind, " 或 ".join(PROMPT_KINDS)))
    return k


def _kind_of(kind):
    """函数层的 kind 收口：None / 空串 → 默认档（分类）。

    【为什么要收这一道，而接口层已经收过了】
      这两个函数也会被**库里的值**喂进来（update 时用的就是取出来的
      cur["kind"]），所以这里不能像接口那样"不认识就报错" ——
      将来库里有个历史值不在白名单里，"改个名字"这种小事会变成 500。
      真正该拦"她手输的 kind"的地方是接口，那里用 check_prompt_kind 拦。

    【但也不能不管】`kind=None` 直接进 SQL 会变成 `kind IS NULL`，
    一条都匹配不上 —— 不报错、只返回空列表。调用方看到的是
    "我存的提示词怎么全没了"，而数据一条没少。这种静默失败最费时间，
    所以在这里兜成默认档：宁可让她看到分类那档的内容（一眼能看出不对），
    也不要让她看到一个空列表还以为东西丢了。
    """
    return (kind or "").strip() or PROMPT_KIND_CLASSIFY


def _owner_label(owner):
    """归属标记（`__u<数字>` / `local`）→ 给人看的名字。

    查不到就原样返回，绝不编 —— 界面上宁可显示 `__u99` 这种丑东西，
    也不能显示一个错的人名（"这是谁写的"会直接影响她敢不敢用）。
    """
    owner = owner or ""
    if owner == "local":
        return "未归属"
    if owner.startswith("__u"):
        try:
            uid = int(owner[3:])
        except ValueError:
            return owner
        u = db.get_user(uid)
        if u:
            return u["username"]
    return owner or "未知"


def _lib_dict(row, with_content):
    """提示词的一条 → 给前端的字典。

    with_content=False 时**绝不**带上 content 字段 —— 这是"公开但私密"的实现点，
    不是可选优化。改这个默认值之前先读模块顶部那段注释。
    """
    d = {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "usage_note": row["usage_note"] or "",
        "summary": row["summary"] or "",
        "visibility": row["visibility"],
        "use_count": row["use_count"],
        "owner": row["owner_id"],
        "owner_label": _owner_label(row["owner_id"]),
        # 字数不是内容，可以给 —— 界面要显示「约 320 字」帮她判断值不值得用。
        "content_length": len(row["content"] or ""),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if with_content:
        d["content"] = row["content"] or ""
    return d


def _check_prompt_fields(name, content, usage_note, summary, visibility):
    """把字段洗一遍并校验。返回洗好的元组。错就抛 ValueError（接口那层转 400）。"""
    name = (name or "").strip()
    content = (content or "").strip()
    usage_note = (usage_note or "").strip()
    summary = (summary or "").strip()
    visibility = (visibility or VIS_PRIVATE).strip()

    if not name:
        raise ValueError("提示词得有个名字，不然下次在快捷选项里认不出它")
    if len(name) > PROMPT_NAME_MAX:
        raise ValueError("名称最长 %d 字，你写了 %d 字"
                         % (PROMPT_NAME_MAX, len(name)))
    if not content:
        raise ValueError("提示词内容是空的 —— 只填名字的话，点「开始分类」没东西可发")
    if len(content) > PROMPT_CONTENT_MAX:
        raise ValueError("提示词内容最长 %d 字，你写了 %d 字，超了 %d 字"
                         % (PROMPT_CONTENT_MAX, len(content),
                            len(content) - PROMPT_CONTENT_MAX))
    if len(usage_note) > PROMPT_USAGE_MAX:
        raise ValueError("使用方法最长 %d 字，你写了 %d 字"
                         % (PROMPT_USAGE_MAX, len(usage_note)))
    if len(summary) > PROMPT_SUMMARY_MAX:
        raise ValueError("介绍最长 %d 字，你写了 %d 字"
                         % (PROMPT_SUMMARY_MAX, len(summary)))
    if visibility not in VISIBILITIES:
        raise ValueError("公开设置只能是 %s 或 %s"
                         % (VIS_PRIVATE, VIS_PUBLIC))
    return name, content, usage_note, summary, visibility


def list_my_library_prompts(owner, kind=PROMPT_KIND_CLASSIFY):
    """我自己攒的提示词，**带内容**。只有本人调得到这个函数。"""
    kind = _kind_of(kind)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM prompt_library"
            " WHERE owner_id=? AND kind=? AND active=1"
            " ORDER BY updated_at DESC, id DESC", (owner, kind)).fetchall()
    return [_lib_dict(r, True) for r in rows]


def list_public_library_prompts(viewer, kind=PROMPT_KIND_CLASSIFY):
    """**别人**公开出来的提示词，**不带内容**。

    自己的那批故意排除（它在"我的"那一栏里，带内容）——
    同一个东西同时出现在两个列表、一个看得见内容一个看不见，只会让人困惑。
    """
    kind = _kind_of(kind)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM prompt_library"
            " WHERE visibility=? AND kind=? AND active=1 AND owner_id<>?"
            " ORDER BY use_count DESC, updated_at DESC, id DESC",
            (VIS_PUBLIC, kind, viewer)).fetchall()
    return [_lib_dict(r, False) for r in rows]


def get_library_prompt(owner, pid, kind=PROMPT_KIND_CLASSIFY):
    """按 id 取**我自己的**一条（带内容）。不是我的 → None。

    注意是 None 不是抛错：接口那层要能区分
    "这条不存在 / 不是你的"（404/403）和"参数写错了"（400）。
    """
    kind = _kind_of(kind)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM prompt_library"
            " WHERE id=? AND owner_id=? AND kind=? AND active=1",
            (int(pid), owner, kind)).fetchone()
    return _lib_dict(row, True) if row else None


def resolve_prompt_for_use(viewer, pid, kind=PROMPT_KIND_CLASSIFY):
    """要拿某条提示词去跑分类：可以是"我自己的"，也可以是"别人公开的"。

    返回 (prompt_id, name, content, owner_id, is_mine)。

    **这是唯一一处"别人的内容"被取出来的地方，取出来直接交给任务，
    绝不回给前端。** 所以它返回的是元组而不是字典 —— 免得有人顺手
    jsonify 一下就发出去了。
    """
    kind = _kind_of(kind)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM prompt_library WHERE id=? AND kind=? AND active=1",
            (int(pid), kind)).fetchone()
    if not row:
        raise ValueError("这条提示词不存在，或者已经被删了")

    mine = (row["owner_id"] == viewer)
    if not mine and row["visibility"] != VIS_PUBLIC:
        # 说清楚是"没公开"而不是"不存在" —— 她自己是作者时能立刻明白
        raise ValueError("这条提示词是别人私有的，用不了")
    return (row["id"], row["name"], row["content"] or "",
            row["owner_id"], mine)


def create_library_prompt(owner, name, content, usage_note="", summary="",
                          visibility=VIS_PRIVATE, kind=PROMPT_KIND_CLASSIFY):
    """新建一条。同名直接拒绝（不覆盖）—— 覆盖会静默毁掉她之前写的那份。"""
    kind = _kind_of(kind)
    name, content, usage_note, summary, visibility = _check_prompt_fields(
        name, content, usage_note, summary, visibility)
    ts = now_str()
    with db.connect() as conn:
        dup = conn.execute(
            "SELECT id FROM prompt_library"
            " WHERE owner_id=? AND kind=? AND name=? AND active=1",
            (owner, kind, name)).fetchone()
        if dup:
            raise ValueError("你已经有一条叫「%s」的了，换个名字"
                             "（或者在原来那条上改）" % name)
        # 之前删过的同名条目还占着 UNIQUE 位，先把它的名字腾出来
        conn.execute(
            "UPDATE prompt_library SET name = name || '（已删除' || id || '）'"
            " WHERE owner_id=? AND kind=? AND name=? AND active=0",
            (owner, kind, name))
        cur = conn.execute(
            "INSERT INTO prompt_library"
            " (owner_id, kind, name, content, usage_note, summary,"
            "  visibility, use_count, active, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,0,1,?,?)",
            (owner, kind, name, content, usage_note, summary,
             visibility, ts, ts))
        pid = cur.lastrowid
    return get_library_prompt(owner, pid, kind)


def _get_library_prompt_anykind(owner, pid):
    """按 id 取我的一条，**不过滤 kind**。只给"改一条 / 删一条"用。

    【为什么改一条不能过滤 kind】
      改的时候界面上只知道 id，不知道这条属于哪一档（也不该知道 ——
      她点的是"编辑这条"，不是"编辑分类那一档里的这条"）。
      要是这里按默认的 classify 去取，内化的条目永远取不到，
      表现是"点保存没反应"或者更糟：报"没有这条提示词"，
      而她明明看见它就在列表里。
      kind 该由 **取出来的那条自己**说了算（后面 cur["kind"] 就是这么用的）。
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM prompt_library"
            " WHERE id=? AND owner_id=? AND active=1",
            (int(pid), owner)).fetchone()
    return _lib_dict(row, True) if row else None


def update_library_prompt(owner, pid, patch):
    """改一条。patch 里"字段在不在"决定改不改 —— 跟别的接口一个规矩。

    content 允许为空串吗？不允许（跟新建一致），否则会留下一条点不动的东西。
    """
    cur = _get_library_prompt_anykind(owner, pid)
    if not cur:
        return None
    merged = {
        "name": cur["name"],
        "content": cur["content"],
        "usage_note": cur["usage_note"],
        "summary": cur["summary"],
        "visibility": cur["visibility"],
    }
    for k in merged:
        if k in patch and patch[k] is not None:
            merged[k] = patch[k]
    name, content, usage_note, summary, visibility = _check_prompt_fields(
        merged["name"], merged["content"], merged["usage_note"],
        merged["summary"], merged["visibility"])
    with db.connect() as conn:
        dup = conn.execute(
            "SELECT id FROM prompt_library"
            " WHERE owner_id=? AND kind=? AND name=? AND active=1 AND id<>?",
            (owner, cur["kind"], name, int(pid))).fetchone()
        if dup:
            raise ValueError("你已经有一条叫「%s」的了" % name)
        conn.execute(
            "UPDATE prompt_library SET name=?, content=?, usage_note=?,"
            " summary=?, visibility=?, updated_at=?"
            " WHERE id=? AND owner_id=?",
            (name, content, usage_note, summary, visibility,
             now_str(), int(pid), owner))
    # 按**它自己的** kind 取回来，别用默认值 —— 内化的条目用默认值取不到
    return get_library_prompt(owner, pid, _kind_of(cur["kind"]))


def delete_library_prompt(owner, pid):
    """软删（active=0）。任务记录里还引用着它，物理删掉历史就查不到名字了。"""
    with db.connect() as conn:
        c = conn.execute(
            "UPDATE prompt_library SET active=0, updated_at=?"
            " WHERE id=? AND owner_id=? AND active=1",
            (now_str(), int(pid), owner))
        return c.rowcount > 0


def bump_prompt_use(pid):
    """用一次就记一笔。纯计数，失败了也不影响分类，所以不抛错。"""
    try:
        with db.connect() as conn:
            conn.execute("UPDATE prompt_library SET use_count=use_count+1"
                         " WHERE id=?", (int(pid),))
    except Exception:                                        # pragma: no cover
        pass


def build_messages(categories, sub_tags, material_title, items,
                   user_prompt="", template=None, src=None, warn=None):
    """拼这次要发出去的提示词。返回 (messages, 来源, 警告语)。

    【这段是准确率的关键】
    模型判得准不准，八成看这里写得好不好。三个要点：

      1. 判据用**她写在数据库里的原文**，不改写、不美化 ——
         她写「和「心理」的分界：神态看得见、心理看不见」，
         就把这句原样递给它，别翻译成自己的说法。

      2. 明确说「拿不准就留空」，并且说清为什么。
         不说的话，模型倾向每条都硬塞一个类 —— 而判错的主类
         比没有主类更难发现，因为它披着「AI 建议」的外衣。

      3. 要它说理由，而且要求具体。
         理由空洞（「描写生动」）她就没法判断模型到底有没有读错。

    【为什么返回来源和警告】
    模板可能来自文件、也可能退回通用版。任务表里要记准是哪一版，
    否则她改完文件重跑两次、拿结果比准不准，却不知道两次是不是同一版。
    模板写坏了（少了占位符）也必须说出来 —— 那是静默丢内容，最难查。
    """
    cats_block = []
    for i, c in enumerate(categories, 1):
        line = "%d. %s\n   判据：%s" % (
            i, c["name"], (c.get("description") or "").strip())
        sug = c.get("suggested_tags") or []
        if sug:
            line += "\n   这类常用到的副标签：%s" % "、".join(sug)
        cats_block.append(line)

    if template is None:
        template, src, warn = prompt_template()
    elif src is None:
        src, warn = "custom", ""

    # 补充提示词：她自己写的那几句，拼成一块塞进模板的 {user_prompt}。
    # 为什么要把它单独框起来、还写明"不能违反上面那些"：
    #   她写的东西和硬约束混在一起，模型有可能为了满足她的额外要求
    #   而改掉主类清单或输出格式 —— 那整批就解析失败了。
    #   所以这里既给她自由，又把它排在硬约束后面。
    extra = (user_prompt or "").strip()
    if extra:
        extra_block = ("\n【作者本人的补充要求】\n"
                       "下面是作者这次特意交代的，请尽量照做。\n"
                       "但它**不能推翻上面任何一条硬规则** ——"
                       "主类只能从清单里选、判不出就填 null、"
                       "只输出 JSON 数组，这三条永远有效。\n"
                       "-----------\n%s\n-----------\n" % extra)
    else:
        extra_block = ""

    system = _fill_slots(
        template,
        categories="\n\n".join(cats_block),
        sub_tags=("、".join(sub_tags) if sub_tags else "（暂时没有）"),
        user_prompt=extra_block,
    )

    head = "请判断下面 %d 条素材。" % len(items)
    if material_title:
        head += ("（它们来自一份叫「%s」的素材，这个信息只当背景，"
                 "不用写进理由。）" % material_title)
    body = "\n\n".join(
        "[%s] %s" % (it.get("card_id"), (it.get("text") or "").strip())
        for it in items)

    # 这几行不走模板：它们是每次都要现拼的（条数 / 标题 / 正文），
    # 换模板的人不该有机会把它们写丢。模板只管"怎么问"。
    return ([{"role": "system", "content": system},
             {"role": "user", "content": head + "\n\n" + body}],
            src, warn)



def _as_list(d):
    """模型有时给数组、有时给包着数组的对象。抹平成数组。"""
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in ("items", "results", "data", "suggestions", "cards", "list"):
            v = d.get(k)
            if isinstance(v, list):
                return v
        if "card_id" in d:            # 只判了一条，直接给了个对象
            return [d]
    raise SuggestionError("模型的返回不是数组，是 %s" % type(d).__name__)


def _extract_json_array(text):
    """从模型返回的文本里抠出 JSON 数组。

    【为什么不直接 json.loads】
    模型很爱在 JSON 外面裹东西：```json 围栏、「好的，结果如下：」、
    末尾再补一句「以上共 25 条」。这些都是常态，不是极端情况。

    三步走：剥围栏 → 直接 parse → 找第一个 [ 到最后一个 ] 截出来 parse。
    """
    s = (text or "").strip()

    if s.startswith("```"):
        nl = s.find("\n")
        if nl >= 0:
            s = s[nl + 1:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3].rstrip()

    try:
        return _as_list(json.loads(s))
    except SuggestionError:
        raise
    except Exception:
        pass

    for a, b in (("[", "]"), ("{", "}")):
        i, j = s.find(a), s.rfind(b)
        if i >= 0 and j > i:
            try:
                return _as_list(json.loads(s[i:j + 1]))
            except SuggestionError:
                raise
            except Exception:
                continue

    raise SuggestionError(
        "模型的返回里找不到 JSON 数组。它说的前 200 字：%s" % s[:200])


def pick_model(model_key=None):
    """挑一条能用的模型配置（返回的那条里带明文密钥，别往外露）。

    指定了按指定的；没指定就用清单里第一个填了 Key 的。
    一个都用不了就报错，**并且说清去哪儿填** ——
    「一个能用的模型都没有」这种话没有信息量。
    """
    if model_key:
        m = llm.get_model(model_key)
        if m is None:
            raise ValueError("没有这个模型：%s" % model_key)
        if not (m.get("api_key") or "").strip():
            raise ValueError(
                "「%s」还没填 API Key。去「模型设置」里填上，"
                "或者换一个已经配好的模型。" % (m.get("label") or model_key))
        return m

    avail = llm.usable_models()
    if not avail:
        raise ValueError(
            "现在一个能用的模型都没有 —— 去「模型设置」里给至少一个模型"
            "填上 API Key（比如通义千问那个），再回来点自动分类。")
    return avail[0]


class LlmClassifier(object):
    """真·大模型分类器。

    这一层只干三件事：
        1. 把「她的 11 类判据 + 副标签 + 这一批正文」拼成提示词
        2. 通过 llm.py 发出去（用哪个模型由任务指定）
        3. 把模型说的话读成结构化结果，交给 _validate_suggestions 校验

    【为什么不在这里写 HTTP】
    写在这儿的话，以后加一家新模型就得改这个文件。分出去之后，
    加模型只要往 data/models.json 里加一条 —— 不用碰代码。
    """

    name = "llm"
    label = "大模型"
    model_version = ""            # 运行时才知道（用哪个模型由任务定）

    @property
    def prompt_version(self):
        """这一版提示词是哪一份。

        为什么做成动态的而不是写死一个常量：
            她改 prompts/classify.txt 之后，"这次用的是她调的那版、
            还是退回通用版了"必须能从任务记录里看出来。
            写死常量的话，两次任务的提示词版本会一模一样 ——
            她拿两次结果比准不准，却不知道中间换了提示词。
        """
        _, src, _w = prompt_template()
        return PROMPT_VERSION if src == "file" else PROMPT_VERSION_GENERIC

    def classify_batch(self, items, ctx):
        if not items:
            return []

        cfg = pick_model(ctx.get("model_key"))
        messages, _src, warn = build_messages(
            ctx.get("categories_detail") or [],
            ctx.get("all_sub_tags") or [],
            ctx.get("material_title") or "",
            items,
            user_prompt=ctx.get("user_prompt") or "",
            # 整批共用一份模板（任务开头解析一次就固定下来）。
            # 为什么不在每批里现读文件：跑的过程中她改了文件的话，
            # 同一份素材会前后用两版提示词判 ——
            # 结果里混着两套判断依据，而她看不出来。
            template=ctx.get("prompt_template"),
            src=ctx.get("prompt_src"),
            warn=ctx.get("prompt_warn") or "",
        )
        if warn:
            ctx["prompt_warn"] = warn     # 收尾时写进任务备注给她看

        text = _ask(cfg, messages)

        out = []
        for r in _extract_json_array(text):
            if not isinstance(r, dict):
                continue                      # 数组里混进别的类型，跳过

            # **原样带上它给的所有字段**，再往下归一化我认识的那几个。
            #
            # 为什么不能只挑自己认识的字段：
            #   _validate_suggestions 里有一条「返回里带了正文就整批拒收」的
            #   规矩（正文只能来自原材料）。如果这里先把不认识的字段丢掉，
            #   那条规矩就永远拦不到任何东西 —— 闸门修得再好，
            #   前面有人把水滤干净了也没用。
            item = dict(r)
            cat = r.get("primary_category")
            item.update({
                "card_id": r.get("card_id"),
                # 字符串就去个空格；不是字符串就原样留着，
                # 让 _validate_suggestions 去拒收（类型坏了说明整批不可信）
                "primary_category": cat.strip() if isinstance(cat, str) else cat,
                "tags": r.get("tags") or [],
                "reason": str(r.get("reason") or ""),
                "confidence": r.get("confidence"),
                "suggest_exclude": bool(r.get("suggest_exclude")),
                "unsure": bool(r.get("unsure")),
            })
            out.append(item)
        return out


def _ask(cfg, messages):
    """问一次模型，返回它说的话（原始文本）。

    json_mode 先开着试：它让服务端保证返回合法 JSON。
    但不是所有兼容实现都支持这个参数，报 400 就关掉再来一次 ——
    提示词里已经写死了「只输出一个 JSON 数组」，关掉也能解析，
    只是少一道保险，所以在这里补一次重试，而不是直接失败。
    """
    try:
        return llm.chat(cfg, messages, temperature=0.0, json_mode=True)["content"]
    except llm.LlmError as e:
        if e.status != 400:
            raise
        return llm.chat(cfg, messages, temperature=0.0, json_mode=False)["content"]


CLASSIFIERS = {
    PlaceholderClassifier.name: PlaceholderClassifier(),
    LlmClassifier.name: LlmClassifier(),
}


def get_classifier(name=None):
    """拿一个分类器。名字不认识就直接报错，不悄悄退回默认值 ——
    静默降级会让人以为用的是 A，其实跑的是 B。"""
    key = name or PlaceholderClassifier.name
    c = CLASSIFIERS.get(key)
    if c is None:
        raise ValueError("没有这个分类器：%s（可选：%s）"
                         % (key, "、".join(sorted(CLASSIFIERS))))
    return c


def classifier_label(name=None):
    """给界面用的中文名。界面上不该出现 placeholder / llm 这种内部键名。"""
    key = name or PlaceholderClassifier.name
    c = CLASSIFIERS.get(key)
    return getattr(c, "label", None) or key


def _resolve_engine(classifier_name=None, model_key=None):
    """把「用哪种方法 + 用哪个模型」定下来。

    返回 (分类器, 提示词版本, 模型版本, 模型 key, 提示词来源)。

    用大模型但没配好（没填 Key、模型不存在）会直接抛 ——
    要求调用方在**建任务之前**就把它报出来。等后台线程跑起来才发现的话，
    她看到的是任务闪一下变成「失败」，真正的原因（没填 Key）埋在里面。

    抽成一个函数是因为 create_run 和 retry_run 都要这套判断，
    而"重试"和"新建"绝不能各判各的 —— 那正是最容易跑出两种行为的地方。
    """
    clf = get_classifier(classifier_name)
    prompt_version = getattr(clf, "prompt_version", PROMPT_VERSION_PLACEHOLDER)
    model_version = clf.model_version
    # 提示词来源也在这里定：界面要如实显示"这次用的是谁调的那版"，
    # 不然她改完 prompts/classify.txt 重跑，两次结果对不上却找不到原因。
    _tpl, prompt_src, _warn = prompt_template()
    if isinstance(clf, LlmClassifier):
        cfg = pick_model(model_key)
        model_key = cfg["key"]
        model_version = cfg["model"]
    else:
        model_key = ""
        prompt_src = "local"      # 本地规则不用提示词，"file/builtin" 会误导
    return clf, prompt_version, model_version, model_key, prompt_src


# ----------------------------------------------------------------------
# 四、服务端校验：AI 说什么不算数，得先过这里
# ----------------------------------------------------------------------

class SuggestionError(Exception):
    """返回值不合规。整批拒收，不写进正式分类。"""


def _validate_suggestions(raw, valid_ids, category_names):
    """检查分类器返回的东西合不合规，返回 (干净的列表, 不认识的类名集合)。

    任务书第七节的每一条都落在这里了。为什么必须校验：
        模型的输出是"外部输入"，和用户填的表单一样不可信。
        它会编出不存在的 card_id、会返回 1.7 这种置信度、
        甚至会把原文粘回来 —— 这些都是真实会发生的。
        让不合规的数据写进卡片库，以后就再也分不清
        "这是 AI 判的"还是"这是脏数据"。

    【两种"不合格"要分开处理 —— 这是 2026-09-24 修的一个真错误】

    一类是**整批拒收**：返回里带了正文。
        因为 AI 一旦可以"重写正文"，卡片和原材料就对不上了，
        而偏移锚点这套设计的前提是"卡片正文永远等于原文那一段"。
        宁可整批失败，也不能让正文出现第二个来源。

    另一类是**只降级这一条**：主类名不存在。
        原来这里也是整批拒收，结果换分类体系那天出了个大坑 ——
        占位规则还在按旧九类输出（"世界观设定"这种），
        而当前只有八类，于是**每一批都被拒，整份素材 0/15 全失败**，
        界面上看着像"功能坏了"，实际只是类名对不上。

        而且"模型偶尔编个类名"本来就是常态（它可能把「外貌」写成
        「外貌描写」）。为一条小毛病把另外 19 条对的也扔掉，
        代价太大 —— 重跑一次还得再花钱、再等一遍。

        所以现在的做法：**这一条不给主类**（按"判不出"落库，
        状态是「分类失败」，她一眼能筛出来自己处理），
        并把编出来的类名收集起来交给调用方写进任务备注 ——
        她看得见"模型说了三个我不认识的类名：xxx"，
        才能判断是模型乱说，还是自己该建一个类。
    """
    allowed = set(category_names)
    ok, seen, unknown = [], set(), set()
    valid_ids = set(valid_ids or ())

    for r in raw or []:
        if not isinstance(r, dict):
            raise SuggestionError("返回里有一项不是对象：%r" % (r,))

        cid = r.get("card_id")
        if cid is None:
            raise SuggestionError("有一项没有 card_id")
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            raise SuggestionError("card_id 不是整数：%r" % (r.get("card_id"),))

        if cid not in valid_ids:
            raise SuggestionError("返回了这次没让它判的 card_id：%s" % cid)
        if cid in seen:
            raise SuggestionError("同一个 card_id 返回了两次：%s" % cid)
        seen.add(cid)

        # 带了正文 → 立刻整批拒收
        for bad_key in ("text", "content", "body", "正文"):
            if r.get(bad_key):
                raise SuggestionError(
                    "返回里带了正文（字段 %s）。正文只能来自原材料，"
                    "不许由分类器提供 —— 整批拒收。" % bad_key)

        bad_cat = None
        cat = r.get("primary_category")
        if cat not in (None, ""):
            if cat not in allowed:
                # 不认识 → 只降级这一条，不整批拒收（理由见上面的 docstring）
                bad_cat = str(cat).strip()[:40]
                unknown.add(bad_cat)
                cat = None
        else:
            cat = None

        tags = r.get("tags") or []
        if not isinstance(tags, list):
            raise SuggestionError("tags 不是数组：%r" % (tags,))
        tags = [str(t).strip() for t in tags if str(t).strip()]

        conf = r.get("confidence")
        if conf is not None:
            try:
                conf = float(conf)
            except (TypeError, ValueError):
                raise SuggestionError("confidence 不是数字：%r" % (conf,))
            if not 0.0 <= conf <= 1.0:
                raise SuggestionError("confidence 不在 0~1 之间：%r" % (conf,))

        # 类名不认识也算"没把握"：理由要写在卡片上给她看。
        unsure = bool(r.get("unsure")) or bad_cat is not None

        # 说了"拿不准"或者没给主类 → 那就不该有主类，置信度也不该显得很确定。
        # 这一步是为了让界面能靠"有没有主类"直接判断要不要人工看，
        # 不用再解析理由文本。
        if unsure or cat is None:
            cat = None
            conf = None if unsure else conf

        reason = (r.get("reason") or "").strip()
        if bad_cat:
            reason = ("【类名不认】模型说的是「%s」，不在当前分类体系里。"
                      % bad_cat) + reason

        ok.append({
            "card_id": cid,
            "primary_category": cat,
            "tags": tags,
            "reason": reason[:1000],
            "confidence": conf,
            "unsure": unsure,
            "suggest_exclude": bool(r.get("suggest_exclude")),
        })

    # 少返回了几条不算错（模型偶尔会漏），但一条都没有就是错 ——
    # 那通常意味着整批失败了，不该当成"这一批都没意见"悄悄过掉。
    if valid_ids and not ok:
        raise SuggestionError("这一批一条建议都没返回")
    return ok, unknown


# ----------------------------------------------------------------------
# 五、任务的读写
# ----------------------------------------------------------------------

RUN_FIELDS = ("status", "segment_run_id", "retry_of_run_id", "rule",
              "rule_version", "norm_version", "category_set_version",
              "prompt_version", "model_version", "classifier", "model_key",
              "total_items", "done_items",
              "failed_items", "created_cards", "skipped_items",
              "cancel_requested", "error", "note", "created_at", "started_at",
              "finished_at", "heartbeat_at", "user_prompt",
              "prompt_ref_id", "prompt_name", "prompt_owner")


def _run_dict(row):
    d = dict(row)
    d["status_text"] = RUN_STATUS_TEXT.get(d["status"], d["status"])
    d["active"] = d["status"] in RUN_ACTIVE

    # ---- 「公开但私密」的脱敏点 ----
    # 这次用的提示词要是**别人**库里的那条，就把 user_prompt 抹掉。
    # 理由：任务记录会原样返回给发起人（/api/classification-runs），
    # 而"发起人"不等于"提示词的作者" —— 不抹的话，
    # 乙用甲公开的提示词跑一次，翻一下任务历史就拿到甲写的内容了。
    #
    # 做在这一层（序列化）而不是各个接口里，是为了让**所有出口自动安全**：
    # 将来新加一个"导出任务记录"之类的接口，也不会漏。
    p_owner = d.get("prompt_owner") or ""
    if p_owner and p_owner != (d.get("owner_id") or ""):
        d["user_prompt_len"] = len(d.get("user_prompt") or "")
        d["user_prompt"] = ""
        d["prompt_masked"] = True
    else:
        d["prompt_masked"] = False

    total = d["total_items"] or 0
    d["progress"] = 0.0 if not total else round(
        min(1.0, (d["done_items"] + d["failed_items"]) / float(total)), 4)

    # 心跳：从"最后动过"到现在过了几秒。
    #
    # 【为什么界面需要这个数，而不是自己算时间差】
    #   前端算的话得依赖浏览器时钟，她这台机器的时间要是偏了几分钟，
    #   就会看到"最后更新 -320 秒前"这种荒唐的数。
    #   服务端算好秒数传出去，界面只管显示。
    d["stale_seconds"] = None
    if d.get("heartbeat_at"):
        try:
            t0 = _parse_ts(d["heartbeat_at"])
            t1 = _parse_ts(d["finished_at"]) if d.get("finished_at") else datetime.now()
            d["stale_seconds"] = max(0, int((t1 - t0).total_seconds()))
        except Exception:
            d["stale_seconds"] = None
    return d


def _parse_ts(s):
    """解析库里那种 '2026-09-24 18:45:50' 的时间串。

    自己写而不是用 fromisoformat，是因为 Python 3.7 以前不认空格分隔，
    而这个项目在不止一台机器上跑过，没必要为这点小事赌版本。
    """
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError("时间格式不认识：%r" % s)


def get_run(run_id, owner):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM classification_runs WHERE id=? AND owner_id=?",
            (run_id, owner)).fetchone()
        return _run_dict(row) if row else None


def list_runs(owner, material_id=None, limit=50):
    where = ["owner_id=?"]
    params = [owner]
    if material_id:
        where.append("material_id=?")
        params.append(material_id)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM classification_runs WHERE " + " AND ".join(where) +
            " ORDER BY id DESC LIMIT ?", params + [limit]).fetchall()
        return [_run_dict(r) for r in rows]


def list_items(run_id, owner, status=None, limit=500, offset=0):
    """任务明细。只返回属于这个人的任务里的条目。"""
    with db.connect() as conn:
        run = conn.execute(
            "SELECT id FROM classification_runs WHERE id=? AND owner_id=?",
            (run_id, owner)).fetchone()
        if not run:
            return None
        where = ["i.run_id=?"]
        params = [run_id]
        if status:
            where.append("i.status=?")
            params.append(status)
        total = conn.execute(
            "SELECT COUNT(*) FROM classification_items i WHERE " +
            " AND ".join(where), params).fetchone()[0]
        rows = conn.execute(
            "SELECT i.*, c.material_id, c.start_offset, c.end_offset,"
            " c.status AS card_status, c.primary_category_id,"
            " c.ai_reason, c.ai_confidence"
            " FROM classification_items i LEFT JOIN cards c ON c.id = i.card_id"
            " WHERE " + " AND ".join(where) +
            " ORDER BY i.seq, i.id LIMIT ? OFFSET ?",
            params + [limit, offset]).fetchall()
        return {"total": total, "items": [dict(r) for r in rows],
                "offset": offset, "limit": limit}


def active_run(owner, material_id):
    """这个文件现在有没有正在跑的任务。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM classification_runs WHERE owner_id=? AND material_id=?"
            " AND status IN (%s) ORDER BY id DESC LIMIT 1"
            % ",".join("?" * len(RUN_ACTIVE)),
            [owner, material_id] + list(RUN_ACTIVE)).fetchone()
        return _run_dict(row) if row else None


def latest_run(owner, material_id):
    rs = list_runs(owner, material_id=material_id, limit=1)
    return rs[0] if rs else None


# ----------------------------------------------------------------------
# 六、候选卡片：哪些卡片会被交给分类器
# ----------------------------------------------------------------------

def _target_card_ids(conn, owner, material_id, only_card_ids=None):
    """挑出这次要处理的卡片。

    三条筛选，每条都有原因：

      状态必须是「待确认」或「分类失败」
          「已确认」的绝不能碰 —— 那是人工成果。
          「已排除」的也不碰 —— 人已经说了不要它。
          「已内化」「暂不用」同理，所以这里用"白名单"而不是"黑名单"。

      来源不能是 human
          人手动改过主类的卡片，再让规则覆盖一遍就是把她的劳动抹掉。
          （update_cards 在改主类时会把 source 标成 human，见那边的注释。）

      主类必须是空的，或者本来就是 AI 给的
          已经有主类的卡片（比如从文件名继承来的）不覆盖。
          这是"自动分类"和"来源批量采纳"两条线的分工：前者填空，后者改已有。
    """
    where = ["owner_id=?",
             "material_id=?",
             "status IN (%s)" % ",".join("?" * len(TARGET_STATUS)),
             "source<>?",
             "(primary_category_id IS NULL OR source=?)"]
    params = [owner, material_id] + list(TARGET_STATUS) + \
             [sg.SOURCE_HUMAN, sg.SOURCE_AI]

    if only_card_ids:
        where.append("id IN (%s)" % ",".join("?" * len(only_card_ids)))
        params = params + list(only_card_ids)

    rows = conn.execute(
        "SELECT id FROM cards WHERE " + " AND ".join(where) +
        " ORDER BY start_offset, id", params).fetchall()
    return [r["id"] for r in rows]


def target_preview(owner, material_id):
    """不写任何数据，只回答"点一下自动分类会处理多少张卡"。

    为什么要单独给一个预览：她点按钮之前应该能知道
    "这次会动 630 张卡"还是"这次会动 0 张（因为都已经有主类了）"。
    后一种情况如果不提前说，点完什么也没发生，她会以为坏了。
    """
    with db.connect() as conn:
        m = conn.execute("SELECT id, title FROM materials WHERE id=? AND owner_id=?",
                         (material_id, owner)).fetchone()
        if not m:
            return None
        def one(sql, *p):
            return conn.execute(sql, p).fetchone()[0]

        todo = _target_card_ids(conn, owner, material_id)
        n_all = one("SELECT COUNT(*) FROM cards WHERE owner_id=? AND material_id=?",
                    owner, material_id)
        n_confirmed = one("SELECT COUNT(*) FROM cards WHERE owner_id=? AND "
                          "material_id=? AND status=?",
                          owner, material_id, sg.STATUS_CONFIRMED)
        n_human = one("SELECT COUNT(*) FROM cards WHERE owner_id=? AND "
                      "material_id=? AND source=?",
                      owner, material_id, sg.SOURCE_HUMAN)
        n_excluded = one("SELECT COUNT(*) FROM cards WHERE owner_id=? AND "
                         "material_id=? AND status=?",
                         owner, material_id, sg.STATUS_EXCLUDED)

    # 有了主类所以不会被自动分类碰的 = 总数 - 这次要动的 - 人工改过的 - 已排除
    n_has_cat = max(0, n_all - len(todo) - n_human - n_excluded)
    return {"material_id": material_id, "title": m["title"],
            "cards": n_all, "will_process": len(todo),
            "confirmed": n_confirmed, "already_classified": n_has_cat,
            "human_edited": n_human, "excluded": n_excluded}


# ----------------------------------------------------------------------
# 七、创建任务
# ----------------------------------------------------------------------

def create_run(owner, material_id, classifier_name=None, model_key=None,
               retry_of_run_id=None, background=True, user_prompt=None,
               prompt_id=None):
    """建一个分类任务。返回 (结果字典, run_id)。

    classifier_name  用哪种方法：留空/placeholder = 本地规则，llm = 大模型
    model_key        用哪个模型（只有大模型才看这一项，比如 qwen-plus）
    user_prompt      作者这次写的补充提示词；None = 用她存的那份
    prompt_id        用「提示词库」里的哪一条（可以是别人公开出来的那些）。

    【prompt_id 和 user_prompt 的关系】
      两个都传时 **prompt_id 说了算**，user_prompt 被忽略。
      理由是"她刚从快捷选项里挑了一条"是个明确得多的意图，
      而输入框里可能还留着上一次敲的半句话。
      所以前端挑中库里某条之后，要负责把输入框清掉（不然她会以为两个都发了）。

    【prompt_id 最要紧的一条：别人的内容不许外泄】
      这条提示词可能是**别人公开出来的**。我们要用它的内容去发请求，
      但**绝不能把内容回给前端**（公开 = 她能用，不是她能看到）。
      所以下面解析出来的 content 只落在 user_prompt 这个字段里直接进库，
      返回值里只带 name，不带 content。

    background=True  → 起一个后台线程跑，立刻返回（给接口用）
    background=False → 当场跑完再返回（给测试和命令行用）

    为什么默认后台：任务书第十五节明确禁止"把任务执行放在 HTTP 请求里
    长时间阻塞页面"。一个 663 条卡片的文件，就算全是本地规则也要几秒；
    接上模型之后是几十秒到几分钟 —— 放在请求里，页面就死了。
    """
    # 先把这个任务要用的「方法 + 模型」定下来。
    # 分类器名字写错、模型没填 Key、模型 key 不存在 —— 都在这里当场报，
    # 不要等建完任务才发现（那时她看到的是任务闪一下就变「失败」）。
    clf, prompt_version, model_version, model_key, prompt_src = _resolve_engine(
        classifier_name, model_key)

    # ---- 提示词：库里的某一条，优先于自由文本 ----
    ref_id, ref_name, ref_owner, ref_owner_mine = 0, "", "", True
    if prompt_id:
        try:
            ref_id, ref_name, lib_content, ref_owner, ref_owner_mine = \
                resolve_prompt_for_use(owner, prompt_id)
        except ValueError as e:
            return {"ok": False, "reason": "bad_prompt",
                    "message": str(e)}, None
        user_prompt = lib_content

    # 补充提示词：**存快照**，不存"她当前写着什么"。
    # 她跑完一轮会去改这句再跑第二轮，回头必须还能答出"第一轮到底怎么问的"。
    if prompt_id:
        pass          # 上面已经从库里取了内容，别再被 get_user_prompt 覆盖
    elif user_prompt is None:
        user_prompt = get_user_prompt(owner)
    user_prompt = (user_prompt or "").strip()
    if len(user_prompt) > USER_PROMPT_MAX:
        return {"ok": False, "reason": "prompt_too_long",
                "message": "补充提示词最长 %d 字，现在 %d 字。"
                           % (USER_PROMPT_MAX, len(user_prompt))}, None

    if retry_of_run_id is not None and not isinstance(retry_of_run_id, int):
        retry_of_run_id = None

    with _create_lock:
        # ---- 先确认这份素材是她的 ----
        with db.connect() as conn:
            m = conn.execute(
                "SELECT id, title FROM materials WHERE id=? AND owner_id=?",
                (material_id, owner)).fetchone()
            if not m:
                return {"ok": False, "reason": "no_material",
                        "message": "没有这份素材，或者它不属于你"}, None

            # ---- 规矩五：同一个文件不许同时跑两个任务 ----
            row = conn.execute(
                "SELECT id FROM classification_runs WHERE owner_id=? AND "
                "material_id=? AND status IN (%s) LIMIT 1"
                % ",".join("?" * len(RUN_ACTIVE)),
                [owner, material_id] + list(RUN_ACTIVE)).fetchone()
            if row:
                return {"ok": False, "reason": "already_running", "run_id": row["id"],
                        "message": "这个文件已经有一个分类任务在跑了（任务 #%d），"
                                   "等它结束或者先取消它。" % row["id"]}, row["id"]

            card_ids = _target_card_ids(conn, owner, material_id)
            will_split = False
            if not card_ids:
                # 有两种"一张都不处理"的情况，必须分开对待：
                #
                # 一、这份文件压根还没切过 → 应该让任务先替她切一次
                #     （等于在分类页点了一下"按这个切法重切"）。
                #     这是"直接点自动分类"的正常路径，不是错误。
                # 二、卡片都在，但全都有主类 / 全被确认了 → 真的是没事可做，
                #     要如实告诉她，不能建一个空任务让她干等。
                n_all = conn.execute(
                    "SELECT COUNT(*) FROM cards WHERE owner_id=? AND material_id=?",
                    (owner, material_id)).fetchone()[0]
                has_split = conn.execute(
                    "SELECT 1 FROM segment_runs WHERE material_id=? AND is_current=1",
                    (material_id,)).fetchone()
                if n_all == 0 and not has_split:
                    will_split = True
                else:
                    return {"ok": False, "reason": "nothing_to_do",
                            "message": "这个文件没有需要自动分类的卡片。"
                                       "（已经有主类的卡片不会被覆盖，"
                                       "已确认的卡片也不会被动。）"}, None

            now = now_str()
            cur = conn.execute(
                """INSERT INTO classification_runs
                   (owner_id, material_id, retry_of_run_id, status,
                    category_set_version, prompt_version, model_version,
                    classifier, model_key, prompt_source, user_prompt,
                    prompt_ref_id, prompt_name, prompt_owner,
                    total_items, created_at, heartbeat_at, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (owner, material_id, retry_of_run_id, RUN_QUEUED,
                 cls.CATEGORY_SET_VERSION, prompt_version, model_version,
                 clf.name, model_key, prompt_src, user_prompt,
                 ref_id, ref_name, ref_owner,
                 len(card_ids), now, now,
                 "这个文件还没有切分记录，任务开始时自动切了一次"
                 if will_split else ""))
            run_id = cur.lastrowid

    # 用了一次就记一笔（在锁外面做，计数失败不影响分类）。
    if ref_id:
        bump_prompt_use(ref_id)

    if background:
        th = threading.Thread(target=_run_worker, args=(run_id, owner,
                                                        classifier_name, card_ids),
                              daemon=True)
        th.start()
        return {"ok": True, "run_id": run_id, "total_items": len(card_ids),
                "status": RUN_QUEUED, "status_text": RUN_STATUS_TEXT[RUN_QUEUED],
                "will_split_first": will_split,
                "classifier": clf.name, "classifier_label": clf.label,
                "model_key": model_key, "model_version": model_version,
                "prompt_ref_id": ref_id, "prompt_name": ref_name,
                "prompt_from_library": bool(ref_id),
                "message": ("这份文件还没有切分记录，任务会先切一次再分类，"
                            "后台正在跑。" if will_split
                            else "任务已创建（共 %d 张卡片），后台正在跑。"
                                 % len(card_ids)),
                "background": True}, run_id

    _run_worker(run_id, owner, classifier_name, card_ids)
    return {"ok": True, "run_id": run_id, "total_items": len(card_ids),
            "will_split_first": will_split, "background": False,
            "classifier": clf.name, "classifier_label": clf.label,
            "model_key": model_key, "model_version": model_version,
            "prompt_ref_id": ref_id, "prompt_name": ref_name,
            "prompt_from_library": bool(ref_id)}, run_id


# ----------------------------------------------------------------------
# 八、后台执行
# ----------------------------------------------------------------------

def _set_run(conn, run_id, **fields):
    if not fields:
        return
    keys = list(fields)
    conn.execute("UPDATE classification_runs SET %s WHERE id=?"
                 % ", ".join("%s=?" % k for k in keys),
                 [fields[k] for k in keys] + [run_id])


def _cancelled(run_id):
    """外面有没有人点了取消。"""
    with db.connect() as conn:
        r = conn.execute("SELECT cancel_requested, status FROM classification_runs"
                         " WHERE id=?", (run_id,)).fetchone()
        if not r:
            return True
        return bool(r["cancel_requested"]) or r["status"] == RUN_CANCELLED


def reap_orphan_runs(reason="服务重启了"):
    """把上一个进程留下的"还在跑"的任务收尾。返回收尾了几条。

    【为什么必须有这一步】
    后台任务是跑在**进程内的线程**里的。进程一没了（关服务、热重载、
    断电），线程也就没了 —— 但任务表里那几条仍然写着 running。
    后果是界面上永远显示「分类中 xx%」、进度再也不动，
    而她没有任何办法知道它其实早就死了（点取消才行，但她会以为是在跑）。

    【为什么要顺手把没轮到的条目记成"失败"】
    这样下次点「重试」正好只补这些没跑的，**已经判好的那几百张不会重花钱**。
    不这么做的话，重试会走"整份重来"那条路 —— 350 张已经付过钱的
    片段会被再判一遍。接上真模型之后，那是真金白银。

    调用时机：服务启动时（见 main.py 的 lifespan）。
    """
    ts = now_str()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, done_items, total_items FROM classification_runs"
            " WHERE status IN (%s)" % ",".join("?" * len(RUN_ACTIVE)),
            list(RUN_ACTIVE)).fetchall()
        for r in rows:
            cur = conn.execute(
                "UPDATE classification_items SET status=?, error=?, updated_at=?"
                " WHERE run_id=? AND status=?",
                (ITEM_FAILED, reason + "，这一条还没轮到", ts, r["id"],
                 ITEM_PENDING))
            conn.execute(
                "UPDATE classification_runs SET status=?, error=?, note=?,"
                " finished_at=?, heartbeat_at=? WHERE id=?",
                (RUN_FAILED,
                 "%s，这个任务中断了（已经判好的部分都留着）。" % reason,
                 "点「重试分类」会从断的地方接着跑，已经判好的 %s 张不会重花钱。"
                 % (r["done_items"] or 0),
                 ts, ts, r["id"]))
            report_count = cur.rowcount
            print("[分类任务] 任务 #%s 是上次留下的（%s/%s 张），已标成中断；"
                  "其中 %s 条没轮到的记成待重试"
                  % (r["id"], r["done_items"], r["total_items"], report_count))
    return len(rows)


def _run_worker(run_id, owner, classifier_name, card_ids):
    """后台线程真正干活的地方。

    这个函数里**不允许**抛异常出去 —— 抛出去线程就死了，
    任务会永远停在 running。所有异常都在这里被接住、写进任务里。
    """
    try:
        _execute(run_id, owner, classifier_name, card_ids)
    except Exception as e:                                # pragma: no cover
        tb = traceback.format_exc()
        try:
            with db.connect() as conn:
                _set_run(conn, run_id, status=RUN_FAILED,
                         error="任务异常：%s" % e, finished_at=now_str())
        except Exception:
            pass
        print("[分类任务] 任务 #%s 崩了：%s\n%s" % (run_id, e, tb))


def _execute(run_id, owner, classifier_name, card_ids):
    # ---- 用哪个分类器 / 哪个模型，以**任务记录**为准 ----
    #
    # 为什么不信任外面传进来的参数：
    #   任务记录是唯一真相（重试出来的任务是从老记录复制的），
    #   而参数链要经过 create_run → 后台线程 → _run_worker 三层，
    #   中间任何一环传丢，都会静默跑成另一种分类器。
    #   记录里写着用 qwen-max，就绝不该用本地规则跑完 ——
    #   那种错最难发现，因为结果看起来「也像那么回事」。
    with db.connect() as conn:
        run = conn.execute("SELECT * FROM classification_runs WHERE id=?",
                           (run_id,)).fetchone()
        if not run:
            return
        material_id = run["material_id"]
        classifier_name = run["classifier"] or classifier_name
        model_key = run["model_key"] or ""

    try:
        clf = get_classifier(classifier_name)
    except Exception as e:
        with db.connect() as conn:
            _set_run(conn, run_id, status=RUN_FAILED, error=str(e),
                     finished_at=now_str())
        return

    with db.connect() as conn:
        # ---- 切分：复用现成的那一次，不重切（这是她拍板的第 3 条）----
        #
        # 情况一：这个文件已经切过了 → 直接用当前的 segment_run。
        # 情况二：从没切过（她可能直接点的"自动分类"）→ 现在替她切一次，
        #         等于在界面上点了那个按钮。旧卡片不会被删，已有卡片会被跳过。
        srun = conn.execute(
            "SELECT id, rule, rule_version, norm_version FROM segment_runs"
            " WHERE material_id=? AND is_current=1", (material_id,)).fetchone()
        _set_run(conn, run_id, status=RUN_RUNNING, started_at=now_str())

    if not srun:
        sp = cls.apply_split(material_id, owner)
        if not sp or not sp.get("ok"):
            with db.connect() as conn:
                _set_run(conn, run_id, status=RUN_FAILED,
                         error="这个文件还没切分，自动切分也没成功：%s"
                               % ((sp or {}).get("message") or "未知原因"),
                         finished_at=now_str())
            return
        with db.connect() as conn:
            srun = conn.execute(
                "SELECT id, rule, rule_version, norm_version FROM segment_runs"
                " WHERE material_id=? AND is_current=1",
                (material_id,)).fetchone()
            _set_run(conn, run_id, segment_run_id=srun["id"],
                     rule=srun["rule"], rule_version=srun["rule_version"],
                     norm_version=srun["norm_version"],
                     created_cards=sp.get("cards_created", 0),
                     note="这个文件还没有切分记录，任务开始时自动切了一次")

    # 自动切分可能新建了卡片，候选集要重新算一遍（这次真的按库里现状来）
    with db.connect() as conn:
        card_ids = _target_card_ids(conn, owner, material_id)
        _set_run(conn, run_id, total_items=len(card_ids))
        # 明细表先占位，界面上就能看到"共 N 条，还没开始"
        ts = now_str()
        for i, cid in enumerate(card_ids, 1):
            conn.execute(
                "INSERT OR IGNORE INTO classification_items"
                " (run_id, card_id, seq, status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (run_id, cid, i, ITEM_PENDING, ts, ts))

    # 带上 owner：她自己加的主类也要进这一轮的判据清单。
    # 不加的话，她新建的类 AI 永远判不出来，她还以为是模型不行。
    cats = cls.list_categories(owner)
    cat_names = [c["name"] for c in cats]
    cat_id_by_name = {c["name"]: c["id"] for c in cats}

    with db.connect() as conn:
        m = conn.execute("SELECT title FROM materials WHERE id=?",
                         (material_id,)).fetchone()
    sub_rows = cls.list_sub_tags(owner, active_only=True)
    tag_id_by_name = {t["name"]: t["id"] for t in sub_rows}

    ctx = {
        "material_title": m["title"] if m else "",
        # categories 只给占位规则用（它只认名字）
        "categories": cat_names,
        # categories_detail 是给大模型的 —— 它要判据。
        # 光给名字，它只能按字面猜「神态」是什么意思，那跟占位规则没区别。
        "categories_detail": cats,
        # 副标签清单：它只许从这里面挑（提示词里已经说清了）
        "all_sub_tags": [t["name"] for t in sub_rows],
        # 用哪个模型。LlmClassifier 从这个口子拿 ——
        # 模型不写死在分类器实例上，因为同一个实例要能跑不同模型。
        "model_key": model_key,
        # 她写的补充提示词。从**任务记录**里读，不是现读她存的那份 ——
        # 任务跑到一半她去改提示词，不该影响这个已经在跑的任务。
        "user_prompt": "",
    }

    # 提示词模板在这里解析一次，整批任务共用这一份。
    # 理由同 user_prompt：跑到一半改文件不该影响在跑的任务。
    ctx["prompt_template"], ctx["prompt_src"], ctx["prompt_warn"] = \
        prompt_template()
    if run.keys() and "prompt_source" in run.keys() and run["prompt_source"]:
        ctx["prompt_src"] = run["prompt_source"]
    if run.keys() and "user_prompt" in run.keys():
        ctx["user_prompt"] = run["user_prompt"] or ""

    done = failed = 0
    error_samples = []
    # 模型编出来的、不在当前分类体系里的类名。
    # 攒起来写进任务备注 —— 她要能看见"模型说了 xxx 这个类"，才知道
    # 是模型乱说，还是自己该补一个类。
    unknown_cats = set()
    cancelled = False

    # 第一批故意小一点。
    #
    # 【为什么】630 张的素材按 25 条一批是 26 批，每批得等模型回话
    #（实测一批约 50 秒）。也就是说点完「开始」之后，要盯着一个不动的
    # 0% 等将近一分钟 —— 看起来就跟卡死一样。这个误会真的发生过。
    # 第一批只发 6 条，十来秒就能写回一次进度，界面立刻"动"起来。
    # 之后就恢复正常批次：一直用小批次的话，每批都要重发一遍
    # 主类判据（一千多字），总体耗时和费用都会明显涨。
    first_batch = min(FIRST_BATCH_SIZE, len(card_ids))

    def _batches(ids):
        if not ids:
            return
        yield ids[:FIRST_BATCH_SIZE]
        for s in range(FIRST_BATCH_SIZE, len(ids), BATCH_SIZE):
            yield ids[s:s + BATCH_SIZE]

    pos = 0        # 已经发出去的条数，用来给 seq 编号
    for batch_ids in _batches(card_ids):
        if _cancelled(run_id):
            cancelled = True
            break

        base_seq = pos + 1
        pos += len(batch_ids)

        # ---- 取正文：只在这一刻从 materials.content 现算 ----
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT id, start_offset, end_offset FROM cards WHERE owner_id=?"
                " AND id IN (%s)" % ",".join("?" * len(batch_ids)),
                [owner] + batch_ids).fetchall()
            by_id = {r["id"]: r for r in rows}
            content = None
            if rows:
                content = conn.execute("SELECT content FROM materials WHERE id=?",
                                       (material_id,)).fetchone()["content"]
            items = []
            for i, cid in enumerate(batch_ids, base_seq):
                r = by_id.get(cid)
                if r is None:
                    continue
                items.append({"card_id": cid, "seq": i,
                              "text": content[r["start_offset"]:r["end_offset"]]})

        if not items:
            continue

        # ---- 让分类器判 ----
        try:
            raw = clf.classify_batch(items, ctx)
            sugs, batch_unknown = _validate_suggestions(
                raw, {it["card_id"] for it in items}, cat_names)
            unknown_cats |= batch_unknown
            err = ""
        except SuggestionError as e:
            # 整批拒收：不写正式分类，全部记失败并留下原因。
            # 为什么整批而不是逐条挑好的用：
            #   一旦某一批的结果不可信，我们无法知道"看起来对的那几条"是不是
            #   也一起错位了（比如 card_id 整体串了一位）。
            #   宁可整批下次重来，也不要让来源不明的结果混进去。
            #
            # 注意：**只有"带了正文"这类硬伤才走这里**。
            # "主类名不认识"不整批拒收，在校验里就把那一条降级成"判不出"了 ——
            # 理由见 _validate_suggestions 的 docstring。
            sugs, err = [], "返回格式不合规：%s" % e
        except Exception as e:
            sugs, err = [], "分类器报错：%s" % e

        ts = now_str()
        if err:
            failed += len(items)
            if len(error_samples) < 3:
                error_samples.append(err)
            with db.connect() as conn:
                for it in items:
                    conn.execute(
                        "UPDATE classification_items SET status=?, error=?,"
                        " retry_count=retry_count+1, updated_at=?"
                        " WHERE run_id=? AND card_id=?",
                        (ITEM_FAILED, err, ts, run_id, it["card_id"]))
                    conn.execute(
                        "UPDATE cards SET status=?, updated_at=?"
                        " WHERE id=? AND owner_id=? AND status<>? AND source<>?",
                        (sg.STATUS_FAILED, ts, it["card_id"], owner,
                         sg.STATUS_CONFIRMED, sg.SOURCE_HUMAN))
        else:
            _write_suggestions(run_id, owner, material_id, sugs,
                               cat_id_by_name, tag_id_by_name,
                               run["model_version"] or clf.model_version, ctx)
            got = {s["card_id"] for s in sugs}
            with db.connect() as conn:
                for it in items:
                    if it["card_id"] in got:
                        st = ITEM_DONE
                        done += 1
                    else:
                        # 分类器漏了这一条。卡片还是「待确认」，
                        # 下次重跑还会被捞起来，不需要额外标记。
                        st = ITEM_FAILED
                        failed += 1
                    conn.execute(
                        "UPDATE classification_items SET status=?, updated_at=?"
                        " WHERE run_id=? AND card_id=?",
                        (st, ts, run_id, it["card_id"]))

        # ---- 更新进度（每批一次，界面上能一条条往上走）----
        #
        # 顺带写心跳和一句进度说明。心跳回答的是"它还活着吗"：
        # 数字不动有两种可能 —— 在慢慢跑、或者线程早死了，
        # 光看 done_items 分不出来。有心跳，界面就能说「最后更新 3 秒前」。
        with db.connect() as conn:
            _set_run(conn, run_id, done_items=done, failed_items=failed,
                     heartbeat_at=now_str(),
                     note=_progress_note(done, failed, len(card_ids),
                                         error_samples))

    # ---- 收尾：算最终状态 ----
    with db.connect() as conn:
        if cancelled:
            status, final_note = RUN_CANCELLED, "任务被取消（已处理的部分保留）"
        elif failed and done:
            status, final_note = RUN_PARTIAL, ""
        elif failed and not done:
            status, final_note = RUN_FAILED, ""
        else:
            status, final_note = RUN_COMPLETED, ""
        old = conn.execute("SELECT note FROM classification_runs WHERE id=?",
                           (run_id,)).fetchone()
        note = old["note"] if old and old["note"] else final_note
        # 跑的时候每批往 note 里写一行进度，那是临时的 ——
        # 留着的话，任务跑完了界面上还挂着一句"已判 300/630"，
        # 看着像没跑完。所以收尾时把它换掉。
        if note.startswith(PROGRESS_NOTE_MARK):
            note = final_note
        if unknown_cats:
            # 明说：模型说了几个体系里没有的类名。
            # 不说的话，她只会看到一堆「分类失败」而不知道为什么 ——
            # 那些卡片的理由是「类名不认」，但任务级的汇总更一目了然。
            tip = ("模型说出了 %d 个不在分类体系里的类名：%s"
                   "（这些卡片已按「判不出」落库，卡片理由里标了「类名不认」）"
                   % (len(unknown_cats), "、".join(sorted(unknown_cats))))
            note = (note + "；" + tip) if note else tip
        # 提示词文件写坏了 → 必须说出来。
        # 这种情况会静默换用通用模板，她不看任务备注就永远不知道
        # 自己辛苦调的那版根本没生效。
        if ctx.get("prompt_warn"):
            note = ((note + "；") if note else "") + \
                "提示词文件有问题，这次用的是通用模板：" + ctx["prompt_warn"]
        _set_run(conn, run_id, status=status, done_items=done,
                 failed_items=failed, finished_at=now_str(),
                 heartbeat_at=now_str(),
                 error=("；".join(error_samples))[:1000],
                 note=note)
        # 失败原因也要落到任务级 error 上（只有真出错才算），
        # 这样"部分完成"的任务在界面上能直接说出原因，不用翻明细。


# 跑的过程中写进 note 的那一行，收尾时会被替换掉。
PROGRESS_NOTE_MARK = "【进度】"


def _progress_note(done, failed, total, error_samples):
    """跑的过程中那一行进度说明。

    【为什么连错误也写在这里】
    原来运行中的任务在界面上**完全不显示失败**：某批报错只是把卡片标成
    分类失败，任务状态还是 running。如果每一批都报错（比如 Key 中途失效），
    她会看到一个 0% 的进度条一直转到跑完，然后才知道全失败了 ——
    中间那几分钟完全不知道该不该等。所以第一条错误要立刻露出来。
    """
    parts = ["已判 %d/%d 张" % (done, total)]
    if failed:
        parts.append("失败 %d 张" % failed)
    if error_samples:
        parts.append("首个错误：%s" % (error_samples[0] or "")[:150])
    return PROGRESS_NOTE_MARK + "；".join(parts)


def _write_suggestions(run_id, owner, material_id, sugs, cat_id_by_name,
                       tag_id_by_name, model_version, ctx):
    """把校验过的建议写进卡片，同时留一条 AI 判断记录。

    写成什么样是这一步最要紧的事：

      · 主类   → cards.primary_category_id（判不出来就不写）
      · 副标签 → card_tags（只写 AI 给的、未确认的那些，不动她手工挂的）
      · 理由   → cards.ai_reason，置信度 → cards.ai_confidence
      · 来源   → cards.source = 'ai'      ← 界面靠它显示「AI 建议」
      · 状态   → 判出来了 =「待确认」；判不出来 =「分类失败」
                 **绝不写「已确认」** —— 那是人的动作
      · 建议排除 → 只写进理由，不动状态（排除是人的动作，AI 不许自己排）

    【为什么"判不出来"要单独一个状态】
    「待确认」是"AI 给了建议，等人看一眼"，
    「分类失败」是"AI 没辙了，得人自己来"。
    两种混在一起，她筛"需要我处理的"就会把完全不同的东西堆成一堆。

    注意**「建议排除」算"给了建议"，不算"没辙"** —— 所以它进「待确认」。
    早先这里只看"有没有主类"，于是"建议排除"的行（没有主类）被标成
    「分类失败」，她会以为 AI 报错了然后去点重试 —— 重试一百次结果还是一样，
    因为那张卡本来就有明确结论。
    """
    ts = now_str()
    with db.connect() as conn:
        for s in sugs:
            cid = s["card_id"]
            cat_id = cat_id_by_name.get(s["primary_category"]) \
                if s["primary_category"] else None
            reason = s["reason"]
            if s["suggest_exclude"]:
                reason = ("【建议排除】" + reason) if reason else "【建议排除】"

            if cat_id or s["suggest_exclude"]:
                # 判出了主类，或者明确说了"这不是素材" —— 都是给了结论，等她看
                new_status = sg.STATUS_PENDING
            else:
                # 真没辙了（含"类名不认识"那种，理由里已经写了为什么）
                new_status = sg.STATUS_FAILED

            # 只动"还是待确认/分类失败、且不是人工成果"的卡片 ——
            # 后台跑的这段时间里，她可能正在界面上改这张卡。
            # 不加这一层，后台会把她刚改完的东西覆盖掉。
            cur = conn.execute(
                """UPDATE cards
                   SET primary_category_id = CASE WHEN ? IS NULL
                                                  THEN primary_category_id
                                                  ELSE ? END,
                       source=?, ai_reason=?, ai_confidence=?,
                       status=?, updated_at=?
                   WHERE id=? AND owner_id=? AND status<>? AND source<>?""",
                (cat_id, cat_id, sg.SOURCE_AI, reason, s["confidence"],
                 new_status, ts, cid, owner, sg.STATUS_CONFIRMED,
                 sg.SOURCE_HUMAN))

            # 卡片在这期间被人动过（改好了/确认了）→ 标签也别往它身上挂。
            # 主类和标签要么一起是 AI 的，要么一起不是 —— 混着最容易看错。
            if cur.rowcount:
                # 先清掉这张卡上「AI 给的、还没被确认」的标签，再写新的。
                # 为什么只清这一部分：她手工挂的标签、以及她确认过的标签，
                # 不能被下一次自动分类抹掉 —— 那是她的劳动。
                conn.execute(
                    "DELETE FROM card_tags WHERE card_id=? AND source=?"
                    " AND confirmed=0", (cid, sg.SOURCE_AI))
                for tname in s["tags"]:
                    tid = tag_id_by_name.get(tname)
                    if not tid:
                        # 标签清单里没有 → 不认。
                        # 这里**不报错**而是跳过：标签是次要信息，
                        # 为了一个不认识的标签把整条主类建议丢掉不划算。
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO card_tags"
                        " (card_id, sub_tag_id, source, confirmed)"
                        " VALUES (?,?,?,0)", (cid, tid, sg.SOURCE_AI))

            conn.execute(
                """INSERT INTO ai_judgements
                   (card_id, owner_id, classification_run_id, model_version,
                    prompt_version, category_set_version, raw_response,
                    suggested_category, suggested_tags, reason, confidence,
                    human_category, human_tags, was_modified, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, owner, run_id, model_version, PROMPT_VERSION,
                 cls.CATEGORY_SET_VERSION,
                 json.dumps(s, ensure_ascii=False),
                 s["primary_category"] or "", json.dumps(s["tags"],
                                                         ensure_ascii=False),
                 reason, s["confidence"], "", "", 0, ts))


# ----------------------------------------------------------------------
# 九、取消与重试
# ----------------------------------------------------------------------

def cancel_run(run_id, owner):
    """取消。

    不"强行杀线程"—— 只是挂个牌子，执行器每个批次开头看一眼牌子。
    为什么这样更好：强行中断一个写了一半的批次，会留下"部分卡片改了、
    明细表没跟上"的半截状态。让它自己走到批次边界再停，数据永远是自洽的。
    已经处理完的卡片保留建议（那是有效结果），任务状态记为「已取消」。
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM classification_runs WHERE id=? AND owner_id=?",
            (run_id, owner)).fetchone()
        if not row:
            return {"ok": False, "message": "没有这个任务，或者它不属于你"}
        if row["status"] not in RUN_ACTIVE:
            return {"ok": False, "reason": "not_active",
                    "message": "这个任务已经结束了（%s），不用取消。"
                               % RUN_STATUS_TEXT.get(row["status"], row["status"])}
        if row["status"] == RUN_QUEUED:
            # 还没开跑，直接定案
            _set_run(conn, run_id, status=RUN_CANCELLED, cancel_requested=1,
                     finished_at=now_str(), note="任务在开始前被取消")
            return {"ok": True, "status": RUN_CANCELLED,
                    "message": "任务已取消（还没开始跑）"}
        _set_run(conn, run_id, cancel_requested=1)
        return {"ok": True, "status": RUN_RUNNING,
                "message": "已请求取消，正在跑的这一批结束就停。"}


def retry_run(material_id, owner, classifier_name=None, model_key=None):
    """重试：只重新处理上一次失败的那些卡片；没有失败的就整份重来。

    为什么不干脆整个重来：
        失败往往是"某一批的返回值格式不对"。整份重来会把已经判好的
        几百条重复调用一遍 —— 接上真模型之后那就是白花钱。
    """
    last = latest_run(owner, material_id)
    if not last:
        return {"ok": False, "reason": "no_run",
                "message": "这个文件还没有分类过，直接点「自动分类」就行。"}

    only = None
    if last["status"] in (RUN_FAILED, RUN_PARTIAL):
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT card_id FROM classification_items WHERE run_id=?"
                " AND status=?", (last["id"], ITEM_FAILED)).fetchall()
            only = [r["card_id"] for r in rows]
            if only:
                # 只重试失败的卡片：把候选集限定成这些 id
                alive = _target_card_ids(conn, owner, material_id,
                                         only_card_ids=only)
                only = alive
        if not only:
            only = None       # 失败的卡片都不能再处理了（比如被改成了已确认）→ 整份重来

    with _create_lock:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id FROM classification_runs WHERE owner_id=? AND "
                "material_id=? AND status IN (%s) LIMIT 1"
                % ",".join("?" * len(RUN_ACTIVE)),
                [owner, material_id] + list(RUN_ACTIVE)).fetchone()
            if row:
                return {"ok": False, "reason": "already_running",
                        "message": "还有任务在跑（#%d），先等它结束。" % row["id"]}
            card_ids = only or _target_card_ids(conn, owner, material_id)
            if not card_ids:
                return {"ok": False, "reason": "nothing_to_do",
                        "message": "没有需要重试的卡片。"}

            # ---- 沿用上次用的方法和模型，除非这次明确指定了别的 ----
            #
            # 【为什么默认沿用，而不是"顺便换个更准的"】
            # 换了的话，重试的结果就跟第一次不接续了 —— 同一份文件里
            # 一半的卡片是 A 模型判的、一半是 B 模型判的，而她不会知道。
            # 以后她真想换，界面上明确选一个，那样才是有意识地在对比。
            # （model_key 传 None 表示"沿用上次"，传空串表示"我要默认那个"。）
            clf, prompt_version, model_version, mk, prompt_src = _resolve_engine(
                classifier_name or last.get("classifier") or None,
                model_key if model_key is not None
                else (last.get("model_key") or ""))

            # 补充提示词也沿用上一次那份**快照**，而不是她现在存着的。
            # 理由和沿用模型一样：中途换掉的话，同一份素材里一半是按
            # 上一次的要求判的、一半按新的判的，她看不出这个区别。
            up = last.get("user_prompt") or ""
            # 库里那条的引用也一起沿用（0/空 = 上次没用库里的）。
            # 内容已经在 up 里了，所以这里只是为了让历史记录认得出名字。
            last_ref = last.get("prompt_ref_id") or 0
            last_ref_name = last.get("prompt_name") or ""
            last_ref_owner = last.get("prompt_owner") or ""

            now = now_str()
            cur = conn.execute(
                """INSERT INTO classification_runs
                   (owner_id, material_id, retry_of_run_id, status,
                    category_set_version, prompt_version, model_version,
                    classifier, model_key, prompt_source, user_prompt,
                    prompt_ref_id, prompt_name, prompt_owner,
                    total_items, created_at, heartbeat_at, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (owner, material_id, last["id"], RUN_QUEUED,
                 cls.CATEGORY_SET_VERSION, prompt_version, model_version,
                 clf.name, mk, prompt_src, up,
                 last_ref, last_ref_name, last_ref_owner,
                 len(card_ids), now, now,
                 "重试任务 #%d%s" % (last["id"],
                                     "（只重跑上次失败的部分）" if only else "")))
            run_id = cur.lastrowid

    th = threading.Thread(target=_run_worker,
                          args=(run_id, owner, classifier_name, card_ids),
                          daemon=True)
    th.start()
    return {"ok": True, "run_id": run_id, "total_items": len(card_ids),
            "retried_only_failed": bool(only),
            "classifier": clf.name, "classifier_label": clf.label,
            "model_key": mk, "model_version": model_version,
            "message": "已创建重试任务（%d 张卡片），后台正在跑。"
                       % len(card_ids)}


# ----------------------------------------------------------------------
# 十、总素材库要的状态：这个文件现在是什么情况
# ----------------------------------------------------------------------

def material_state(owner, material_id):
    """给总素材库的一张文档卡片用。

    state 是给界面判断按钮该显示什么的：
        never    从没分类过      → 按钮「自动分类」
        running  正在跑          → 按钮「分类中」+ 进度，禁用
        done     跑完了          → 按钮「重新分类」（点了要二次确认）
        partial  部分完成        → 按钮「重试分类」
        failed   失败            → 按钮「重试分类」+ 失败原因
        cancelled 被取消         → 按钮「继续分类」
    """
    with db.connect() as conn:
        m = conn.execute(
            "SELECT id, title, chars, ext, source_collection FROM materials"
            " WHERE id=? AND owner_id=?", (material_id, owner)).fetchone()
        if not m:
            return None

        def one(sql, *p):
            return conn.execute(sql, p).fetchone()[0]

        run = conn.execute(
            "SELECT * FROM classification_runs WHERE owner_id=? AND material_id=?"
            " ORDER BY id DESC LIMIT 1", (owner, material_id)).fetchone()

        n_cards = one("SELECT COUNT(*) FROM cards WHERE owner_id=? AND material_id=?",
                      owner, material_id)
        n_pending = one("SELECT COUNT(*) FROM cards WHERE owner_id=? AND material_id=?"
                        " AND status=?", owner, material_id, sg.STATUS_PENDING)
        n_confirmed = one("SELECT COUNT(*) FROM cards WHERE owner_id=? AND material_id=?"
                          " AND status=?", owner, material_id, sg.STATUS_CONFIRMED)
        n_failed = one("SELECT COUNT(*) FROM cards WHERE owner_id=? AND material_id=?"
                       " AND status=?", owner, material_id, sg.STATUS_FAILED)
        n_excluded = one("SELECT COUNT(*) FROM cards WHERE owner_id=? AND material_id=?"
                         " AND status=?", owner, material_id, sg.STATUS_EXCLUDED)
        # AI 建议里置信度低、或者干脆没给主类的 → 界面优先让人看这些
        n_low = one("SELECT COUNT(*) FROM cards WHERE owner_id=? AND material_id=?"
                    " AND source=? AND (ai_confidence IS NULL OR ai_confidence<?)",
                    owner, material_id, sg.SOURCE_AI, LOW_CONFIDENCE)
        n_ai = one("SELECT COUNT(*) FROM cards WHERE owner_id=? AND material_id=?"
                   " AND source=?", owner, material_id, sg.SOURCE_AI)
        will = len(_target_card_ids(conn, owner, material_id))

    out = {"material_id": material_id, "title": m["title"], "chars": m["chars"],
           "ext": m["ext"], "source_collection": m["source_collection"] or m["title"],
           "cards": n_cards, "pending": n_pending, "confirmed": n_confirmed,
           "failed_cards": n_failed, "excluded": n_excluded,
           "ai_suggested": n_ai, "low_confidence": n_low,
           "will_process": will, "state": "never", "run": None,
           "progress": 0.0, "status_text": "还没分类过",
           # 按钮该不该能点。
           # 分三种情况，必须分开：
           #   有卡片要处理          → 能点
           #   一张卡都没有（没切过）→ 也能点，任务会先替她切一次
           #   有卡片但全都有主类了   → 不能点，点了也是白点（界面上要说明原因）
           "can_start": bool(will) or n_cards == 0,
           "needs_split": n_cards == 0}

    if run:
        d = _run_dict(run)
        # classifier / model_key 要带上 —— 界面上要如实显示
        # "这次到底是本地规则判的，还是哪个模型判的"。
        # 她要比"哪个模型分得准"，靠的就是把这两项并排看。
        out["run"] = {k: d[k] for k in
                      ("id", "status", "status_text", "progress", "total_items",
                       "done_items", "failed_items", "model_version",
                       "created_at", "finished_at", "error", "note",
                       "retry_of_run_id", "classifier", "model_key",
                       "heartbeat_at", "stale_seconds", "prompt_version",
                       "prompt_source", "prompt_ref_id", "prompt_name")}
        out["progress"] = d["progress"]
        out["status_text"] = d["status_text"]
        mapping = {RUN_QUEUED: "running", RUN_RUNNING: "running",
                   RUN_COMPLETED: "done", RUN_PARTIAL: "partial",
                   RUN_FAILED: "failed", RUN_CANCELLED: "cancelled"}
        out["state"] = mapping.get(run["status"], "never")
        out["last_run_at"] = run["finished_at"] or run["started_at"] or run["created_at"]
    return out


def material_states(owner, material_ids=None):
    """一次取多个文件的状态（总素材库一页几十份素材，不能一份份查库）。"""
    out = {}
    for mid in (material_ids or []):
        s = material_state(owner, mid)
        if s:
            out[mid] = s
    return out


def usage_summary(owner):
    """AI 学习面板要的总数（第一版只统计，不做学习样本）。"""
    with db.connect() as conn:
        def one(sql, *p):
            return conn.execute(sql, p).fetchone()[0]
        n_runs = one("SELECT COUNT(*) FROM classification_runs WHERE owner_id=?",
                     owner)
        n_items = one("SELECT COUNT(*) FROM ai_judgements WHERE owner_id=?", owner)
        n_ai_cards = one("SELECT COUNT(*) FROM cards WHERE owner_id=? AND source=?",
                         owner, sg.SOURCE_AI)
        n_low = one("SELECT COUNT(*) FROM cards WHERE owner_id=? AND source=?"
                    " AND (ai_confidence IS NULL OR ai_confidence<?)",
                    owner, sg.SOURCE_AI, LOW_CONFIDENCE)
        n_feedback = one("SELECT COUNT(*) FROM learning_feedback WHERE owner_id=?",
                         owner)
        n_enabled = one("SELECT COUNT(*) FROM learning_feedback WHERE owner_id=?"
                        " AND enabled=1", owner)
    return {"runs": n_runs, "ai_judgements": n_items, "ai_cards": n_ai_cards,
            "low_confidence": n_low, "feedback": n_feedback,
            "feedback_enabled": n_enabled, "feedback_disabled": n_feedback - n_enabled,
            "model_version": MODEL_VERSION_PLACEHOLDER,
            "prompt_version": PROMPT_VERSION,
            "classifier": PlaceholderClassifier.name,
            "classifier_label": PlaceholderClassifier.label,
            "is_placeholder": True}


# ----------------------------------------------------------------------
# 十一、命令行自测：python backend/classification.py
# ----------------------------------------------------------------------
#
# 这里**只测纯函数**（规则判断 + 返回格式校验），故意不碰数据库。
#
# 为什么不在这个文件里做建库自测：
#     db.DATA_DIR 是在 import 那一刻从环境变量读的。
#     等跑到这个 __main__ 块里再改 os.environ["MOGE_DATA_DIR"]，已经晚了 ——
#     那正是 safe-test-isolation 里记的那个坑（"改环境变量对已经建立的东西无效"）。
#     要自测建库就得另开一个子进程、在启动前把变量设好，
#     那等于把测试脚本塞进生产文件里，不值当。
#     涉及数据库的部分全部在 tests/test_classification_api.py 里，
#     那个文件跑之前会先把库指到临时目录，并且会核对指对了没有。

if __name__ == "__main__":                                # pragma: no cover
    SAMPLES = [
        ("他捏碎了手里的茶杯", "看得见的身体动作 → 神态"),
        ("他生得一双极冷的眼睛，平日很少笑", "静态特征 → 外貌（不能被「笑」抢走）"),
        ("他把那半块玉佩塞回袖子里，转身走了", "有「袖」但是动作 → 不该判成外貌"),
        ("「你回来了。」他侧过身让开", "成对引号 → 对白"),
        ("「你来了。」他把杯子搁在桌上", "短对白 + 长动作 → 仍然算对白"),
        ("他嘟囔了一句「嗯」，又低下头", "只是夹了个词 → 不该算对白"),
        ("「　」", "引号里没内容 → 不该判成对白"),
        ("工作表：Sheet1", "表格残留 → 建议排除"),
        ("12,31,45", "纯数字行 → 建议排除"),
        ("第二章 夜行", "章节标题 → 建议排除"),
        ("他没敢再想下去，怕自己后悔", "内省 → 心理"),
        ("他拔刀冲了出去，血溅在窗纸上", "事件推进 → 情节"),
        # 下面这两条是**故意判不出来**的。
        # 它们属于「梗 / 暧昧拉扯 / 搞笑情节」那一侧，要读懂语义才判得出，
        # 规则不硬猜 —— 这正是要交给大模型的部分（见函数 docstring）。
        ("这规矩是祖师爷定下的，谁也不能破", "设定说明 → 八类里没落点，该判不出来"),
        ("风雨声从巷子那头钻进来，灯影摇了一下", "环境感官 → 八类里没落点，该判不出来"),
        ("仿佛一只被按住的手似的，怎么也挣不开", "比喻结构 → 不能只看「似的」，该判不出来"),
        ("先有这碟醋才包的这盘饺子", "梗 → 要读懂了才算得出，该判不出来"),
        ("那年冬天特别长，长到谁都以为不会再有春天", "没有明显线索 → 应该承认判不出来"),
        ("嗯", "太短 → 应该承认判不出来"),
    ]
    print("=" * 60)
    print("占位规则分类器自测（纯函数，不碰数据库）")
    print("=" * 60)
    miss = 0
    for line, why in SAMPLES:
        r = placeholder_rule_judge(line, 0)
        cat = r["primary_category"] or ("建议排除" if r.get("suggest_exclude")
                                        else "（拿不准）")
        print("  %-30s → %-8s conf=%-5s  %s"
              % (line, cat, r["confidence"], why))

    print("-" * 60)
    print("返回格式校验自测（任务书第七节的每一条）")
    print("-" * 60)
    names = [c[0] for c in cls.CATEGORIES_SEED]

    def check(desc, raw, ids, should_pass):
        global miss
        try:
            _validate_suggestions(raw, ids, names)
            got = "通过"
        except SuggestionError as e:
            got = "拒收：%s" % e
        ok = (got == "通过") == should_pass
        if not ok:
            miss += 1
        print("  [%s] %-26s %s" % ("OK" if ok else "!!", desc, got))

    check("正常一条", [{"card_id": 1, "primary_category": "对话台词",
                        "tags": [], "reason": "x", "confidence": 0.8}],
          {1}, True)
    # 「主类名不认识」原来也走整批拒收。2026-09-24 改成只把这一条降级 ——
    # 因为它在真实数据上炸过：占位规则还在输出旧九类的类名，
    # 而体系已经换成八类，于是整份素材 0/15 全失败，看着像功能坏了。
    check("主类名不认识 → 不拒收（只降级这一条）",
          [{"card_id": 1, "primary_category": "瞎编的类",
            "confidence": 0.8}], {1}, True)
    check("card_id 不在本批", [{"card_id": 99, "primary_category": "对话台词"}],
          {1}, False)
    check("confidence 超出 0~1",
          [{"card_id": 1, "primary_category": "对话台词", "confidence": 1.7}],
          {1}, False)
    check("带了正文（必须整批拒收）",
          [{"card_id": 1, "primary_category": "对话台词", "text": "原文粘回来"}],
          {1}, False)
    check("同一 card_id 重复", [{"card_id": 1}, {"card_id": 1}], {1}, False)
    check("一条都没返回", [], {1}, False)

    print("-" * 60)
    print("「类名不认识」的降级行为（坏的一条不能拖累好的那条）")
    print("-" * 60)
    ok_list, unknown = _validate_suggestions(
        [{"card_id": 1, "primary_category": "外貌", "confidence": 0.9},
         {"card_id": 2, "primary_category": "人物描写", "confidence": 0.9},
         {"card_id": 3, "primary_category": "外貌描写", "confidence": 0.9}],
        {1, 2, 3}, names)
    for desc, cond in [
        ("三条都还在（没被整条丢掉）", len(ok_list) == 3),
        ("认得出的那条主类照写", ok_list[0]["primary_category"] == "外貌"),
        ("好的一条置信度没被牵连", ok_list[0]["confidence"] == 0.9),
        ("不认识的那条不给主类", ok_list[1]["primary_category"] is None),
        ("不认识的那条置信度清空", ok_list[1]["confidence"] is None),
        ("卡片理由里写了「类名不认」", "类名不认" in ok_list[1]["reason"]),
        ("两个不认识的类名都收集起来了",
         unknown == {"人物描写", "外貌描写"}),
    ]:
        if not cond:
            miss += 1
        print("  [%s] %s" % ("OK" if cond else "!!", desc))

    print("-" * 60)
    print("提示词模板：读文件 / 退回通用版 / 拼补充提示词")
    print("-" * 60)

    def chk(desc, cond, extra=""):
        global miss
        if not cond:
            miss += 1
        print("  [%s] %s%s" % ("OK" if cond else "!!", desc,
                               ("　← " + str(extra)) if extra else ""))

    tpl, src, warn = prompt_template()
    chk("能拿到一份模板", bool(tpl.strip()), "来源=%s" % src)
    chk("来源只有 file / builtin 两种", src in ("file", "builtin"), src)
    chk("模板里有 {categories} 和 {sub_tags}",
        all(s in tpl for s in REQUIRED_SLOTS))
    chk("读文件成功时不该报警告", (src != "file") or not warn, warn or "无")

    # 用假类目拼一次，看占位符有没有真的被换掉
    demo_cats = [{"name": "外貌", "description": "看长相和气质的",
                  "suggested_tags": ["直接描写"]},
                 {"name": "梗", "description": "能单独拎出来玩的点子",
                  "suggested_tags": []}]
    msgs, _s, _w = build_messages(demo_cats, ["直接描写", "吃醋"], "测试素材",
                                  [{"card_id": 7, "seq": 1, "text": "他抬起头"}],
                                  user_prompt="带引号的短句优先算对话台词")
    chk("拼出来是两条消息（system + user）", len(msgs) == 2, len(msgs))
    sys_msg, usr_msg = msgs[0]["content"], msgs[1]["content"]
    chk("system 里的 {categories} 被换成了真类目",
        "外貌" in sys_msg and "看长相和气质的" in sys_msg)
    chk("system 里没有漏换的占位符",
        not any(s in sys_msg for s in
                ("{categories}", "{sub_tags}", "{user_prompt}")))
    chk("副标签换成真清单了", "直接描写" in sys_msg and "吃醋" in sys_msg)
    chk("补充提示词进 system 了", "带引号的短句优先算对话台词" in sys_msg)
    chk("补充提示词被标注成「作者本人的补充要求」",
        "作者本人的补充要求" in sys_msg)
    chk("补充提示词里说明了不能推翻硬规则", "不能推翻" in sys_msg)
    chk("正文和编号在 user 消息里", "[7]" in usr_msg and "他抬起头" in usr_msg)
    chk("正文没被塞进 system", "他抬起头" not in sys_msg)

    # 没写补充提示词时，不该留一块空框
    msgs2, _s2, _w2 = build_messages(demo_cats, [], "", 
                                     [{"card_id": 1, "text": "x"}])
    chk("没写补充提示词时不出现那个空标题",
        "作者本人的补充要求" not in msgs2[0]["content"])

    # 模板少了占位符 → 必须退回通用版并给出警告（这是静默丢内容的坑）
    _save = PROMPT_FILE
    _bad = os.path.join(ROOT_DIR, "prompts", "_selftest_bad.txt")
    try:
        with open(_bad, "w", encoding="utf-8") as f:
            f.write("这个模板少了类目占位符，模型会收不到类目清单")
        globals()["PROMPT_FILE"] = "_selftest_bad.txt"
        _t2, _s2b, _w2b = prompt_template()
        chk("模板缺占位符 → 退回通用版", _s2b == "builtin", _s2b)
        chk("退回时给出警告（不能静默）", "占位符" in (_w2b or ""), _w2b)
        _t3, _s3, _w3 = prompt_template()
        chk("缺占位符的模板绝不会被当成 file 用", _s3 != "file", _s3)
    finally:
        globals()["PROMPT_FILE"] = _save
        try:
            os.remove(_bad)
        except OSError:
            pass

    print("-" * 60)
    print("补充提示词：长度上限")
    print("-" * 60)
    chk("上限是 5000 字", USER_PROMPT_MAX == 5000, USER_PROMPT_MAX)
    chk("刚好 5000 字能过", len("字" * USER_PROMPT_MAX) <= USER_PROMPT_MAX)

    print("-" * 60)
    if miss:
        print("自测结束：有 %d 处不符合预期。" % miss)
    else:
        print("自测结束：全部符合预期。")
    print("（数据库部分见 tests/test_classification_api.py）")
