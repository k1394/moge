"""正文创作 —— 数据层。

两张表，全是**纯新增空表**（不 ALTER 任何已有表、不动任何老数据），
所以不需要"整容迁移"那一套。

    chapters       章节：标题 / 梗概 / 正文 / 关联章节设置
    chapter_runs   生成记录：一次「让 AI 写这一章」的完整账

------------------------------------------------------------------
【为什么正文要单独一层，而不是挂在 outlines 上】

大纲那一层的产物是"待写清单"，正文这一层的产物是"写好的字"。
两者的生命周期完全不同：大纲一次生成几个候选、她挑一个推进正式库；
正文是**一稿一稿往下写**的东西，改一句就要能落盘。

【为什么章节只有一层，没有"作品"分组（2026-09-27 她拍板 1A）】
她给参考站是"作品 → 章节"两层。但墨阁里"作品"这一层其实已经存在了 ——
就是大纲/书名。先做一层能最快跑通"零件 → 大纲 → 正文"这条全链路，
哪天确实需要按作品分组再加，改动很小（chapters 加一个 book_id 就够）。

------------------------------------------------------------------
【为什么 run 表里要留 prev_content —— 这一条是必须的】

她选的是"生成完直接写进正文框"（2B），不是候选制。也就是说：
**点一次生成，屏幕上原来那段字就有被覆盖的风险。**

留 prev_content 是唯一能让她"后悔"的东西：
    · 生成前先把当前正文原样存一份
    · 结果摆在编辑器里，她按「还原」就能回到生成前
不留的话，一次误点 = 她写了两千字的东西被一段 AI 文字顶掉，
而且**没有任何地方能找回来**。这跟"花过钱的东西不许作废"是同一条规矩。
"""

import json
from datetime import datetime

from . import db


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------
# 上限
#
# 这些数字是**她定的**（2026-09-27 需求）：故事背景 500、本章剧情 5000、
# 写作风格 5000、写作要求 5000。
# 别在这里"顺手调大" —— 每一个字都要连着上下文一起发给模型，
# 数字直接等于每次生成的钱。
# ----------------------------------------------------------------------

TITLE_MAX = 80
SUMMARY_MAX = 1000
CONTENT_MAX = 200000        # 单章正文上限（汉字数）。够写长篇的一章了。
BACKGROUND_MAX = 500
PLOT_MAX = 5000
STYLE_MAX = 5000
REQUIRE_MAX = 5000

# 参考上限：一次生成最多关联几章（超过就该考虑拆章了）
REF_CHAPTER_MAX = 8

# 每章正文往上下文里塞多少字。
#
# 【为什么必须有这个上限，而不是"有多少塞多少"】
# 关联三章、每章一万字，光上下文就三万 —— 请求直接超模型上下文窗口，
# 或者把她这一笔钱烧在三万字的历史章上，而她要的只是"别失忆"。
# 截断**必须说出来**（界面上和任务记录里都写），
# 偷偷截掉等于让她以为"AI 看过全章"，然后拿结果去对账对不上。
REF_CHAPTER_CHARS = 3000
REF_TOTAL_CHARS = 12000

WORD_MODE_CONTENT = "content"    # 关联章节时取全文
WORD_MODE_SUMMARY = "summary"    # 关联章节时只取梗概
WORD_MODES = (WORD_MODE_CONTENT, WORD_MODE_SUMMARY)

RUN_RUNNING = "running"
RUN_DONE = "done"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_STATUSES = (RUN_RUNNING, RUN_DONE, RUN_FAILED, RUN_CANCELLED)

# 「这次生成的结果已经在正文里了」的几种状态之一 —— 界面靠它显示
# 「当前正文来自哪一次生成」。空串 = 手写的，不是 AI 生成的。
APPLIED_NONE = ""


