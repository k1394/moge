# -*- coding: utf-8 -*-
"""
墨阁 · 素材分类库（数据层）
========================================================
这个文件管的是"素材分类库"这一整块的数据库部分：
建表、迁移、以及卡片/切分的增删改查。

它和 db.py 的分工：
    db.py           账号、素材文件（materials）、标签 —— 已经存在的东西
    classify_db.py  切分、卡片、分类、变更记录 —— 本次新增的东西

为什么不直接塞进 db.py：
    db.py 已经 670 行，再塞 600 行会变成 1300 行的大文件，
    以后找一个函数要在里面翻半天。分开之后，"分类库"这套东西
    要整体删掉或重做，只动这一个文件就行。

========================================================
四张"必须有"的设计底线（都是为了一件事：以后改规则不返工）
========================================================

一、切分任务单独有张表（segment_runs）
    同一个文件可以被切很多次（先按行切，看了觉得太碎，改成按空行切）。
    如果重新切分时把旧段删掉，那些已经人工改过的卡片就会指向
    一条不存在的段 —— 数据就烂了。
    所以：每次切分生成一个新的 run，旧段全部保留。

二、卡片的位置锚点是"偏移"，不是 segment_id
    卡片必须能回答"我在原文的第几个字到第几个字"。
    segment_id 只回答"我当初是从哪一段来的"，字段可以为空，
    重新切分后失效也无所谓 —— 它坏了不影响卡片。
    一句话：段落可以抛弃，偏移不可以抛弃。

三、原文一律现算，卡片不存正文
    卡片正文 = materials.content[start:end]。
    世上只有一个真相（materials.content），
    所以永远不会出现"卡片正文和原文对不上"这种问题 ——
    因为它俩本来就是同一份东西。

四、任何批量操作都要留下"改之前长什么样"
    card_changes 里存 before_snapshot / after_snapshot。
    撤销时只恢复"这次操作改过、且之后没人再动过"的字段，
    不会把她后来的修改一起冲掉。
"""

import json
import os
import shutil
import sqlite3
import secrets
import time
from datetime import datetime

try:
    from backend import db, segmentation as sg
except ImportError:                                   # pragma: no cover
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from backend import db, segmentation as sg


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_group_id():
    """一次操作的分组号。拆分/合并产生的多张卡用同一个号，界面上能显示"同批"。"""
    return secrets.token_hex(6)


# ----------------------------------------------------------------------
# 十一个正式主类（v2）
#
# 【为什么有 v1 / v2 两版】
#   v1 是「按文本类型切」的一套学术分类（人物描写 / 场景氛围 / 语言表达…），
#   那套是错的 —— 它不是她的分类，是别人替她发明的分类。
#   v2 是她自己定的，来源是她手上那批素材文件的名字
#   （外貌 / 神态 / 梗 / 好磕 / 车 / 难过动心 / 搞笑…）。
#   v1 不删，只是标成停用：万一想退回去，定义还在。
#
# 【2026-09-24 晚：她看过分类结果，说准确率不错，加了 3 类】
#   原来 8 类 → 11 类，新增「动作 / 打斗 / 环境」。
#   ⚠️ set_version 仍然留着 "v2"，**没有**升 v3。原因：
#     迁移是 ON CONFLICT(set_version, name) DO UPDATE，
#     名字没变的那 8 条_id 不变_（她库里 630 张卡指着这些 id），
#     新的 3 条按名字插进去。升版本会让老 8 条被标停用，
#     她那些卡的 category_id 立刻变成"停用类"，白折腾一趟。
#
# 【判据怎么写】
#   主类回答的是「这段素材我以后拿它来干嘛」—— 是取材意图，不是文本类型学。
#   描述（description）不是装饰，是给人和 AI 看的判断依据：
#   两个类打架的时候，就看这段描述站哪边。所以判据要照她的用法写，
#   别自己发明一套。她也随时能改（界面上能改）。
#
# 【加类最难的不是加，是划边界】
#   新增的 3 类天然跟老的 3 类抢地盘，所以这一版**同时改写了 3 条老描述**，
#   在两边都写明分界（不是只在新类里说"我不包括什么"）：
#     神态 ↔ 动作    一瞬 vs 一段过程
#     动作 ↔ 打斗    没有对手 vs 有对手
#     打斗 ↔ 情节    怎么打的 vs 打完之后怎么了
#     环境 ↔ 外貌    写地 vs 写人
#   AI 只看得到描述，边界不写清它就自己编，一类多了另一类就空了。
#
# 【括号里的副标签】
#   只是"这类常用到这些"的建议，用来在界面里排前头，**不是约束**。
#   「神态」和「心理」都建议「心动 / 难过」—— 一个是外面看得见的心动，
#   一个是里面看不见的心动。同一个标签挂在两个主类下，这是故意的。
# ----------------------------------------------------------------------

CATEGORY_SET_VERSION = "v2"

CATEGORIES_SEED = [
    ("外貌",
     "这个人长什么样、什么气质。身形、五官、穿着打扮、气场、给人的观感。"
     "写的是「他这个人一直就是这样」，不是「他此刻在做什么」。"
     "直接描（正面写他生得如何）和侧写（借别人的眼睛或反应写他好看）都算。",
     ["直接描写", "侧面描写", "气质"]),

    ("神态",
     "外面看得见的**一瞬**反应：表情变化、眼神、小动作、身体反应（脸红、手抖、僵住）。"
     "判断依据：这一秒他脸上、身上在发生什么，旁边的人看得见。"
     "和「心理」的分界 —— 神态看得见，心理看不见"
     "（心里怎么想，只能靠叙述交代，外边一点动静都没有）。"
     "和「动作」的分界 —— 神态是**掐住的一秒**（他手抖了一下），"
     "动作是**一段能讲出先后的过程**（他伸手、点火、把烟递过去）。",
     ["心动", "难过"]),

    ("动作",
     "一个人身体在做的一连串事情本身：招式、身法、走位、攀爬、翻墙、"
     "开门、点烟、递东西、穿衣解衣、骑马赶路这类肢体过程。"
     "判断依据：这段的价值在「他是怎么动的」，把动作换成另一套动作，"
     "事情走向不变，变的只是好不好看。"
     "和「神态」的分界 —— 神态是一瞬间看得见的反应，动作是一段有始有终的过程。"
     "和「打斗」的分界 —— **没有对手**、就是自己（或跟物件）在动，是动作；"
     "一旦有对手、有攻防意图，进「打斗」。",
     ["招式", "身法"]),

    ("打斗",
     "双方或多方对抗：交手、对招、厮杀、群架、追杀、械斗、比试。"
     "判断依据：**有对手，有攻防**，这段的价值在「这一架是怎么打的」"
     "（你来我往、招式交接、见血受伤）。"
     "和「动作」的分界 —— 一个人自己动是动作，两个人过招才是打斗。"
     "和「情节」的分界 —— 打斗写的是**怎么打的**；"
     "情节写的是**打这一架导致了什么**（谁死了、谁逃了、关系变了）。"
     "两者都有的时候，看这段的重心压在哪边。",
     ["交手", "群架", "追杀"]),

    ("环境",
     "这是哪儿、这时是什么样：天气、光线、时辰、地形、山水、建筑、"
     "屋内陈设、气味、声音、景物、以及整体氛围。"
     "判断依据：**把人物全拿掉，这段照样成立** —— 它写的是场景本身。"
     "和「外貌」的分界 —— 外貌写人，环境写地。"
     "和「情节」的分界 —— 环境可以一动不动地存在，情节必须有事情发生。",
     ["景物", "氛围"]),

    ("梗",
     "可以单独拎出来玩的梗：网络梗、作品梗、语言梗、设定梗，"
     "以及「为了一碟醋包一盘饺子」那种 —— 先有了这个点子，才围着它写的整段。",
     []),

    ("暧昧拉扯",
     "两个人之间的性张力和推拉。吻、肢体接触、眼神纠缠、"
     "舔狗与痴汉那种单向的输出、吃醋、小三上位那种三角张力。"
     "判断依据：这段的价值在于「他们俩之间那股劲儿」，不在事件本身。",
     ["吻", "好磕", "舔狗痴汉", "小三文学", "吃醋"]),

    ("心理",
     "里面看不见的：他在想什么、心里怎么翻腾、怎么说服自己、怎么崩了又爬起来。"
     "判断依据：把这段拿掉，外面什么都不会变，变的只是读者对他内心的了解。"
     "例：「他本该结束这场荒唐」。",
     ["心动", "难过"]),

    ("搞笑情节",
     "整段的价值就是让人笑出来。好笑的段子、好笑的场面、好笑的对话。"
     "和「情节 / 对话台词」的分界 —— 笑点是这段的主体，就进这里；"
     "只是顺便好笑，就按内容进别的类，再挂副标签「纯搞笑」。",
     ["纯搞笑", "好笑且好磕"]),

    ("对话台词",
     "人物说出来的话本身，可以独立拿走用的对白。"
     "判断依据：有人在说话，有说话人和引号。"
     "它被留下是因为「这句话说得妙」，不是因为「这段发生了什么」。",
     []),

    ("情节",
     "发生了什么事。冲突的起因与结果、反转、和解、牺牲、身份揭露、"
     "误会、抉择、事件推进。"
     "判断依据：这段能独立成立成一个「事件」，不依赖某两个人的关系。"
     "和「打斗」的分界 —— 情节写「打这一架导致了什么」，"
     "打斗写「这一架怎么打的」。"
     "（注意：打斗本身不归这里 —— 只是「打了个架」这件事的结果归这里。）",
     []),
]

# 副标签。
# 副标签回答的是「我以后为什么用它」，主观、自由增删，不承担结构化管理。
# 前 11 个是她原来那批（这一次一个都没删，怕删掉她一直在用的）；
# 后面是照 v2 体系补的。
SUB_TAGS_SEED = [
    # 原来那批
    "好磕", "拉扯", "心动", "难过", "搞笑", "牛逼", "玩梗", "好词好句",
    "适合开场", "适合冲突", "适合暧昧",
    # v2 新补的
    "直接描写", "侧面描写", "气质",
    "吻", "舔狗痴汉", "小三文学", "吃醋",
    "纯搞笑", "好笑且好磕",
    # 2026-09-24 晚补：跟着新增的「动作 / 打斗 / 环境」三类一起加的建议副标签。
    # 加进 SUB_TAGS_SEED 是为了它们能真的被建出来（suggested_tags 只是界面提示，
    # 不在这个清单里的话，界面会显示一个点不出来的标签名）。
    "招式", "身法", "交手", "群架", "追杀", "景物", "氛围",
]

# ----------------------------------------------------------------------
# 来源映射的初始建议：**不进这个文件**
#
# 为什么单独拎出来：
#   这份建议是"针对某个人手上那批文件"给的一次性预填
#   （文件名 → 建议主类 → 为什么这么建议）。
#   它属于个人内容，不属于框架 —— 公开仓库里不该出现别人的素材名。
#   而且对别人也没用：换一个人，他的文件名叫什么，只有他自己知道。
#
# 放在哪：
#   data/seed_source_map.json（data/ 整块在 .gitignore 里，永远同步不出去）
#
# 没有这个文件会怎样：
#   建议为空 —— 界面上来源那一列显示"系统未给建议"，她自己逐条填。
#   这是**正确**的降级行为，不是故障。
# ----------------------------------------------------------------------

SEED_SOURCE_MAP_FILE = "seed_source_map.json"


def _seed_source_map():
    """读 data/seed_source_map.json。文件不存在 / 坏了 → 返回空字典。

    格式（每一项）：
        {"source": "文件名", "category": "主类名或 null", "note": "为什么"}
    """
    path = os.path.join(db.DATA_DIR, SEED_SOURCE_MAP_FILE)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:                                   # pragma: no cover
        print("[来源映射] %s 读不了（%s），本次不预填建议。" % (path, e))
        return {}
    out = {}
    for item in (raw.get("items") if isinstance(raw, dict) else raw) or []:
        name = (item.get("source") or "").strip()
        if not name:
            continue
        out[name] = (item.get("category") or None, item.get("note") or "")
    return out


