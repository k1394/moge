# -*- coding: utf-8 -*-
"""
墨阁 · 数据库模块
========================================================
作用：把素材存进本机 SQLite 文件（data/moge.db），并提供增删改查。

三个"从第一天就要做对"的设计决定
（面试官大概率会问，先说清楚为什么）：

1. 每条素材都带 owner_id
   将来上线后是多个用户共用一个数据库，"你的素材"和"别人的素材"
   必须在数据层面就分开。现在虽然只有你一个人用，表结构也照样带 ——
   等上线再加字段，等于把已有数据全部推翻重做。

2. 用 content_hash（内容指纹）去重
   重复导入是常事：同一个文件夹导两遍、文件改个名再导一遍。
   素材内容算一个 MD5 存下来，导入前先查有没有 —— 有就跳过。
   注意指纹算的是**内容**不是文件名，所以改名照样认得出来。

3. local_only 字段
   素材唯一会离开电脑的环节，是调用云端大模型（检索 / 内化 / 大纲）。
   标了 local_only = 1 的素材，将来会被排除在云端调用之外。
   字段现在就留着，用不用是后面的事。

关于隐私：数据库文件放在 data/moge.db，而 data/ 已被 .gitignore 整块排除，
所以素材内容不会跟着代码上 GitHub。
"""

import os
import sqlite3
import hashlib
from contextlib import contextmanager
from datetime import datetime

# ----------------------------------------------------------------------
# 基础配置
# ----------------------------------------------------------------------

# 本项目根目录（backend 的上一层）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 数据库目录与文件。
# 允许用环境变量 MOGE_DATA_DIR 改写 —— 这是给测试留的后门：
# 测试脚本把它指到临时目录，就能在"另一个空数据库"上随便折腾，
# 绝不会碰到你真实的素材库。
# （这条是有血泪教训的：曾经有测试直接跑在真实库上，
#   结果把测试账号名下的素材全删了 —— 而那里面混着真实素材。）
DATA_DIR = os.environ.get("MOGE_DATA_DIR") or os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "moge.db")

# 素材归属谁的标记。加了账号系统之后：
#   注册前用命令行导入的素材，owner_id 是 'local'（还没有任何人）
#   注册后导入的素材，owner_id 是 '__u<用户id>'，比如 '__u1'
# 第一个账号注册时会自动"认领" local 名下的素材，所以那些东西不会丢。
DEFAULT_OWNER = "local"


def owner_of(user_id):
    """把用户 id 变成素材表里用的归属标记。

    为什么要加 '__u' 前缀，不直接用 "1"：
    数据库中"1"这种裸数字肉眼看不出含义，而 'local' 又是个词。
    加前缀后，翻数据库时一眼能分清「这是第 1 号用户的」还是「这是没归属的」。
    """
    return "__u%d" % int(user_id)


# ----------------------------------------------------------------------
# 表结构
# ----------------------------------------------------------------------

# 三张表：
#   materials     素材本体（一条 = 一份素材）
#   tags          标签（一条 = 一个标签名）
#   material_tags 中间表，记录"哪份素材挂了哪些标签"
#
# 为什么标签要单独建表，不直接在素材上写一串逗号分隔的文字？
# 因为这是"多对多"关系：一份素材可以有多个标签，一个标签下有多份素材。
# 用中间表才能在数据层面表达清楚，也才能做"点标签看该标签下有几份素材"。
# 直写逗号分隔串，将来搜"含某个标签的素材"只能靠字符串模糊匹配，很容易出错。

