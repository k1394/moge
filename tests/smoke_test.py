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

它会自己造几份临时素材来测，测完自己删掉，
不会碰你素材库里的真实内容。
"""

import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.environ.get("MOGE_BASE", "http://127.0.0.1:8000")

# 绕开系统代理：本机测试不该走代理，否则可能连不上 127.0.0.1
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

OK = FAIL = 0


# ----------------------------------------------------------------------
# 请求封装
# ----------------------------------------------------------------------

def call(method, path, data=None):
    """发一个请求并返回 JSON。path 里的中文会自动编码。"""
    path = urllib.parse.quote(path, safe="/?=&")
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(BASE + path, data=body, method=method)
    if body:
        req.add_header("Content-Type", "application/json")
    with OPENER.open(req, timeout=120) as r:
        return json.load(r)


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [通过] {name}  {extra}")
    else:
        FAIL += 1
        print(f"  [失败] {name}  {extra}")


# ----------------------------------------------------------------------
# 测试用的临时素材文件夹
# ----------------------------------------------------------------------

def make_fixture():
    """造一个小文件夹，模拟"导入整个文件夹"的场景。

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

    base_count = h["stats"]["materials"]
    print(f"      当前库里已有 {base_count} 条素材（后面会基于这个数来验证）")
    print()

    # --- 1 列表 ---
    d = call("GET", "/api/materials")
    check("素材列表可读", "total" in d and "items" in d, f"共 {d['total']} 条")
    if d["items"]:
        first = d["items"][0]
        check("列表不带正文", "content" not in first, "（正文点开才取，列表更快）")

        # --- 2 详情 ---
        m = call("GET", f"/api/materials/{first['id']}")
        check("详情带正文", len(m["content"]) == first["chars"],
              f"{m['title']} {len(m['content'])} 字")

        # --- 3 搜索 ---
        kw = first["title"][:2] or "a"
        s = call("GET", "/api/materials?keyword=" + kw)
        check("关键词搜索", s["total"] >= 1, f"「{kw}」命中 {s['total']} 条")
    else:
        print("  [跳过] 库里还没有素材，搜索/详情这几项先跳过")

    # --- 4 标签 ---
    t = call("GET", "/api/tags")
    check("标签列表可读", "items" in t and "total_materials" in t,
          f"{len(t['items'])} 个标签")
    if t["items"]:
        tag = t["items"][0]["name"]
        d2 = call("GET", "/api/materials?tag=" + tag)
        check("按标签筛选", d2["total"] >= 1,
              f"标签「{tag}」下 {d2['total']} 条")

    # --- 5 粘贴入库 ---
    text = "冒烟测试用的临时素材，脚本跑完会自己删掉。"
    r = call("POST", "/api/materials", {"title": "冒烟测试-临时", "content": text})
    check("粘贴入库", r["status"] == "new", f"id={r['id']}")
    new_id = r.get("id")

    # --- 6 重复内容去重 ---
    r2 = call("POST", "/api/materials",
              {"title": "换个标题再存一遍", "content": text})
    check("重复内容被去重", r2["status"] == "same")

    # --- 7 统计随之变化 ---
    s2 = call("GET", "/api/stats")
    check("统计已更新", s2["materials"] == base_count + 1,
          f"{base_count} → {s2['materials']}")

    # --- 8 改标题与标签 ---
    r3 = call("PATCH", f"/api/materials/{new_id}",
              {"title": "冒烟测试-改名了", "tags": ["测试", "临时"]})
    check("修改标题与标签",
          r3["material"]["title"] == "冒烟测试-改名了"
          and set(r3["material"]["tags"]) == {"测试", "临时"},
          str(r3["material"]["tags"]))

    # --- 9 仅本地标记 ---
    r4 = call("PATCH", f"/api/materials/{new_id}", {"local_only": True})
    check("切换「仅本地」", r4["material"]["local_only"] is True)

    # --- 10 删除 ---
    call("DELETE", f"/api/materials/{new_id}")
    s3 = call("GET", "/api/stats")
    check("删除后条数复原", s3["materials"] == base_count,
          f"回到 {s3['materials']} 条")

    # --- 11 文件夹导入：预览 ---
    folder = make_fixture()
    try:
        scan = call("POST", "/api/import/scan", {"path": folder})
        sm = scan["summary"]
        check("文件夹预览（不写库）", sm["total"] == 2 and sm["new"] == 2,
              f"扫到 {sm['total']} 个，新 {sm['new']} 个")

        # 标签推导对不对
        tags_by_name = {it["name"]: it["tags"] for it in scan["items"]}
        check("标签推导：单层文件夹",
              tags_by_name.get("外貌.txt") == ["外貌"],
              str(tags_by_name.get("外貌.txt")))
        check("标签推导：多层文件夹",
              tags_by_name.get("打斗.txt") == ["描写", "打斗"],
              str(tags_by_name.get("打斗.txt")))

        s4 = call("GET", "/api/stats")
        check("预览确实没写库", s4["materials"] == base_count,
              f"仍是 {s4['materials']} 条")

        # --- 12 文件夹导入：真导入 ---
        run = call("POST", "/api/import/run", {"path": folder})
        check("文件夹真导入", run["summary"]["new"] == 2,
              f"新增 {run['summary']['new']} 条")

        s5 = call("GET", "/api/stats")
        check("导入后条数增加", s5["materials"] == base_count + 2,
              f"{s5['materials']} 条")

        # --- 13 再导一次：全部去重 ---
        again = call("POST", "/api/import/run", {"path": folder})
        check("重复导入被去重", again["summary"]["new"] == 0
              and again["summary"]["same"] == 2,
              f"新 {again['summary']['new']}，重复 {again['summary']['same']}")

        # --- 14 清理刚才导进去的两条 ---
        lst = call("GET", "/api/materials?tag=外貌")
        for it in lst["items"]:
            if it["source_path"].startswith(folder):
                call("DELETE", f"/api/materials/{it['id']}")
        lst = call("GET", "/api/materials?tag=打斗")
        for it in lst["items"]:
            if it["source_path"].startswith(folder):
                call("DELETE", f"/api/materials/{it['id']}")
        s6 = call("GET", "/api/stats")
        check("测试数据已清理", s6["materials"] == base_count,
              f"回到 {s6['materials']} 条")
    finally:
        shutil.rmtree(folder, ignore_errors=True)

    # --- 15 错误处理 ---
    try:
        call("POST", "/api/import/scan", {"path": r"C:\这个路径肯定不存在\abc"})
        check("不存在的路径报错", False, "（居然没报错）")
    except urllib.error.HTTPError as e:
        body = json.load(e)
        check("不存在的路径报错", e.code == 400, f"HTTP {e.code}：{body.get('detail')}")

    # --- 16 空正文被拦 ---
    try:
        call("POST", "/api/materials", {"title": "空的", "content": "   "})
        check("空正文被拒", False, "（居然存进去了）")
    except urllib.error.HTTPError as e:
        check("空正文被拒", e.code == 400, f"HTTP {e.code}")

    print("-" * 64)
    print(f"  通过 {OK} 项，失败 {FAIL} 项")
    print("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