SCHEMA = """
CREATE TABLE IF NOT EXISTS chapters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id    TEXT    NOT NULL,
    title       TEXT    NOT NULL DEFAULT '',
    summary     TEXT    NOT NULL DEFAULT '',
    content     TEXT    NOT NULL DEFAULT '',
    word_count  INTEGER NOT NULL DEFAULT 0,

    -- 排序：新建的章节排到最后。用整数而不是时间戳，
    -- 因为时间戳只到秒 —— 连着新建两章会撞上同一个值，顺序就飘。
    sort_order  INTEGER NOT NULL DEFAULT 0,

    -- 关联章节设置：[{"id": 12, "mode": "content"|"summary"}]
    --
    -- 【为什么存 id 而不是章节名】改名之后还得跟着变，存 id 才稳。
    -- 【为什么生成时还要再存一份快照】理由跟角色卡一样：
    -- 她以后改了第 3 章，不该影响这一章"当时是照着谁写的"。
    ref_json    TEXT    NOT NULL DEFAULT '[]',

    -- ---- 这一章的写作输入 ----
    --
    -- 【为什么这些要进库，而不是像别处那样只存浏览器草稿】
    -- 草稿层是浏览器本地的，换台机器就没了。而"这一章的故事背景 /
    -- 本章剧情 / 风格 / 要求"是她为这一章定下来的东西，性质跟梗概一样，
    -- 属于章节资料 —— 她下次打开这一章，这些必须还在。
    -- 只活在内存或 localStorage 里的话，她换个浏览器回来会以为白写了。
    background  TEXT    NOT NULL DEFAULT '',
    plot        TEXT    NOT NULL DEFAULT '',
    style       TEXT    NOT NULL DEFAULT '',
    requirement TEXT    NOT NULL DEFAULT '',

    -- 这一章常驻用哪几张角色卡（快照在 chapter_runs 里）
    char_json   TEXT    NOT NULL DEFAULT '[]',

    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chap_own ON chapters(owner_id, sort_order, id);

CREATE TABLE IF NOT EXISTS chapter_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id      TEXT    NOT NULL,
    chapter_id    INTEGER NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'running',

    -- 模型三层名字，跟别处一个规矩：
    --   model_key     内部短名（播种后永不变，靠它认历史任务）
    --   model_name    她看的中文名
    --   model_version 真发给服务商的那个名字（界面上要显示"这次用的哪个"）
    model_key     TEXT    NOT NULL DEFAULT '',
    model_name    TEXT    NOT NULL DEFAULT '',
    model_version TEXT    NOT NULL DEFAULT '',
    prompt_version TEXT   NOT NULL DEFAULT '',

    -- 这次的输入快照（背景 / 角色快照 / 本章剧情 / 风格 / 要求 /
    -- 关联章节取到的上下文）。为什么要快照：她跑完之后改了表单，
    -- 回头看这次记录，得能答出"当时发给模型的是什么"。
    input_json    TEXT    NOT NULL DEFAULT '{}',

    -- 生成前的正文。**后悔药**，理由见模块顶部。
    prev_content  TEXT    NOT NULL DEFAULT '',
    -- 这次的产出。留着是为了「还原后再看下一版」和「复制」。
    ai_content    TEXT    NOT NULL DEFAULT '',

    finish_reason TEXT    NOT NULL DEFAULT '',
    ttft_ms       INTEGER NOT NULL DEFAULT 0,
    elapsed_ms    INTEGER NOT NULL DEFAULT 0,
    chars         INTEGER NOT NULL DEFAULT 0,
    usage_json    TEXT    NOT NULL DEFAULT '{}',
    error         TEXT    NOT NULL DEFAULT '',

    applied_at    TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL,
    finished_at   TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_chrun_chap ON chapter_runs(chapter_id, id);
CREATE INDEX IF NOT EXISTS idx_chrun_own  ON chapter_runs(owner_id, id);
"""