SCHEMA = """
CREATE TABLE IF NOT EXISTS materials (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id     TEXT    NOT NULL DEFAULT 'local',
    title        TEXT    NOT NULL,
    content      TEXT    NOT NULL DEFAULT '',
    chars        INTEGER NOT NULL DEFAULT 0,
    ext          TEXT    NOT NULL DEFAULT '',
    source_path  TEXT    NOT NULL DEFAULT '',
    content_hash TEXT    NOT NULL,
    local_only   INTEGER NOT NULL DEFAULT 0,
    is_public    INTEGER NOT NULL DEFAULT 0,
    note         TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    UNIQUE (owner_id, content_hash)
);

CREATE TABLE IF NOT EXISTS tags (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id TEXT NOT NULL DEFAULT 'local',
    name     TEXT NOT NULL,
    UNIQUE (owner_id, name)
);

CREATE TABLE IF NOT EXISTS material_tags (
    material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    tag_id      INTEGER NOT NULL REFERENCES tags(id)      ON DELETE CASCADE,
    PRIMARY KEY (material_id, tag_id)
);

-- users：账号。一个账号一行。
--   password_hash 存的不是密码本身，是密码"搅碎"之后的结果（见 auth.py）。
--   数据库管理员（也就是将来接手你代码的人）看到这张表，
--   也还原不出任何人的原始密码。这是密码存储的底线。
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    salt          TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    last_login_at TEXT    NOT NULL DEFAULT ''
);

-- sessions：登录凭证。你登录成功后，服务给你发一张"门票"，
--   浏览器每次请求都带着它，服务就知道"这是谁"。
--   token 是随机字符串，本身就是凭证 —— 所以它等于密码，绝不能泄露。
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL,
    expires_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_materials_owner ON materials(owner_id);
CREATE INDEX IF NOT EXISTS idx_material_tags_tag ON material_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- llm_calls：每一次「跟模型说话」都记一行 —— 什么时候、用的哪个模型、
--   等了多久才吐出第一个字（首字耗时）、整段回来花了多久（总耗时）、
--   成功还是失败、烧了多少 token。
--
-- 【为什么首字耗时值得单独记】
--   它和总耗时的含义完全不同：
--     首字慢   = 这家在"想"，或者上游在排队（推理模型尤其慢）
--     总耗时慢 = 它写得太长，或者中途卡住了
--   只看总耗时区分不出这两种。她问"哪个模型快"时，答案多半在首字那一栏。
--
-- 【为什么失败也要记】
--   失败那次也是花了时间（可能还花了钱）的。只记成功的话，
--   一个"十次里失败八次"的模型在对比表里会显得又少又快，结论正好反了。
--
-- 【为什么单独一张表，而不是塞进各个任务表】
--   分类、内化、大纲三边的记录结构完全一样，而且"哪个模型快"这个问题
--   必须跨任务看才有意义 —— 记在任务表里就得 union 三张表，越写越乱。
CREATE TABLE IF NOT EXISTS llm_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT    NOT NULL,
    purpose       TEXT    NOT NULL DEFAULT '',
    model_key     TEXT    NOT NULL DEFAULT '',
    model_name    TEXT    NOT NULL DEFAULT '',
    provider      TEXT    NOT NULL DEFAULT '',
    streamed      INTEGER NOT NULL DEFAULT 0,
    ttft_ms       INTEGER NOT NULL DEFAULT 0,
    elapsed_ms    INTEGER NOT NULL DEFAULT 0,
    ok            INTEGER NOT NULL DEFAULT 0,
    error         TEXT    NOT NULL DEFAULT '',
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    chars         INTEGER NOT NULL DEFAULT 0,
    finish_reason TEXT    NOT NULL DEFAULT '',
    fallback      TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_created ON llm_calls(created_at);
CREATE INDEX IF NOT EXISTS idx_llm_calls_model ON llm_calls(model_key);
"""


# ----------------------------------------------------------------------
# 连接与初始化
# ----------------------------------------------------------------------

def now_str():
    """统一的时间格式：2026-09-23 12:50:31"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def content_hash(text):
    """算出内容的指纹（MD5），用来判断两份素材是不是同一份。"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


