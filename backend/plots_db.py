# -*- coding: utf-8 -*-
"""
墨阁 · 剧情内化库（数据层）
========================================================
这个文件管的是"剧情内化库"这一整块的数据库部分：
剧情零件（plots）、零件与素材卡片的关系（plot_cards）、版本历史（plot_versions）。

它和已有的三层分工：

    db.py           账号、素材文件（materials）、旧标签
    classify_db.py  切分、卡片、主类/副标签、变更记录   ← 素材分类库
    classification.py 自动分类任务、提示词库、AI 学习
    plots_db.py     剧情零件、版本、来源关系            ← 本文件（新）

为什么不塞进 classify_db.py：
    那个文件已经 2200 行。再加 1000 行会变成 3300 行，
    以后找一个函数要翻半天。而且这两层的**生命周期不一样** ——
    素材分类库是地基（改了要整容迁移），剧情内化库是上层建筑，
    将来要整体重做，只动这一个文件就行。

========================================================
四条设计底线
========================================================

一、零件的来源是"卡片"，不是"段落"
    沿用素材库已经定死的那条规矩：**段落可以抛弃，偏移不可以抛弃**。
    零件 → plot_cards → 卡片自带的 material_id + 起止偏移
        → 现算 materials.content[start:end] 得到原文。
    零件和卡片都**不存正文副本**，世上只有一个真相（materials.content）。

二、一条零件可以来自多张卡片（多对多）
    她原话是"选中一段**或几段**"交给 AI 内化。
    所以"点零件定位回原剧情"是**展开一串来源、逐条可点开**，
    不是跳到某一个位置 —— 本来就不止一个位置。
    真正的来源关系**只存在 plot_cards 里**，不许在别处再存一份指向关系。

三、改内容一定产生新版本，永不覆盖
    她改零件不是一次性的审核动作，而是用很久以后还会回来改。
    所以每次保存都新建一条 plot_versions，旧版本只读保留。
    plots 表上那份正文是"当前版本"的**冗余副本**（为了让列表页不用 JOIN），
    和 plot_versions 必须在**同一个事务里双写** ——
    只写一处就会出现"列表显示的和版本历史里最新那条不一样"。

四、排除是状态，不是删除
    零件被排除之后，引用过它的大纲（将来的功能）还得认得它。
    而且她可能是手滑点错了。所以只有"排除/恢复"，没有物理删除。

========================================================
这一轮（第 1 步）建的是三张表
========================================================
    plots          剧情零件
    plot_cards     零件 ←→ 卡片
    plot_versions  版本历史

第 2 步才会建 plot_runs / plot_run_cards / plot_items / plot_candidates
（AI 内化任务与多模型候选），第 5 步建 plot_feedback（延迟反馈学习）。
本文件里凡是依赖那几张表的函数都写了"表不存在就跳过"的判断，
所以现在就能跑，等第 2 步建完表自动生效。
"""

import json
import os

try:
    from backend import db
    from backend import classify_db as cdb
    from backend import segmentation as sg
except ImportError:                                   # pragma: no cover
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from backend import db
    from backend import classify_db as cdb
    from backend import segmentation as sg


# 时间格式全项目只有一种，直接借过来，别在这边再定义一个
now_str = cdb.now_str


# ----------------------------------------------------------------------
# 一、常量（唯一定义处 —— 加值只改这里）
# ----------------------------------------------------------------------

# 零件状态。
#
# 【为什么存中文，不存英文】
#   任务书给的是英文常量（ai_suggested / needs_review / …），
#   但现有的 cards.status 存的就是中文（待确认 / 已确认 / 已排除 / 分类失败），
#   而她是要**自己在 SQLite 里查库**的人。两套风格混着，
#   她查一次库要在脑子里翻译一道。
#   上面那个括号里是跟任务书的英文名对照，改代码时按这个对。
PLOT_STATUS_AI_SUGGESTED = "待确认"        # ai_suggested   AI 提的，等你看
PLOT_STATUS_NEEDS_REVIEW = "待处理"        # needs_review   AI 拿不准，等你定
PLOT_STATUS_CONFIRMED = "已确认"           # human_confirmed
PLOT_STATUS_EDITED = "已编辑"              # human_edited
PLOT_STATUS_UNUSED = "暂不用"              # temporarily_unused
PLOT_STATUS_EXCLUDED = "已排除"            # excluded

# 这条元组是全项目**唯一**的零件状态清单。
# 前端不写死任何状态名 —— 接口把这份清单发下去，界面上是渲染出来的。
# （沿用素材那边"状态轴只在一处定义"的规矩，见 segmentation.py::ALL_STATUS）
ALL_PLOT_STATUS = (
    PLOT_STATUS_AI_SUGGESTED,
    PLOT_STATUS_NEEDS_REVIEW,
    PLOT_STATUS_CONFIRMED,
    PLOT_STATUS_EDITED,
    PLOT_STATUS_UNUSED,
    PLOT_STATUS_EXCLUDED,
)

# 零件是谁提的。
#
# 【为什么这个用英文】existing cards.source 就是 'human' / 'ai' 这种英文，
# 跟它保持一致。它是个内部标记，界面上显示成"AI 提的 / 你写的"由前端做。
PLOT_SOURCE_AI = "ai"
PLOT_SOURCE_HUMAN = "human"
PLOT_SOURCE_AI_EDITED = "ai_edited"
ALL_PLOT_SOURCE = (PLOT_SOURCE_AI, PLOT_SOURCE_HUMAN, PLOT_SOURCE_AI_EDITED)

SOURCE_LABELS = {
    PLOT_SOURCE_AI: "AI 提的",
    PLOT_SOURCE_HUMAN: "你写的",
    PLOT_SOURCE_AI_EDITED: "AI 提的，你改过",
}

# 剧情类型。可以由她自建的性质不设 —— 这一版是固定清单，
# 值存中文（界面上直接显示、她查库直接看得懂）。
PLOT_TYPES = (
    "冲突", "关系推进", "误会", "反转", "保护救援", "牺牲",
    "发现", "谈判", "情绪转折", "其他",
)

# 适合放在大纲的什么位置
USAGE_HINTS = (
    "初遇", "关系升温", "冲突升级", "情绪转折", "真相揭露",
    "危机处理", "收束", "结尾反转", "其他",
)

