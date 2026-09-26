# -*- coding: utf-8 -*-
"""
墨阁 · 逻辑素材组 接口测试
========================================================
测什么：她在「素材分类」页上看到那一排「这几段合起来是一条素材」，
点确认 / 忽略 / 撤销 / 恢复，一路走过去对不对。

守的规矩：
    ① 未登录一律进不去；别人的组看不见也动不了
    ② 没点确认之前，库里一个字节都不变（提议 ≠ 已合并）
    ③ 确认之后：几段标「已排除」、长出一条新卡，原文一个字没变
    ④ 撤销之后完全复原（原几段回来、新卡退场、父子关系清干净）
    ⑤ 「忽略」只动组的状态，卡片一张都不碰
    ⑥ 接口连的不是真实库

怎么跑（不需要先启动墨阁）：

    cd G:\\docker\\moge
    .venv\\Scripts\\python.exe tests\\test_groups_api.py

它自己会起一个用临时库的服务、跑完自己关掉。
"""

import hashlib
import http.cookiejar
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

# 隔离：**必须在 import backend 之前**把数据目录指到临时目录。
# 这个测试进程要直连库造数据（组没法从接口造 —— 它本来是模型产出的），
# 所以它跟服务必须连同一个临时库。晚一步设置就指到她的真库了。
#
# 【为什么允许外面指定】（界面验收脚本 C:\tmp\moge_ui\ui_check_groups.py
# 要复用这里的造数据函数，而临时目录由它自己管）
# 它先把 MOGE_DATA_DIR 设成自己的目录，再 import 本模块 ——
# 于是这里不再新建目录、也不负责删，两边用同一个库。
_GIVEN = os.environ.get("MOGE_DATA_DIR")
_TMP = _GIVEN or tempfile.mkdtemp(prefix="moge_gapi_")
_OWN_TMP = _GIVEN is None
os.environ["MOGE_DATA_DIR"] = _TMP

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backend import db, classify_db as cdb, segmentation as sg   # noqa: E402

OK = FAIL = 0


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  [通过] %s %s" % (name, extra))
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, extra))


def same(name, got, want):
    global OK, FAIL
    if got == want:
        OK += 1
        print("  [通过] %s" % name)
    else:
        FAIL += 1
        print("  [失败] %s  得到 %r，期望 %r" % (name, got, want))


# ----------------------------------------------------------------------
# 起一个用临时库的服务
# ----------------------------------------------------------------------

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_server(data_dir, port):
    """这个测试起的服务用的是当前解释器（sys.executable），
    它必须装了 uvicorn 和 fastapi —— 也就是项目自己的 .venv\\Scripts\\python.exe。
    输出写进文件而不是丢掉：起不来时能看见原因。"""
    env = dict(os.environ)
    env["MOGE_DATA_DIR"] = data_dir
    env["PYTHONIOENCODING"] = "utf-8"
    log = open(os.path.join(data_dir, "server.log"), "w", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=PROJECT_DIR, env=env,
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)


def dump_server_log(data_dir, tail=15):
    path = os.path.join(data_dir, "server.log")
    print("  用的解释器：%s" % sys.executable)
    if not os.path.isfile(path):
        print("  没有服务日志（%s），服务可能在启动前就退出了。" % path)
        return
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().strip().splitlines()
    for ln in (lines or ["(空)"])[-tail:]:
        print("    | " + ln)


def wait_ready(base, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(base + "/api/health"), timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def stop_server(proc):
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass


class Client(object):
    """一个"浏览器" = 一个 Cookie 罐 = 一个账号。"""

    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method, path, data=None):
        p = urllib.parse.quote(path, safe="/?=&#")
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(self.base + p, data=body, method=method)
        if body:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=120) as r:
                raw, code = r.read(), r.status
        except urllib.error.HTTPError as e:
            raw, code = e.read(), e.code
        try:
            return code, json.loads(raw.decode("utf-8"))
        except Exception:
            return code, raw.decode("utf-8", "replace")

    def ok(self, method, path, data=None):
        code, d = self.call(method, path, data)
        if code >= 300:
            raise AssertionError("%s %s → HTTP %s %s" % (method, path, code, d))
        return d