@contextmanager
def connect():
    """打开数据库连接，用完自动提交并关闭。

    为什么每次操作都新开一个连接：
    SQLite 的连接对象不能跨线程安全共用，而 FastAPI 会把不同的网页请求
    分给不同线程处理。每次新开最省心，代价也极小（打开 SQLite 就是读一个本地文件）。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row          # 让结果能按列名取：row["title"]
    conn.execute("PRAGMA foreign_keys = ON")  # 外键约束默认是关的，必须手动开，
                                              # 否则删素材时中间表不会跟着清理
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """建表。反复调用是安全的（IF NOT EXISTS）。"""
    with connect() as conn:
        conn.executescript(SCHEMA)
    return DB_PATH


# ----------------------------------------------------------------------
# 标签
# ----------------------------------------------------------------------

def _tag_id(conn, name, owner):
    """拿到标签的 id，没有就建一个。"""
    name = (name or "").strip()
    if not name:
        return None

    row = conn.execute(
        "SELECT id FROM tags WHERE owner_id = ? AND name = ?", (owner, name)
    ).fetchone()
    if row:
        return row["id"]

    cur = conn.execute(
        "INSERT INTO tags (owner_id, name) VALUES (?, ?)", (owner, name)
    )
    return cur.lastrowid


def _set_tags(conn, material_id, tags, owner):
    """重新设置一份素材的标签（先清空旧的，再挂新的）"""
    conn.execute("DELETE FROM material_tags WHERE material_id = ?", (material_id,))
    seen = set()
    for t in tags or []:
        t = (t or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        tid = _tag_id(conn, t, owner)
        if tid:
            conn.execute(
                "INSERT OR IGNORE INTO material_tags (material_id, tag_id) VALUES (?, ?)",
                (material_id, tid),
            )
    # 顺手清理：没有任何素材在用的标签就删掉，避免标签栏越积越脏
    conn.execute(
        """DELETE FROM tags WHERE owner_id = ?
           AND id NOT IN (SELECT DISTINCT tag_id FROM material_tags)""",
        (owner,),
    )


def tags_of(conn, material_id):
    """取一份素材的标签名列表"""
    rows = conn.execute(
        """SELECT t.name FROM tags t
           JOIN material_tags mt ON mt.tag_id = t.id
           WHERE mt.material_id = ?
           ORDER BY t.name""",
        (material_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def list_tags(owner=DEFAULT_OWNER):
    """所有标签 + 每个标签下的素材数（按用得多的排前面）"""
    with connect() as conn:
        rows = conn.execute(
            """SELECT t.name AS name, COUNT(mt.material_id) AS count
               FROM tags t
               LEFT JOIN material_tags mt ON mt.tag_id = t.id
               WHERE t.owner_id = ?
               GROUP BY t.id
               ORDER BY count DESC, t.name""",
            (owner,),
        ).fetchall()
        return [{"name": r["name"], "count": r["count"]} for r in rows]


# ----------------------------------------------------------------------
# 素材：写入
# ----------------------------------------------------------------------

def save_material(title, text, ext="", source_path="", note="",
                  tags=None, owner=DEFAULT_OWNER, local_only=False):
    """存一份素材。返回 {status, id, ...}

    status 有三种：
        "new"      新入库
        "same"     内容已存在（重复导入），跳过
    """
    text = text or ""
    h = content_hash(text)

    with connect() as conn:
        old = conn.execute(
            "SELECT id FROM materials WHERE owner_id = ? AND content_hash = ?",
            (owner, h),
        ).fetchone()
        if old:
            return {"status": "same", "id": old["id"],
                    "title": title, "chars": len(text)}

        ts = now_str()
        cur = conn.execute(
            """INSERT INTO materials
               (owner_id, title, content, chars, ext, source_path,
                content_hash, local_only, is_public, note, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
            (owner, title, text, len(text), ext, source_path,
             h, 1 if local_only else 0, note, ts, ts),
        )
        mid = cur.lastrowid
        _set_tags(conn, mid, tags, owner)
        return {"status": "new", "id": mid, "title": title, "chars": len(text)}


# ----------------------------------------------------------------------
# 素材：读取
# ----------------------------------------------------------------------