# beats：剧情结构化的七个点位。
#
# 【为什么键用英文、值存中文】任务书给的就是这个结构
# （{"setup": "前提", …}）。键是纯程序结构，不会给她看；
# 界面上显示的是 BEAT_LABELS 里那套中文标签，由接口下发。
# 这样将来想改措辞（"前提"→"起手"）不用动任何一行数据。
#
# 【为什么是七个不是六个】2026-09-25 补的 "motivation"。
#   她的剧情内化提示词（prompts/infuse.txt）里 beats 就是七个：
#   setup / trigger / action / **motivation** / conflict / turn / result。
#   而 _clean_beats 只留存在于 BEAT_KEYS 里的键 —— 不补这一格的话，
#   AI 判出来的「行动动机／隐藏目的」会被**静默丢掉**，
#   她看到的零件会永远少一块，还查不出是谁弄丢的。
#   顺序按叙事逻辑排：动机紧跟在行动后面。
BEAT_KEYS = ("setup", "trigger", "action", "motivation",
             "conflict", "turn", "result")

BEAT_LABELS = {
    "setup": "前提",
    "trigger": "触发事件",
    "action": "关键行动",
    "motivation": "动机",
    "conflict": "冲突或阻碍",
    "turn": "转折",
    "result": "结果",
}

# 长度上限。校验在数据层，接口层把 ValueError 转成 400。
# 【为什么要卡上限】零件正文是要**整段发给模型**的（以后做大纲生成时）。
# 一条两万字的"摘要"传上去，费用和延迟都不可控，而且它已经不是摘要了。
TITLE_MAX = 60
SUMMARY_MAX = 2000
ITEM_MAX = 20               # 单个剧情类型 / 使用场景 / 角色位的长度
ROLE_SLOTS_MAX = 8          # 最多几个角色位
USAGE_HINTS_MAX = 5         # 一条零件最多挂几个使用场景
NOTE_MAX = 500              # "为什么改" 的说明


# ----------------------------------------------------------------------
# 二、建表
# ----------------------------------------------------------------------

SCHEMA = """
-- 剧情零件：把一段具体素材提炼成"换个角色名还成立"的通用剧情。
--
-- 【为什么正文在 plots 里也存一份】
--   列表页要显示几百条零件的标题和摘要。如果正文只存在 plot_versions，
--   列表就要按 current_version_id 每条 JOIN 一次版本表。
--   代价是"两处都存"，所以写入时**必须在同一个事务里双写**（见 _write_version）。
--
-- 【ai_reason / ai_confidence 只对 AI 提的有值】
--   她以后回头看时能知道"这条当初是怎么来的"，以及 AI 当时有多大把握。
--   摊在界面上，不藏起来。
CREATE TABLE IF NOT EXISTS plots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id            TEXT    NOT NULL,
    title               TEXT    NOT NULL DEFAULT '',
    summary             TEXT    NOT NULL DEFAULT '',
    plot_type           TEXT    NOT NULL DEFAULT '',
    usage_hint_json     TEXT    NOT NULL DEFAULT '[]',
    beats_json          TEXT    NOT NULL DEFAULT '{}',
    role_slots_json     TEXT    NOT NULL DEFAULT '[]',
    primary_category_id INTEGER DEFAULT NULL,
    source              TEXT    NOT NULL DEFAULT 'human',
    status              TEXT    NOT NULL DEFAULT '已确认',
    current_version_id  INTEGER DEFAULT NULL,
    ai_reason           TEXT    NOT NULL DEFAULT '',
    ai_confidence       REAL    DEFAULT NULL,
    created_by          TEXT    NOT NULL DEFAULT '',
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);

-- 零件 ←→ 素材卡片。**真正的来源关系只存在这张表里。**
--
-- 【为什么带四列冗余快照】
--   cards 表里已经有 material_id + 起止偏移，按理说 JOIN 一下就有。
--   冗余的理由是两条：
--     1. 显示来源清单时省一次 JOIN（零件详情页每次都要列来源）
--     2. **能校验"偏移处的文字还是不是当初那段"** ——
--        卡片被拆分/合并之后，原卡会被标成"已排除"、新卡另立，
--        存着快照才能发现"这条来源的原文已经不在了"，
--        而不是悄悄给她看一段错的内容。
--   这和 cards 自己带 source_text_hash 是同一个理由：
--   引用完整性不能只靠一个会变的 id。
--
-- 【为什么主键是 (plot_id, card_id)】
--   同一张卡不能在同一条零件里出现两次（任务书第九节第 6 条）。
--   而"一张卡出现在多条零件里"是这个主键天然允许的（多对多）。
CREATE TABLE IF NOT EXISTS plot_cards (
    plot_id          INTEGER NOT NULL,
    card_id          INTEGER NOT NULL,
    material_id      INTEGER NOT NULL,
    start_offset     INTEGER NOT NULL DEFAULT 0,
    end_offset       INTEGER NOT NULL DEFAULT 0,
    source_text_hash TEXT    NOT NULL DEFAULT '',
    created_at       TEXT    NOT NULL,
    PRIMARY KEY (plot_id, card_id)
);

-- 版本历史。只读保留，永不物理删。
--
-- 【为什么编辑一次就建一行，而不是 UPDATE】
--   她改零件不是"审一遍就完"，而是用很久以后还会回来改。
--   覆盖掉旧版本，就等于把"当初 AI 是怎么写的""我为什么改"一起删了 ——
--   而这两样恰恰是以后判断"哪版提示词更好"的唯一依据。
--
-- 【恢复历史版本也是"新建一个版本"，不是回退指针】
--   见 restore_version()：把 v1 恢复出来会产生 v3（内容同 v1），
--   v1 / v2 一条不动。这样"什么时候恢复过什么"也留了痕。
--
-- model_id / prompt_version 只有 AI 生成或 AI 改的版本才有值，
-- 手工编辑的留空 —— 界面上一眼能看出这个版本是人改的还是机器写的。
CREATE TABLE IF NOT EXISTS plot_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    plot_id         INTEGER NOT NULL,
    version_no      INTEGER NOT NULL,
    title           TEXT    NOT NULL DEFAULT '',
    summary         TEXT    NOT NULL DEFAULT '',
    plot_type       TEXT    NOT NULL DEFAULT '',
    usage_hint_json TEXT    NOT NULL DEFAULT '[]',
    beats_json      TEXT    NOT NULL DEFAULT '{}',
    role_slots_json TEXT    NOT NULL DEFAULT '[]',
    editor_type     TEXT    NOT NULL DEFAULT 'human',
    model_id        TEXT    NOT NULL DEFAULT '',
    prompt_version  TEXT    NOT NULL DEFAULT '',
    change_note     TEXT    NOT NULL DEFAULT '',
    created_by      TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL,
    UNIQUE (plot_id, version_no)
);

CREATE INDEX IF NOT EXISTS idx_plots_own      ON plots(owner_id, id);
CREATE INDEX IF NOT EXISTS idx_plots_cat      ON plots(primary_category_id);
CREATE INDEX IF NOT EXISTS idx_plots_status   ON plots(status);
CREATE INDEX IF NOT EXISTS idx_plotcards_card ON plot_cards(card_id);
CREATE INDEX IF NOT EXISTS idx_plotcards_mat  ON plot_cards(material_id);
CREATE INDEX IF NOT EXISTS idx_plotver_plot   ON plot_versions(plot_id, version_no);
"""


