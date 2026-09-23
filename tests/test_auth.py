# -*- coding: utf-8 -*-
"""
墨阁 · 账号模块单元测试
========================================================
测的是 auth.py 和 db.py 里的账号逻辑，不需要启动服务，直接跑：

    .venv\\Scripts\\python.exe tests\\test_auth.py

单元测试和冒烟测试的区别：
    冒烟测试  把整个网站跑一遍，看有没有哪块坏了（粗筛）
    单元测试  盯着一个函数，把各种输入都试一遍（细查）

这个文件用的是 Python 自带的 unittest，不用额外装东西。

--------------------------------------------------------
⚠ 这个测试跑在"另一个数据库"上，不会碰你的真实素材
--------------------------------------------------------
它做的第一件事，是把环境变量 MOGE_DATA_DIR 指到一个临时目录，
于是 db.py 认为自己的数据库在临时目录里 —— 你真实的
G:\\docker\\moge\\data\\moge.db 从头到尾没有被打开过。

为什么必须这么小心（真实教训）：
    早期版本的这个测试直接在真实库上跑。其中"认领素材"那项测试
    会把没有归属的素材全搬到一个测试账号下，测试结束时
    按惯例清场，就连素材带账号一起删了 ——
    测试全绿，真实素材没了。
    这类事故在工业界有个名字：测试污染了生产数据。

另外文件末尾还有一道保险：跑之前先核对数据库路径，
万一环境变量没生效，直接中止，绝不带病运行。
"""

import atexit
import os
import secrets
import shutil
import sys
import tempfile
import unittest

# ---- 隔离：必须在导入 db 之前把数据目录改掉 ----
# 因为 db 里的 DATA_DIR 是"模块级变量"，在 import 的那一刻就定下来了。
_TESTDB_DIR = tempfile.mkdtemp(prefix="moge_testdb_")
os.environ["MOGE_DATA_DIR"] = _TESTDB_DIR
atexit.register(lambda: shutil.rmtree(_TESTDB_DIR, ignore_errors=True))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backend import auth, db


# ---- 保险：确认真的隔离成功了，否则宁可整个不跑 ----
if not os.path.abspath(db.DB_PATH).startswith(os.path.abspath(_TESTDB_DIR)):
    print("=" * 64)
    print("  [已中止] 测试并没有跑在隔离的临时数据库上！")
    print("  当前会动到的数据库：", db.DB_PATH)
    print("  这是为了保护你的真实素材，请不要绕过这道检查。")
    print("=" * 64)
    sys.exit(1)


def _tmp_name():
    """造一个本次测试专用的用户名，避免和你真实的账号撞车"""
    return "测试用户_" + secrets.token_hex(4)


class TestPassword(unittest.TestCase):
    """密码搅拌"""

    def test_salt_is_random(self):
        """两次生成的盐必须不同，否则盐就没意义了"""
        self.assertNotEqual(auth.make_salt(), auth.make_salt())

    def test_same_password_different_salt_gives_different_hash(self):
        """同一个密码配不同的盐，搅碎结果必须不同 —— 这是盐存在的全部理由"""
        s1, s2 = auth.make_salt(), auth.make_salt()
        self.assertNotEqual(auth.hash_password("pass-a", s1),
                            auth.hash_password("pass-a", s2))

    def test_same_password_same_salt_is_stable(self):
        """同一密码同一个盐，每次算出来必须一样 —— 登录比对靠这条"""
        s = auth.make_salt()
        self.assertEqual(auth.hash_password("pass-a", s),
                         auth.hash_password("pass-a", s))

    def test_hash_is_not_the_password(self):
        """库里存的绝不能是原密码本身"""
        s = auth.make_salt()
        h = auth.hash_password("pass-a", s)
        self.assertNotIn("pass-a", h)
        self.assertEqual(len(h), 64)          # sha256 → 32 字节 → 64 位十六进制

    def test_verify_accepts_correct(self):
        s = auth.make_salt()
        h = auth.hash_password("pass-b", s)
        self.assertTrue(auth.verify_password("pass-b", s, h))

    def test_verify_rejects_wrong(self):
        s = auth.make_salt()
        h = auth.hash_password("pass-b", s)
        self.assertFalse(auth.verify_password("pass-c", s, h))
        self.assertFalse(auth.verify_password("", s, h))
        self.assertFalse(auth.verify_password("pass-b", s, ""))

    def test_verify_is_case_sensitive(self):
        s = auth.make_salt()
        h = auth.hash_password("passD", s)
        self.assertFalse(auth.verify_password("passd", s, h))