# ----------------------------------------------------------------------
# 建表
# ----------------------------------------------------------------------

SCHEMA = """
-- 切分任务：某个文件"何时、按什么规则"切过一次
CREATE TABLE IF NOT EXISTS segment_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id  INTEGER NOT NULL,
    rule         TEXT    NOT NULL DEFAULT 'line',
    rule_version TEXT    NOT NULL DEFAULT 'v1',
    norm_version TEXT    NOT NULL DEFAULT 'v1',
    segment_count INTEGER NOT NULL DEFAULT 0,
    is_current   INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT    NOT NULL
);

-- 机械切分结果。不承载分类，只回答"原文的哪一段到哪一段"
CREATE TABLE IF NOT EXISTS segments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL,
    material_id      INTEGER NOT NULL,
    seq              INTEGER NOT NULL,
    start_offset     INTEGER NOT NULL,
    end_offset       INTEGER NOT NULL,
    source_text_hash TEXT    NOT NULL DEFAULT '',
    head_check       TEXT    NOT NULL DEFAULT '',
    tail_check       TEXT    NOT NULL DEFAULT '',
    excluded         INTEGER NOT NULL DEFAULT 0,
    noise_reason     TEXT    NOT NULL DEFAULT '',
    line_from        INTEGER NOT NULL DEFAULT 0,
    line_to          INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL
);

-- 卡片：界面上真正一条条看的"素材条"
-- 正文不存在这里 —— 现算 materials.content[start_offset:end_offset]
CREATE TABLE IF NOT EXISTS cards (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id            TEXT    NOT NULL,
    material_id         INTEGER NOT NULL,
    start_offset        INTEGER NOT NULL,
    end_offset          INTEGER NOT NULL,
    source_text_hash    TEXT    NOT NULL DEFAULT '',
    segment_id          INTEGER DEFAULT NULL,
    primary_category_id INTEGER DEFAULT NULL,
    source              TEXT    NOT NULL DEFAULT 'human',
    ai_reason           TEXT    NOT NULL DEFAULT '',
    ai_confidence       REAL    DEFAULT NULL,
    status              TEXT    NOT NULL DEFAULT '待确认',
    note                TEXT    NOT NULL DEFAULT '',
    parent_card_id      INTEGER DEFAULT NULL,
    operation_group_id  TEXT    NOT NULL DEFAULT '',
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);

-- 副标签字典。active=0 表示"停用"，停用不删，历史记录还认得出它
CREATE TABLE IF NOT EXISTS sub_tags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   TEXT    NOT NULL,
    name       TEXT    NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL,
    UNIQUE (owner_id, name)
);

CREATE TABLE IF NOT EXISTS card_tags (
    card_id    INTEGER NOT NULL REFERENCES cards(id)    ON DELETE CASCADE,
    sub_tag_id INTEGER NOT NULL REFERENCES sub_tags(id) ON DELETE CASCADE,
    source     TEXT    NOT NULL DEFAULT 'human',
    confirmed  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (card_id, sub_tag_id)
);

-- 正式主类。parent_id 先留着但第一版不用（不建多级分类界面）
--
-- suggested_tags：这一类"常用到哪些副标签"，存 JSON 数组。
--   只是给界面排序用的建议（选了「外貌」就先显示直接描写/侧面描写/气质），
--   **不是约束** —— 副标签本身是全局共享的，同一个标签能被多个主类用
--   （「心动」在「神态」和「心理」下面都出现，这是故意的）。
-- 主类（分类体系）。
--
-- owner_id = ''  ←→ 系统自带的，所有人共用（那 11 个）
-- owner_id = 别的 ←→ 某个账号自己建的主类，只有他自己看得见、用得上
--
-- 【UNIQUE 为什么带 owner_id】
--   老结构是 UNIQUE(set_version, name)，也就是全站只许有一个「情感」。
--   她要求"用户能自己建主类"之后，这条约束就成了：别人建过的名字我建不了，
--   而且我还看不见那一条 —— 报错会说"已经有一条叫这个名字的了"，我却找不到它。
--   带上 owner_id 之后，每个账号各有一份自己的命名空间，互不打扰。
CREATE TABLE IF NOT EXISTS categories (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id       TEXT    NOT NULL DEFAULT '',
    set_version    TEXT    NOT NULL DEFAULT 'v1',
    name           TEXT    NOT NULL,
    description    TEXT    NOT NULL DEFAULT '',
    aliases        TEXT    NOT NULL DEFAULT '',
    suggested_tags TEXT    NOT NULL DEFAULT '[]',
    parent_id      INTEGER DEFAULT NULL,
    sort           INTEGER NOT NULL DEFAULT 0,
    active         INTEGER NOT NULL DEFAULT 1,
    UNIQUE (owner_id, set_version, name)
);

-- 来源集合 → 建议主类 的对照表（可编辑，但必须人工确认才执行）
CREATE TABLE IF NOT EXISTS source_mappings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id         TEXT    NOT NULL,
    source_collection TEXT   NOT NULL,
    category_id      INTEGER DEFAULT NULL,
    note             TEXT    NOT NULL DEFAULT '',
    confirmed        INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT    NOT NULL,
    UNIQUE (owner_id, source_collection)
);

-- 变更记录。批量操作的生命线：没有它就不能撤销
CREATE TABLE IF NOT EXISTS card_changes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id         TEXT    NOT NULL,
    action_type      TEXT    NOT NULL,
    operator_id      INTEGER DEFAULT NULL,
    material_id      INTEGER DEFAULT NULL,
    affected_count   INTEGER NOT NULL DEFAULT 0,
    affected_card_ids TEXT   NOT NULL DEFAULT '[]',
    before_snapshot  TEXT    NOT NULL DEFAULT '{}',
    after_snapshot   TEXT    NOT NULL DEFAULT '{}',
    sample_ids       TEXT    NOT NULL DEFAULT '[]',
    sample_result    TEXT    NOT NULL DEFAULT '',
    summary          TEXT    NOT NULL DEFAULT '',
    undoable         INTEGER NOT NULL DEFAULT 1,
    undone_at        TEXT    NOT NULL DEFAULT '',
    created_at       TEXT    NOT NULL
);

-- AI 判断留痕。第一版不写入，但表先建好，第二阶段直接用
CREATE TABLE IF NOT EXISTS ai_judgements (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id              INTEGER NOT NULL,
    model_version        TEXT NOT NULL DEFAULT '',
    prompt_version       TEXT NOT NULL DEFAULT '',
    category_set_version TEXT NOT NULL DEFAULT '',
    raw_response         TEXT NOT NULL DEFAULT '',
    suggested_category   TEXT NOT NULL DEFAULT '',
    suggested_tags       TEXT NOT NULL DEFAULT '',
    reason               TEXT NOT NULL DEFAULT '',
    confidence           REAL DEFAULT NULL,
    human_category       TEXT NOT NULL DEFAULT '',
    human_tags           TEXT NOT NULL DEFAULT '',
    was_modified         INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_segrun_mat   ON segment_runs(material_id, is_current);
CREATE INDEX IF NOT EXISTS idx_seg_run      ON segments(run_id);
CREATE INDEX IF NOT EXISTS idx_seg_mat      ON segments(material_id);
CREATE INDEX IF NOT EXISTS idx_cards_owner  ON cards(owner_id, material_id);
CREATE INDEX IF NOT EXISTS idx_cards_cat    ON cards(primary_category_id);
CREATE INDEX IF NOT EXISTS idx_cards_status ON cards(status);
CREATE INDEX IF NOT EXISTS idx_cardtags_tag ON card_tags(sub_tag_id);
CREATE INDEX IF NOT EXISTS idx_subtags_own  ON sub_tags(owner_id);
CREATE INDEX IF NOT EXISTS idx_changes_own  ON card_changes(owner_id, id);
"""


def _columns(conn, table):
    """看一张表现在有哪些列。用来判断"这列加过了吗"。"""
    return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}


def _backup_db(reason):
    """动手改结构之前，先把整个库复制一份。返回备份文件名（失败返回 None）。

    为什么连"只是加一列"也要备份：她写的小说素材全在这个库里，
    而备份的代价只是一次文件复制。真出事的时候，这一份就是全部的退路。
    """
    try:
        if not os.path.exists(db.DB_PATH):
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = "%s.bak-%s-%s" % (db.DB_PATH, reason, stamp)
        shutil.copy2(db.DB_PATH, dest)
        return os.path.basename(dest)
    except Exception:                                        # pragma: no cover
        return None


def _rebuild_categories_if_needed():
    """老库的 categories 表要整容成"带 owner_id"的新结构。返回备份文件名或 None。

    【为什么必须整容，不能只补列】
    SQLite 能 ALTER TABLE ADD COLUMN，但**改不了已经写死的 UNIQUE 约束**。
    老表的 UNIQUE(set_version, name) 意味着全站只许有一个「情感」——
    换成"每个账号能建自己的主类"之后，这条约束就成了拦路虎。
    所以只能按官方那套办法：建新表 → 搬数据 → 删旧表 → 改名。

    【为什么敢在她的真库上做】
    没有任何一张表用外键指向 categories：卡片上那个 primary_category_id
    只是个普通整数列（不是 REFERENCES），来源映射表里的 category_id 同样。
    而且 id 是**原样搬过去**的 —— 卡片上指着 8 的那个数字，搬完还是 8。
    动手前还会先把整个库复制一份（见 _backup_db）。
    """
    if not os.path.exists(db.DB_PATH):
        return None                                  # 全新的库：交给 SCHEMA 直接建新结构
    with db.connect() as conn:
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                           "AND name='categories'").fetchone()
        if not row:
            return None
        if "owner_id" in _columns(conn, "categories"):
            return None                              # 已经是新结构，什么都不做

    # 到这里说明是老结构。先备份，再动。
    bak = _backup_db("categories")
    with db.connect() as conn:
        conn.executescript("""
CREATE TABLE categories__new (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id       TEXT    NOT NULL DEFAULT '',
    set_version    TEXT    NOT NULL DEFAULT 'v1',
    name           TEXT    NOT NULL,
    description    TEXT    NOT NULL DEFAULT '',
    aliases        TEXT    NOT NULL DEFAULT '',
    suggested_tags TEXT    NOT NULL DEFAULT '[]',
    parent_id      INTEGER DEFAULT NULL,
    sort           INTEGER NOT NULL DEFAULT 0,
    active         INTEGER NOT NULL DEFAULT 1,
    UNIQUE (owner_id, set_version, name)
);
INSERT INTO categories__new
    (id, owner_id, set_version, name, description, aliases,
     suggested_tags, parent_id, sort, active)
    SELECT id, '', set_version, name, description, aliases,
           suggested_tags, parent_id, sort, active
      FROM categories;
DROP TABLE categories;
ALTER TABLE categories__new RENAME TO categories;
""")
        # AUTOINCREMENT 的计数器跟着旧表名走了，得搬回来 ——
        # 不搬的话下一条主类的 id 会从 1 重新数，跟卡片上存的数字撞车。
        conn.execute("UPDATE sqlite_sequence SET name='categories' "
                     "WHERE name='categories__new'")
    return bak


