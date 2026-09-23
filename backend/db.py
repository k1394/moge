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

# 数据库目录与文件
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "moge.db")

# 现阶段只有本机一个用户。将来接入登录后，这里换成真实用户 ID。
DEFAULT_OWNER = "local"


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

CREATE INDEX IF NOT EXISTS idx_materials_owner ON materials(owner_id);
CREATE INDEX IF NOT EXISTS idx_material_tags_tag ON material_tags(tag_id);
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