class TestRules(unittest.TestCase):
    """用户名与密码的规则校验"""

    def test_username_ok(self):
        for n in ["折腰", "zheyao", "zhe_yao", "abc123", "折腰-01"]:
            self.assertEqual(auth.check_username(n), "", n)

    def test_username_bad(self):
        for n in ["", " ", "a", "x" * 21, "折腰 呀", "a@b"]:
            self.assertNotEqual(auth.check_username(n), "", repr(n))

    def test_password_ok(self):
        for p in ["123456", "a" * 128]:
            self.assertEqual(auth.check_password(p), "")

    def test_password_bad(self):
        for p in ["", "12345", "a" * 129]:
            self.assertNotEqual(auth.check_password(p), "", repr(p))


class TestSession(unittest.TestCase):
    """登录凭据"""

    def test_token_is_random_and_long(self):
        t1, t2 = auth.new_token(), auth.new_token()
        self.assertNotEqual(t1, t2)
        self.assertGreaterEqual(len(t1), 32)

    def test_session_lifecycle(self):
        """建号 → 登录 → 查得到 → 退出 → 查不到"""
        db.init_db()
        name = _tmp_name()
        salt = auth.make_salt()
        u = db.create_user(name, auth.hash_password("pw-123", salt), salt)
        self.assertIsNotNone(u)

        try:
            token = auth.new_token()
            db.create_session(token, u["id"], auth.expires_at_str())

            sess = db.get_session(token)
            self.assertIsNotNone(sess)
            self.assertEqual(sess["username"], name)

            # 退出后，同一张票立刻作废
            self.assertTrue(db.delete_session(token))
            self.assertIsNone(db.get_session(token))
        finally:
            _purge_user(u["id"])

    def test_unknown_token_returns_none(self):
        self.assertIsNone(db.get_session("这肯定不是一张有效的票"))

    def test_expired_session_is_rejected(self):
        """过期的票要被认出来，并且顺手清掉"""
        db.init_db()
        name = _tmp_name()
        salt = auth.make_salt()
        u = db.create_user(name, auth.hash_password("pw-123", salt), salt)
        try:
            token = auth.new_token()
            db.create_session(token, u["id"], "2000-01-01 00:00:00")   # 早就过期了
            self.assertIsNone(db.get_session(token))
        finally:
            _purge_user(u["id"])