def migrate(verbose=False):
    """建表。反复调用是安全的（全是 IF NOT EXISTS）。

    【这一轮为什么不需要整库备份】
   上一轮改 categories 时备份，是因为那条路要**改已经写死的 UNIQUE 约束** ——
   只能"建新表 → 搬数据 → 删旧表 → 改名"，中途出错就是数据没了。
    这一轮纯粹是**新增三张空表**，一个 ALTER 都没有、一行已有数据都不碰。
    所以幂等执行即可，热重载触发多少次都一样。
    """
    with db.connect() as conn:
        conn.executescript(SCHEMA)
    if verbose:
        print("剧情内化库：三张表就位 →", db.DB_PATH)
    return db.DB_PATH


# ----------------------------------------------------------------------
# 三、小工具
# ----------------------------------------------------------------------

def _columns(conn, table):
    """看一张表现在有哪些列。"""
    return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}


def _has_table(conn, table):
    """这张表建了没有。第 2 步那几张表现在还没有，靠它优雅跳过。"""
    return bool(conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone())


def _dumps(v):
    return json.dumps(v, ensure_ascii=False)


def _loads(s, fallback):
    """把 JSON 列解出来。解不开就给兜底值 —— 手工改坏过的库不该让整页崩掉。"""
    if not s:
        return fallback
    try:
        v = json.loads(s)
    except Exception:
        return fallback
    return v if isinstance(v, type(fallback)) else fallback


def _clean_text(v, limit, name):
    """一段纯文本：去两端空白、卡长度。超了直接报错，不静默截断。

    【为什么不静默截断】她填 300 字被悄悄砍成 60 字，
    界面上还是显示"保存成功"，她要过很久才会发现内容少了。
    """
    s = (v or "").strip()
    if len(s) > limit:
        raise ValueError("%s最长 %d 个字，现在有 %d 个。" % (name, limit, len(s)))
    return s


def _clean_list(v, allowed, limit, name, item_max=ITEM_MAX):
    """一组标签：去空白、去重、保住顺序、逐个卡长度、可选地按白名单过滤。"""
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple)):
        raise ValueError("%s得是一组文字，不是一个「%s」。" % (name, type(v).__name__))
    out = []
    for x in v:
        s = str(x).strip()
        if not s:
            continue
        if len(s) > item_max:
            raise ValueError("%s里的「%s…」太长了（最长 %d 个字）。"
                             % (name, s[:10], item_max))
        if allowed is not None and s not in allowed:
            raise ValueError("%s里没有「%s」这一项。" % (name, s))
        if s not in out:
            out.append(s)
    if len(out) > limit:
        raise ValueError("%s最多 %d 个，现在填了 %d 个。" % (name, limit, len(out)))
    return out


def _clean_beats(v):
    """beats 只留固定那六个键，值一律转成字符串。

    【为什么要过滤键】模型返回 JSON 时经常多加几个自己想出来的键
    （"climax"、"note"…）。放进去的话界面上会出现一行没标签的空框，
    而她不知道那是什么。不认识的一律丢掉。
    """
    if not v or not isinstance(v, dict):
        return {}
    out = {}
    for k in BEAT_KEYS:
        s = str(v.get(k, "") or "").strip()
        if s:
            out[k] = _clean_text(s, SUMMARY_MAX, "「%s」那一段" % BEAT_LABELS[k])
    return out


def _row_to_plot(row):
    """数据库一行 → 界面上要用的字典。

    在哪一层把 JSON 解开？选在这里，理由：
    接口层和前端都不用再判一次"这是字符串还是数组"，
    少一处判断就少一个出错的地方。
    """
    if row is None:
        return None
    d = dict(row)
    d["usage_hints"] = _loads(d.pop("usage_hint_json", "[]"), [])
    d["beats"] = _loads(d.pop("beats_json", "{}"), {})
    d["role_slots"] = _loads(d.pop("role_slots_json", "[]"), [])
    d["source_label"] = SOURCE_LABELS.get(d.get("source"), d.get("source") or "")
    return d


def _visible_category_ids(conn, owner):
    """这个账号能用的主类 id：系统共用那批 + 他自己建的。

    为什么要这个：零件可以挂主类，而她可能挂到"别人的私人主类 id"上
    （id 是连号猜得出来的）。不校验的话，别人私人类的名字会从零件上漏出来。
    """
    rows = conn.execute(
        "SELECT id FROM categories WHERE set_version=? "
        "AND (owner_id='' OR owner_id=?)",
        (cdb.CATEGORY_SET_VERSION, owner)).fetchall()
    return {r["id"] for r in rows}


# ----------------------------------------------------------------------
# 四、写版本（内部用）
# ----------------------------------------------------------------------