def _row_to_dict(conn, row, with_content=False):
    """把数据库一行转成好用的字典"""
    d = {
        "id": row["id"],
        "title": row["title"],
        "chars": row["chars"],
        "ext": row["ext"],
        "source_path": row["source_path"],
        "local_only": bool(row["local_only"]),
        "note": row["note"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "tags": tags_of(conn, row["id"]),
    }
    if with_content:
        d["content"] = row["content"]
    return d


def list_materials(keyword="", tag="", owner=DEFAULT_OWNER, limit=200, offset=0):
    """列出素材。

    keyword  在标题或正文里找这个词
    tag      只看挂了这个标签的素材
    """
    sql = ["SELECT DISTINCT m.* FROM materials m"]
    params = []

    if tag:
        sql.append("""JOIN material_tags mt ON mt.material_id = m.id
                      JOIN tags t ON t.id = mt.tag_id""")

    sql.append("WHERE m.owner_id = ?")
    params.append(owner)

    if tag:
        sql.append("AND t.name = ?")
        params.append(tag)

    if keyword:
        sql.append("AND (m.title LIKE ? OR m.content LIKE ?)")
        like = "%" + keyword + "%"
        params.extend([like, like])

    sql.append("ORDER BY m.updated_at DESC, m.id DESC LIMIT ? OFFSET ?")
    params.extend([limit, offset])

    with connect() as conn:
        rows = conn.execute(" ".join(sql), params).fetchall()
        items = [_row_to_dict(conn, r) for r in rows]
        total = count_materials(keyword=keyword, tag=tag, owner=owner, conn=conn)
        return {"total": total, "items": items}


def count_materials(keyword="", tag="", owner=DEFAULT_OWNER, conn=None):
    """统计符合条件的素材数（用于列表分页显示"共 N 条"）"""
    own = conn is None
    if own:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

    try:
        sql = ["SELECT COUNT(DISTINCT m.id) AS c FROM materials m"]
        params = []
        if tag:
            sql.append("""JOIN material_tags mt ON mt.material_id = m.id
                          JOIN tags t ON t.id = mt.tag_id""")
        sql.append("WHERE m.owner_id = ?")
        params.append(owner)
        if tag:
            sql.append("AND t.name = ?")
            params.append(tag)
        if keyword:
            sql.append("AND (m.title LIKE ? OR m.content LIKE ?)")
            like = "%" + keyword + "%"
            params.extend([like, like])
        row = conn.execute(" ".join(sql), params).fetchone()
        return row["c"]
    finally:
        if own:
            conn.close()


def find_by_content(text, owner=DEFAULT_OWNER):
    """按内容找素材，返回 id 或 None。

    导入前的"预览"要用它来提示：这个文件其实已经在库里了。
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM materials WHERE owner_id = ? AND content_hash = ?",
            (owner, content_hash(text or "")),
        ).fetchone()
        return row["id"] if row else None


def get_material(mid, owner=DEFAULT_OWNER):
    """取一份素材的完整内容（含正文）"""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM materials WHERE id = ? AND owner_id = ?", (mid, owner)
        ).fetchone()
        if not row:
            return None
        return _row_to_dict(conn, row, with_content=True)


def stats(owner=DEFAULT_OWNER):
    """总览：素材数、总字数、标签数"""
    with connect() as conn:
        r = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(chars), 0) AS c "
            "FROM materials WHERE owner_id = ?",
            (owner,),
        ).fetchone()
        t = conn.execute(
            "SELECT COUNT(*) AS n FROM tags WHERE owner_id = ?", (owner,)
        ).fetchone()
        return {"materials": r["n"], "chars": r["c"], "tags": t["n"]}


# ----------------------------------------------------------------------
# 素材：修改与删除
# ----------------------------------------------------------------------

def update_material(mid, owner=DEFAULT_OWNER, title=None, tags=None,
                    local_only=None):
    """改素材的标题 / 标签 / 仅本地标记。传 None 表示这一项不动。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM materials WHERE id = ? AND owner_id = ?", (mid, owner)
        ).fetchone()
        if not row:
            return False

        sets, params = [], []
        if title is not None:
            sets.append("title = ?")
            params.append(title)
        if local_only is not None:
            sets.append("local_only = ?")
            params.append(1 if local_only else 0)
        if sets:
            sets.append("updated_at = ?")
            params.append(now_str())
            params.extend([mid, owner])
            conn.execute(
                "UPDATE materials SET %s WHERE id = ? AND owner_id = ?" % ", ".join(sets),
                params,
            )

        if tags is not None:
            _set_tags(conn, mid, tags, owner)

        return True


def delete_material(mid, owner=DEFAULT_OWNER):
    """删一份素材（中间表的关联记录会跟着自动清掉）"""
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM materials WHERE id = ? AND owner_id = ?", (mid, owner)
        )
        if cur.rowcount:
            # 顺手清理没有被任何素材使用的标签
            conn.execute(
                """DELETE FROM tags WHERE owner_id = ?
                   AND id NOT IN (SELECT DISTINCT tag_id FROM material_tags)""",
                (owner,),
            )
        return cur.rowcount > 0


# ----------------------------------------------------------------------
# 用户
#
# 注意这一层不知道"密码"是什么 —— 它只负责把别人算好的
# password_hash 和 salt 存进去。密码怎么搅碎是 auth.py 的事。
# 这么分是为了：想换加密算法时，只改 auth.py，数据库这层不用动。
# ----------------------------------------------------------------------