# ----------------------------------------------------------------------
# 造数据（直连库 —— 组本来是模型产出的，没有"手工建组"的接口）
# ----------------------------------------------------------------------

LINES = ["第一段的内容", "第二段的内容", "第三段的内容",
         "第四段的内容", "第五段的内容", "第六段的内容"]
TEXT = "\n".join(LINES)
CAT_NAME = "神态"


def owner_of(username):
    with db.connect() as conn:
        r = conn.execute("SELECT id FROM users WHERE username=?",
                         (username,)).fetchone()
        return db.owner_of(r["id"]) if r else None


def make_material(owner, title, text):
    r = db.save_material(title, text, owner=owner)
    mid = r["id"]
    offs, pos = [], 0
    for line in text.split("\n"):
        offs.append((pos, pos + len(line)))
        pos += len(line) + 1
    ids = []
    ts = db.now_str()
    with db.connect() as conn:
        for s, e in offs:
            cur = conn.execute(
                """INSERT INTO cards
                   (owner_id, material_id, start_offset, end_offset,
                    source_text_hash, segment_id, source, status,
                    operation_group_id, created_at, updated_at)
                   VALUES (?,?,?,?,?,NULL,?,?,?,?,?)""",
                (owner, mid, s, e, sg.text_hash(text[s:e]),
                 sg.SOURCE_INHERITED, sg.STATUS_PENDING, "", ts, ts))
            ids.append(cur.lastrowid)
    return mid, ids


def card_of(cid, owner):
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM cards WHERE id=? AND owner_id=?",
                         (cid, owner)).fetchone()
        return dict(r) if r else None


# ======================================================================