def migrate(verbose=False):
    """建表 + 补列 + 播种初始数据。反复调用是安全的。

    为什么"补列"要单独写：
        SQLite 不支持 CREATE TABLE IF NOT EXISTS 更新已有表的列。
        已经存在的 moge.db 里 materials 表是没有 norm_version 的，
        所以得用 ALTER TABLE ADD COLUMN 补上去。
        而 ALTER 加一个已经存在的列会报错，所以要先查一遍再决定加不加。

    返回一份报告，方便启动时打印出来（她想知道到底改了什么）。
    """
    report = {"tables": [], "added_columns": [], "categories": 0,
              "retired_categories": 0,
              "sub_tags": 0, "source_mappings": 0, "tagged_source": 0,
              "categories_rebuilt": None, "backup": None}
    db.init_db()

    # 老库先把 categories 整容成"带 owner_id"的新结构（新库自动跳过）。
    # 必须在 executescript(SCHEMA) 之前做：SCHEMA 里那句是 CREATE TABLE IF NOT
    # EXISTS，表已经存在时它什么都不干，整容不能指望它。
    bak = _rebuild_categories_if_needed()
    if bak:
        report["categories_rebuilt"] = "categories"
        report["backup"] = bak

    with db.connect() as conn:
        conn.executescript(SCHEMA)

        # ---- 给 materials 补两列 ----
        cols = _columns(conn, "materials")
        if "norm_version" not in cols:
            conn.execute("ALTER TABLE materials ADD COLUMN norm_version TEXT NOT NULL DEFAULT ''")
            report["added_columns"].append("materials.norm_version")
        if "source_collection" not in cols:
            conn.execute("ALTER TABLE materials ADD COLUMN source_collection TEXT NOT NULL DEFAULT ''")
            report["added_columns"].append("materials.source_collection")

        # ---- 来源集合：默认取原文件名（也就是 title）----
        # 规格说「原文件名分类只能作为来源集合」，所以这里就是 title。
        # 旧 tags 表一个字都不动 —— 迁移不是删除，是"多了一个更合适的字段"。
        cur = conn.execute(
            "UPDATE materials SET source_collection = title WHERE source_collection = ''")
        report["tagged_source"] = cur.rowcount

        # ---- 给 categories 补 suggested_tags 列 ----
        ccols = _columns(conn, "categories")
        if "suggested_tags" not in ccols:
            conn.execute("ALTER TABLE categories "
                         "ADD COLUMN suggested_tags TEXT NOT NULL DEFAULT '[]'")
            report["added_columns"].append("categories.suggested_tags")

        # ---- 播种主类（当前版本 = v2）----
        # 已有同名的不覆盖 description？不，要覆盖 ——
        # 因为描述可能会修订（规格要求"主类描述以后可以修订"），
        # 而这里就是"系统默认描述"的唯一来源。
        # active=1 也要一起写回：万一某条被标成停用（迁移中途崩过、手动改过库），
        # 光靠 INSERT 是补不回来的，那一类就会凭空消失、看着像没加成功。
        # 当前界面上没有"停用主类"的入口，所以这里强制启用不会盖掉她的选择。
        for i, (name, desc, sug) in enumerate(CATEGORIES_SEED):
            conn.execute(
                """INSERT INTO categories
                       (owner_id, set_version, name, description,
                        suggested_tags, sort, active)
                   VALUES ('', ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(owner_id, set_version, name)
                   DO UPDATE SET description=excluded.description,
                                 suggested_tags=excluded.suggested_tags,
                                 sort=excluded.sort,
                                 active=1""",
                (CATEGORY_SET_VERSION, name, desc,
                 json.dumps(sug, ensure_ascii=False), i))
        report["categories"] = conn.execute(
            "SELECT COUNT(*) FROM categories WHERE set_version=? AND owner_id=''",
            (CATEGORY_SET_VERSION,)).fetchone()[0]

        # ---- 旧版本的主类标停用（v1 那 9 个 → active=0）----
        # 【为什么是停用不是删除】
        #   卡片表里可能还引用着旧主类的 id（比如换体系之前 AI 已经分好的那批）。
        #   物理删掉的话，category_name_map() 查不到名字，界面上那些卡会显示成空白 ——
        #   看起来像"数据丢了"，其实只是名字没了。留着就能查出来，只是不再出现在
        #   新建卡片的可选项里。
        cur = conn.execute(
            "UPDATE categories SET active=0 WHERE set_version<>? AND active=1"
            " AND owner_id=''",
            (CATEGORY_SET_VERSION,))
        report["retired_categories"] = cur.rowcount

        # ---- 播种状态常量表（不建表，用 Python 常量，见 segmentation.ALL_STATUS）----
        report["statuses"] = list(sg.ALL_STATUS)

        # ---- 表清单 ----
        report["tables"] = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]

        # ---- 副标签 / 来源映射 / 账号相关：按账号播种 ----
        owners = [r[0] for r in conn.execute(
            "SELECT DISTINCT owner_id FROM materials").fetchall()]
        owners = [o for o in owners if o]
        report["owners"] = owners
        for o in owners:
            report["sub_tags"] += _seed_owner(conn, o)

    if verbose:
        print("[迁移] 新增列：", report["added_columns"])
        print("[迁移] 主类数：", report["categories"])
        print("[迁移] 处理过的账号：", report["owners"])
    return report


def _seed_owner(conn, owner):
    """给一个账号播种副标签和来源映射表。返回新增的副标签数。"""
    n = 0
    ts = now_str()
    for name in SUB_TAGS_SEED:
        cur = conn.execute(
            "INSERT OR IGNORE INTO sub_tags (owner_id, name, active, created_at) "
            "VALUES (?, ?, 1, ?)", (owner, name, ts))
        n += 1 if cur.rowcount else 0

    mp = _seed_source_map()
    for src, (cat_name, note) in mp.items():
        row = conn.execute(
            "SELECT id FROM source_mappings WHERE owner_id=? AND source_collection=?",
            (owner, src)).fetchone()
        if row:
            continue
        cat_id = None
        if cat_name:
            r = conn.execute(
                "SELECT id FROM categories WHERE set_version=? AND name=?",
                (CATEGORY_SET_VERSION, cat_name)).fetchone()
            cat_id = r[0] if r else None
        conn.execute(
            """INSERT INTO source_mappings
               (owner_id, source_collection, category_id, note, confirmed, updated_at)
               VALUES (?, ?, ?, ?, 0, ?)""",
            (owner, src, cat_id, note, ts))

    # 库里出现过的、但种子表没覆盖的来源，也要各来一行（建议为空）
    for r in conn.execute(
            "SELECT DISTINCT source_collection FROM materials WHERE owner_id=?", (owner,)):
        src = r[0]
        if not src:
            continue
        conn.execute(
            """INSERT OR IGNORE INTO source_mappings
               (owner_id, source_collection, category_id, note, confirmed, updated_at)
               VALUES (?, ?, NULL, ?, 0, ?)""",
            (owner, src, "系统未给建议（来源名与主类对不上），请逐条判断", ts))
    return n


# ----------------------------------------------------------------------
# 主类 / 副标签 / 来源映射：读
# ----------------------------------------------------------------------

def list_categories(owner=None, include_inactive=False):
    """我能用的主类清单 = 系统自带的（所有人共用）+ 这个账号自己建的。

    【owner 不传会怎样】
    只给系统那套。老调用点（概览统计之类）要的就是"全站统一的那 11 个"，
    带上别人的私有类反而会让数字对不上。要她自己那套就老老实实传 owner。

    suggested_tags 存在库里是 JSON 字符串，出门前解回数组 ——
    前端拿到的是 list，不用自己再 JSON.parse 一次。
    """
    sql = "SELECT * FROM categories WHERE set_version=?"
    args = [CATEGORY_SET_VERSION]
    if owner:
        sql += " AND (owner_id='' OR owner_id=?)"
        args.append(owner)
    else:
        sql += " AND owner_id=''"
    if not include_inactive:
        sql += " AND active=1"
    sql += " ORDER BY sort, id"
    with db.connect() as conn:
        out = []
        for r in conn.execute(sql, args).fetchall():
            d = dict(r)
            try:
                d["suggested_tags"] = json.loads(d.get("suggested_tags") or "[]")
            except Exception:                                # pragma: no cover
                d["suggested_tags"] = []
            d["mine"] = bool(d.get("owner_id"))     # 界面上要区分"我建的"和"系统的"
            out.append(d)
        return out


def all_categories():
    """**所有版本**的主类，含停用、含旧版本。

    【为什么单开一个，不让 name_map 用 list_categories(include_inactive=True)】
    换过分类体系之后，卡片上可能还留着旧版本的 category_id
    （比如 v1 的「情绪心理」）。只查当前版本的话，这些 id 查不到名字，
    界面上那些卡的主类会显示成空白 —— 看着像数据丢了，其实只是名字没查出来。
    所以"查 id 换名字"这件事必须跨版本。

    排序是 v1 在前、v2 在后（字符串比较，"v1" < "v2"），
    同名类撞车时后写的覆盖前面的，也就是**当前版本优先** —— 正好是要的效果。
    """
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM categories ORDER BY set_version, sort, id").fetchall()]


def category_name_map():
    return {c["id"]: c["name"] for c in all_categories()}


def category_id_map():
    """类名 → id。跨版本找，同名时当前版本优先。"""
    return {c["name"]: c["id"] for c in all_categories()}


def list_sub_tags(owner, active_only=False):
    """副标签清单。

    active_only=True 只给"还能选的" —— 界面上那个标签选择器要用这个，
    否则她停用过的标签还会一直出现在下拉框里，越用越长。
    管理面板则要看到全部（含停用的），所以默认不过滤。
    """
    sql = "SELECT * FROM sub_tags WHERE owner_id=?"
    if active_only:
        sql += " AND active=1"
    sql += " ORDER BY active DESC, name"
    with db.connect() as conn:
        rows = conn.execute(sql, (owner,)).fetchall()
        return [dict(r) for r in rows]


# ----------------------------------------------------------------------
# 主类 / 副标签：她自己加的那几个
#
# 【为什么允许她自己建】
#   那 11 个主类是我照着她手上素材的名字拟的，属于"我替她猜的一版"。
#   她实际写着写着一定会冒出新类（比如「打斗」当时就是她后加的）。
#   与其每次都来改代码，不如让她自己在界面上加。
#
# 【加出来的类是"只有我自己"的】
#   owner_id 存的是她的账号，别人看不到、也选不到。理由：分类体系是
#   一个人的私人语法 —— 她的「梗」和别人的「梗」未必是同一件事。
#   系统自带的那 11 个仍是全站共用，谁都能用。
# ----------------------------------------------------------------------

CATEGORY_NAME_MAX = 20     # 主类名要短：界面上是个小圆钮，太长会把那一行撑爆
CATEGORY_DESC_MAX = 300    # 判据：一两句话说清"什么时候算这一类"
SUB_TAG_NAME_MAX = 20      # 副标签同样是圆钮，同样怕长


def _find_category(conn, owner, name, include_inactive=True):
    """按名字找一条**我的**主类（不含系统的）。"""
    sql = ("SELECT * FROM categories WHERE owner_id=? AND set_version=? AND name=?"
           + ("" if include_inactive else " AND active=1"))
    return conn.execute(sql, (owner, CATEGORY_SET_VERSION, name)).fetchone()


