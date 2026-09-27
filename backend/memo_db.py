"""备忘录 —— 数据层。

一张表 memos，纯新增空表，按账号隔离。

------------------------------------------------------------------
【为什么备忘录要进数据库，而不是存在浏览器里】

它跟"还没点保存的表单草稿"不是一回事：

    草稿是"我正在填的东西"，浏览器缓存丢了重填一遍就行；
    备忘录是"我攒下来准备用的东西" —— 她是从素材浮窗里一行一行
    挑出来粘进去的，**挑这个动作本身就是成本**。

存在浏览器里，换个浏览器 / 清一次缓存就没了，而且不会有任何提示。
所以它跟素材、章节一样，是账号下的数据，进库。

------------------------------------------------------------------
【为什么一个账号可以有好几条】

她要的入口是"一栏空白的备忘录区域"，所以第一次打开时就是**一条空白的**。

但"攒参考"这件事天然会分成几摊：这一章要用的、外貌描写的备选、
以后想用还没想好放哪的。一格装不下。

    → 默认一条；点「＋」能再加。
    → 界面在只有一条时**不显示条目列表**，
      跟"没有这个功能"长得一模一样，不给她添认知负担。

------------------------------------------------------------------
【为什么这里不做"自动清理空条目"】

她新建了又没写字的空条目，看着是垃圾。但"它是空的"这件事只有
我们觉得，她可能只是先占个位。**不替她删东西** ——
真嫌乱，界面上那个「删」是她自己的手。
"""

from datetime import datetime

from . import db


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------
# 上限
#
# 备忘录就是"粘贴板"，她粘一整章进来也得装得下 —— 所以给到 20 万，
# 跟单章正文一个量级。**但仍然是超长直接报错、不静默截断**：
# 背着她切掉一段，比报错恶劣得多（她会以为系统存下了）。
# ----------------------------------------------------------------------

TITLE_MAX = 60
CONTENT_MAX = 200000

# 一个账号最多攒这么多条。给得很宽 —— 这个数字唯一的用途是
# 挡住"脚本疯狂建条目把库撑爆"，不是限制她正常用。
MEMO_MAX_COUNT = 200


SCHEMA = """
CREATE TABLE IF NOT EXISTS memos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id    TEXT    NOT NULL,
    title       TEXT    NOT NULL DEFAULT '',
    content     TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT '',
    updated_at  TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_memos_owner ON memos (owner_id, id);
"""


# 表驱动补列。现在是空的 —— memos 是**一次性建好的新表**，
# 没有"先建了表、后来才想起要加列"的历史。
# 留着这个变量是因为以后真加了列，必须走这里：
# CREATE TABLE IF NOT EXISTS **不会**给已经建过的表补列。
# （这条 2026-09-27 在 chapters 上真踩过，写作输入那五列全丢。）
_ADDED_COLUMNS = ()


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


def migrate(verbose=False):
    """建表。反复跑是安全的。"""
    with db.connect() as conn:
        conn.executescript(SCHEMA)
        _add_missing_columns(conn)
    if verbose:
        print("备忘录：一张表就位 →", db.DB_PATH)
    return db.DB_PATH


# ----------------------------------------------------------------------
# 小工具
#
# 【为什么不借 chapter_db 的那几个】它们是那个模块的私有函数，
# 借过来的话，那边哪天改了行为，这边会**静默**跟着变。
# 这十几行重复不值得省。
# ----------------------------------------------------------------------

def _txt(v, limit, name, allow_empty=True):
    """一段纯文本：去两端空白、卡长度。

    这里是**她手填的**内容，所以超长直接报错、绝不偷偷截断。
    理由见模块头。
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


def display_title(memo):
    """界面上那条备忘录叫什么。

    【为什么标题可以是空的】她打开浮窗就该直接开始粘贴，
    不该先被迫起个名字。所以标题留空是正常状态，
    显示时退到"正文第一行的前 20 字"，再不行就叫「空白备忘录」。

    这个退路只影响**显示**，不写回库里 ——
    否则她清空标题、我们塞一个进去，下次她再清就清不掉了。
    """
    t = (memo.get("title") or "").strip()
    if t:
        return t
    first = ""
    for line in (memo.get("content") or "").splitlines():
        if line.strip():
            first = line.strip()
            break
    if first:
        return first[:20] + ("…" if len(first) > 20 else "")
    return "空白备忘录"


def _memo_row(row, with_content=False):
    if row is None:
        return None
    content = row["content"] or ""
    title = row["title"] or ""
    d = {
        "id": row["id"],
        "title": title,
        "chars": len(content),
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
        # 界面上那条叫什么，**服务端算好一起发下去**。
        # 为什么不留给前端算：清单接口默认不带正文（几万字），
        # 前端手里没有 content，就退不到"正文第一行"这个兜底，
        # 于是同一件事会出现两个不同的名字（清单里叫「空白备忘录」、
        # 编辑区标题却叫「第一行」）。名字只许有一处定。
        "display_title": display_title({"title": title, "content": content}),
    }
    if with_content:
        d["content"] = content
    return d


# ----------------------------------------------------------------------
# 读
# ----------------------------------------------------------------------

def list_memos(owner, with_content=False):
    """这个账号的所有备忘录。

    【默认不带正文】她会往里粘几万字。列表上只需要"叫什么、多少字、
       什么时候动的" —— 每条都带正文的话，一次开窗就是几万字的传输。

    排序：最近动的在前。她刚粘进去的那条应该在最上面。
        （用 id DESC 当第二排序键 —— 同一秒建的两条要有稳定顺序，
          否则列表会在两次刷新之间换位置。）
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM memos WHERE owner_id=? ORDER BY id DESC",
            (owner,)).fetchall()
    return [_memo_row(r, with_content) for r in rows]


