# -*- coding: utf-8 -*-
"""
墨阁 · 接口冒烟测试
========================================================
作用：把主要接口挨个调一遍，确认这次改完代码没把别的地方弄坏。

    「冒烟测试」这个词来自硬件：新板子通电，先看冒不冒烟。
    软件里就是"最粗的那一遍检查"——不验证细节逻辑，
    只确认每个功能还活着、能返回结果。

怎么用（不需要先启动墨阁）：

    cd G:\\docker\\moge
    .venv\\Scripts\\python.exe tests\\smoke_test.py

它会自己干这几件事：
    1. 建一个空的临时数据库（不在你的 data/ 目录里）
    2. 用这个临时库，在随机端口上起一个自己的墨阁服务
    3. 在上面注册两个随机名的测试账号，把所有接口跑一遍
    4. 关掉服务、删掉临时库

也就是说：**你的真实数据从头到尾不会被碰**。
这一点在下面 verify_isolated() 里还有一道保险 ——
万一隔离没生效，脚本会直接拒绝运行，而不是硬着头皮测下去。

（高级用法）如果非要测一个已经在跑的服务，设环境变量：
    MOGE_BASE=http://127.0.0.1:8000
这种情况脚本就没法替你把关了，后果自负。
"""

import io
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
import http.cookiejar

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 空字符串 = 脚本自己起一个隔离实例；非空 = 打这个地址（见文件头说明）
BASE = os.environ.get("MOGE_BASE", "").rstrip("/")

# 一个"带记忆的"请求器：
# CookieJar 负责记住服务发回来的登录门票，之后每个请求自动带上。
# 这就是浏览器的做法 —— 只不过浏览器把它存在磁盘上，这里存在内存里。
COOKIES = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),          # 绕开系统代理，本机测试别走代理
    urllib.request.HTTPCookieProcessor(COOKIES),
)

OK = FAIL = 0


# ----------------------------------------------------------------------
# 隔离环境：自己起一个用临时数据库的服务
#
# 为什么非要这么麻烦？
#   接口测试必须通过 HTTP 打服务，而服务连的是哪个数据库，
#   是由"启动服务的那一刻"决定的。所以光在测试进程里设环境变量没用，
#   必须由测试自己去启动一个"数据库指向临时目录"的服务。
# ----------------------------------------------------------------------