def create_category(owner, name, description, suggested_tags=None):
    """建一个只有我自己能用的主类。返回那一条（新建或复活）。

    【三种会当场拒绝的情况 —— 都是为了让"拒绝了"是件能看懂的事】
      1. 名字跟系统自带的那 11 个撞：允许的话界面上会出现两个「外貌」，
         一个全站共用、一个只有你看得见，判据还可能不一样 ——
         她分完卡自己都说不清归到了哪个。
      2. 名字跟自己已有的撞：直接告诉她去改那一条，别悄悄建第二条。
      3. 判据空着：AI 判类时**只看这段判据**。空着它就只能照名字猜，
         分出来的东西对不对全凭运气，而她还以为"自己建的类不管用"。

    已经停用过的同名类会被**复活**（而不是新建）：历史卡片上挂的是老那条的
    id，复活它，那些卡上的类名就跟着回来了；新建一条只会留下一堆查不到名字的孤儿。
    """
    name = (name or "").strip()
    description = (description or "").strip()
    if not name:
        raise ValueError("主类名不能空着。")
    if len(name) > CATEGORY_NAME_MAX:
        raise ValueError("主类名最长 %d 个字，现在 %d 个 —— 界面上那一行放不下。"
                         % (CATEGORY_NAME_MAX, len(name)))
    if not description:
        raise ValueError("判据得写一句 —— AI 判类时只看这段说明，空着它就只能猜。"
                         "例如「这一段能独立成立成一个事件」。")
    if len(description) > CATEGORY_DESC_MAX:
        raise ValueError("判据最长 %d 个字，现在 %d 个。"
                         % (CATEGORY_DESC_MAX, len(description)))
    sug = [s.strip() for s in (suggested_tags or []) if (s or "").strip()]
    ts = now_str()
    with db.connect() as conn:
        sysrow = conn.execute(
            "SELECT name FROM categories WHERE set_version=? AND owner_id='' AND name=?",
            (CATEGORY_SET_VERSION, name)).fetchone()
        if sysrow:
            raise ValueError("系统里已经有「%s」这一类了，直接用它就行，不用新建。"
                             % name)
        mine = _find_category(conn, owner, name)
        if mine and mine["active"]:
            raise ValueError("你已经有一条叫「%s」的主类了。要改就去改那一条，"
                             "或者换个名字。" % name)
        if mine:
            conn.execute(
                "UPDATE categories SET description=?, suggested_tags=?, active=1"
                " WHERE id=?", (description, json.dumps(sug, ensure_ascii=False),
                                mine["id"]))
            cid = mine["id"]
        else:
            # sort 排到所有系统类后面：新加的类出现在清单末尾、挨着「＋新建」按钮，
            # 一眼就能找到，也不会把她天天用的那几类挤走位。
            mx = conn.execute(
                "SELECT COALESCE(MAX(sort), 0) FROM categories WHERE set_version=?",
                (CATEGORY_SET_VERSION,)).fetchone()[0]
            cur = conn.execute(
                """INSERT INTO categories
                       (owner_id, set_version, name, description,
                        suggested_tags, sort, active)
                   VALUES (?,?,?,?,?,?,1)""",
                (owner, CATEGORY_SET_VERSION, name, description,
                 json.dumps(sug, ensure_ascii=False), mx + 1))
            cid = cur.lastrowid
        row = conn.execute("SELECT * FROM categories WHERE id=?", (cid,)).fetchone()
        d = dict(row)
        d["suggested_tags"] = sug
        d["mine"] = True
        return d


def set_category_active(owner, category_id, active):
    """停用/恢复一个**我自己建的**主类。返回更新后的那一条，不是我的返回 None。

    【为什么只让动自己的】
    系统那 11 个是迁移脚本的管辖范围（每次启动都会把它们写回 active=1）。
    允许她停用的话，重启一次就被悄悄恢复了 —— 那还不如一开始就不给这个口子。
    【为什么是停用不是删除】
    卡片上存着 primary_category_id。物理删掉的话那些卡的主类会显示成空白，
    看着像数据丢了。停用只是不再出现在"选主类"的清单里，老卡片照样认得出它。
    """
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM categories WHERE id=? AND owner_id=?",
                           (category_id, owner)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE categories SET active=? WHERE id=?",
                     (1 if active else 0, category_id))
        d = dict(row)
        d["active"] = 1 if active else 0
        try:
            d["suggested_tags"] = json.loads(d.get("suggested_tags") or "[]")
        except Exception:                                    # pragma: no cover
            d["suggested_tags"] = []
        d["mine"] = True
        return d