def count_users():
    """现在一共有几个账号"""
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def create_user(username, password_hash, salt):
    """建一个账号。用户名重复会返回 None（不抛异常，让上层决定怎么提示）。"""
    username = (username or "").strip()
    if not username:
        return None

    with connect() as conn:
        old = conn.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()
        if old:
            return None

        cur = conn.execute(
            """INSERT INTO users (username, password_hash, salt, created_at, last_login_at)
               VALUES (?, ?, ?, ?, '')""",
            (username, password_hash, salt, now_str()),
        )
        return {"id": cur.lastrowid, "username": username,
                "created_at": now_str()}


def get_user_by_name(username):
    """按用户名查账号（登录时用）。返回带 password_hash 和 salt 的完整行。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", ((username or "").strip(),)
        ).fetchone()
        return dict(row) if row else None


def get_user(user_id):
    """按 id 查账号（不带密码信息，可以放心返回给前端）"""
    with connect() as conn:
        row = conn.execute(
            "SELECT id, username, created_at, last_login_at FROM users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
        return dict(row) if row else None


def touch_last_login(user_id):
    """记一下"他刚登录过" """
    with connect() as conn:
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?",
                     (now_str(), int(user_id)))


def adopt_owner(old_owner, new_owner):
    """把 old_owner 名下的素材和标签全部转给 new_owner。

    用途：第一个账号注册时，把之前用命令行导入的 'local' 素材交接过去。
    限制：只有新账号名下一份素材都没有时才搬 ——
          否则两边的素材可能撞车（同一个内容指纹撞唯一约束），
          与其做复杂的合并，不如干脆不动，更安全。
    返回：搬过去的素材条数（0 表示什么都没搬）。
    """
    with connect() as conn:
        n_new = conn.execute(
            "SELECT COUNT(*) AS n FROM materials WHERE owner_id = ?", (new_owner,)
        ).fetchone()["n"]
        if n_new > 0:
            return 0

        n_old = conn.execute(
            "SELECT COUNT(*) AS n FROM materials WHERE owner_id = ?", (old_owner,)
        ).fetchone()["n"]
        if n_old == 0:
            return 0

        # 素材：直接改归属
        conn.execute("UPDATE materials SET owner_id = ? WHERE owner_id = ?",
                     (new_owner, old_owner))

        # 标签：先看新账号有没有同名标签，有就把关联改指过去，没有就整条改归属
        for t in conn.execute("SELECT id, name FROM tags WHERE owner_id = ?",
                              (old_owner,)).fetchall():
            exist = conn.execute(
                "SELECT id FROM tags WHERE owner_id = ? AND name = ?",
                (new_owner, t["name"]),
            ).fetchone()
            if exist:
                conn.execute(
                    "UPDATE OR IGNORE material_tags SET tag_id = ? WHERE tag_id = ?",
                    (exist["id"], t["id"]),
                )
                conn.execute("DELETE FROM material_tags WHERE tag_id = ?", (t["id"],))
                conn.execute("DELETE FROM tags WHERE id = ?", (t["id"],))
            else:
                conn.execute("UPDATE tags SET owner_id = ? WHERE id = ?",
                             (new_owner, t["id"]))

        # 收尾：清掉没素材在用的空标签
        conn.execute(
            """DELETE FROM tags WHERE owner_id = ?
               AND id NOT IN (SELECT DISTINCT tag_id FROM material_tags)""",
            (old_owner,),
        )
        return n_old


# ----------------------------------------------------------------------
# 登录凭据（会话）
#
# 流程：
#   登录成功 → 服务生成一串随机 token → 存进 sessions 表 + 塞进浏览器 Cookie
#   之后的每次请求 → 浏览器自动带回 token → 服务拿 token 查表 → 知道你是谁
#
# token 就是"门票"，它本身等价于密码。所以：
#   1. 用 secrets 生成（密码学安全的随机数），不是 random
#   2. 带有效期，过期自动失效
#   3. Cookie 设成 HttpOnly，网页里的 JS 读不到它（防脚本偷票）
# ----------------------------------------------------------------------

def create_session(token, user_id, expires_at):
    """发一张门票"""
    with connect() as conn:
        conn.execute(
            """INSERT INTO sessions (token, user_id, created_at, expires_at)
               VALUES (?, ?, ?, ?)""",
            (token, int(user_id), now_str(), expires_at),
        )


def get_session(token):
    """拿门票换人。门票不存在或已过期，都返回 None。"""
    token = (token or "").strip()
    if not token:
        return None

    with connect() as conn:
        row = conn.execute(
            """SELECT s.token, s.user_id, s.created_at, s.expires_at,
                      u.username
               FROM sessions s
               JOIN users u ON u.id = s.user_id
               WHERE s.token = ?""",
            (token,),
        ).fetchone()
        if not row:
            return None

        # 时间都是 "2026-09-23 21:30:00" 这种固定格式，
        # 这种格式下字符串比大小 = 时间比先后，不用转成日期对象。
        if row["expires_at"] <= now_str():
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            return None

        return {"token": row["token"], "user_id": row["user_id"],
                "username": row["username"], "expires_at": row["expires_at"]}


def delete_session(token):
    """退出登录：作废这张门票"""
    with connect() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE token = ?",
                           ((token or "").strip(),))
        return cur.rowcount > 0


def purge_expired_sessions():
    """清掉过期的门票（每次有人登录时顺手做一次，表不会越积越大）"""
    with connect() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_str(),))
        return cur.rowcount


# ----------------------------------------------------------------------
# 每次响应的耗时（llm_calls）
#
# 用途只有一个：回答"哪个模型快、哪个模型稳"。
# 界面上那是模型设置页里的一张对比表，所以这里的取舍都围着那张表转。
# ----------------------------------------------------------------------

# 保留多久。这不是素材，是性能数据 —— 半年前的网络状况对今天的判断没有帮助，
# 而表一直不清理的话，每次启动都要扫一遍全表。180 天足够她比出结论。
LLM_CALL_KEEP_DAYS = 180


def _ms(v):
    """把拿到的耗时折成一个安全的整数毫秒。

    为什么要过这一道：耗时是从 time.time() 差值算出来的浮点数，
    上游偶尔会给出负数或 None（时钟回拨、或者调用方没量），
    直接塞进库会变成奇怪的值，界面上显示成 -0.00 秒。"""
    try:
        n = int(round(float(v or 0)))
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _tok(usage, name):
    """从 usage 里取一个 token 数。取不到就是 0 —— 有些服务商不返回。"""
    try:
        return max(0, int((usage or {}).get(name) or 0))
    except (TypeError, ValueError, AttributeError):
        return 0


def record_llm_call(purpose="", model_key="", model_name="", provider="",
                    streamed=False, ttft_ms=0, elapsed_ms=0, ok=True, error="",
                    usage=None, chars=0, finish_reason="", fallback=""):
    """记一行"这次跟模型说话花了多久"。

    【为什么整个函数包在 try 里】
    它是在任务线程里被调用的，而它记的只是"性能数据"——
    为了让对比表多一行，把一整批分类搞失败，是彻底不划算的。
    所以这里出错就**安静地放弃这一行**，绝不让异常冒到调用方。

    参数都是"能少给就少给"，缺的一方按默认值走：
      purpose       用途（classify / infuse / outline / probe），界面上分组用
      model_key     内部短名字（认历史记录的那个）
      model_name    真发给服务商的名字
      provider      接入点名字（可能为空，老配置没有这一层）
      streamed      这次是不是按流式收的
      ttft_ms       首字耗时。**非流式收的时候量不出来，记 0** ——
                    记 0 表示"这次没量到"，跟"首字 0 毫秒"不是一回事，
                    所以下面求平均时会把它排除在外。
      fallback      退回非流式的原因（'' = 没退）
    """
    try:
        with connect() as conn:
            conn.execute(
                """INSERT INTO llm_calls
                   (created_at, purpose, model_key, model_name, provider,
                    streamed, ttft_ms, elapsed_ms, ok, error,
                    prompt_tokens, completion_tokens, total_tokens,
                    chars, finish_reason, fallback)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (now_str(), str(purpose or ""), str(model_key or ""),
                 str(model_name or ""), str(provider or ""),
                 1 if streamed else 0, _ms(ttft_ms), _ms(elapsed_ms),
                 1 if ok else 0, str(error or "")[:500],
                 _tok(usage, "prompt_tokens"), _tok(usage, "completion_tokens"),
                 _tok(usage, "total_tokens"), max(0, int(chars or 0)),
                 str(finish_reason or ""), str(fallback or "")))
    except BaseException:
        return False
    return True