def get_memo(owner, memo_id):
    """一条备忘录的全文。不是自己的，一律当"没有这条"。"""
    try:
        mid = int(memo_id)
    except (TypeError, ValueError):
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM memos WHERE id=? AND owner_id=?",
            (mid, owner)).fetchone()
    return _memo_row(row, True)


def count_memos(owner):
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM memos WHERE owner_id=?",
                            (owner,)).fetchone()["n"]


# ----------------------------------------------------------------------
# 写
# ----------------------------------------------------------------------

def create_memo(owner, title="", content=""):
    """新建一条。标题可以空 —— 让她打开就能粘。"""
    t = _txt(title, TITLE_MAX, "标题")
    c = _txt(content, CONTENT_MAX, "内容")
    ts = now_str()
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM memos WHERE owner_id=?",
                         (owner,)).fetchone()["n"]
        if n >= MEMO_MAX_COUNT:
            raise ValueError("一个账号最多 %d 条备忘录，现在已经有 %d 条了。"
                             "先删掉几条不用的再建。" % (MEMO_MAX_COUNT, n))
        cur = conn.execute(
            "INSERT INTO memos (owner_id, title, content, created_at, updated_at)"
            " VALUES (?,?,?,?,?)", (owner, t, c, ts, ts))
        mid = cur.lastrowid
        row = conn.execute("SELECT * FROM memos WHERE id=?", (mid,)).fetchone()
    return _memo_row(row, True)


def update_memo(owner, memo_id, patch):
    """改一条。PATCH 语义：只改 patch 里**出现过**的字段。

    空串 = 清空（她真的把内容全删了），不是"没改"。
    这两件事必须分开 —— 合起来的话她删不掉东西，
    而且**删完一刷新内容又回来了**，看起来像"保存没生效"。

    返回 (备忘录, 错误信息)。错误信息非空时备忘录为 None。

    【为什么不用 ValueError 往外抛】这是接口层直接调的函数，
    校验失败是**预期内**的结果（她粘了 30 万字），
    抛异常会让调用处得包一层 try 才能区分"她写超了"和"数据库坏了"。
    """
    try:
        mid = int(memo_id)
    except (TypeError, ValueError):
        return None, "找不到这条备忘录。"
    patch = patch or {}
    try:
        sets, args = [], []
        if "title" in patch:
            sets.append("title=?")
            args.append(_txt(patch.get("title"), TITLE_MAX, "标题"))
        if "content" in patch:
            sets.append("content=?")
            args.append(_txt(patch.get("content"), CONTENT_MAX, "内容"))
    except ValueError as e:
        return None, str(e)
    if not sets:
        # 一个字段都没传 = 她只是切了个条目、没打字。
        # 不当成错误（前端会把"切条目"也走一遍保存），原样返回。
        return get_memo(owner, mid), ""
    sets.append("updated_at=?")
    args.append(now_str())
    with db.connect() as conn:
        cur = conn.execute("UPDATE memos SET " + ", ".join(sets) +
                           " WHERE id=? AND owner_id=?", args + [mid, owner])
        if not cur.rowcount:
            return None, "找不到这条备忘录。"
        row = conn.execute("SELECT * FROM memos WHERE id=?", (mid,)).fetchone()
    return _memo_row(row, True), ""


def delete_memo(owner, memo_id):
    try:
        mid = int(memo_id)
    except (TypeError, ValueError):
        return False
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM memos WHERE id=? AND owner_id=?",
                           (mid, owner))
        return bool(cur.rowcount)


# ----------------------------------------------------------------------
# 自检
# ----------------------------------------------------------------------

def _self_check():
    """只测纯函数和不落库的那部分 —— **不碰数据库**。

    【为什么不能在这里建临时库】
    db.py 在 import 的那一刻就把 DATA_DIR 定死了（见 db.py 第 48 行），
    运行时再改 MOGE_DATA_DIR 是没用的 —— 会直接写到她真实的库上。
    要测"建/改/删/跨账号"这些真落库的行为，走 tests/test_memo.py，
    那个脚本在 import 之前就把数据目录指到临时目录了。
    """
    ok = True

    def eq(a, b, what):
        nonlocal ok
        if a != b:
            ok = False
            print("  [x] %s: %r != %r" % (what, a, b))
        else:
            print("  [v] %s" % what)

    eq(display_title({"title": "", "content": ""}), "空白备忘录",
       "全空的叫「空白备忘录」")
    eq(display_title({"title": "", "content": "\n\n  第一行\n第二行"}),
       "第一行", "没标题时退到正文第一行（跳过空行）")
    eq(display_title({"title": "", "content": "x" * 30}),
       "x" * 20 + "…", "退路标题也会截到 20 字并带省略号")
    eq(display_title({"title": "我叫这个", "content": "别的"}), "我叫这个",
       "有标题就用标题")

    eq(_txt("  a  ", 10, "测试项"), "a", "两端空白去掉")
    eq(_txt("", 10, "测试项"), "", "空串正常返回（标题可以空）")
    try:
        _txt("啊" * 10, 5, "测试项")
        ok = False
        print("  [x] 超长没报错")
    except ValueError as e:
        eq("最长 5 字" in str(e), True, "手填内容超长会报错（不偷偷截断）")
    try:
        _txt("", 5, "测试项", allow_empty=False)
        ok = False
        print("  [x] 必填项给了空串却没报错")
    except ValueError:
        print("  [v] 必填项给空串会报错")

    print("备忘录自检：" + ("全部通过" if ok else "有问题，见上面"))
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if _self_check() else 1)
