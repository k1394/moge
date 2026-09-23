# -*- coding: utf-8 -*-
"""
墨阁 · 接口冒烟测试
========================================================
作用：把主要接口挨个调一遍，确认这次改完代码没把别的地方弄坏。

    「冒烟测试」这个词来自硬件：新板子通电，先看冒不冒烟。
    软件里就是"最粗的那一遍检查"——不验证细节逻辑，
    只确认每个功能还活着、能返回结果。

怎么用（需要服务已经在跑）：

    1. 先双击「启动墨阁.bat」，让服务起来（窗口别关）
    2. 再开一个命令行窗口，在项目目录下执行：
           .venv\\Scripts\\python.exe tests\\smoke_test.py

它不会用你的真实账号：自己注册两个随机名的测试账号，
用它们跑完所有检查，最后把这两个账号和它们的素材全部删掉。
所以你库里的真实素材（以及你的账号）不受任何影响。
"""

import io
import json
import os
import secrets
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.environ.get("MOGE_BASE", "http://127.0.0.1:8000")

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
        print("    先把「启动墨阁.bat」跑起来，再执行这个脚本。")
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


if __name__ == "__main__":
    sys.exit(main())