# 后加的列：(表名, 列名, 列定义)。以后加列就往这里添一行。
# 表驱动，理由见 outline_db._ADDED_COLUMNS 的注释 ——
# CREATE TABLE IF NOT EXISTS **不会**给已经建过的表补列，必须显式 ALTER。
#
# 下面这五条是"这一章的写作输入"。为什么必须先声明在这儿：
# chapters 表在开发过程中已经建过一次（我先建了表才想起来输入要落库），
# 那个库不会因为 CREATE TABLE IF NOT EXISTS 就多出列 —— 不补的话，
# 她机器上那张表永远是 10 列，界面一保存就报 "no such column"。
_ADDED_COLUMNS = (
    ("chapters", "background", "TEXT NOT NULL DEFAULT ''"),
    ("chapters", "plot", "TEXT NOT NULL DEFAULT ''"),
    ("chapters", "style", "TEXT NOT NULL DEFAULT ''"),
    ("chapters", "requirement", "TEXT NOT NULL DEFAULT ''"),
    ("chapters", "char_json", "TEXT NOT NULL DEFAULT '[]'"),
)


def _has_table(conn, name):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone())


def _add_missing_columns(conn):
    for table, col, decl in _ADDED_COLUMNS:
        if not _has_table(conn, table):
            continue
        have = {r["name"] for r in conn.execute(
            "PRAGMA table_info(%s)" % table).fetchall()}
        if col in have:
            continue
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))


def init_chapters(verbose=False):
    """建这两张表，再给老库补上后加的列。反复跑是安全的。

    【调用顺序有依赖】必须在 outline_db.migrate() **之后**调 ——
    关联角色卡要读 characters 表，函数里会查它。顺序反了会报
    "no such table: characters"，而那句话跟"顺序"毫无关系，查起来要绕。
    （main.py 的 lifespan 里就是按这个顺序摆的。）
    """
    with db.connect() as conn:
        conn.executescript(SCHEMA)
        _add_missing_columns(conn)
    if verbose:
        print("正文创作：两张表就位 →", db.DB_PATH)
    return db.DB_PATH


# ----------------------------------------------------------------------
# 小工具
#
# 【为什么不直接借 outline_db 的那几个】它们是那个模块的私有函数，
# 借过来的话，那边哪天改了行为，这边会**静默**跟着变。
# 这十行重复不值得省。
# ----------------------------------------------------------------------

def _dumps(v):
    return json.dumps(v, ensure_ascii=False)


def _loads(s, fallback):
    """解 JSON 列。解不开给兜底 —— 手工改坏过的库不该让整页崩掉。

    fallback 的类型决定返回什么类型：一律传 [] 或 {}，别传 None。
    """
    if not s:
        return fallback
    try:
        v = json.loads(s)
    except Exception:
        return fallback
    return v if isinstance(v, type(fallback)) else fallback


def _txt(v, limit, name, allow_empty=True):
    """一段纯文本：去两端空白、卡长度。

    这里是**她手填的**内容，所以超长直接报错、绝不偷偷截断 ——
    背着她切掉她写的一段，比报错恶劣得多（她会以为系统把它存下了）。
    """
    s = (v or "")
    if not isinstance(s, str):
        s = str(s)
    s = s.strip()
    if not s and not allow_empty:
        raise ValueError("%s不能是空的。" % name)
    if len(s) > limit:
        raise ValueError("%s最长 %d 字，你写了 %d 字，超了 %d 字。"
                         % (name, limit, len(s), len(s) - limit))
    return s


def word_count(text):
    """字数 = 去掉所有空白之后的字符数。

    【为什么不按"空格分词"算】中文没有词边界，按空白分永远只有一段。
    她也从不关心"英文单词数" —— 界面上那个数字的用途是
    「这一章够不够长」，去掉空白数字符最接近她的直觉。
    """
    return len("".join((text or "").split()))


# ----------------------------------------------------------------------
# 章节
# ----------------------------------------------------------------------