def purge_llm_calls(days=None):
    """丢掉太老的耗时记录。启动时顺手做一次。"""
    n = int(days or LLM_CALL_KEEP_DAYS)
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM llm_calls WHERE created_at <= datetime('now', 'localtime', ?)",
            ("-%d days" % n,))
        return cur.rowcount


def llm_call_stats(days=30, limit=50000):
    """按模型汇总：调用几次、成功几次、首字多快、整段多久、烧了多少 token。

    【为什么在 Python 里汇总，不写成一条 SQL】
    这条汇总要把"成功"和"失败"分开算（失败那次的首字耗时是量不到的，
    混进去会把平均首字拉低，得出"这个模型很快"的错误结论）。
    SQL 里写一堆 CASE WHEN 是能写出来，但以后谁改都会改错；
    这点数据量（本机自用、几千行）在 Python 里算，一眼能看懂。

    days     只看最近多少天
    limit    最多扫多少行，防止表被养到几十万行时把界面拖住
    """
    d = max(1, int(days or 30))
    with connect() as conn:
        rows = conn.execute(
            """SELECT model_key, model_name, purpose, ok, ttft_ms, elapsed_ms,
                      total_tokens, chars, streamed, fallback, created_at
               FROM llm_calls
               WHERE created_at > datetime('now','localtime', ?)
               ORDER BY id DESC LIMIT ?""",
            ("-%d days" % d, max(1, int(limit or 50000)))).fetchall()

    agg = {}
    for r in rows:
        k = r["model_key"] or r["model_name"] or "（没记模型）"
        a = agg.get(k)
        if a is None:
            a = agg[k] = {"model_key": k, "model_name": r["model_name"] or "",
                          "calls": 0, "ok": 0, "bad": 0,
                          "ttft_n": 0, "ttft_sum": 0, "ttft_min": 0,
                          "el_sum": 0, "tokens": 0, "chars": 0,
                          "streamed": 0, "fallback": 0, "last": ""}
        a["calls"] += 1
        a["el_sum"] += r["elapsed_ms"] or 0       # 总耗时连失败的算进去
        a["tokens"] += r["total_tokens"] or 0
        if not a["last"] or r["created_at"] > a["last"]:
            a["last"] = r["created_at"]
        if r["streamed"]:
            a["streamed"] += 1
        if r["fallback"]:
            a["fallback"] += 1
        if r["ok"]:
            a["ok"] += 1
            a["chars"] += r["chars"] or 0
            t = r["ttft_ms"] or 0
            if t > 0:                             # 0 = 这次没量到，不算进平均
                a["ttft_n"] += 1
                a["ttft_sum"] += t
                if not a["ttft_min"] or t < a["ttft_min"]:
                    a["ttft_min"] = t
        else:
            a["bad"] += 1

    out = []
    for a in agg.values():
        n = a["ttft_n"]
        out.append({
            "model_key": a["model_key"],
            "model_name": a["model_name"],
            "calls": a["calls"],
            "ok": a["ok"],
            "bad": a["bad"],
            "ttft_n": n,
            "ttft_avg": int(round(a["ttft_sum"] / n)) if n else 0,
            "ttft_min": a["ttft_min"],
            "elapsed_avg": int(round(a["el_sum"] / a["calls"])) if a["calls"] else 0,
            "tokens": a["tokens"],
            "chars": a["chars"],
            "streamed": a["streamed"],
            "fallback": a["fallback"],
            "last": a["last"],
        })
    # 默认按"调用次数多的排前面"—— 样本太少的那几行排在后面，
    # 免得她拿一次调用的平均值当结论。
    out.sort(key=lambda x: (-x["calls"], x["model_key"]))
    return {"days": d, "rows": out, "scanned": len(rows)}


# ----------------------------------------------------------------------
# 命令行自测：python backend/db.py
# ----------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    init_db()
    print("数据库位置：", DB_PATH)
    print("统计：", stats())
    print("标签：", list_tags())
    print("素材条数：", list_materials(limit=5)["total"])
    print("账号数：", count_users())
    print("尚未归属任何账号的素材：", stats(owner=DEFAULT_OWNER)["materials"], "条")