def create_sub_tag(owner, name):
    """加一个小标签。返回那一条 + 是不是"复活"的老标签。

    【注意：接口层没有直接用这个函数】
    小标签的新增/改名/停用/合并早就有一条现成的接口（POST /api/sub-tags，
    带 action 参数），那边是这个函数的等价实现。这里留着一份，
    是给命令行、脚本和测试用的（tests/test_custom_categories.py 就靠它）。
    两边行为要保持一致：同名标签不新建，而是把停用过的那条复活 ——
    新建会撞 UNIQUE(owner_id, name)，而且老卡片上挂的是老那条的 id，
    复活它，那些卡上的标签就跟着回来了。
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("标签名不能空着。")
    if len(name) > SUB_TAG_NAME_MAX:
        raise ValueError("标签名最长 %d 个字，现在 %d 个。"
                         % (SUB_TAG_NAME_MAX, len(name)))
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM sub_tags WHERE owner_id=? AND name=?",
                           (owner, name)).fetchone()
        if row:
            if row["active"]:
                raise ValueError("已经有一个叫「%s」的标签了。" % name)
            conn.execute("UPDATE sub_tags SET active=1 WHERE id=?", (row["id"],))
            d = dict(row)
            d["active"] = 1
            d["revived"] = True
            return d
        cur = conn.execute(
            "INSERT INTO sub_tags (owner_id, name, active, created_at) VALUES (?,?,1,?)",
            (owner, name, now_str()))
        d = dict(conn.execute("SELECT * FROM sub_tags WHERE id=?",
                              (cur.lastrowid,)).fetchone())
        d["revived"] = False
        return d


def set_sub_tag_active(owner, tag_id, active):
    """停用/恢复一个小标签。只能动自己的。返回更新后的那一条或 None。"""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM sub_tags WHERE id=? AND owner_id=?",
                           (tag_id, owner)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE sub_tags SET active=? WHERE id=?",
                     (1 if active else 0, tag_id))
        d = dict(row)
        d["active"] = 1 if active else 0
        return d


def list_source_mappings(owner):
    """来源映射表：每一个来源名 + 建议主类 + 理由 + 是否已确认 + 实际有多少条"""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT sm.*, c.name AS category_name
               FROM source_mappings sm
               LEFT JOIN categories c ON c.id = sm.category_id
               WHERE sm.owner_id = ?
               ORDER BY sm.source_collection""", (owner,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["card_count"] = conn.execute(
                """SELECT COUNT(*) FROM cards ca
                   JOIN materials m ON m.id = ca.material_id
                   WHERE m.owner_id=? AND m.source_collection=? AND ca.status<>?
                """, (owner, r["source_collection"], sg.STATUS_EXCLUDED)).fetchone()[0]
            out.append(d)
        return out


def set_source_mapping(owner, source_collection, category_id=None, note=None,
                       confirmed=None):
    with db.connect() as conn:
        sets, params = [], []
        if category_id is not None:
            sets.append("category_id = ?")
            params.append(category_id if category_id else None)
        if note is not None:
            sets.append("note = ?")
            params.append(note)
        if confirmed is not None:
            sets.append("confirmed = ?")
            params.append(1 if confirmed else 0)
        sets.append("updated_at = ?")
        params.append(now_str())
        params.extend([owner, source_collection])
        conn.execute("UPDATE source_mappings SET %s WHERE owner_id=? AND source_collection=?"
                     % ", ".join(sets), params)
        row = conn.execute(
            "SELECT * FROM source_mappings WHERE owner_id=? AND source_collection=?",
            (owner, source_collection)).fetchone()
        return dict(row) if row else None


# ----------------------------------------------------------------------
# 素材 / 切分
# ----------------------------------------------------------------------

def _material_row(conn, material_id, owner):
    return conn.execute(
        "SELECT * FROM materials WHERE id=? AND owner_id=?",
        (material_id, owner)).fetchone()


def _ensure_source(conn, owner, material_id):
    """保证一份文件有「来源名」，并且来源映射表里有它那一行。

    为什么需要：粘贴进来的文本、网页上传的文件，入库时 source_collection 是空的，
    而卡片上的来源标签、按来源筛选、按来源批量采纳，全靠这个字段。
    与其在上传、粘贴、导入三个地方各写一遍，不如在"切分"这一个必经路口补一次。

    来源名的取法：原来有就用原来的；没有就用文件标题；标题也是空的就叫「未命名」。
    """
    row = conn.execute(
        "SELECT id, title, source_collection FROM materials WHERE id=? AND owner_id=?",
        (material_id, owner)).fetchone()
    if not row:
        return ""
    sc = (row["source_collection"] or "").strip() \
        or (row["title"] or "").strip() or "未命名"
    if (row["source_collection"] or "") != sc:
        conn.execute(
            "UPDATE materials SET source_collection=? WHERE id=? AND owner_id=?",
            (sc, material_id, owner))
    conn.execute(
        """INSERT OR IGNORE INTO source_mappings
           (owner_id, source_collection, category_id, note, confirmed, updated_at)
           VALUES (?, ?, NULL, '', 0, ?)""", (owner, sc, now_str()))
    return sc


def source_list(owner, limit=500):
    """左栏的"文件 / 来源列表"。

    每个文件显示：来源名、原文件名、类型、原文长度、切分状态、
    已生成卡片数、待确认数、已排除数、当前切分规则、异常行数。
    """
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT id, title, ext, chars, source_collection, created_at,
                      norm_version
               FROM materials WHERE owner_id=? ORDER BY id LIMIT ?""",
            (owner, limit)).fetchall()

        out = []
        for r in rows:
            mid = r["id"]
            run = conn.execute(
                """SELECT * FROM segment_runs WHERE material_id=?
                   ORDER BY id DESC LIMIT 1""", (mid,)).fetchone()

            n_cards = conn.execute(
                "SELECT COUNT(*) FROM cards WHERE material_id=? AND owner_id=? "
                "AND status<>?", (mid, owner, sg.STATUS_EXCLUDED)).fetchone()[0]
            n_pending = conn.execute(
                "SELECT COUNT(*) FROM cards WHERE material_id=? AND owner_id=? "
                "AND status=?", (mid, owner, sg.STATUS_PENDING)).fetchone()[0]
            n_excluded = conn.execute(
                "SELECT COUNT(*) FROM cards WHERE material_id=? AND owner_id=? "
                "AND status=?", (mid, owner, sg.STATUS_EXCLUDED)).fetchone()[0]

            d = {
                "id": mid, "title": r["title"], "ext": r["ext"],
                "chars": r["chars"],
                "source_collection": r["source_collection"] or r["title"],
                "norm_version": r["norm_version"] or "",
                "has_run": run is not None,
                "rule": run["rule"] if run else "",
                "run_id": run["id"] if run else None,
                "segment_count": run["segment_count"] if run else 0,
                "cards": n_cards, "pending": n_pending, "card_excluded": n_excluded,
            }

            if run:
                d["segments_excluded"] = conn.execute(
                    "SELECT COUNT(*) FROM segments WHERE run_id=? AND excluded=1",
                    (run["id"],)).fetchone()[0]
                d["noise_reason_summary"] = {
                    rr[0]: rr[1] for rr in conn.execute(
                        """SELECT noise_reason, COUNT(*) FROM segments
                           WHERE run_id=? AND excluded=1 AND noise_reason<>''
                           GROUP BY noise_reason""", (run["id"],)).fetchall()}
                d["predicted"] = None
            else:
                # 还没切过 → 顺手算一下"预计能切多少条"，让她切之前心里有数
                try:
                    pv = sg.preview(_content(conn, mid), with_text=False)
                    d["auto_rule"] = pv["auto_rule"]
                    d["auto_reason"] = pv["auto_reason"]
                    d["predicted"] = pv["counts"]["total"]
                    d["predicted_cards"] = pv["counts"]["will_create_cards"]
                    d["noise_summary"] = pv["noise_summary"]
                    d["noise_high"] = pv["counts"]["noise_high"]
                    d["noise_hint"] = pv["counts"]["noise_hint"]
                except Exception as e:                      # pragma: no cover
                    d["predicted"] = None
                    d["auto_reason"] = "预览失败：%s" % e
            out.append(d)
        return out


def _content(conn, material_id):
    row = conn.execute("SELECT content FROM materials WHERE id=?",
                       (material_id,)).fetchone()
    return row["content"] if row else ""


def split_preview(material_id, owner, rule=None, text_limit=80, offset=0,
                  limit=None):
    """切分预览。不写任何数据 —— 这一步的价值就是"先看，再决定"。"""
    with db.connect() as conn:
        m = _material_row(conn, material_id, owner)
        if not m:
            return None
        content = m["content"]
        if rule is None:
            row = conn.execute(
                "SELECT rule FROM segment_runs WHERE material_id=? ORDER BY id DESC LIMIT 1",
                (material_id,)).fetchone()
            rule = row["rule"] if row else None

    pv = sg.preview(content, rule=rule, text_limit=text_limit)
    pv["material"] = {
        "id": material_id, "title": m["title"], "ext": m["ext"],
        "chars": m["chars"],
        "source_collection": m["source_collection"] or m["title"],
    }
    pv["norm_report"] = sg.normalize_report(content)
    pv["already_has_run"] = bool(m["norm_version"])

    # 把"这份文件最后一次切分是哪一次"也带上。
    # 为什么预览要管这件事：界面上有个「把被自动排除的噪音段恢复成卡片」的按钮，
    #   它必须知道要恢复哪一次切分的结果；而且重切之后，这个 id 会变。
    with db.connect() as conn:
        run = conn.execute(
            "SELECT id, rule, rule_version, norm_version, created_at, is_current"
            " FROM segment_runs WHERE material_id=? ORDER BY id DESC LIMIT 1",
            (material_id,)).fetchone()
    pv["run"] = dict(run) if run else None
    pv["run_id"] = run["id"] if run else None

    # 支持分页看：663 条一次全给前端，界面会卡
    if limit is not None:
        pv["segments"] = pv["segments"][offset:offset + limit]
        pv["page"] = {"offset": offset, "limit": limit,
                      "returned": len(pv["segments"])}
    return pv


def apply_split(material_id, owner, rule=None, force=False):
    """确认生成：真的切一遍，写 segment_runs + segments + cards。

    回滚安全的地方：
      · 旧 run 不删，只把 is_current 置 0
      · 旧 segments 一律保留
      · 已经存在的卡片不动；新切出来的段如果和已有卡片重叠，跳过不重复建卡

    force=True 才允许"换一种切法重切"，避免手滑把切法换了。
    """
    with db.connect() as conn:
        m = _material_row(conn, material_id, owner)
        if not m:
            return None
        content = m["content"]

        # 先保证这份文件有来源名（粘贴 / 上传进来的可能是空的），
        # 并让来源映射表里有它的一行 —— 后面的卡片和批量采纳都要用。
        _ensure_source(conn, owner, material_id)

        prev = conn.execute(
            "SELECT * FROM segment_runs WHERE material_id=? AND is_current=1",
            (material_id,)).fetchone()

        auto = sg.auto_rule(content)
        use = rule if rule in sg.RULES else auto["rule"]

        if prev and prev["rule"] != use and not force:
            return {"ok": False, "need_force": True,
                    "message": "这个文件上次是用「%s」切的，这次选的是「%s」。"
                               "换切法会重新生成一套段落（旧卡片不会被删），"
                               "确认要换吗？"
                               % (sg.RULES[prev["rule"]]["name"],
                                  sg.RULES[use]["name"])}

        segs = sg.build_segments(content, use, with_text=False)

        # 旧 run 退位（不删！）
        if prev:
            conn.execute("UPDATE segment_runs SET is_current=0 WHERE material_id=?",
                         (material_id,))

        ts = now_str()
        cur = conn.execute(
            """INSERT INTO segment_runs
               (material_id, rule, rule_version, norm_version, segment_count,
                is_current, created_at)
               VALUES (?, ?, ?, ?, ?, 1, ?)""",
            (material_id, use, sg.RULE_VERSION, sg.NORM_VERSION,
             len(segs), ts))
        run_id = cur.lastrowid

        # 已有的卡片区间（用来避免重复建卡）
        existing = [(r["start_offset"], r["end_offset"]) for r in conn.execute(
            "SELECT start_offset, end_offset FROM cards WHERE material_id=? AND owner_id=?",
            (material_id, owner)).fetchall()]

        def overlaps(a, b):
            for s, e in existing:
                if a < e and s < b:
                    return True
            return False

        n_noise = n_cards = n_skipped = 0
        for s in segs:
            excluded = 1 if s["noise_level"] == "high" else 0
            if excluded:
                n_noise += 1
            seg_cur = conn.execute(
                """INSERT INTO segments
                   (run_id, material_id, seq, start_offset, end_offset,
                    source_text_hash, head_check, tail_check, excluded,
                    noise_reason, line_from, line_to, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, material_id, s["seq"], s["start"], s["end"],
                 s["source_text_hash"], s["head_check"], s["tail_check"],
                 excluded, s["noise_reason"], s["line_from"], s["line_to"], ts))
            seg_id = seg_cur.lastrowid

            # 噪音段不建卡（可以之后在界面上恢复 / 或点"恢复为卡片"）
            if excluded:
                continue
            if overlaps(s["start"], s["end"]):
                n_skipped += 1
                continue
            conn.execute(
                """INSERT INTO cards
                   (owner_id, material_id, start_offset, end_offset,
                    source_text_hash, segment_id, source, status,
                    operation_group_id, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (owner, material_id, s["start"], s["end"],
                 s["source_text_hash"], seg_id,
                 sg.SOURCE_INHERITED, sg.STATUS_PENDING, "", ts, ts))
            existing.append((s["start"], s["end"]))
            n_cards += 1

        conn.execute("UPDATE materials SET norm_version=? WHERE id=?",
                     (sg.NORM_VERSION, material_id))

        summary = {
            "ok": True, "run_id": run_id, "rule": use,
            "rule_name": sg.RULES[use]["name"],
            "segments": len(segs), "noise_excluded": n_noise,
            "cards_created": n_cards, "cards_skipped": n_skipped,
            "message": "切分完成：共 %d 段，其中 %d 段识别为噪音已排除，"
                       "新建 %d 张卡片%s。"
                       % (len(segs), n_noise, n_cards,
                          ("，%d 段已有卡片覆盖，未重复创建" % n_skipped)
                          if n_skipped else ""),
        }

        # 记一条变更（切分也是变更，也该能追溯）
        _write_change(conn, owner, "split", None, material_id,
                      n_cards, [], {}, {},
                      summary=summary["message"])
        return summary


# ----------------------------------------------------------------------
# 卡片：读
# ----------------------------------------------------------------------

def _card_tags(conn, card_id):
    rows = conn.execute(
        """SELECT t.name FROM sub_tags t
           JOIN card_tags ct ON ct.sub_tag_id = t.id
           WHERE ct.card_id=? ORDER BY t.name""", (card_id,)).fetchall()
    return [r[0] for r in rows]


def _set_card_tags(conn, owner, card_id, names, source="human", confirmed=1):
    """把卡片的副标签设成 names（先清后加）。names 里没有的标签会被创建。"""
    ts = now_str()
    keep = []
    for n in names or []:
        n = (n or "").strip()
        if not n:
            continue
        row = conn.execute(
            "SELECT id, active FROM sub_tags WHERE owner_id=? AND name=?",
            (owner, n)).fetchone()
        if row:
            tid = row[0]
            if not row[1]:                      # 之前停用的，重新启用
                conn.execute("UPDATE sub_tags SET active=1 WHERE id=?", (tid,))
        else:
            cur = conn.execute(
                "INSERT INTO sub_tags (owner_id, name, active, created_at) "
                "VALUES (?,?,1,?)", (owner, n, ts))
            tid = cur.lastrowid
        keep.append(tid)

    conn.execute("DELETE FROM card_tags WHERE card_id=?", (card_id,))
    for tid in keep:
        conn.execute(
            "INSERT OR IGNORE INTO card_tags (card_id, sub_tag_id, source, confirmed) "
            "VALUES (?,?,?,?)", (card_id, tid, source, confirmed))


def _decorate(conn, row, content, cat_names):
    """把一行 cards 补成前端要的形状（正文、标签、校验结果都在这里算）。"""
    d = dict(row)
    d["text"] = content[d["start_offset"]:d["end_offset"]]
    d["chars"] = len(d["text"])
    d["category_name"] = cat_names.get(d["primary_category_id"], "")
    d["sub_tags"] = _card_tags(conn, d["id"])
    v = sg.verify(content, d["start_offset"], d["end_offset"],
                  d["source_text_hash"])
    d["verify_ok"] = v["ok"]
    d["verify_reason"] = v["reason"]
    return d


def list_cards(owner, material_id=None, category_id=None, sub_tag=None,
               status=None, include_excluded=False, only_verify_error=False,
               only_duplicate=False, source_collection=None, keyword=None,
               order="seq", limit=100, offset=0):
    """卡片流。带分页和筛选。

    order 可选：seq（按原文顺序，默认）/ updated（最近改的在前）/ chars
    """
    where = ["c.owner_id = ?"]
    params = [owner]

    if material_id:
        where.append("c.material_id = ?")
        params.append(material_id)
    if source_collection:
        where.append("""c.material_id IN (SELECT id FROM materials
                        WHERE owner_id = ? AND source_collection = ?)""")
        params.extend([owner, source_collection])
    if status:
        where.append("c.status = ?")
        params.append(status)
    if not include_excluded:
        where.append("c.status <> ?")
        params.append(sg.STATUS_EXCLUDED)
    if sub_tag:
        where.append("""EXISTS (SELECT 1 FROM card_tags ct JOIN sub_tags st
                        ON st.id = ct.sub_tag_id
                        WHERE ct.card_id = c.id AND st.name = ?)""")
        params.append(sub_tag)

    # 先留一份**不含主类筛选**的条件，用来算「每个主类各装了多少张」。
    # 为什么：她点了「外貌」之后，如果计数也跟着筛，其他类全变成 0 ——
    # 看起来像刚才的成果没了，其实只是视角问题。
    # 主类条件因此被挪到这一段的最后再加（顺序不影响 SQL 语义，都是 AND）。
    # 这份条件里不含 only_verify_error / only_duplicate（那两个在下面才算得出来），
    # 所以开那两个开关时这里的计数会略偏大 —— 可以接受：
    # 那两个开关问的是"这张卡是不是坏了"，跟"各类装了多少"是两回事。
    where_no_cat, params_no_cat = list(where), list(params)

    if category_id == 0:
        where.append("c.primary_category_id IS NULL")
    elif category_id:
        where.append("c.primary_category_id = ?")
        params.append(category_id)

    order_sql = {
        "seq": "c.material_id, c.start_offset",
        "updated": "c.updated_at DESC, c.id DESC",
        "chars": "c.end_offset - c.start_offset DESC",
        "created": "c.id DESC",
        # 按主分类分段。段的先后顺序跟着 categories.sort 走 ——
        # 也就是界面上那排圆钮的顺序（外貌 → 神态 → 动作…），
        # 不是按 id（id 是插入顺序，看着是乱的）。
        # 未分类捆在最后：它是"还没归置的尾巴"，不是一类；
        # 真要单独看它，点「未分类」那个圆钮就行。
        "category": ("CASE WHEN c.primary_category_id IS NULL THEN 1 ELSE 0 END,"
                     " COALESCE(cat.sort, 9999), cat.id,"
                     " c.material_id, c.start_offset"),
    }.get(order, "c.material_id, c.start_offset")

    # 只有"按主分类分段"这一档才需要连 categories。
    join_cat = (" LEFT JOIN categories cat ON cat.id = c.primary_category_id"
                if order == "category" else "")

    with db.connect() as conn:
        # ---- 两个"算出来才知道"的筛选项 ----
        #
        # 为什么不能在 SQL 里筛：
        #   "校验失败"要真的把正文切出来算哈希，"有重复"要两两比对。
        #   这两个都必须在 Python 里算。
        # 做法：先按条件把全部候选的 id 和区间取出来（不加 limit），
        #   算完得到一串 id，再把 id 当成筛选条件塞回 SQL 里分页 ——
        #   这样"共 N 条"这个数字才是准的。
        if only_verify_error or only_duplicate:
            cand = conn.execute(
                "SELECT c.id, c.material_id, c.start_offset, c.end_offset,"
                " c.source_text_hash FROM cards c WHERE " + " AND ".join(where),
                params).fetchall()
            cache = {}
            keep = []
            if only_duplicate:
                items = []
                for r in cand:
                    mid = r["material_id"]
                    if mid not in cache:
                        cache[mid] = _content(conn, mid)
                    items.append({"id": r["id"],
                                  "text": cache[mid][r["start_offset"]:r["end_offset"]]})
                keep = sorted(_duplicate_ids(conn, items))
            else:
                for r in cand:
                    mid = r["material_id"]
                    if mid not in cache:
                        cache[mid] = _content(conn, mid)
                    if not sg.verify(cache[mid], r["start_offset"], r["end_offset"],
                                     r["source_text_hash"])["ok"]:
                        keep.append(r["id"])
            if not keep:
                return {"total": 0, "items": [], "offset": offset,
                        "limit": limit, "cat_counts": {}}
            where.append("c.id IN (%s)" % ",".join("?" * len(keep)))
            params = params + keep

        # 每个主类各多少张（不含主类筛选，见上面的说明）。
        # 空 key 是"未分类"，前端按它渲染成「未分类」那一格。
        counts = {}
        for cr in conn.execute(
                "SELECT c.primary_category_id AS cid, COUNT(*) AS n"
                " FROM cards c WHERE " + " AND ".join(where_no_cat) +
                " GROUP BY c.primary_category_id", params_no_cat).fetchall():
            counts["" if cr["cid"] is None else str(cr["cid"])] = cr["n"]

        total = conn.execute(
            "SELECT COUNT(*) FROM cards c WHERE " + " AND ".join(where),
            params).fetchone()[0]

        rows = conn.execute(
            "SELECT c.*, m.title AS material_title,"
            " m.source_collection AS source_collection,"
            " (SELECT COUNT(*) FROM cards x WHERE x.material_id = c.material_id"
            "  AND x.owner_id = c.owner_id AND x.status <> ?"
            "  AND x.start_offset < c.start_offset) + 1 AS seq_no"
            " FROM cards c JOIN materials m ON m.id = c.material_id" + join_cat +
            " WHERE " + " AND ".join(where) +
            " ORDER BY " + order_sql + " LIMIT ? OFFSET ?",
            [sg.STATUS_EXCLUDED] + params + [limit, offset]).fetchall()

        cat_names = category_name_map()

        # 先把涉及到的素材正文一次性取出来，避免每张卡都查一次库
        content_cache = {}
        items = []
        for r in rows:
            mid = r["material_id"]
            if mid not in content_cache:
                content_cache[mid] = _content(conn, mid)
            items.append(_decorate(conn, r, content_cache[mid], cat_names))

    return {"total": total, "items": items,
            "offset": offset, "limit": limit, "cat_counts": counts}


def _duplicate_ids(conn, items):
    """在一批卡片里找出有近重复提示的卡片 id 集合。"""
    pairs = sg.find_duplicates([{"id": x["id"], "text": x["text"]} for x in items])
    ids = set()
    for p in pairs:
        ids.add(p["a_id"])
        ids.add(p["b_id"])
    return ids


def get_card(card_id, owner, context_chars=120):
    """单张卡片详情：正文 + 前后文 + 原文位置。

    前后文很关键：一段素材单独看常常看不出好在哪（
    「他捏碎了手里的茶杯」单独看很普通，放在上下两句里才知道分量）。
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM cards WHERE id=? AND owner_id=?",
            (card_id, owner)).fetchone()
        if not row:
            return None
        content = _content(conn, row["material_id"])
        cat_names = category_name_map()
        d = _decorate(conn, row, content, cat_names)

        d["before_text"] = content[max(0, d["start_offset"] - context_chars):
                                   d["start_offset"]]
        d["after_text"] = content[d["end_offset"]:
                                  d["end_offset"] + context_chars]

        # 这张卡前后各有哪些卡（界面上"上一张/下一张"用）
        d["prev_id"] = None
        d["next_id"] = None
        pr = conn.execute(
            """SELECT id FROM cards WHERE owner_id=? AND material_id=?
               AND status<>? AND start_offset < ?
               ORDER BY start_offset DESC LIMIT 1""",
            (owner, d["material_id"], sg.STATUS_EXCLUDED,
             d["start_offset"])).fetchone()
        nr = conn.execute(
            """SELECT id FROM cards WHERE owner_id=? AND material_id=?
               AND status<>? AND start_offset >= ?
               ORDER BY start_offset LIMIT 1""",
            (owner, d["material_id"], sg.STATUS_EXCLUDED,
             d["end_offset"])).fetchone()
        if pr:
            d["prev_id"] = pr["id"]
        if nr:
            d["next_id"] = nr["id"]

        m = conn.execute("SELECT title, source_collection FROM materials WHERE id=?",
                         (d["material_id"],)).fetchone()
        d["material_title"] = m["title"]
        d["source_collection"] = m["source_collection"] or m["title"]
        return d