def _chapter_row(row, with_content=True):
    d = {
        "id": row["id"],
        "title": row["title"],
        "summary": row["summary"],
        "word_count": row["word_count"],
        "sort_order": row["sort_order"],
        "refs": _loads(row["ref_json"], []),
        # 这一章的写作输入（跟梗概一样是章节资料，切章 / 换设备都要在）
        "background": row["background"],
        "plot": row["plot"],
        "style": row["style"],
        "requirement": row["requirement"],
        "char_ids": _loads(row["char_json"], []),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    # 列表页只给梗概就够，正文可能几十万字 —— 一次拉全部会把页面拖死。
    if with_content:
        d["content"] = row["content"]
    return d


def _clean_char_ids(owner, ids):
    """洗一遍关联角色卡：只留**真的是她的**那些。

    跟 _clean_refs 同一条理由 —— 角色卡内容会原样发给模型，
    不筛的话改一下请求体就能把别人的角色设定塞进这次生成。
    （角色卡表在 outline_db 里建，所以本模块的初始化必须在它之后。）
    """
    if not ids:
        return []
    out, seen = [], set()
    for x in ids:
        try:
            x = int(x)
        except (TypeError, ValueError):
            continue
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
        if len(out) >= 50:
            break
    if not out:
        return []
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM characters WHERE owner_id=? AND id IN (%s)"
            % ",".join("?" * len(out)), [owner] + out).fetchall()
    mine = {r["id"] for r in rows}
    return [x for x in out if x in mine]


def list_chapters(owner, with_content=False, limit=500):
    """章节列表。默认不带正文（列表上只显示标题和字数）。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM chapters WHERE owner_id=?"
            " ORDER BY sort_order ASC, id ASC LIMIT ?",
            (owner, int(limit))).fetchall()
        return [_chapter_row(r, with_content) for r in rows]


def get_chapter(owner, cid, with_content=True):
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        return None
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM chapters WHERE id=? AND owner_id=?",
                           (cid, owner)).fetchone()
        return _chapter_row(row, with_content) if row else None


def _clean_refs(owner, refs):
    """洗一遍关联章节：只留**真的是她的**那些章，去掉重名重复。

    【为什么要在这里过滤，而不是信前端传的】
    关联章节会**直接把那一章的正文发给模型**。不筛的话，
    改一下请求体里的 id 就能把别人（或者她自己另一本书）的正文
    送进这次生成 —— 那是数据泄漏，而且是静默的。
    """
    if not refs:
        return []
    if not isinstance(refs, (list, tuple)):
        raise ValueError("关联章节得是一组章节。")
    out, seen = [], set()
    for it in refs:
        if isinstance(it, dict):
            rid = it.get("id")
            mode = str(it.get("mode") or WORD_MODE_SUMMARY).strip()
        else:
            rid, mode = it, WORD_MODE_SUMMARY
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            continue
        if mode not in WORD_MODES:
            mode = WORD_MODE_SUMMARY
        if rid in seen:
            continue
        seen.add(rid)
        out.append({"id": rid, "mode": mode})
        if len(out) >= REF_CHAPTER_MAX:
            break
    if not out:
        return []
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM chapters WHERE owner_id=? AND id IN (%s)"
            % ",".join("?" * len(out)), [owner] + [x["id"] for x in out]
        ).fetchall()
    mine = {r["id"] for r in rows}
    return [x for x in out if x["id"] in mine]


def create_chapter(owner, data, created_by=""):
    data = data or {}
    # 标题可以留空 —— 空着就自动起个「第 N 章」。
    # 为什么允许：她的习惯是"先把这一章写出来再想叫什么"，
    # 逼她先起名等于在门口设一道没必要的坎。
    title = _txt(data.get("title"), TITLE_MAX, "章节标题")
    if not title:
        with db.connect() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM chapters WHERE owner_id=?",
                             (owner,)).fetchone()["n"]
        title = "第 %d 章" % (n + 1)
    summary = _txt(data.get("summary"), SUMMARY_MAX, "本章梗概")
    content = _txt(data.get("content"), CONTENT_MAX, "正文")
    refs = _clean_refs(owner, data.get("refs"))
    bg = _txt(data.get("background"), BACKGROUND_MAX, "故事背景")
    plot = _txt(data.get("plot"), PLOT_MAX, "本章剧情")
    style = _txt(data.get("style"), STYLE_MAX, "写作风格")
    req = _txt(data.get("requirement"), REQUIRE_MAX, "写作要求")
    char_ids = _clean_char_ids(owner, data.get("char_ids"))
    ts = now_str()
    with db.connect() as conn:
        nxt = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) AS m FROM chapters WHERE owner_id=?",
            (owner,)).fetchone()["m"]
        cur = conn.execute(
            """INSERT INTO chapters
               (owner_id, title, summary, content, word_count, sort_order,
                ref_json, background, plot, style, requirement, char_json,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (owner, title, summary, content, word_count(content), nxt + 1,
             _dumps(refs), bg, plot, style, req, _dumps(char_ids), ts, ts))
        cid = cur.lastrowid
        row = conn.execute("SELECT * FROM chapters WHERE id=?", (cid,)).fetchone()
        return _chapter_row(row)