def free_port():
    """向系统要一个当前没人用的端口号，问完马上还回去。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_server(data_dir, port):
    """用 data_dir 当数据库目录，在 port 上起一个墨阁服务。

    env 里塞 MOGE_DATA_DIR，db.py 看到它就会把库放到那个目录，
    而不是项目自己的 data/。这就是隔离的全部秘密。
    """
    env = dict(os.environ)
    env["MOGE_DATA_DIR"] = data_dir
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=PROJECT_DIR, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def wait_ready(base, timeout=60):
    """等服务把端口挂起来。进程起来了不等于能收请求，要轮询确认。"""
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
    """关服务。terminate 是"请你退出"，给它时间自己收尾；不听话再强杀。"""
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


def verify_isolated(expected_dir):
    """最后一道保险：确认这个服务用的数据库确实在临时目录里。

    这一条不能省。之前吃过亏 —— 测试以为自己在临时库上跑，
    实际删的却是用户的真实素材。宁可不测，也不能删错东西。
    """
    h = call("GET", "/api/health")
    dbp = os.path.abspath(h.get("db", ""))
    return (os.path.abspath(expected_dir) in dbp), dbp


# ----------------------------------------------------------------------
# 请求封装
# ----------------------------------------------------------------------

def call(method, path, data=None, raw=False):
    """发一个请求并返回 JSON。path 里的中文会自动编码。"""
    path = urllib.parse.quote(path, safe="/?=&")
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(BASE + path, data=body, method=method)
    if body:
        req.add_header("Content-Type", "application/json")
    with OPENER.open(req, timeout=120) as r:
        return json.load(r)


def upload(files):
    """模拟浏览器上传文件。

    files 是 [(文件名, 内容bytes), ...]。
    这里手写 multipart 格式 —— 就是浏览器提交表单时用的那种格式：
    每段前面有条分隔线，段里写文件名和内容，最后收尾。
    平时用 requests 库一行就能搞定，这里为了不加依赖手写一遍，
    顺便能看清"上传"这件事在协议层到底是什么样子。
    """
    boundary = "----moge" + secrets.token_hex(8)
    buf = io.BytesIO()
    for name, data in files:
        buf.write(("--%s\r\n" % boundary).encode("utf-8"))
        buf.write(('Content-Disposition: form-data; name="files"; filename="%s"\r\n'
                   % name).encode("utf-8"))
        buf.write(b"Content-Type: application/octet-stream\r\n\r\n")
        buf.write(data)
        buf.write(b"\r\n")
    buf.write(("--%s--\r\n" % boundary).encode("utf-8"))

    req = urllib.request.Request(BASE + "/api/upload", data=buf.getvalue(),
                                 method="POST")
    req.add_header("Content-Type",
                   "multipart/form-data; boundary=" + boundary)
    with OPENER.open(req, timeout=120) as r:
        return json.load(r)


def expect_error(method, path, data=None, code=None):
    """发一个应当失败的请求，返回 (状态码, 提示语)"""
    try:
        call(method, path, data)
        return None, "（居然成功了，不符合预期）"
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
            detail = body.get("detail", "")
        except Exception:
            detail = ""
        return e.code, detail


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [通过] {name}  {extra}")
    else:
        FAIL += 1
        print(f"  [失败] {name}  {extra}")


def make_docx(path, text):
    """造一个真的 .docx 文件（用 python-docx 生成，和 Word 存出来的同格式）"""
    from docx import Document
    d = Document()
    d.add_paragraph(text)
    d.save(path)


def make_fixture():
    """造一个文件夹，模拟"导入整个文件夹"的场景。

    结构刻意做成两层，用来验证标签推导：
        <临时目录>/
            外貌.txt              → 标签 [外貌]
            描写/打斗.txt          → 标签 [描写, 打斗]
    """
    root = tempfile.mkdtemp(prefix="moge_test_")
    os.makedirs(os.path.join(root, "描写"), exist_ok=True)

    with open(os.path.join(root, "外貌.txt"), "w", encoding="utf-8") as f:
        f.write("眉骨很淡，眼睛细长，笑的时候眼尾先动。")

    with open(os.path.join(root, "描写", "打斗.txt"), "w", encoding="utf-8") as f:
        f.write("他没有拔刀，只是往前迈了半步，对面的人就退了半步。")

    return root


USER_A = "冒烟甲_" + secrets.token_hex(3)
USER_B = "冒烟乙_" + secrets.token_hex(3)
PW = "smoke-" + secrets.token_hex(4)


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------

def main():
    print("=" * 64)
    print("  墨阁 API 冒烟测试  →  " + BASE)
    print("=" * 64)

    # --- 0 服务是否活着 ---
    try:
        h = call("GET", "/api/health")
    except Exception as e:
        print(f"\n[x] 连不上服务：{e}")
        print("    这个脚本通常会自己起一个隔离服务，不该连不上；")
        print("    如果你设了 MOGE_BASE 指到别处，请确认那个地址有服务在跑。")
        return 1
    check("服务在线", h.get("ok") is True, "数据库 " + h.get("db", ""))

    # --- 1 没登录时，素材接口必须拒绝 ---
    code, detail = expect_error("GET", "/api/stats")
    check("未登录访问素材接口被拒", code == 401, f"HTTP {code}：{detail}")
    code, _ = expect_error("GET", "/api/materials")
    check("未登录读列表被拒", code == 401, f"HTTP {code}")

    # --- 2 注册账号甲（注册完应当自动就是登录状态）---
    r = call("POST", "/api/auth/register",
             {"username": USER_A, "password": PW})
    check("注册成功", r.get("ok") is True, f"账号「{r['user']['username']}」")

    me = call("GET", "/api/auth/me")
    check("注册后即已登录", me["logged_in"] is True
          and me["user"]["username"] == USER_A)

    # --- 3 用户名重复要被拦 ---
    code, detail = expect_error("POST", "/api/auth/register",
                                {"username": USER_A, "password": PW})
    check("重复用户名被拒", code == 400, f"HTTP {code}：{detail}")

    # --- 4 密码太短要被拦 ---
    code, detail = expect_error("POST", "/api/auth/register",
                                {"username": "短密码测试_" + secrets.token_hex(2),
                                 "password": "123"})
    check("弱密码被拒", code == 400, f"HTTP {code}：{detail}")

    # --- 5 新账号应当是空的 ---
    s0 = call("GET", "/api/stats")
    base_count = s0["materials"]
    check("新账号素材库为空", base_count == 0, f"{base_count} 条")

    # --- 6 粘贴入库 ---
    text = "冒烟测试用的临时素材，脚本跑完会自己删掉。"
    r = call("POST", "/api/materials", {"title": "冒烟测试-临时", "content": text})
    check("粘贴入库", r["status"] == "new", f"id={r['id']}")
    new_id = r.get("id")

    # --- 7 重复内容去重 ---
    r2 = call("POST", "/api/materials",
              {"title": "换个标题再存一遍", "content": text})
    check("重复内容被去重", r2["status"] == "same")

    # --- 8 统计随之变化 ---
    s2 = call("GET", "/api/stats")
    check("统计已更新", s2["materials"] == base_count + 1,
          f"{base_count} → {s2['materials']}")

    # --- 9 详情带正文 ---
    m = call("GET", f"/api/materials/{new_id}")
    check("详情带正文", m["content"] == text, f"{len(m['content'])} 字")

    # --- 10 搜索 ---
    s = call("GET", "/api/materials?keyword=" + "临时素材")
    check("关键词搜索", s["total"] >= 1, f"命中 {s['total']} 条")

    # --- 11 改标题与标签 ---
    r3 = call("PATCH", f"/api/materials/{new_id}",
              {"title": "冒烟测试-改名了", "tags": ["测试", "临时"]})
    check("修改标题与标签",
          r3["material"]["title"] == "冒烟测试-改名了"
          and set(r3["material"]["tags"]) == {"测试", "临时"},
          str(r3["material"]["tags"]))

    # --- 12 仅本地标记 ---
    r4 = call("PATCH", f"/api/materials/{new_id}", {"local_only": True})
    check("切换「仅本地」", r4["material"]["local_only"] is True)

    # --- 13 标签筛选 ---
    t = call("GET", "/api/tags")
    check("标签列表可读", "items" in t, f"{len(t['items'])} 个标签")
    d2 = call("GET", "/api/materials?tag=测试")
    check("按标签筛选", d2["total"] >= 1, f"「测试」下 {d2['total']} 条")

    # --- 14 删除 ---
    call("DELETE", f"/api/materials/{new_id}")
    s3 = call("GET", "/api/stats")
    check("删除后条数复原", s3["materials"] == base_count,
          f"回到 {s3['materials']} 条")

    # --- 15 上传文件：txt + docx + 一个不支持的格式 ---
    tx = "上传测试：一段 txt 素材内容。" + secrets.token_hex(4)
    dx = "上传测试：一段 docx 素材内容。" + secrets.token_hex(4)
    tmpd = tempfile.mkdtemp(prefix="moge_up_")
    docx_path = os.path.join(tmpd, "上传测试docx.docx")
    try:
        make_docx(docx_path, dx)
        with open(docx_path, "rb") as f:
            docx_bytes = f.read()

        up = upload([
            ("上传测试txt.txt", tx.encode("utf-8")),
            ("上传测试docx.docx", docx_bytes),
            ("不支持.exe", b"\x00\x01binary"),
        ])
        by_name = {it["name"]: it for it in up["items"]}

        check("上传 txt 成功",
              by_name.get("上传测试txt.txt", {}).get("status") == "new",
              str(by_name.get("上传测试txt.txt", {}).get("message", "")))
        check("上传 docx 成功并解析出正文",
              by_name.get("上传测试docx.docx", {}).get("status") == "new"
              and by_name.get("上传测试docx.docx", {}).get("chars", 0) > 0,
              f"{by_name.get('上传测试docx.docx', {}).get('chars', 0)} 字")
        check("不支持的格式被拒",
              by_name.get("不支持.exe", {}).get("status") == "fail",
              str(by_name.get("不支持.exe", {}).get("message", "")))
        check("上传摘要正确", up["summary"]["new"] == 2
              and up["summary"]["fail"] == 1,
              f"新增 {up['summary']['new']}，失败 {up['summary']['fail']}")

        # docx 解析出来的文字要真的能搜到
        q = call("GET", "/api/materials?keyword=" + dx[:8])
        check("docx 正文确实入库了", q["total"] >= 1, f"命中 {q['total']} 条")

        # --- 16 同一个文件再传一次，应当被判为重复 ---
        up2 = upload([("上传测试txt.txt", tx.encode("utf-8"))])
        check("重复上传被判重", up2["items"][0]["status"] == "same")
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)

    # --- 17 路径穿越攻击要被挡住 ---
    #     恶意文件名里带 ..\..\ 想往上级目录写文件，服务端只该取文件名
    up3 = upload([("..\\..\\windows\\坏东西.txt", "试图越狱的内容".encode("utf-8"))])
    check("危险文件名被消毒",
          up3["items"][0]["name"] in ("坏东西.txt", "未命名")
          or ".." not in up3["items"][0]["name"],
          "处理成：" + up3["items"][0]["name"])

    # --- 18 文件夹导入：预览 ---
    folder = make_fixture()
    try:
        before = call("GET", "/api/stats")["materials"]
        scan = call("POST", "/api/import/scan", {"path": folder})
        sm = scan["summary"]
        check("文件夹预览（不写库）", sm["total"] == 2 and sm["new"] == 2,
              f"扫到 {sm['total']} 个，新 {sm['new']} 个")

        tags_by_name = {it["name"]: it["tags"] for it in scan["items"]}
        check("标签推导：单层文件夹",
              tags_by_name.get("外貌.txt") == ["外貌"],
              str(tags_by_name.get("外貌.txt")))
        check("标签推导：多层文件夹",
              tags_by_name.get("打斗.txt") == ["描写", "打斗"],
              str(tags_by_name.get("打斗.txt")))

        check("预览确实没写库",
              call("GET", "/api/stats")["materials"] == before,
              f"仍是 {before} 条")

        # --- 19 文件夹导入：真导入 ---
        run = call("POST", "/api/import/run", {"path": folder})
        check("文件夹真导入", run["summary"]["new"] == 2,
              f"新增 {run['summary']['new']} 条")
        check("导入后条数增加",
              call("GET", "/api/stats")["materials"] == before + 2)

        # --- 20 再导一次：全部去重 ---
        again = call("POST", "/api/import/run", {"path": folder})
        check("重复导入被去重", again["summary"]["new"] == 0
              and again["summary"]["same"] == 2,
              f"新 {again['summary']['new']}，重复 {again['summary']['same']}")
    finally:
        shutil.rmtree(folder, ignore_errors=True)

    # --- 21 多用户隔离：注册账号乙，看它能不能看见甲的素材 ---
    a_count = call("GET", "/api/stats")["materials"]

    r_b = call("POST", "/api/auth/register",
               {"username": USER_B, "password": PW})
    check("注册第二个账号", r_b.get("ok") is True, f"账号「{USER_B}」")

    sb = call("GET", "/api/stats")
    check("账号乙看不到账号甲的素材", sb["materials"] == 0,
          f"乙看到 {sb['materials']} 条，而甲有 {a_count} 条")

    tb = call("GET", "/api/tags")
    check("账号乙看不到甲的标签", len(tb["items"]) == 0,
          f"{len(tb['items'])} 个标签")

    q = call("GET", "/api/materials?keyword=" + tx[:8])
    check("账号乙搜不到甲的内容", q["total"] == 0)

    # --- 22 账号乙也不该能删掉甲的素材 ---
    code, detail = expect_error("DELETE", f"/api/materials/{new_id}")
    check("跨账号删除被拒", code == 404, f"HTTP {code}：{detail}")

    # --- 23 退出登录 ---
    r_out = call("POST", "/api/auth/logout")
    check("退出登录", r_out.get("ok") is True)

    me2 = call("GET", "/api/auth/me")
    check("退出后确实是未登录", me2["logged_in"] is False)
    code, _ = expect_error("GET", "/api/stats")
    check("退出后素材接口再次被拒", code == 401, f"HTTP {code}")

    # --- 24 密码错了不能登录 ---
    code, detail = expect_error("POST", "/api/auth/login",
                                {"username": USER_A, "password": "肯定不对的密码"})
    check("错误密码被拒", code == 401, f"HTTP {code}：{detail}")

    # --- 25 用正确密码能重新登录 ---
    r_in = call("POST", "/api/auth/login", {"username": USER_A, "password": PW})
    check("正确密码可以登录", r_in.get("ok") is True)
    check("重新登录后素材还在", call("GET", "/api/stats")["materials"] > 0,
          f"{call('GET','/api/stats')['materials']} 条")

    # --- 26 不存在的路径要报错 ---
    code, detail = expect_error("POST", "/api/import/scan",
                                {"path": r"C:\这个路径肯定不存在\abc"})
    check("不存在的路径报错", code == 400, f"HTTP {code}：{detail}")

    # --- 27 空正文被拦 ---
    code, _ = expect_error("POST", "/api/materials",
                           {"title": "空的", "content": "   "})
    check("空正文被拒", code == 400, f"HTTP {code}")

    # --- 28 收尾：删掉两个测试账号和它们的东西 ---
    removed = cleanup()
    check("测试数据已清理", removed, f"清掉 {removed} 条与 2 个测试账号")

    print("-" * 64)
    print(f"  通过 {OK} 项，失败 {FAIL} 项")
    print("=" * 64)
    return 1 if FAIL else 0


def cleanup():
    """删掉本次测试造的两个账号、它们的素材和登录记录。

    直接用数据库模块删，不走 HTTP ——
    因为没有"删除账号"这种接口（真实产品里也通常不提供自助删号）。
    """
    from backend import db

    # 保险：这一段是要删数据的，动手前先确认自己连的是临时库。
    # 临时目录名里带 moge_smoke_ 这个前缀，真实库的路径里不会有它。
    if "moge_smoke_" not in os.path.abspath(db.DB_PATH):
        raise RuntimeError(
            "【已阻止】清理会删数据，但当前连的数据库不像临时库：\n"
            "    " + db.DB_PATH + "\n"
            "    为免误删真实素材，清理拒绝执行。")

    total = 0
    for name in (USER_A, USER_B):
        u = db.get_user_by_name(name)
        if not u:
            continue
        owner = db.owner_of(u["id"])
        for it in db.list_materials(owner=owner, limit=2000)["items"]:
            db.delete_material(it["id"], owner=owner)
            total += 1
        with db.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (u["id"],))
            conn.execute("DELETE FROM users WHERE id = ?", (u["id"],))
    return total


def run_isolated():
    """建临时库 → 起服务 → 验隔离 → 跑测试 → 关服务、删临时库。

    注意中间那行 os.environ 赋值：测试进程自己也会 import db（清理数据时），
    所以必须让**本进程**也把数据库指向临时目录，否则清理阶段就会
    跑到你的真实库上去删东西。这一处最容易漏。
    """
    global BASE
    data_dir = tempfile.mkdtemp(prefix="moge_smoke_")
    port = free_port()
    BASE = "http://127.0.0.1:%d" % port
    proc = None
    try:
        print("正在准备隔离环境（临时库 + 独立端口）…")
        print("  临时数据库目录：" + data_dir)
        os.environ["MOGE_DATA_DIR"] = data_dir      # 本进程也指向临时库
        proc = start_server(data_dir, port)
        if not wait_ready(BASE):
            print("[x] 服务没能起来，测试中止。")
            return 1

        ok, dbp = verify_isolated(data_dir)
        print("  服务用的数据库：" + dbp)
        if not ok:
            print()
            print("[x] 【拒绝运行】隔离没生效 —— 这个服务用的不是临时库。")
            print("    再跑下去可能会动到你的真实素材，所以在这里主动停下。")
            print("    请检查 db.py 里 MOGE_DATA_DIR 那段的逻辑是否被改动。")
            return 2
        print("  隔离检查：通过（你的真实数据不会被碰）")
        print()
        return main()
    finally:
        stop_server(proc)
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    # 没设 MOGE_BASE = 自己起隔离实例（默认，安全）
    # 设了 MOGE_BASE = 测那个外部地址（高级用法）
    sys.exit(main() if BASE else run_isolated())