# ----------------------------------------------------------------------
# 变更记录与撤销
# ----------------------------------------------------------------------

# 撤销时要逐字段比对的字段清单。
#
# 注意 source（这条卡的主类是谁定的）**故意不在这个清单里**，
# 它由 undo_change 单独处理。原因：
#   清单里的字段按"当前值 == 本次写入值"逐个判断，命中一个就算"恢复了一张"。
#   如果 source 也放进来，那么一张被她后来又改过主类的卡片，
#   会因为"来源还是本次写进去的 human"而被算成恢复成功 ——
#   结果主类留在她改的值、来源却退回继承，两者配不上，
#   而那张卡下一次自动分类又会被 AI 覆盖一遍。所以它必须跟着主类一起退。
CARD_FIELDS = ("primary_category_id", "status", "note")

# 单独跟着主类一起回滚的字段
SOURCE_FIELD = "source"


def _write_change(conn, owner, action_type, operator_id, material_id,
                  affected_count, affected_ids, before, after,
                  sample_ids=None, sample_result="", summary="", undoable=True):
    cur = conn.execute(
        """INSERT INTO card_changes
           (owner_id, action_type, operator_id, material_id, affected_count,
            affected_card_ids, before_snapshot, after_snapshot, sample_ids,
            sample_result, summary, undoable, undone_at, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (owner, action_type, operator_id, material_id, affected_count,
         json.dumps(affected_ids, ensure_ascii=False),
         json.dumps(before, ensure_ascii=False),
         json.dumps(after, ensure_ascii=False),
         json.dumps(sample_ids or [], ensure_ascii=False),
         sample_result, summary, 1 if undoable else 0, "", now_str()))
    return cur.lastrowid


def _load_cards(conn, owner, card_ids):
    if not card_ids:
        return []
    marks = ",".join("?" * len(card_ids))
    return conn.execute(
        "SELECT * FROM cards WHERE owner_id=? AND id IN (%s)" % marks,
        [owner] + list(card_ids)).fetchall()


def update_cards(owner, card_ids, patch, action_type="batch_update",
                 operator_id=None, sample_ids=None, sample_result="",
                 summary=""):
    """改一批卡片的主类 / 副标签 / 状态 / 备注，并留下可撤销的记录。

    patch 里没出现的字段一律不动 —— 这很重要：
    界面上只改了状态，绝不能顺手把主类也覆盖了。

    注意 patch 里出现的字段哪怕值是 None 也会被写进去
    （用来表达"清空主类"这个动作）。
    所以调用方要遵守约定：不想改的字段就别放进 patch，
    而不是"放个 None 表示不改"。
    """
    with db.connect() as conn:
        rows = _load_cards(conn, owner, card_ids)
        if not rows:
            return None

        # 规格里的一条硬要求：校验不过的卡片，不许标成「已确认」。
        #
        # 为什么必须拦在这里：标签会一路流向内化素材库和大纲生成。
        #   一条对不上原文的卡片被标成"已确认"，错误就会跟着往下传，
        #   而且因为它披着"已确认"的外衣，以后没人会回头查它。
        #   宁可现在拒绝整批，也不要埋一颗以后查不出来的雷。
        if patch.get("status") == sg.STATUS_CONFIRMED:
            bad, cache = [], {}
            for r in rows:
                mid = r["material_id"]
                if mid not in cache:
                    cache[mid] = _content(conn, mid)
                v = sg.verify(cache[mid], r["start_offset"], r["end_offset"],
                              r["source_text_hash"])
                if not v["ok"]:
                    bad.append({"id": r["id"], "reason": v["reason"]})
            if bad:
                return {
                    "ok": False, "bad": bad, "affected": 0,
                    "message": "有 %d 张卡片与原文对不上，不能标为「已确认」"
                               "（第一张：%s）" % (len(bad), bad[0]["reason"]),
                }

        before, after, valid_ids = {}, {}, []
        for r in rows:
            cid = str(r["id"])
            b = {f: r[f] for f in CARD_FIELDS}
            b["sub_tags"] = _card_tags(conn, r["id"])
            # source 只记进快照、不参与逐字段比对（理由见 CARD_FIELDS 上面的注释）
            b[SOURCE_FIELD] = r[SOURCE_FIELD]
            a = dict(b)
            for f in CARD_FIELDS:
                if f in patch:
                    a[f] = patch[f]
            # 人动了主类 → 这条卡的主类从此算"人定的"。
            #
            # 为什么必须记这一笔：自动分类靠 source 判断"这张卡能不能碰"。
            # 不记的话，人刚手工改对的主类，下一次自动分类又会被覆盖回去，
            # 而且改完看不出是被覆盖的（理由字段会显示 AI 的说法）。
            if "primary_category_id" in patch:
                a[SOURCE_FIELD] = sg.SOURCE_HUMAN
            if "sub_tags" in patch:
                a["sub_tags"] = sorted(
                    {(t or "").strip() for t in (patch["sub_tags"] or []) if (t or "").strip()})
            before[cid] = b
            after[cid] = a
            valid_ids.append(r["id"])

        ts = now_str()
        material_id = rows[0]["material_id"]
        for r in rows:
            cid = str(r["id"])
            a = after[cid]
            conn.execute(
                """UPDATE cards SET primary_category_id=?, status=?, note=?,
                   source=?, updated_at=? WHERE id=? AND owner_id=?""",
                (a["primary_category_id"], a["status"], a["note"],
                 a["source"] or r["source"], ts, r["id"], owner))
            if "sub_tags" in patch:
                _set_card_tags(conn, owner, r["id"], a["sub_tags"],
                               source=sg.SOURCE_HUMAN, confirmed=1)

        change_id = _write_change(
            conn, owner, action_type, operator_id, material_id,
            len(valid_ids), valid_ids, before, after,
            sample_ids=sample_ids, sample_result=sample_result,
            summary=summary or _describe_patch(patch, len(valid_ids)))

        return {"ok": True, "change_id": change_id, "affected": len(valid_ids),
                "affected_card_ids": valid_ids,
                "message": "已修改 %d 张卡片（可撤销）" % len(valid_ids)}


def _describe_patch(patch, n):
    parts = []
    if "primary_category_id" in patch:
        cm = category_name_map()
        parts.append("主类 → %s"
                     % (cm.get(patch["primary_category_id"]) or "（清空）"))
    if "sub_tags" in patch:
        parts.append("副标签 → %s" % ("、".join(patch["sub_tags"] or []) or "（清空）"))
    if "status" in patch:
        parts.append("状态 → %s" % patch["status"])
    if "note" in patch:
        parts.append("备注")
    return "批量修改 %d 张：%s" % (n, "，".join(parts) or "无变化")


def list_changes(owner, limit=50, material_id=None):
    where = ["owner_id=?"]
    params = [owner]
    if material_id:
        where.append("material_id=?")
        params.append(material_id)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM card_changes WHERE " + " AND ".join(where) +
            " ORDER BY id DESC LIMIT ?", params + [limit]).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["affected_card_ids"] = json.loads(d["affected_card_ids"] or "[]")
            d["sample_ids"] = json.loads(d["sample_ids"] or "[]")
            d["before_snapshot"] = json.loads(d["before_snapshot"] or "{}")
            d["after_snapshot"] = json.loads(d["after_snapshot"] or "{}")
            d["undoable"] = bool(d["undoable"]) and not d["undone_at"]
            out.append(d)
        return out


def undo_change(owner, change_id):
    """撤销一次变更。

    关键规则：只恢复「这次改过、且之后没人再动过」的字段。

    为什么必须这么写：
        假设你先批量把 100 张卡的主类设成「神态与动作」，
        然后单独把其中 3 张改成了「对话台词」。
        这时如果你点撤销，那 3 张不该被退回 ——
        因为退回去，等于撤销这一下把她后来的修改也抹掉了。
        所以逐字段比对：当前值 == 本次操作写入的值 → 才恢复。
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM card_changes WHERE id=? AND owner_id=?",
            (change_id, owner)).fetchone()
        if not row:
            return {"ok": False, "message": "没有这条变更记录"}
        if row["undone_at"]:
            return {"ok": False, "message": "这次操作已经撤销过了"}
        if not row["undoable"]:
            return {"ok": False, "message": "这次操作不支持撤销"}

        before = json.loads(row["before_snapshot"] or "{}")
        after = json.loads(row["after_snapshot"] or "{}")
        if not after:
            return {"ok": False, "message": "这次操作没有可恢复的字段（例如切分）"}

        restored = skipped = created_undone = 0
        ts = now_str()
        for cid, a in after.items():
            r = conn.execute(
                "SELECT * FROM cards WHERE id=? AND owner_id=?",
                (int(cid), owner)).fetchone()
            if not r:
                continue

            # ---- 这次操作"新建"出来的卡：撤销 = 把它排除掉 ----
            #
            # 为什么不是删掉：规格要求"不能物理删除，必须可以恢复"。
            # 排除之后它还躺在库里，以后再点一次"恢复"就能回来。
            # 如果不处理这一步，撤销合并就会变成
            # "原来 2 张回来了 + 合并出来的 1 张还在" = 凭空多出一张重复卡。
            if cid not in before:
                if r["status"] != sg.STATUS_EXCLUDED:
                    conn.execute(
                        "UPDATE cards SET status=?, updated_at=? WHERE id=? AND owner_id=?",
                        (sg.STATUS_EXCLUDED, ts, int(cid), owner))
                    created_undone += 1
                continue

            b = before.get(cid, {})
            sets, params, rolled = [], [], []
            for f in CARD_FIELDS:
                cur = r[f]
                if f in a and cur == a[f] and f in b and b[f] != a[f]:
                    rolled.append(f)
                    sets.append("%s = ?" % f)
                    params.append(b[f])

            # ---- 来源标记：跟着主类一起退 ----
            #
            # 只有在"主类真的被退回去了"、并且当前来源仍是本次写进去的那个值、
            # 而且本来和它不一样的时候，才把来源也退回去。
            # 这样做的目的：主类退回原样了、来源却留着"人工"标记，
            # 会让这张卡以后再也不被自动分类碰（标记和事实不一致）；
            # 反过来，主类留在她改的值、来源却退回继承，
            # 又会让 AI 下次把她改的主类覆盖掉。两种错配都不行。
            if ("primary_category_id" in rolled
                    and SOURCE_FIELD in b
                    and r[SOURCE_FIELD] == a.get(SOURCE_FIELD)
                    and b[SOURCE_FIELD] != a.get(SOURCE_FIELD)):
                rolled.append(SOURCE_FIELD)
                sets.append("%s = ?" % SOURCE_FIELD)
                params.append(b[SOURCE_FIELD])

            touched = bool(sets)

            # 副标签：当前值仍等于本次写入值，才恢复
            if "sub_tags" in a:
                cur_tags = sorted(_card_tags(conn, r["id"]))
                if cur_tags == sorted(a.get("sub_tags") or []) and \
                        cur_tags != sorted(b.get("sub_tags") or []):
                    _set_card_tags(conn, owner, r["id"],
                                   b.get("sub_tags") or [])
                    touched = True

            if touched:
                if sets:
                    sets.append("updated_at = ?")
                    params.append(ts)
                    params.extend([int(cid), owner])
                    conn.execute("UPDATE cards SET %s WHERE id=? AND owner_id=?"
                                 % ", ".join(sets), params)
                restored += 1
            else:
                skipped += 1

        conn.execute("UPDATE card_changes SET undone_at=? WHERE id=?",
                     (ts, change_id))

        msg = "已撤销，恢复 %d 张" % restored
        if created_undone:
            msg += "；撤掉本次新建的 %d 张（已标为排除，可恢复）" % created_undone
        if skipped:
            msg += "；%d 张因为你后来又改过，保持原样没有动" % skipped
        return {"ok": True, "restored": restored, "skipped": skipped,
                "created_undone": created_undone, "message": msg}


