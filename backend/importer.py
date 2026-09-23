# -*- coding: utf-8 -*-
"""
墨阁 · 素材导入模块
========================================================
作用：把一个文件、或一整个文件夹里的素材，走完这条路：

    读文件（parsers.py） → 推导标签 → 存进数据库（db.py）

它是 parsers.py 和 db.py 之间的搬运工，自己不解析文件、也不碰 SQL。

--------------------------------------------------------
标签规则（对着你现有的素材库设计的）
--------------------------------------------------------
导入文件夹时，标签 = 子文件夹的每一级名字 + 文件名（去掉扩展名）。
导入根目录自己那一层的名字不算 —— 否则所有素材都会挂一个"分类"
这种没有意义的标签。

以你桌面「分类」文件夹为例，导入根目录 = 分类：

    分类\\打斗.docx                 → 标签 [打斗]
    分类\\外貌.docx                 → 标签 [外貌]
    分类\\世界观\\九门\\九门\\职业.txt → 标签 [世界观, 九门, 职业]

好处是你现有的分类直接变成标签体系，不用手工重打一遍。

--------------------------------------------------------
为什么要有 dry_run（预览）
--------------------------------------------------------
导入是不可逆的批量写库。先跑一遍只统计不写库，
让你看清"会进来多少文件、多少字、哪些读不出来、哪些其实已经在库里"，
确认了再真正导入。一个坏文件不会中断整批（parsers 保证不抛异常）。
"""

import os
import sys

# 让这个文件无论怎么启动都能导入同目录的其他模块
try:
    from backend import db, parsers
except ImportError:                                   # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from backend import db, parsers


# ----------------------------------------------------------------------
# 标签推导
# ----------------------------------------------------------------------

def tags_from_path(path, root=None):
    """根据「文件相对导入根目录的位置」推导出一组标签。

    规则见文件开头的说明。返回的标签已去重、已去掉空白。
    """
    path = os.path.abspath(path)
    folder = os.path.dirname(path)

    middle = []
    if root:
        rel = os.path.relpath(folder, os.path.abspath(root))
        if rel and rel != ".":
            middle = [p for p in rel.replace("/", "\\").split("\\") if p]
    else:
        # 没给根目录时（单文件导入），只取它所在文件夹的名字
        base = os.path.basename(folder)
        middle = [base] if base else []

    stem = os.path.splitext(os.path.basename(path))[0]

    out = []
    for name in middle + [stem]:
        name = (name or "").strip()
        if name and name not in out:
            out.append(name)
    return out


# ----------------------------------------------------------------------
# 单个文件
# ----------------------------------------------------------------------

def import_file(path, root=None, owner=db.DEFAULT_OWNER, extra_tags=None,
                local_only=False, dry_run=False):
    """导入一个文件，返回结果字典。永远不抛异常。

    返回里的 status 有四种：
        new    新入库（或预览时"将会入库"）
        same   内容已存在，跳过（重复导入）
        empty  文件能打开，但里面没有可读的文字（例如扫描件 PDF）
        fail   读不出来（格式不支持 / 文件损坏）
    """
    name = os.path.basename(path)

    # 标签：从路径推导 + 调用方额外指定的
    tags = tags_from_path(path, root)
    for t in (extra_tags or []):
        t = (t or "").strip()
        if t and t not in tags:
            tags.append(t)

    r = parsers.parse_file(path)

    if not r["ok"]:
        return {"path": path, "name": name, "status": "fail", "chars": 0,
                "tags": tags, "message": r["note"]}

    if not r["text"].strip():
        return {"path": path, "name": name, "status": "empty", "chars": 0,
                "tags": tags, "message": r["note"] or "没有提取到文字"}

    title = os.path.splitext(name)[0]

    # 预览模式：只报告，不写库
    if dry_run:
        existing = db.find_by_content(r["text"], owner=owner)
        return {"path": path, "name": name,
                "status": "same" if existing else "new",
                "chars": r["chars"], "tags": tags, "message": r["note"]}

    saved = db.save_material(
        title=title, text=r["text"], ext=r["ext"], source_path=r["path"],
        note=r["note"], tags=tags, owner=owner, local_only=local_only,
    )
    return {"path": path, "name": name, "status": saved["status"],
            "id": saved["id"], "chars": saved["chars"], "tags": tags,
            "message": r["note"]}