def main():
    data_dir = _TMP
    proc = None
    try:
        port = free_port()
        base = "http://127.0.0.1:%d" % port
        print("=" * 66)
        print("逻辑素材组 · 接口测试")
        print("临时库目录：", data_dir)
        print("=" * 66)

        proc = start_server(data_dir, port)
        if not wait_ready(base):
            print("服务没起来，测试中止。")
            dump_server_log(data_dir)
            return 1

        probe = Client(base)

        # ---- 隔离保险 -----------------------------------------------
        print("\n【0】隔离保险")
        _, health = probe.call("GET", "/api/health")
        dbp = os.path.abspath(health.get("db", ""))
        if os.path.abspath(data_dir) not in dbp:
            print("！！服务连的不是临时库：", dbp)
            print("！！为避免动到真实数据，测试拒绝继续。")
            return 1
        check("服务连的是临时库", True, dbp)

        # ---- 未登录 -------------------------------------------------
        print("\n【1】未登录一律进不去")
        for path, method in [("/api/groups", "GET"),
                             ("/api/groups/1/confirm", "POST"),
                             ("/api/groups/1/ignore", "POST"),
                             ("/api/groups/1/undo", "POST"),
                             ("/api/groups/1/restore", "POST")]:
            code, _ = probe.call(method, path)
            check("未登录 %s %s → 401" % (method, path), code == 401,
                  "HTTP %s" % code)

        # ---- 账号 ---------------------------------------------------
        print("\n【2】账号与造数据")
        A = Client(base)
        B = Client(base)
        ua = "grp_a_" + secrets.token_hex(3)
        ub = "grp_b_" + secrets.token_hex(3)
        A.ok("POST", "/api/auth/register", {"username": ua, "password": "pw123456"})
        B.ok("POST", "/api/auth/register", {"username": ub, "password": "pw123456"})
        oa, ob = owner_of(ua), owner_of(ub)
        check("两个账号的 owner 各不相同", oa != ob, "%s / %s" % (oa, ob))

        mid, ids = make_material(oa, "组接口测试稿", TEXT)
        c1, c2, c3, c4, c5, c6 = ids
        cats = {c["name"]: c["id"] for c in cdb.list_categories(oa)}
        check("测试用的主类名在库里有", CAT_NAME in cats)

        r = cdb.create_group_proposals(oa, mid, None, [
            {"card_ids": [c1, c2], "action": "merge",
             "primary_category": CAT_NAME, "tags": ["心动"],
             "reason": "一问一答，拆开就丢了回应", "confidence": 0.9},
            {"card_ids": [c3, c4, c5], "action": "review",
             "reason": "边界拿不准", "confidence": 0.45},
        ], cats, {})
        same("造出两条待确认的组", r["created"], 2)
        gid_merge = r["group_ids"][0]
        gid_review = r["group_ids"][1]

        # ---- 列出来 -------------------------------------------------
        print("\n【3】列出来给界面用")
        d = A.ok("GET", "/api/groups")
        same("A 看到 2 条组", len(d["groups"]), 2)
        same("待确认计数是 2", d["counts"][cdb.GROUP_PENDING], 2)
        g0 = d["groups"][0]
        check("组里带上了成员卡的正文（她要能直接看）",
              all(m.get("text") for m in g0["members"]))
        check("理由原样带回来了", bool(g0["reason"]))
        same("主类名解出来了", g0["category_name"], CAT_NAME)

        db_ = B.ok("GET", "/api/groups")
        same("B 一条都看不到（组的 owner 隔离）", len(db_["groups"]), 0)

        # ---- 未确认之前，库不该变 -----------------------------------
        print("\n【4】没点确认之前，库里一个字节都不变")
        same("卡片状态还是待确认",
             [card_of(c, oa)["status"] for c in (c1, c2)],
             [sg.STATUS_PENDING, sg.STATUS_PENDING])
        with db.connect() as conn:
            n_cards = conn.execute(
                "SELECT COUNT(*) FROM cards WHERE material_id=?",
                (mid,)).fetchone()[0]
        same("卡片总数没变（还是 6 张）", n_cards, 6)

        # ---- 确认合并 -----------------------------------------------
        print("\n【5】确认合并")
        code, _ = B.call("POST", "/api/groups/%d/confirm" % gid_merge)
        check("别人的账号动不了这一组 → 400", code == 400, "HTTP %s" % code)

        res = A.ok("POST", "/api/groups/%d/confirm" % gid_merge)
        new_id = res["card_id"]
        check("合并成功，长出新的卡片", bool(new_id))

        same("原来那两段标成已排除",
             [card_of(c, oa)["status"] for c in (c1, c2)],
             [sg.STATUS_EXCLUDED, sg.STATUS_EXCLUDED])
        same("它们指回了新卡（界面靠这个说「已并入」）",
             [card_of(c, oa)["parent_card_id"] for c in (c1, c2)],
             [new_id, new_id])

        nr = card_of(new_id, oa)
        with db.connect() as conn:
            content = conn.execute("SELECT content FROM materials WHERE id=?",
                                   (mid,)).fetchone()["content"]
        same("新卡正文就是那一段连续原文",
             content[nr["start_offset"]:nr["end_offset"]],
             TEXT[card_of(c1, oa)["start_offset"]:card_of(c2, oa)["end_offset"]])

        d = A.ok("GET", "/api/groups")
        g1 = [g for g in d["groups"] if g["id"] == gid_merge][0]
        same("组状态变成已合并", g1["status"], cdb.GROUP_MERGED)
        same("组记下了合并出的卡号", g1["merged_card_id"], new_id)
        same("待确认计数降到 1", d["counts"][cdb.GROUP_PENDING], 1)
        same("已合并计数是 1", d["counts"][cdb.GROUP_MERGED], 1)

        check("重复确认被拦住",
              A.call("POST", "/api/groups/%d/confirm" % gid_merge)[0] == 400)

        # 卡片流里那条新的能看见，老的默认藏起来
        cards = A.ok("GET", "/api/cards?material_id=%d" % mid)
        live_ids = [x["id"] for x in cards["items"]]
        check("新卡出现在卡片流里", new_id in live_ids)
        check("被合掉的两段默认不出现",
              c1 not in live_ids and c2 not in live_ids)

        # 勾上「显示已排除」之后，它们要说明白自己是被合掉的
        cards = A.ok("GET", "/api/cards?material_id=%d&include_excluded=true" % mid)
        old = [x for x in cards["items"] if x["id"] == c1][0]
        check("被合掉的卡带 merged_into 标记（界面据此说「已并入 #N」）",
              old.get("merged_into") is True)
        same("它指向的正是新卡", old["parent_card_id"], new_id)

        # ---- 撤销 ---------------------------------------------------
        print("\n【6】撤销合并")
        A.ok("POST", "/api/groups/%d/undo" % gid_merge)
        same("原来那两段回来了",
             [card_of(c, oa)["status"] for c in (c1, c2)],
             [sg.STATUS_PENDING, sg.STATUS_PENDING])
        same("父子关系清干净了",
             [card_of(c, oa)["parent_card_id"] for c in (c1, c2)], [None, None])
        same("合并出来的那张退场了",
             card_of(new_id, oa)["status"], sg.STATUS_EXCLUDED)
        d = A.ok("GET", "/api/groups")
        g1 = [g for g in d["groups"] if g["id"] == gid_merge][0]
        same("组回到待确认", g1["status"], cdb.GROUP_PENDING)
        same("待确认计数回到 2", d["counts"][cdb.GROUP_PENDING], 2)

        # ---- 忽略 / 恢复 --------------------------------------------
        print("\n【7】忽略一组，再放回来")
        snap = [card_of(c, oa)["status"] for c in (c3, c4, c5)]
        A.ok("POST", "/api/groups/%d/ignore" % gid_review)
        d = A.ok("GET", "/api/groups")
        g2 = [g for g in d["groups"] if g["id"] == gid_review][0]
        same("组变成已忽略", g2["status"], cdb.GROUP_IGNORED)
        same("卡片状态一张都没动",
             [card_of(c, oa)["status"] for c in (c3, c4, c5)], snap)
        same("待确认计数降到 1", d["counts"][cdb.GROUP_PENDING], 1)

        check("已忽略的组不能直接确认（要先恢复）",
              A.call("POST", "/api/groups/%d/confirm" % gid_review)[0] == 400)

        A.ok("POST", "/api/groups/%d/restore" % gid_review)
        d = A.ok("GET", "/api/groups")
        g2 = [g for g in d["groups"] if g["id"] == gid_review][0]
        same("恢复了就回到待确认", g2["status"], cdb.GROUP_PENDING)
        check("恢复之后就能确认了",
              A.call("POST", "/api/groups/%d/confirm" % gid_review)[0] == 200)

        # ---- 原文一字未改 -------------------------------------------
        print("\n【8】原文一字未改")
        got = A.ok("GET", "/api/materials/%d" % mid)["content"]
        same("materials.content 跟入库时完全一样",
             hashlib.md5(got.encode("utf-8")).hexdigest(),
             hashlib.md5(TEXT.encode("utf-8")).hexdigest())

        # 前端要的状态定义也得下发
        opt = A.ok("GET", "/api/categories")
        same("下发了组的动作键", opt["group_action_keys"],
             {"merge": cdb.GROUP_ACTION_MERGE,
              "review": cdb.GROUP_ACTION_REVIEW})
        same("下发了组的状态键", opt["group_status_keys"],
             {"pending": cdb.GROUP_PENDING, "merged": cdb.GROUP_MERGED,
              "ignored": cdb.GROUP_IGNORED})
        check("下发了每组最多几条", opt["max_group_cards"] >= 2)

    finally:
        stop_server(proc)

    print("\n通过 %d 项，失败 %d 项" % (OK, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        if _OWN_TMP:
            shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(code)