# ----------------------------------------------------------------------
# 拆分 / 合并 / 排除
# ----------------------------------------------------------------------

def split_card(card_id, owner, cuts, operator_id=None):
    """把一张卡拆成几张。

    cuts 是"从哪里断开"的位置列表，用原文里的绝对偏移表示。
    例：一张卡覆盖 [100, 200)，传 cuts=[150] → 变成 [100,150) 和 [150,200) 两张。

    原卡不删，状态改成「已排除」——
    这样既不会在列表里重复出现，又随时能恢复，也留下了"它被拆过"的痕迹。
    """
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM cards WHERE id=? AND owner_id=?",
                           (card_id, owner)).fetchone()
        if not row:
            return None
        content = _content(conn, row["material_id"])

        s, e = row["start_offset"], row["end_offset"]
        cuts = sorted({int(c) for c in (cuts or []) if s < int(c) < e})
        if not cuts:
            return {"ok": False, "message": "没有有效的分割点（必须在卡片内部）"}

        bounds = [s] + cuts + [e]
        ranges = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]

        ts = now_str()
        gid = new_group_id()
        new_ids, before, after = [], {}, {}

        before[str(row["id"])] = {"status": row["status"],
                                  "primary_category_id": row["primary_category_id"],
                                  "sub_tags": _card_tags(conn, row["id"])}

        conn.execute("UPDATE cards SET status=?, updated_at=? WHERE id=?",
                     (sg.STATUS_EXCLUDED, ts, card_id))
        after[str(row["id"])] = {"status": sg.STATUS_EXCLUDED,
                                 "primary_category_id": row["primary_category_id"],
                                 "sub_tags": _card_tags(conn, card_id)}

        # 新卡继承原卡的来源，但状态回到「待确认」——
        # 因为切法变了，主类得重新看一眼。
        for a, b in ranges:
            piece = content[a:b]
            cur = conn.execute(
                """INSERT INTO cards
                   (owner_id, material_id, start_offset, end_offset,
                    source_text_hash, segment_id, primary_category_id, source,
                    status, note, parent_card_id, operation_group_id,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (owner, row["material_id"], a, b, sg.text_hash(piece),
                 row["segment_id"], row["primary_category_id"],
                 sg.SOURCE_HUMAN, sg.STATUS_PENDING, row["note"],
                 card_id, gid, ts, ts))
            nid = cur.lastrowid
            # 副标签继承
            _set_card_tags(conn, owner, nid, _card_tags(conn, card_id))
            new_ids.append(nid)
            # 新卡只出现在 after 里、不出现在 before 里 ——
            # 撤销时看到"有 after 没 before"就知道这是本次新建的，
            # 会把它标为排除（而不是留下变成重复卡）。
            after[str(nid)] = {}

        change_id = _write_change(
            conn, owner, "split", operator_id, row["material_id"],
            len(new_ids) + 1, [card_id] + new_ids, before, after,
            summary="拆分卡片 #%d → %d 张（原卡已排除，可恢复）"
                    % (card_id, len(new_ids)))
        return {"ok": True, "change_id": change_id, "group_id": gid,
                "new_card_ids": new_ids,
                "message": "已拆成 %d 张，原卡已排除（可恢复）" % len(new_ids)}


def merge_cards(card_ids, owner, operator_id=None):
    """把几张相邻的卡合并成一张。

    限制：必须真的相邻 —— 中间夹着别的卡片就不让合。
    理由：合并后的区间必须是连续的一段原文。
    如果中间隔着一张别的卡，硬合会把它一并吞掉，那张卡就凭空消失了。
    """
    ids = list(dict.fromkeys(int(i) for i in (card_ids or [])))
    if len(ids) < 2:
        return {"ok": False, "message": "至少要选两张卡才能合并"}

    with db.connect() as conn:
        rows = _load_cards(conn, owner, ids)
        if len(rows) < 2:
            return {"ok": False, "message": "选中的卡片里有不存在或不属于你的"}
        mids = {r["material_id"] for r in rows}
        if len(mids) != 1:
            return {"ok": False, "message": "不同文件的卡片不能合并"}
        material_id = mids.pop()

        rows = sorted(rows, key=lambda r: r["start_offset"])
        start = rows[0]["start_offset"]
        end = max(r["end_offset"] for r in rows)

        inner = conn.execute(
            """SELECT COUNT(*) FROM cards WHERE owner_id=? AND material_id=?
               AND id NOT IN (%s)
               AND start_offset < ? AND end_offset > ?
               AND status <> ?"""
            % ",".join("?" * len(ids)),
            [owner, material_id] + ids + [end, start,
                                          sg.STATUS_EXCLUDED]).fetchone()[0]
        if inner:
            return {"ok": False,
                    "message": "这几张卡中间还夹着 %d 张别的卡片，不能合并"
                               "（会把它们一起吞掉）。请先只选相邻的。" % inner}

        content = _content(conn, material_id)
        ts = now_str()
        gid = new_group_id()
        before, after = {}, {}

        main = rows[0]
        for r in rows:
            before[str(r["id"])] = {"status": r["status"],
                                    "primary_category_id": r["primary_category_id"],
                                    "sub_tags": _card_tags(conn, r["id"])}
            conn.execute("UPDATE cards SET status=?, updated_at=? WHERE id=?",
                         (sg.STATUS_EXCLUDED, ts, r["id"]))
            after[str(r["id"])] = {"status": sg.STATUS_EXCLUDED,
                                   "primary_category_id": r["primary_category_id"],
                                   "sub_tags": _card_tags(conn, r["id"])}

        piece = content[start:end]
        cur = conn.execute(
            """INSERT INTO cards
               (owner_id, material_id, start_offset, end_offset, source_text_hash,
                segment_id, primary_category_id, source, status, note,
                parent_card_id, operation_group_id, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (owner, material_id, start, end, sg.text_hash(piece),
             main["segment_id"], main["primary_category_id"], sg.SOURCE_HUMAN,
             sg.STATUS_PENDING, "", main["id"], gid, ts, ts))
        new_id = cur.lastrowid
        # 副标签取所有参与卡的并集
        union = sorted({t for r in rows for t in _card_tags(conn, r["id"])})
        _set_card_tags(conn, owner, new_id, union)
        # 同上：新卡只在 after 里
        after[str(new_id)] = {}

        change_id = _write_change(
            conn, owner, "merge", operator_id, material_id,
            len(rows) + 1, [r["id"] for r in rows] + [new_id], before, after,
            summary="合并 %d 张卡片 → #%d（原卡已排除，可恢复）"
                    % (len(rows), new_id))
        return {"ok": True, "change_id": change_id, "group_id": gid,
                "card_id": new_id,
                "message": "已合并成 1 张（#%d），原 %d 张已排除（可恢复）"
                           % (new_id, len(rows))}


def restore_segments_as_cards(run_id, owner, segment_ids=None, operator_id=None):
    """把被自动排除的噪音段恢复成卡片。

    规格要求"识别后只做默认排除提示，不自动删除。真实内容仍然可以恢复"——
    就是这个函数。
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM segment_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return {"ok": False, "message": "没有这个切分任务"}
        m = _material_row(conn, row["material_id"], owner)
        if not m:
            return {"ok": False, "message": "这个文件不属于你"}
        content = m["content"]

        where = ["run_id=?", "excluded=1"]
        params = [run_id]
        if segment_ids:
            where.append("id IN (%s)" % ",".join("?" * len(segment_ids)))
            params.extend(segment_ids)
        segs = conn.execute("SELECT * FROM segments WHERE " + " AND ".join(where),
                            params).fetchall()
        if not segs:
            return {"ok": False, "message": "没有可恢复的段落"}

        ts = now_str()
        n = 0
        existing = [(r["start_offset"], r["end_offset"]) for r in conn.execute(
            "SELECT start_offset, end_offset FROM cards WHERE material_id=? AND owner_id=?",
            (row["material_id"], owner)).fetchall()]
        new_ids = []
        for s in segs:
            if any(s["start_offset"] < e and a < s["end_offset"]
                   for a, e in existing):
                continue
            cur = conn.execute(
                """INSERT INTO cards
                   (owner_id, material_id, start_offset, end_offset,
                    source_text_hash, segment_id, source, status,
                    operation_group_id, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (owner, s["material_id"], s["start_offset"], s["end_offset"],
                 s["source_text_hash"], s["id"], sg.SOURCE_HUMAN,
                 sg.STATUS_PENDING, "", ts, ts))
            new_ids.append(cur.lastrowid)
            existing.append((s["start_offset"], s["end_offset"]))
            conn.execute("UPDATE segments SET excluded=0 WHERE id=?", (s["id"],))
            n += 1

        change_id = _write_change(
            conn, owner, "restore_noise", operator_id, row["material_id"],
            n, new_ids, {}, {str(i): {} for i in new_ids},
            summary="恢复 %d 条被排除的噪音段为卡片" % n)
        return {"ok": True, "change_id": change_id, "restored": n,
                "card_ids": new_ids,
                "message": "已恢复 %d 条" % n}


# ----------------------------------------------------------------------
# 来源批量采纳 / 抽样
# ----------------------------------------------------------------------

def all_card_ids(owner, material_id=None, source_collection=None,
                 include_excluded=False):
    """取符合条件的全部卡片 id（不分页）。

    为什么需要它：批量采纳要一次改几百张卡，
    如果先用 list_cards(limit=500) 拿 id，卡片一超过 500 就会漏掉一半 ——
    这种错很难发现（界面显示"已修改 500 张"，看着挺正常）。
    所以批量操作必须有一个"老老实实数完"的取法。
    """
    where = ["c.owner_id=?"]
    params = [owner]
    if not include_excluded:
        where.append("c.status<>?")
        params.append(sg.STATUS_EXCLUDED)
    if material_id:
        where.append("c.material_id=?")
        params.append(material_id)
    if source_collection:
        where.append("""c.material_id IN (SELECT id FROM materials
                        WHERE owner_id=? AND source_collection=?)""")
        params.extend([owner, source_collection])
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT c.id FROM cards c WHERE " + " AND ".join(where) +
            " ORDER BY c.material_id, c.start_offset", params).fetchall()
        return [r["id"] for r in rows]