# ----------------------------------------------------------------------
# 整个文件夹
# ----------------------------------------------------------------------

def import_folder(folder, owner=db.DEFAULT_OWNER, recursive=True,
                  local_only=False, dry_run=False):
    """导入一个文件夹里的所有可解析文件。

    recursive=True 时子文件夹也一起扫（你的「世界观\\九门」就在子目录里）。
    """
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        return {"ok": False, "error": "文件夹不存在：" + folder,
                "folder": folder, "summary": _empty_summary(), "items": []}

    files = parsers.scan_folder(folder, recursive=recursive)
    items = [
        import_file(p, root=folder, owner=owner, local_only=local_only,
                    dry_run=dry_run)
        for p in files
    ]

    summary = _summarize(items)
    summary["scanned"] = len(files)
    return {"ok": True, "folder": folder, "summary": summary, "items": items}


def import_folders(folders, owner=db.DEFAULT_OWNER, recursive=True,
                   local_only=False, dry_run=False):
    """一次导入多个文件夹（结果合并成一份）"""
    all_items = []
    details = []
    for f in folders:
        one = import_folder(f, owner=owner, recursive=recursive,
                            local_only=local_only, dry_run=dry_run)
        details.append({"folder": f, "ok": one["ok"],
                        "error": one.get("error", "")})
        all_items.extend(one["items"])

    summary = _summarize(all_items)
    summary["scanned"] = len(all_items)
    return {"ok": True, "folders": details, "summary": summary,
            "items": all_items}


def _empty_summary():
    return {"total": 0, "scanned": 0, "new": 0, "same": 0,
            "empty": 0, "fail": 0, "chars": 0}


def _summarize(items):
    s = _empty_summary()
    s["total"] = len(items)
    for it in items:
        st = it["status"]
        if st in s:
            s[st] += 1
        if st in ("new", "same"):
            s["chars"] += it.get("chars", 0)
    return s


# ----------------------------------------------------------------------
# 命令行自测
#     python backend/importer.py "C:\\Users\\Administrator\\Desktop\\分类"
#     末尾加 preview 只看预览，不写库
# ----------------------------------------------------------------------

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if len(sys.argv) < 2:
        print("用法：")
        print('    python backend/importer.py "文件夹路径"           真正导入')
        print('    python backend/importer.py "文件夹路径" preview    只看预览，不写库')
        raise SystemExit

    target = sys.argv[1]
    is_preview = len(sys.argv) > 2 and sys.argv[2].lower() in ("preview", "-p")

    db.init_db()
    res = import_folder(target, dry_run=is_preview)

    if not res["ok"]:
        print("[x]", res["error"])
        raise SystemExit(1)

    mode = "预览（未写入数据库）" if is_preview else "导入"
    print("=" * 62)
    print(f"  {mode}：{res['folder']}")
    print("=" * 62)

    for it in res["items"]:
        mark = {"new": "新", "same": "重", "empty": "空", "fail": "错"}[it["status"]]
        tagtxt = "、".join(it["tags"]) or "（无）"
        print(f"[{mark}] {it['name']}  {it['chars']} 字  标签：{tagtxt}")
        if it["status"] in ("empty", "fail"):
            print(f"      └ {it['message']}")

    s = res["summary"]
    print("-" * 62)
    print(f"共 {s['total']} 个文件　新入库 {s['new']}　重复跳过 {s['same']}　"
          f"无文字 {s['empty']}　失败 {s['fail']}")
    print(f"合计 {s['chars']} 字")
    print("数据库：", db.DB_PATH)