class TestOwnerIsolation(unittest.TestCase):
    """素材归属：两个账号之间必须互相看不见"""

    def test_materials_are_isolated(self):
        db.init_db()
        a_name, b_name = _tmp_name(), _tmp_name()
        sa, sb = auth.make_salt(), auth.make_salt()
        ua = db.create_user(a_name, auth.hash_password("pw-a", sa), sa)
        ub = db.create_user(b_name, auth.hash_password("pw-b", sb), sb)
        oa, ob = db.owner_of(ua["id"]), db.owner_of(ub["id"])

        try:
            db.save_material(title="A的素材", text="这是甲写的东西",
                             tags=["甲标签"], owner=oa)
            db.save_material(title="B的素材", text="这是乙写的东西",
                             tags=["乙标签"], owner=ob)

            self.assertEqual(db.stats(owner=oa)["materials"], 1)
            self.assertEqual(db.stats(owner=ob)["materials"], 1)

            ta = [t["name"] for t in db.list_tags(owner=oa)]
            tb = [t["name"] for t in db.list_tags(owner=ob)]
            self.assertIn("甲标签", ta)
            self.assertNotIn("乙标签", ta)
            self.assertIn("乙标签", tb)

            # A 搜不到 B 的内容
            self.assertEqual(
                db.list_materials(keyword="乙写", owner=oa)["total"], 0)
            # B 也搜不到 A 的
            self.assertEqual(
                db.list_materials(keyword="甲写", owner=ob)["total"], 0)
        finally:
            _purge_user(ua["id"])
            _purge_user(ub["id"])

    def test_same_content_can_exist_in_two_accounts(self):
        """同一条内容，两个账号各存一份是允许的（去重只在同一个账号内生效）"""
        db.init_db()
        a_name, b_name = _tmp_name(), _tmp_name()
        sa, sb = auth.make_salt(), auth.make_salt()
        ua = db.create_user(a_name, auth.hash_password("pw-a", sa), sa)
        ub = db.create_user(b_name, auth.hash_password("pw-b", sb), sb)
        oa, ob = db.owner_of(ua["id"]), db.owner_of(ub["id"])
        text = "一段两边都想存下来的描写"

        try:
            r1 = db.save_material(title="甲存", text=text, owner=oa)
            r2 = db.save_material(title="乙也存", text=text, owner=ob)
            self.assertEqual(r1["status"], "new")
            self.assertEqual(r2["status"], "new")

            # 同一个账号里再存一遍，才该被认成重复
            r3 = db.save_material(title="甲又存", text=text, owner=oa)
            self.assertEqual(r3["status"], "same")
        finally:
            _purge_user(ua["id"])
            _purge_user(ub["id"])


class TestAdopt(unittest.TestCase):
    """认领：第一个账号把 local 素材接过去"""

    def test_adopt_moves_materials_and_tags(self):
        db.init_db()
        name = _tmp_name()
        salt = auth.make_salt()
        u = db.create_user(name, auth.hash_password("pw-x", salt), salt)
        owner = db.owner_of(u["id"])
        text = "认领测试专用内容_" + secrets.token_hex(4)

        db.save_material(title="游离素材", text=text,
                         tags=["游离标签"], owner=db.DEFAULT_OWNER)
        try:
            moved = db.adopt_owner(db.DEFAULT_OWNER, owner)
            self.assertGreaterEqual(moved, 1)

            got = db.list_materials(keyword=text, owner=owner)
            self.assertGreaterEqual(got["total"], 1)

            names = [t["name"] for t in db.list_tags(owner=owner)]
            self.assertIn("游离标签", names)
        finally:
            _purge_user(u["id"])
            # 把这次测试塞进 local 的素材清掉
            for it in db.list_materials(keyword=text, owner=db.DEFAULT_OWNER)["items"]:
                db.delete_material(it["id"], owner=db.DEFAULT_OWNER)

    def test_adopt_skips_when_new_owner_has_data(self):
        """目标账号已经有素材时不搬运，避免撞车"""
        db.init_db()
        name = _tmp_name()
        salt = auth.make_salt()
        u = db.create_user(name, auth.hash_password("pw-y", salt), salt)
        owner = db.owner_of(u["id"])
        db.save_material(title="已有", text="目标账号本来就有的东西", owner=owner)
        try:
            self.assertEqual(db.adopt_owner(db.DEFAULT_OWNER, owner), 0)
        finally:
            _purge_user(u["id"])


def _purge_user(user_id):
    """测试收尾：把这个测试账号的东西全删掉"""
    owner = db.owner_of(user_id)
    for it in db.list_materials(owner=owner, limit=1000)["items"]:
        db.delete_material(it["id"], owner=owner)
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


if __name__ == "__main__":
    print("=" * 64)
    print("  墨阁账号模块测试（跑在隔离的临时数据库上）")
    print("  本次使用的库：", db.DB_PATH)
    print("  （你的真实素材库 data/moge.db 本次不会被打开）")
    print("=" * 64)
    unittest.main(verbosity=2)