def sample_cards(owner, material_id, source_collection=None, size=10, seed=None):
    """按规格抽固定样本：随机 + 最长 + 最短 + 异常 + 疑似重复。

    为什么不能只随机抽：
        随机抽 10 条，很可能恰好抽不到"最长的那条"（而最长的往往最容易出问题），
        也抽不到重复项。所以样本要"刻意不均匀"。

    返回 {"ids": [...], "items": [...], "composition": {...}}
    """
    import random
    rnd = random.Random(seed)

    where = ["c.owner_id=?", "c.status<>?"]
    params = [owner, sg.STATUS_EXCLUDED]
    if material_id:
        where.append("c.material_id=?")
        params.append(material_id)
    elif source_collection:
        where.append("""c.material_id IN (SELECT id FROM materials
                        WHERE owner_id=? AND source_collection=?)""")
        params.extend([owner, source_collection])

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT c.* FROM cards c WHERE " + " AND ".join(where) +
            " ORDER BY c.start_offset", params).fetchall()
        if not rows:
            return {"ids": [], "items": [], "composition": {}}
        content_cache = {}
        cat_names = category_name_map()
        items = []
        for r in rows:
            mid = r["material_id"]
            if mid not in content_cache:
                content_cache[mid] = _content(conn, mid)
            items.append(_decorate(conn, r, content_cache[mid], cat_names))

    comp = {}
    picked = []

    def take(item, kind):
        if item["id"] in {p["id"] for p in picked}:
            return
        picked.append(item)
        comp[kind] = comp.get(kind, 0) + 1

    by_len = sorted(items, key=lambda x: x["chars"])
    if by_len:
        take(by_len[-1], "最长")
        take(by_len[0], "最短")

    # 异常项：含数字/表格残留/极短/含引号比例异常
    for x in items:
        t = x["text"]
        if "Sheet" in t or "工作表" in t or any(ch.isdigit() for ch in t):
            take(x, "含数字或表格残留")
            break
    for x in items:
        if x["chars"] <= 8:
            take(x, "极短")
            break

    # 疑似重复：先真做一次近重复检测，把撞上的那两条抽进来。
    # 为什么这么绕：光看"两段文字一模一样"是不够的 ——
    # 实测这份文件里没有完全一样的两条，但有一条把另一条包住的情况。
    # 用和"重复提示"同一个函数，样本才和界面上看到的一致。
    dup_pairs = sg.find_duplicates([{"id": x["id"], "text": x["text"]}
                                    for x in items], max_pairs=5)
    if dup_pairs:
        for pid in (dup_pairs[0]["a_id"], dup_pairs[0]["b_id"]):
            for x in items:
                if x["id"] == pid:
                    take(x, "疑似重复")
                    break

    # 剩下用随机补齐
    pool = [x for x in items if x["id"] not in {p["id"] for p in picked}]
    rnd.shuffle(pool)
    while len(picked) < size and pool:
        take(pool.pop(), "随机")

    # 最后把序号排一遍，保证样本按原文顺序显示
    picked.sort(key=lambda x: (x["material_id"], x["start_offset"]))
    return {"ids": [p["id"] for p in picked],
            "items": picked[:size],
            "composition": comp,
            "pool_size": len(items)}


# ----------------------------------------------------------------------
# 近重复提示
# ----------------------------------------------------------------------

def duplicates(owner, material_id=None, source_collection=None, limit=200):
    """找近重复。只提示，不自动处理。"""
    where = ["c.owner_id=?", "c.status<>?"]
    params = [owner, sg.STATUS_EXCLUDED]
    if material_id:
        where.append("c.material_id=?")
        params.append(material_id)
    elif source_collection:
        where.append("""c.material_id IN (SELECT id FROM materials
                        WHERE owner_id=? AND source_collection=?)""")
        params.extend([owner, source_collection])

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT c.id, c.material_id, m.title AS material_title,"
            " c.start_offset, c.end_offset"
            " FROM cards c JOIN materials m ON m.id=c.material_id"
            " WHERE " + " AND ".join(where) + " ORDER BY c.material_id, c.start_offset",
            params).fetchall()
        cache, items = {}, []
        for r in rows:
            mid = r["material_id"]
            if mid not in cache:
                cache[mid] = _content(conn, mid)
            items.append({
                "id": r["id"], "text": cache[mid][r["start_offset"]:r["end_offset"]],
                "material_id": mid, "material_title": r["material_title"],
                "start": r["start_offset"], "end": r["end_offset"],
            })

    pairs = sg.find_duplicates(items, max_pairs=limit)
    by_id = {x["id"]: x for x in items}
    for p in pairs:
        for side in ("a", "b"):
            src = by_id.get(p["%s_id" % side], {})
            p["%s_material" % side] = src.get("material_title", "")
            p["%s_start" % side] = src.get("start")
            p["%s_end" % side] = src.get("end")
    return {"total": len(pairs), "items": pairs,
            "note": "这些只是提示，系统不会自动删或自动合并 —— "
                    "两条相似素材可能一条是完整段、一条是能单独用的短句，"
                    "两个都该留，得你自己判断。"}


def overview(owner):
    """素材分类库的总览数字，页面顶部的统计条用。"""
    with db.connect() as conn:
        def one(sql, *p):
            return conn.execute(sql, p).fetchone()[0]
        n_cards = one("SELECT COUNT(*) FROM cards WHERE owner_id=?", owner)
        n_pending = one("SELECT COUNT(*) FROM cards WHERE owner_id=? AND status=?",
                        owner, sg.STATUS_PENDING)
        n_confirmed = one("SELECT COUNT(*) FROM cards WHERE owner_id=? AND status=?",
                          owner, sg.STATUS_CONFIRMED)
        n_excluded = one("SELECT COUNT(*) FROM cards WHERE owner_id=? AND status=?",
                         owner, sg.STATUS_EXCLUDED)
        n_nomcat = one("SELECT COUNT(*) FROM cards WHERE owner_id=? "
                       "AND primary_category_id IS NULL AND status<>?",
                       owner, sg.STATUS_EXCLUDED)
        n_materials = one("SELECT COUNT(*) FROM materials WHERE owner_id=?", owner)
        n_split = one("""SELECT COUNT(DISTINCT r.material_id) FROM segment_runs r
                         JOIN materials m ON m.id = r.material_id
                         WHERE m.owner_id=?""", owner)
        n_changes = one("SELECT COUNT(*) FROM card_changes WHERE owner_id=?",
                        owner)
        # 被自动判为噪音、从而没有生成卡片的段（界面上要能一次点"全部恢复"）
        n_noise_seg = one(
            """SELECT COUNT(*) FROM segments s
               JOIN segment_runs r ON r.id = s.run_id
               JOIN materials m    ON m.id = s.material_id
               WHERE m.owner_id=? AND s.excluded=1""", owner)
    return {"cards": n_cards, "pending": n_pending, "confirmed": n_confirmed,
            "excluded": n_excluded, "no_category": n_nomcat,
            "materials": n_materials, "split_files": n_split,
            "noise_segments": n_noise_seg,
            "changes": n_changes}


def material_map(owner):
    """素材 id → 标题 / 来源名。列表页一次性取走，省得每条都查库。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, title, source_collection, chars FROM materials WHERE owner_id=?",
            (owner,)).fetchall()
        return {r["id"]: {"title": r["title"],
                          "source_collection": r["source_collection"] or r["title"],
                          "chars": r["chars"]} for r in rows}


# ----------------------------------------------------------------------
# 命令行自测：python backend/classify_db.py
# ----------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    rep = migrate(verbose=True)
    print()
    print("表：", rep["tables"])
    print()
    print("系统自带的主类：")
    for c in list_categories():
        print("  %-8s %s" % (c["name"], c["description"][:44]))
    print()
    owners = rep.get("owners") or []
    for o in owners:
        print("账号 %s 的副标签：%s" % (o, [t["name"] for t in list_sub_tags(o)]))
        print("账号 %s 的来源映射：" % o)
        for m in list_source_mappings(o):
            print("   %-12s → %-14s [%s] %s"
                  % (m["source_collection"], m["category_name"] or "（无建议）",
                     "已确认" if m["confirmed"] else "未确认", m["note"][:40]))