def update_chapter(owner, cid, patch):
    """改一章。**只改传进来的字段**（PATCH 语义）。

    这条规矩在这儿格外要紧：正文框是超长的，前端每次保存都会把
    整段正文发上来；如果"没传 = 清空"，她只想改个标题就会把
    两千字正文清掉，而且没有撤销。
    """
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        return None
    patch = patch or {}
    sets, args = [], []
    if "title" in patch:
        t = _txt(patch.get("title"), TITLE_MAX, "章节标题")
        if not t:
            raise ValueError("章节标题不能是空的。想换名字就写个新的。")
        sets.append("title=?")
        args.append(t)
    if "summary" in patch:
        sets.append("summary=?")
        args.append(_txt(patch.get("summary"), SUMMARY_MAX, "本章梗概"))
    if "content" in patch:
        c = _txt(patch.get("content"), CONTENT_MAX, "正文")
        sets.append("content=?")
        args.append(c)
        sets.append("word_count=?")
        args.append(word_count(c))
    if "refs" in patch:
        sets.append("ref_json=?")
        args.append(_dumps(_clean_refs(owner, patch.get("refs"))))
    # 写作输入那四项：跟梗概一个待遇（章节资料，落库）
    for f, lim, label in (("background", BACKGROUND_MAX, "故事背景"),
                          ("plot", PLOT_MAX, "本章剧情"),
                          ("style", STYLE_MAX, "写作风格"),
                          ("requirement", REQUIRE_MAX, "写作要求")):
        if f in patch:
            sets.append(f + "=?")
            args.append(_txt(patch.get(f), lim, label))
    if "char_ids" in patch:
        sets.append("char_json=?")
        args.append(_dumps(_clean_char_ids(owner, patch.get("char_ids"))))
    if "sort_order" in patch:
        try:
            so = int(patch.get("sort_order"))
        except (TypeError, ValueError):
            so = None
        if so is not None:
            sets.append("sort_order=?")
            args.append(so)
    if not sets:
        return get_chapter(owner, cid)
    sets.append("updated_at=?")
    args.append(now_str())
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM chapters WHERE id=? AND owner_id=?",
                           (cid, owner)).fetchone()
        if not row:
            return None
        args.extend([cid, owner])
        conn.execute("UPDATE chapters SET %s WHERE id=? AND owner_id=?"
                     % ", ".join(sets), args)
        r = conn.execute("SELECT * FROM chapters WHERE id=?", (cid,)).fetchone()
        return _chapter_row(r)


def delete_chapter(owner, cid):
    """删一章，连同它的生成记录。

    【为什么生成记录也要删】它们除了这一章没别的归属 ——
    留着就是一堆再也点不开的孤儿行（界面上一堆"找不到那一章"的报错）。
    生成记录不是成品，不需要像剧情零件那样"只排除不删除"。
    """
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        return False
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM chapters WHERE id=? AND owner_id=?",
                           (cid, owner)).fetchone()
        if not row:
            return False
        # 先删子表再删主表：这两张表**没有外键约束**，
        # 顺序反了不会报错，只会留下一堆孤儿行。
        conn.execute("DELETE FROM chapter_runs WHERE chapter_id=? AND owner_id=?",
                     (cid, owner))
        conn.execute("DELETE FROM chapters WHERE id=? AND owner_id=?", (cid, owner))
        return True