def _write_version(conn, plot_id, data, editor_type="human",
                   model_id="", prompt_version="", change_note="",
                   created_by=""):
    """给某个零件写一条新版本，并把 plots 上的"当前版本"同步过去。

    【这个函数存在的唯一理由：双写必须在一起】
    plots 里那份正文和 plot_versions 里最新的一条是同一份内容的两个位置。
    两处分别写的话，中间任何一步失败（或者将来有人加了个分支忘了写另一处），
    就会出现"列表上显示 A、点进去版本历史最新是 B"。
    所以只留这一个出口，两处写在同一个事务里。
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(version_no), 0) AS n FROM plot_versions "
        "WHERE plot_id=?", (plot_id,)).fetchone()
    ver_no = int(row["n"]) + 1

    cur = conn.execute(
        "INSERT INTO plot_versions (plot_id, version_no, title, summary, "
        " plot_type, usage_hint_json, beats_json, role_slots_json, "
        " editor_type, model_id, prompt_version, change_note, created_by, "
        " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (plot_id, ver_no, data["title"], data["summary"], data["plot_type"],
         _dumps(data["usage_hints"]), _dumps(data["beats"]),
         _dumps(data["role_slots"]), editor_type, model_id, prompt_version,
         change_note, created_by, now_str()))
    vid = cur.lastrowid

    conn.execute(
        "UPDATE plots SET title=?, summary=?, plot_type=?, usage_hint_json=?, "
        " beats_json=?, role_slots_json=?, current_version_id=?, updated_at=? "
        "WHERE id=?",
        (data["title"], data["summary"], data["plot_type"],
         _dumps(data["usage_hints"]), _dumps(data["beats"]),
         _dumps(data["role_slots"]), vid, now_str(), plot_id))
    return vid, ver_no


def _link_cards(conn, owner, plot_id, card_ids):
    """把来源卡片挂到零件上。返回真正挂上的条数。

    【为什么要逐张校验 owner 和存在性】
    card_id 是连号的。不校验的话，前端传 99999 就能把别人账号的卡片
    当成自己零件的"来源"，然后点进来源就看到了别人的原文。
    这是全项目反复强调的那条：接口只信服务端门票，不信前端传来的东西。

    【为什么校验失败要抛错、而不是跳过那一张】
    静默跳过 = 她选了 5 张，实际只挂上 3 张，界面上却显示成功。
    调用方在 with db.connect() 里，抛错会整体 rollback ——
    不会留下"半条零件"这种烂数据。
    """
    if not card_ids:
        return 0
    ids = []
    for x in card_ids:
        try:
            i = int(x)
        except (TypeError, ValueError):
            raise ValueError("来源卡片的编号得是数字（收到的是「%s」）。" % (x,))
        if i not in ids:
            ids.append(i)

    q = ("SELECT id, owner_id, material_id, start_offset, end_offset, "
         "source_text_hash FROM cards WHERE id IN (%s)"
         % ",".join("?" * len(ids)))
    got = {r["id"]: r for r in conn.execute(q, ids).fetchall()}

    for i in ids:
        if i not in got:
            raise ValueError(
                "编号 %d 这张素材卡片不存在（可能已经被删了）。" % i)
        if got[i]["owner_id"] != owner:
            raise ValueError("编号 %d 这张素材卡片不是你的。" % i)

    for i in ids:
        r = got[i]
        conn.execute(
            "INSERT OR REPLACE INTO plot_cards (plot_id, card_id, material_id, "
            " start_offset, end_offset, source_text_hash, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (plot_id, i, r["material_id"], r["start_offset"], r["end_offset"],
             r["source_text_hash"], now_str()))
    return len(ids)


# ----------------------------------------------------------------------
# 五、零件：新建 / 读取 / 列表
# ----------------------------------------------------------------------

def create_plot(owner, title, summary="", plot_type="", usage_hints=None,
                beats=None, role_slots=None, category_id=None,
                source=PLOT_SOURCE_HUMAN, status=None, card_ids=None,
                ai_reason="", ai_confidence=None,
                change_note="", created_by=""):
    """手工（或由 AI 候选采用后）新建一条剧情零件。返回新建的那条。

    【默认状态为什么跟 source 走】
    任务书第 511 行：手工建的默认 `human_confirmed`（已确认），
    AI 提的默认 `ai_suggested`（待确认）。
    这条不能反过来 —— AI 提的零件如果默认就是"已确认"，
    她自己都分不清哪些是她认过的、哪些是机器塞进来的。
    """
    title = _clean_text(title, TITLE_MAX, "标题")
    if not title:
        raise ValueError("标题不能空着 —— 库里几百条零件，没有标题她认不出哪条是哪条。")
    summary = _clean_text(summary, SUMMARY_MAX, "摘要")
    plot_type = _clean_text(plot_type, ITEM_MAX, "剧情类型")
    if plot_type and plot_type not in PLOT_TYPES:
        raise ValueError("剧情类型里没有「%s」。" % plot_type)
    usage_hints = _clean_list(usage_hints, USAGE_HINTS, USAGE_HINTS_MAX, "使用场景")
    role_slots = _clean_list(role_slots, None, ROLE_SLOTS_MAX, "角色位")
    beats = _clean_beats(beats)

    if source not in ALL_PLOT_SOURCE:
        raise ValueError("来源标记只能是 %s 之一。" % "、".join(ALL_PLOT_SOURCE))
    if status is None:
        status = (PLOT_STATUS_CONFIRMED if source == PLOT_SOURCE_HUMAN
                  else PLOT_STATUS_AI_SUGGESTED)
    if status not in ALL_PLOT_STATUS:
        raise ValueError("零件状态里没有「%s」。" % status)

    with db.connect() as conn:
        if category_id is not None:
            cid = int(category_id)
            if cid not in _visible_category_ids(conn, owner):
                raise ValueError("这个主分类不存在，或者不是你能用的。")
        else:
            cid = None

        cur = conn.execute(
            "INSERT INTO plots (owner_id, title, summary, plot_type, "
            " usage_hint_json, beats_json, role_slots_json, primary_category_id, "
            " source, status, ai_reason, ai_confidence, created_by, "
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (owner, title, summary, plot_type, _dumps(usage_hints),
             _dumps(beats), _dumps(role_slots), cid, source, status,
             _clean_text(ai_reason, NOTE_MAX, "AI 理由"),
             ai_confidence, created_by, now_str(), now_str()))
        pid = cur.lastrowid

        _write_version(conn, pid,
                       {"title": title, "summary": summary, "plot_type": plot_type,
                        "usage_hints": usage_hints, "beats": beats,
                        "role_slots": role_slots},
                       editor_type=("ai" if source == PLOT_SOURCE_AI else "human"),
                       change_note=change_note or "新建",
                       created_by=created_by)
        _link_cards(conn, owner, pid, card_ids)

        row = conn.execute("SELECT * FROM plots WHERE id=?", (pid,)).fetchone()
        out = _row_to_plot(row)
    return out


def get_plot(owner, plot_id):
    """取一条零件。不是自己的返回 None（接口层转 404）。

    【为什么别人的也统一说"不存在"】返回 403 等于告诉对方
    "这个 id 是有的，只是不是你的" —— 那就成了可以拿 id 一个个试的探针。
    """
    try:
        pid = int(plot_id)
    except (TypeError, ValueError):
        return None
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM plots WHERE id=? AND owner_id=?",
                           (pid, owner)).fetchone()
        if row is None:
            return None
        out = _row_to_plot(row)
        out["sources"] = _sources_of(conn, owner, pid)
        v = conn.execute("SELECT * FROM plot_versions WHERE id=?",
                         (out.get("current_version_id"),)).fetchone()
        out["current_version_no"] = v["version_no"] if v else None
        out["version_count"] = conn.execute(
            "SELECT COUNT(*) AS n FROM plot_versions WHERE plot_id=?",
            (pid,)).fetchone()["n"]
        # 带一带主类名，省得前端再拉一次分类清单
        if out.get("primary_category_id"):
            c = conn.execute("SELECT name FROM categories WHERE id=?",
                             (out["primary_category_id"],)).fetchone()
            out["category_name"] = c["name"] if c else ""
        else:
            out["category_name"] = ""
    return out


def list_plots(owner, category_id=None, status=None, plot_type=None,
               keyword=None, order="category", limit=300, offset=0):
    """零件列表。

    【order='category' 是默认】按主分类分段看 —— 这是她要的组织方式
    （内化剧情仍按原素材的分类归档）。
    段序跟主类圆钮一致（走 categories.sort，不是按 id 乱排），
    **未分类永远排在最后**。跟素材分类库那边的排法完全一样。

    顺手把当前版本号（current_version_no）一起带出来 ——
    列表上要显示"这条现在是第几版"。前端光有 current_version_id 是不够的：
    那只是个自增主键，显示成 v12 会让人以为她改过 12 次。
    """
    sql = ["SELECT p.*, c.name AS category_name, c.sort AS category_sort, "
           "v.version_no AS current_version_no "
           "FROM plots p "
           "LEFT JOIN categories c ON c.id = p.primary_category_id "
           "LEFT JOIN plot_versions v ON v.id = p.current_version_id "
           "WHERE p.owner_id=?"]
    args = [owner]
    if category_id == "0":
        sql.append("AND p.primary_category_id IS NULL")
    elif category_id is not None:
        sql.append("AND p.primary_category_id=?")
        args.append(int(category_id))
    if status:
        sql.append("AND p.status=?")
        args.append(status)
    if plot_type:
        sql.append("AND p.plot_type=?")
        args.append(plot_type)
    if keyword:
        sql.append("AND (p.title LIKE ? OR p.summary LIKE ?)")
        kw = "%" + keyword.strip() + "%"
        args.extend([kw, kw])

    order_sql = {
        "category": ("CASE WHEN p.primary_category_id IS NULL THEN 1 ELSE 0 END,"
                     " COALESCE(c.sort, 9999), c.id, p.id"),
        "recent": "p.updated_at DESC, p.id DESC",
        "created": "p.created_at DESC, p.id DESC",
        "title": "p.title",
    }.get(order, "p.id DESC")
    sql.append("ORDER BY " + order_sql + " LIMIT ? OFFSET ?")
    args.extend([int(limit), int(offset)])

    with db.connect() as conn:
        rows = conn.execute(" ".join(sql), args).fetchall()
        out = []
        for r in rows:
            d = _row_to_plot(r)
            d.pop("category_sort", None)
            d["source_count"] = conn.execute(
                "SELECT COUNT(*) AS n FROM plot_cards WHERE plot_id=?",
                (d["id"],)).fetchone()["n"]
            out.append(d)
        counts = _category_counts(conn, owner, status, plot_type)
    return {"plots": out, "counts": counts}


def _category_counts(conn, owner, status=None, plot_type=None):
    """每个主类下有多少条零件 —— 界面上段标题里那个数字。

    为什么要带上目前生效的筛选条件：她筛了"已确认"之后，
    段标题还写着"外貌 12 条"会很怪（点开只有 3 条）。
    """
    sql = ["SELECT primary_category_id AS cid, COUNT(*) AS n FROM plots "
           "WHERE owner_id=?"]
    args = [owner]
    if status:
        sql.append("AND status=?")
        args.append(status)
    if plot_type:
        sql.append("AND plot_type=?")
        args.append(plot_type)
    sql.append("GROUP BY primary_category_id")
    return {(r["cid"] if r["cid"] is not None else "0"): r["n"]
            for r in conn.execute(" ".join(sql), args).fetchall()}


# ----------------------------------------------------------------------
# 六、零件：编辑 / 版本 / 状态
# ----------------------------------------------------------------------

def update_plot(owner, plot_id, patch, change_note="", created_by="",
                model_id="", prompt_version="", editor_type="human"):
    """改内容 → 产生一个新版本。返回 (零件, 新版本号)，找不到返回 (None, None)。

    【PATCH 语义】沿用模型配置那套：「字段在不在这次传来的东西里」
    决定改不改；「值是不是空」决定改成什么。
    只传 {"title": "..."} 就只动标题，别的字段原样保留 ——
    否则前端每次得把整条零件回传一遍，漏一个字段就被清空。

    【状态怎么变】只有"待确认"（AI 刚提、她还没认）会被顺手升成"已编辑"。
    其他状态一律不动 —— 她主动把一条设成"暂不用"，改一下错别字
    不该把它变回在用。
    """
    try:
        pid = int(plot_id)
    except (TypeError, ValueError):
        return None, None
    if not isinstance(patch, dict):
        raise ValueError("改零件得给一组字段。")
    change_note = _clean_text(change_note, NOTE_MAX, "修改说明")

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM plots WHERE id=? AND owner_id=?",
                           (pid, owner)).fetchone()
        if row is None:
            return None, None
        cur = _row_to_plot(row)

        data = {"title": cur["title"], "summary": cur["summary"],
                "plot_type": cur["plot_type"], "usage_hints": cur["usage_hints"],
                "beats": cur["beats"], "role_slots": cur["role_slots"]}

        if "title" in patch:
            data["title"] = _clean_text(patch["title"], TITLE_MAX, "标题")
            if not data["title"]:
                raise ValueError("标题不能改空 —— 库里几百条零件，没标题她认不出哪条是哪条。")
        if "summary" in patch:
            data["summary"] = _clean_text(patch["summary"], SUMMARY_MAX, "摘要")
        if "plot_type" in patch:
            pt = _clean_text(patch["plot_type"], ITEM_MAX, "剧情类型")
            if pt and pt not in PLOT_TYPES:
                raise ValueError("剧情类型里没有「%s」。" % pt)
            data["plot_type"] = pt
        if "usage_hints" in patch:
            data["usage_hints"] = _clean_list(
                patch["usage_hints"], USAGE_HINTS, USAGE_HINTS_MAX, "使用场景")
        if "beats" in patch:
            data["beats"] = _clean_beats(patch["beats"])
        if "role_slots" in patch:
            data["role_slots"] = _clean_list(
                patch["role_slots"], None, ROLE_SLOTS_MAX, "角色位")

        new_status = cur["status"]
        new_source = cur["source"]
        if cur["status"] == PLOT_STATUS_AI_SUGGESTED:
            new_status = PLOT_STATUS_EDITED
        if cur["source"] == PLOT_SOURCE_AI:
            new_source = PLOT_SOURCE_AI_EDITED

        if "category_id" in patch:
            raw = patch["category_id"]
            if raw in (None, "", 0, "0"):
                cid = None
            else:
                cid = int(raw)
                if cid not in _visible_category_ids(conn, owner):
                    raise ValueError("这个主分类不存在，或者不是你能用的。")
            conn.execute("UPDATE plots SET primary_category_id=? WHERE id=?",
                         (cid, pid))

        vid, ver_no = _write_version(
            conn, pid, data, editor_type=editor_type, model_id=model_id,
            prompt_version=prompt_version, change_note=change_note,
            created_by=created_by)
        conn.execute("UPDATE plots SET status=?, source=? WHERE id=?",
                     (new_status, new_source, pid))
        conn.execute("UPDATE plots SET updated_at=? WHERE id=?", (now_str(), pid))

        out = _row_to_plot(conn.execute("SELECT * FROM plots WHERE id=?",
                                        (pid,)).fetchone())
    return out, ver_no


def set_plot_status(owner, plot_id, status):
    """只改状态，**不产生新版本**。

    任务书第 168 行：「用户确认不产生新内容版本，只改变零件状态。」
    确认、暂不用、排除、恢复都走这里 —— 内容一个字没动，
    给它建个版本只会让版本历史里堆满一模一样的内容，以后翻起来更累。
    """
    if status not in ALL_PLOT_STATUS:
        raise ValueError("零件状态里没有「%s」。" % status)
    try:
        pid = int(plot_id)
    except (TypeError, ValueError):
        return None
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM plots WHERE id=? AND owner_id=?",
                           (pid, owner)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE plots SET status=?, updated_at=? WHERE id=?",
                     (status, now_str(), pid))
        return _row_to_plot(conn.execute("SELECT * FROM plots WHERE id=?",
                                         (pid,)).fetchone())


def list_versions(owner, plot_id):
    """版本历史（新的在前）。不是自己的返回 None。"""
    try:
        pid = int(plot_id)
    except (TypeError, ValueError):
        return None
    with db.connect() as conn:
        if not conn.execute("SELECT id FROM plots WHERE id=? AND owner_id=?",
                            (pid, owner)).fetchone():
            return None
        rows = conn.execute(
            "SELECT * FROM plot_versions WHERE plot_id=? ORDER BY version_no DESC",
            (pid,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["usage_hints"] = _loads(d.pop("usage_hint_json", "[]"), [])
            d["beats"] = _loads(d.pop("beats_json", "{}"), {})
            d["role_slots"] = _loads(d.pop("role_slots_json", "[]"), [])
            out.append(d)
    return out


def restore_version(owner, plot_id, version_id, change_note="", created_by=""):
    """把某个历史版本的内容恢复出来。

    【为什么是"产生新版本"而不是"把指针挪回去"】
    挪指针的话，v3 v4 会凭空消失 —— 而"我什么时候恢复过 v1"
    本身就是有用的痕迹（她可能恢复完觉得还是 v3 好，想再换回去）。
    所以恢复 = 拿旧版本的内容，写成一个新版本，旧的全都留着。
    """
    try:
        pid, vid = int(plot_id), int(version_id)
    except (TypeError, ValueError):
        return None, None
    with db.connect() as conn:
        if not conn.execute("SELECT id FROM plots WHERE id=? AND owner_id=?",
                            (pid, owner)).fetchone():
            return None, None
        old = conn.execute("SELECT * FROM plot_versions WHERE id=? AND plot_id=?",
                           (vid, pid)).fetchone()
        if old is None:
            return None, None
        data = {
            "title": old["title"], "summary": old["summary"],
            "plot_type": old["plot_type"],
            "usage_hints": _loads(old["usage_hint_json"], []),
            "beats": _loads(old["beats_json"], {}),
            "role_slots": _loads(old["role_slots_json"], []),
        }
        note = _clean_text(change_note, NOTE_MAX, "修改说明")
        _, ver_no = _write_version(
            conn, pid, data, editor_type="human",
            change_note=note or ("恢复自 v%d" % old["version_no"]),
            created_by=created_by)
        # 恢复出来的内容是人定的，不是机器新提的
        conn.execute("UPDATE plots SET status=?, source=?, updated_at=? "
                     "WHERE id=?",
                     (PLOT_STATUS_EDITED,
                      PLOT_SOURCE_AI_EDITED if old["editor_type"] == "ai"
                      else PLOT_SOURCE_HUMAN, now_str(), pid))
        out = _row_to_plot(conn.execute("SELECT * FROM plots WHERE id=?",
                                        (pid,)).fetchone())
    return out, ver_no


# ----------------------------------------------------------------------
# 七、来源：零件 → 卡片 → 原文
# ----------------------------------------------------------------------

def _sources_of(conn, owner, plot_id):
    """这条零件的来源卡片清单。

    每条都带上"现在还能不能定位回去"的判断：
        ok        原文还在原地，点开就能看
        moved     卡片被拆分/合并过，原文位置变了
        missing   卡片没了（这种正常情况下不会发生，但库里可能有历史脏数据）
    """
    rows = conn.execute(
        "SELECT pc.*, m.title AS material_title, m.content AS material_content "
        "FROM plot_cards pc LEFT JOIN materials m ON m.id = pc.material_id "
        "WHERE pc.plot_id=? ORDER BY pc.card_id", (plot_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        content = d.pop("material_content", "") or ""
        # 正文摘要**现算**，不存副本 —— 跟卡片本身一样，世上只有一个真相。
        # 界面靠它一眼认出"这条来源是哪一段"，不用再拉一次卡片接口。
        if content and d["end_offset"] > d["start_offset"]:
            d["head"] = content[d["start_offset"]:d["end_offset"]][:48]
        else:
            d["head"] = ""
        c = conn.execute(
            "SELECT id, owner_id, material_id, start_offset, end_offset, "
            "source_text_hash, status, primary_category_id "
            "FROM cards WHERE id=?", (d["card_id"],)).fetchone()
        if c is None or c["owner_id"] != owner:
            d["locate"] = "missing"
            d["locate_note"] = "这张来源卡片已经不在了"
        else:
            d["card_status"] = c["status"]
            d["card_material_id"] = c["material_id"]
            d["card_start"] = c["start_offset"]
            d["card_end"] = c["end_offset"]
            # 偏移变了（被拆/被合并）或者原文内容变了（指纹对不上）
            if (c["material_id"] != d["material_id"]
                    or c["start_offset"] != d["start_offset"]
                    or c["end_offset"] != d["end_offset"]):
                d["locate"] = "moved"
                d["locate_note"] = "这张来源已被拆分或合并，位置变了"
            elif d["source_text_hash"] and c["source_text_hash"] \
                    and c["source_text_hash"] != d["source_text_hash"]:
                d["locate"] = "moved"
                d["locate_note"] = "这段原文后来被改过了"
            else:
                d["locate"] = "ok"
                d["locate_note"] = ""
            if c["status"] == sg.STATUS_EXCLUDED and d["locate"] == "ok":
                d["locate_note"] = "这条来源卡已被排除（拆分或合并过），但原文还在原地"
        out.append(d)
    return out


def list_sources(owner, plot_id):
    """对外的来源清单接口。不是自己的返回 None。"""
    try:
        pid = int(plot_id)
    except (TypeError, ValueError):
        return None
    with db.connect() as conn:
        if not conn.execute("SELECT id FROM plots WHERE id=? AND owner_id=?",
                            (pid, owner)).fetchone():
            return None
        return _sources_of(conn, owner, pid)


def plots_of_card(owner, card_id):
    """反向链接：这张素材卡片被哪些零件用到了。

    素材分类库的卡片上要显示「已内化 · 看零件 →」，靠的就是它。
    """
    try:
        cid = int(card_id)
    except (TypeError, ValueError):
        return []
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT p.id, p.title, p.status, p.plot_type FROM plot_cards pc "
            "JOIN plots p ON p.id = pc.plot_id "
            "WHERE pc.card_id=? AND p.owner_id=? "
            "AND p.status<>? ORDER BY p.id",
            (cid, owner, PLOT_STATUS_EXCLUDED)).fetchall()
        return [dict(r) for r in rows]


# ----------------------------------------------------------------------
# 八、卡片的内化状态（**不在 cards 表上加任何字段**）
# ----------------------------------------------------------------------

CARD_INFUSE_NONE = "not_processed"
CARD_INFUSE_QUEUED = "queued"
CARD_INFUSE_HAS_PLOT = "has_plot"
CARD_INFUSE_UNSUITABLE = "not_suitable"
CARD_INFUSE_REVIEW = "needs_review"

CARD_INFUSE_LABELS = {
    CARD_INFUSE_NONE: "还没内化",
    CARD_INFUSE_QUEUED: "排队中",
    CARD_INFUSE_HAS_PLOT: "已有零件",
    CARD_INFUSE_UNSUITABLE: "不适合内化",
    CARD_INFUSE_REVIEW: "待定",
}


def card_infuse_states(owner, card_ids=None):
    """卡片的内化状态 —— **全部由关联表算出来**，cards 表一个字段都不加。

    【为什么不给 cards 加一列】
    任务书第 131/141 行讲得很清楚：
        「卡片的分类状态和零件的内化状态不能混用」
        「一张卡片可以是『分类已确认 + 不适合内化』，不能因此改变它的分类状态」
    一条卡本来就可能同时是"分类已确认"和"不适合内化"，
    硬压进一条轴会出现两个互斥的值抢位，而且 AI 判"不适合内化"
    就等于顺手动了她的分类流程。
    算出来的代价只是一次 JOIN，而她的库才 793 张卡。

    第 2 步（plots_ai.py）建了那四张表之后，queued / not_suitable /
    needs_review 就开始有值了。判定口径（优先级从高到低，先到先得）：

        has_plot      查到零件关联   —— 最要紧的信息，压过其它一切
        queued        任务还活着，且这张卡还没轮到（待处理 / 处理中）
        needs_review  这一批模型"拿不准"（plot_candidates.result=unsure）
        not_suitable  这张卡被处理过，但没拿它组出零件（outcome=看过没用）

    【这四个状态词是接口下发给前端的枚举，不是往库里写的东西】
    库里存的是中文（跟 segmentation.ALL_STATUS 一致），下发时换英文 key。
    两张皮之间靠 CARD_INFUSE_LABELS 对应。

    【为什么向 plots_ai 现取状态词，不在这儿再抄一份】
    那些字的唯一定义处在建表的那个模块。抄一份的下场是那边改了词、
    这边**算错但不报错** —— 内化状态会静默全部退化成"还没内化"。
    """
    if card_ids is not None:
        ids = [int(x) for x in card_ids]
        if not ids:
            return {}
        ph = ",".join("?" * len(ids))
        where_cards, args = "AND pc.card_id IN (%s)" % ph, ids
    else:
        where_cards, args = "", []

    out = {}
    with db.connect() as conn:
        # 1) 已有零件（排除掉的零件不算"已内化"——她排除了就是不要了）
        q = ("SELECT DISTINCT pc.card_id FROM plot_cards pc "
             "JOIN plots p ON p.id = pc.plot_id "
             "WHERE p.owner_id=? AND p.status<>? " + where_cards)
        for r in conn.execute(q, [owner, PLOT_STATUS_EXCLUDED] + args).fetchall():
            out[r["card_id"]] = CARD_INFUSE_HAS_PLOT

        # 2) 排队中 / 待定 / 不适合 —— 这三样要 plot_runs / plot_items /
        #    plot_run_cards / plot_candidates，都是第 2 步（plots_ai.py）建的表。
        #    表还没建就跳过，不写死假设。
        #
        #    【状态词一个都不许在这边再抄一遍】
        #    "排队中""看过没用"这些字的**唯一定义处在 plots_ai**（建那些表的模块），
        #    跟 segmentation.ALL_STATUS 一个道理。在这边重抄一份字符串，
        #    哪天那边改个词，这里只会**算错且不报错**。
        #    import 写在函数里而不是文件顶上：plots_ai 反过来 import 本模块，
        #    放顶上就成环了。函数被调到的时候两个模块都加载完了。
        if (_has_table(conn, "plot_items") and _has_table(conn, "plot_runs")):
            from backend import plots_ai as pai

            where_i = ("AND i.card_id IN (%s) " % ph) if card_ids is not None else ""

            # 2a) 排队中：任务还活着，且这张卡本身还没轮到 / 正在处理
            q2 = ("SELECT i.card_id FROM plot_items i "
                  "JOIN plot_runs r ON r.id = i.run_id "
                  "WHERE r.owner_id=? AND r.status IN (%s) "
                  "AND i.status IN (%s) " % (
                      ",".join("?" * len(pai.RUN_ACTIVE)),
                      ",".join("?" * len(pai.ITEM_ACTIVE))) +
                  where_i + "ORDER BY i.id")
            for r in conn.execute(
                    q2, [owner] + list(pai.RUN_ACTIVE) + list(pai.ITEM_ACTIVE)
                    + args).fetchall():
                cid = r["card_id"]
                if out.get(cid) != CARD_INFUSE_HAS_PLOT:
                    out[cid] = CARD_INFUSE_QUEUED

            # 2b) 待定：这一批模型"拿不准" —— 比"看过没用"更该让她看一眼，
            #     所以排在 2c 前面，先到先得。
            #     批次级的 result 落在 plot_candidates 上，用 plot_run_cards
            #     的 batch_no 摊回该批的每一张卡。
            if _has_table(conn, "plot_candidates") and _has_table(conn, "plot_run_cards"):
                q3 = ("SELECT DISTINCT rc.card_id FROM plot_candidates c "
                      "JOIN plot_run_cards rc "
                      "ON rc.run_id = c.run_id AND rc.batch_no = c.batch_no "
                      "WHERE c.owner_id=? AND c.result=? ")
                a3 = [owner, pai.RESULT_UNSURE]
                if card_ids is not None:
                    q3 += "AND rc.card_id IN (%s) " % ph
                    a3 += args
                for r in conn.execute(q3, a3).fetchall():
                    cid = r["card_id"]
                    if out.get(cid) not in (CARD_INFUSE_HAS_PLOT,
                                            CARD_INFUSE_QUEUED):
                        out[cid] = CARD_INFUSE_REVIEW

            # 2c) 不适合内化：模型看过这张卡，但没拿它组出任何零件。
            #     **这是"这张卡没产出零件"的事实，不是对她的分类下判断** ——
            #     所以它只出现在这个算出来的状态里，不写回 cards 表。
            q4 = ("SELECT i.card_id FROM plot_items i "
                  "JOIN plot_runs r ON r.id = i.run_id "
                  "WHERE r.owner_id=? AND i.outcome=? " + where_i +
                  "ORDER BY i.id")
            for r in conn.execute(q4, [owner, pai.CARD_UNUSED] + args).fetchall():
                cid = r["card_id"]
                if out.get(cid) not in (CARD_INFUSE_HAS_PLOT,
                                        CARD_INFUSE_QUEUED,
                                        CARD_INFUSE_REVIEW):
                    out[cid] = CARD_INFUSE_UNSUITABLE

    if card_ids is not None:
        for i in [int(x) for x in card_ids]:
            out.setdefault(i, CARD_INFUSE_NONE)
    return out


def card_infuse_label(state):
    return CARD_INFUSE_LABELS.get(state, state)


# ----------------------------------------------------------------------
# 九、自测（python backend/plots_db.py 直接跑）
# ----------------------------------------------------------------------

def _self_check():                                       # pragma: no cover
    import tempfile
    ok = [0, 0]

    def chk(name, cond, extra=""):
        ok[0 if cond else 1] += 1
        print("  %s %s%s" % ("[OK]" if cond else "[!!]", name,
                             ("  → " + str(extra)) if extra and not cond else ""))

    print("=" * 62)
    print("  plots_db 自测")
    print("=" * 62)

    tmp = tempfile.mkdtemp(prefix="moge_plots_self_")
    old = db.DATA_DIR, db.DB_PATH
    m = __import__(__name__, fromlist=["x"])
    try:
        db.DATA_DIR = tmp
        db.DB_PATH = os.path.join(tmp, "moge.db")
        m.db = db
        m.now_str = cdb.now_str
        db.init_db()
        cdb.migrate()
        migrate()

        with db.connect() as conn:
            tabs = {r[0] for r in conn.execute(
                "select name from sqlite_master where type='table'")}
        for t in ("plots", "plot_cards", "plot_versions"):
            chk("建出表 %s" % t, t in tabs)

        with db.connect() as conn:
            sql = conn.execute("select sql from sqlite_master where name='plot_versions'"
                               ).fetchone()[0]
        chk("plot_versions 有 (plot_id, version_no) 唯一约束",
            "UNIQUE (plot_id, version_no)" in sql)

        p = create_plot("__u1", "以罚代护", summary="师兄借责罚把人关起来，实为护他。",
                        plot_type="保护救援", usage_hints=["冲突升级"],
                        beats={"turn": "真相揭开"}, role_slots=["保护者", "被保护者"],
                        change_note="自测")
        chk("建出一条零件", bool(p and p["id"]), p)
        chk("默认状态是已确认（手工建的）",
            p["status"] == PLOT_STATUS_CONFIRMED, p["status"])
        chk("beats 只留认识的键", set(p["beats"]) <= set(BEAT_KEYS), p["beats"])

        try:
            create_plot("__u1", "")
            chk("标题空着要报错", False)
        except ValueError as e:
            chk("标题空着要报错", "标题" in str(e), e)

        try:
            create_plot("__u1", "x", plot_type="不存在的类型")
            chk("乱编剧情类型要报错", False)
        except ValueError as e:
            chk("乱编剧情类型要报错", True, e)

        v = list_versions("__u1", p["id"])
        chk("新建时自动写了一条 v1", len(v) == 1 and v[0]["version_no"] == 1, v)

        p2, no2 = update_plot("__u1", p["id"], {"title": "以罚代护（改）"},
                              change_note="改标题")
        chk("编辑产生 v2", no2 == 2, no2)
        chk("双写一致：plots 的正文 = 最新版本",
            p2["title"] == "以罚代护（改）", p2["title"])
        chk("v1 还在", len(list_versions("__u1", p["id"])) == 2)

        v1 = [x for x in list_versions("__u1", p["id"]) if x["version_no"] == 1][0]
        p3, no3 = restore_version("__u1", p["id"], v1["id"])
        chk("恢复 v1 产生 v3（不是回退指针）", no3 == 3, no3)
        chk("恢复后内容等于 v1", p3["title"] == "以罚代护", p3["title"])
        chk("v1/v2 一条没删", len(list_versions("__u1", p["id"])) == 3)

        chk("不是自己的零件取不到", get_plot("__u9", p["id"]) is None)
        chk("不是自己的零件改不动",
            update_plot("__u9", p["id"], {"title": "偷改"}) == (None, None))

        r = set_plot_status("__u1", p["id"], PLOT_STATUS_EXCLUDED)
        chk("能排除", r["status"] == PLOT_STATUS_EXCLUDED)
        chk("排除不产生新版本", len(list_versions("__u1", p["id"])) == 3)

        chk("卡片内化状态：没零件的卡是 not_processed",
            card_infuse_states("__u1", [999]) == {999: CARD_INFUSE_NONE})
    finally:
        db.DATA_DIR, db.DB_PATH = old
        shutil_rm(tmp)

    print()
    print("通过 %d 项，失败 %d 项" % (ok[0], ok[1]))
    return ok[1] == 0


def shutil_rm(path):                                     # pragma: no cover
    import shutil
    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":                               # pragma: no cover
    import sys
    sys.exit(0 if _self_check() else 1)