def reorder_chapters(owner, order):
    """按给的一串 id 重排。没出现在里面的章节不动。"""
    ids = []
    for x in (order or []):
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            continue
    if not ids:
        return 0
    ts = now_str()
    with db.connect() as conn:
        n = 0
        for i, cid in enumerate(ids):
            cur = conn.execute(
                "UPDATE chapters SET sort_order=?, updated_at=?"
                " WHERE id=? AND owner_id=?", (i, ts, cid, owner))
            n += cur.rowcount
        return n


# ----------------------------------------------------------------------
# 生成记录
# ----------------------------------------------------------------------

def _run_row(row, with_prev=False):
    """一条生成记录 → 前端字典。

    with_prev 控制要不要带上 prev_content（生成前那份正文）。
    **列表一律不带**：那是整章正文，十来次生成叠起来能有好几万字，
    列表页一次拉全会明显卡。要它的地方（单查、刷新后重挂、刚建好）
    自己传 True —— 那些地方本来就只有一条。
    """
    d = {
        "id": row["id"],
        "chapter_id": row["chapter_id"],
        "status": row["status"],
        "model_key": row["model_key"],
        "model_name": row["model_name"],
        "model_version": row["model_version"],
        "prompt_version": row["prompt_version"],
        "input": _loads(row["input_json"], {}),
        "ai_content": row["ai_content"],
        "ai_chars": len(row["ai_content"] or ""),
        "finish_reason": row["finish_reason"],
        "ttft_ms": row["ttft_ms"],
        "elapsed_ms": row["elapsed_ms"],
        "chars": row["chars"],
        "usage": _loads(row["usage_json"], {}),
        "error": row["error"],
        "applied": bool(row["applied_at"]),
        "applied_at": row["applied_at"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
    }
    if with_prev:
        d["prev_content"] = row["prev_content"]
    return d


def create_run(owner, chapter_id, payload):
    """开一次生成。

    prev_content 在这一刻就抓下来 —— **必须在这一刻**：
    任务跑到一半她要是手改了正文，那时再抓就不是"生成前"了，
    还原回去反而会把她的改动弄丢。
    """
    payload = payload or {}
    ch = get_chapter(owner, chapter_id, with_content=True)
    if not ch:
        return None
    ts = now_str()
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO chapter_runs
               (owner_id, chapter_id, status, model_key, model_name,
                model_version, prompt_version, input_json, prev_content,
                created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (owner, ch["id"], RUN_RUNNING,
             payload.get("model_key") or "", payload.get("model_name") or "",
             payload.get("model_version") or "", payload.get("prompt_version") or "",
             _dumps(payload.get("input") or {}), ch.get("content") or "", ts))
        rid = cur.lastrowid
        r = conn.execute("SELECT * FROM chapter_runs WHERE id=?", (rid,)).fetchone()
        # with_prev：刚建好的这一条要带上一份"生成前的正文"，
        # 前端的「还原到生成前」就靠它 —— 中间那次刷新不用再单查一遍。
        return _run_row(r, with_prev=True)


def set_run(conn, rid, **fields):
    if not fields:
        return
    sets, args = [], []
    for k, v in fields.items():
        sets.append("%s=?" % k)
        args.append(_dumps(v) if k.endswith("_json") and not isinstance(v, str) else v)
    args.append(rid)
    conn.execute("UPDATE chapter_runs SET %s WHERE id=?" % ", ".join(sets), args)


def finish_run(conn, rid, **fields):
    """收尾统一走这里：顺手补 finished_at —— 漏了它界面会一直转圈。"""
    fields.setdefault("finished_at", now_str())
    set_run(conn, rid, **fields)


def get_run(owner, rid):
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        return None
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM chapter_runs WHERE id=? AND owner_id=?",
                           (rid, owner)).fetchone()
        return _run_row(row, with_prev=True) if row else None


def list_runs(owner, chapter_id, limit=30):
    """某一章的生成记录，新的在前。

    **不带 ai_content** —— 十来次生成的全文字段加起来能有好几万字，
    列表页一次拉全会明显卡。要看某一次的结果就单查那一条。
    """
    try:
        chapter_id = int(chapter_id)
    except (TypeError, ValueError):
        return []
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM chapter_runs WHERE owner_id=? AND chapter_id=?"
            " ORDER BY id DESC LIMIT ?",
            (owner, chapter_id, int(limit))).fetchall()
    out = []
    for r in rows:
        d = _run_row(r)
        d["ai_content"] = ""          # 列表不带全文（见上面注释）
        d["ai_chars"] = len(r["ai_content"] or "")
        out.append(d)
    return out


def running_run_for(owner, chapter_id):
    """这一章正在跑的那一次（有就返回）。界面靠它判断"要不要接着转圈"。"""
    try:
        chapter_id = int(chapter_id)
    except (TypeError, ValueError):
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM chapter_runs WHERE owner_id=? AND chapter_id=?"
            " AND status=? ORDER BY id DESC LIMIT 1",
            (owner, chapter_id, RUN_RUNNING)).fetchone()
        # with_prev：页面刷新之后重挂实时区时要顺便能「还原到生成前」，
        # 所以这条也得带上那一份。
        return _run_row(row, with_prev=True) if row else None


def cancel_run(owner, rid):
    """取消。**只改状态，不删记录** —— 已经吐出来的字还有用，
    而且"这次我取消了"本身也是她要看得见的一笔账。"""
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        return False
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM chapter_runs WHERE id=? AND owner_id=?",
                           (rid, owner)).fetchone()
        if not row:
            return False
        if row["status"] != RUN_RUNNING:
            return False
        set_run(conn, rid, status=RUN_CANCELLED, finished_at=now_str())
        return True


def mark_applied(owner, rid):
    """记下"这次的结果已经被写进正文了"。

    为什么单独记：她在编辑器里再手改几笔之后，正文和 ai_content
    就对不上了。有这个时间戳 + 存着 ai_content，才答得出
    "现在这一稿是从哪一次改出来的"。
    """
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        return None
    ts = now_str()
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM chapter_runs WHERE id=? AND owner_id=?",
                           (rid, owner)).fetchone()
        if not row:
            return None
        set_run(conn, rid, applied_at=ts)
        r = conn.execute("SELECT * FROM chapter_runs WHERE id=?", (rid,)).fetchone()
        return _run_row(r)


def delete_run(owner, rid):
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        return False
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM chapter_runs WHERE id=? AND owner_id=?",
                           (rid, owner))
        return cur.rowcount > 0


def chapter_stats(owner):
    with db.connect() as conn:
        r = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(word_count),0) AS w"
            " FROM chapters WHERE owner_id=?", (owner,)).fetchone()
        done = conn.execute(
            "SELECT COUNT(*) AS n FROM chapter_runs WHERE owner_id=?"
            " AND status=?", (owner, RUN_DONE)).fetchone()["n"]
    return {"chapters": r["n"], "words": r["w"], "runs_done": done}


def _self_check():
    """只测纯函数，不碰数据库、不联网。"""
    ok = True

    def eq(a, b, what):
        nonlocal ok
        if a != b:
            ok = False
            print("  [x] %s: %r != %r" % (what, a, b))
        else:
            print("  [v] %s" % what)

    eq(word_count("你好 世界\n换行"), 6, "字数按去空白算")
    eq(word_count(""), 0, "空串是 0 字")
    eq(_loads("", []), [], "空 JSON 列 → 空表")
    eq(_loads("{{{坏掉的", {"a": 1}), {"a": 1}, "坏 JSON 走兜底且类型对")
    eq(_loads("[1,2]", {}), {}, "类型不对也走兜底")
    try:
        _txt("啊" * 10, 5, "测试项")
        ok = False
        print("  [x] 超长没报错")
    except ValueError:
        print("  [v] 手填内容超长会报错（不偷偷截断）")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if _self_check() else 1)
