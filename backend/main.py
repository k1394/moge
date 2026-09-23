"""
墨阁 · 后端入口
========================================================
这是整个网站的"总机"：浏览器发出的每一个请求，都先到这里，
由它决定交给哪个函数处理。

FastAPI 里三个最基本的概念
    app                     应用本体，所有接口都挂在它身上
    @app.get("/xxx")        注册一个网址（装饰器 = 给函数贴标签）
    return                  返回什么，浏览器就看到什么

接口分成两组
    页面类   /                         返回前端页面
    API 类   /api/...                  返回 JSON 数据，给前端用

启动方式（在 G:\\docker\\moge 目录下）：
    ./.venv/Scripts/python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
或者直接双击「启动墨阁.bat」。
"""

import os
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    from backend import db, importer, parsers
except ImportError:                                   # pragma: no cover
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from backend import db, importer, parsers


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
INDEX_HTML = os.path.join(FRONTEND_DIR, "index.html")


# lifespan = 应用启动和关闭时要做的事。
# 在这里建表：服务每次启动都确保表存在，反复启动也不会出错（IF NOT EXISTS）。
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    print("[墨阁] 数据库就绪：", db.DB_PATH)
    yield


app = FastAPI(title="墨阁", version="0.1.0", lifespan=lifespan)


# ----------------------------------------------------------------------
# 请求体格式（前端传上来的 JSON 长什么样）
# BaseModel 是 Pydantic 的写法：把它当成"参数说明书 + 自动校验器"。
# 前端少传了字段，FastAPI 会直接用默认值；类型不对会返回 422 报错。
# ----------------------------------------------------------------------

class PasteIn(BaseModel):
    """粘贴文本入库"""
    title: str = ""
    content: str = ""
    tags: List[str] = []


class PathIn(BaseModel):
    """按文件夹路径导入（本机自用版最顺手的导入方式）"""
    path: str = ""
    recursive: bool = True
    local_only: bool = False


class UpdateIn(BaseModel):
    """修改素材：只传需要改的字段，没传的不动"""
    title: Optional[str] = None
    tags: Optional[List[str]] = None
    local_only: Optional[bool] = None


# ----------------------------------------------------------------------
# 页面
# ----------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home():
    """打开 http://localhost:8000 —— 直接返回素材库页面。"""
    if os.path.isfile(INDEX_HTML):
        return FileResponse(INDEX_HTML)
    return HTMLResponse(
        "<meta charset='utf-8'>"
        "<div style='font-family:sans-serif;padding:40px'>"
        "<h1>墨阁已启动</h1>"
        "<p>但还没有前端页面（frontend/index.html 不存在）。</p>"
        "</div>"
    )


@app.get("/hello")
def hello():
    """健康检查：确认后端活着。"""
    return {"message": "你好，墨阁", "status": "ok"}


# frontend 目录挂到 /static 下。
# 现在页面只有一个 index.html（由下面的 home() 直接返回），
# 将来拆出 css / js / 图片时，就从 /static/xxx 取。
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/api/health")
def health():
    """给前端用的健康检查，顺便报一下数据库在哪。"""
    s = db.stats()
    return {"ok": True, "db": db.DB_PATH, "stats": s,
            "supported": list(parsers.SUPPORTED_EXTS)}


# ----------------------------------------------------------------------
# 素材：读
# ----------------------------------------------------------------------

@app.get("/api/stats")
def api_stats():
    """首页统计条：素材数 / 总字数 / 标签数"""
    return db.stats()


@app.get("/api/tags")
def api_tags():
    """标签栏：每个标签 + 它下面有几份素材"""
    return {"items": db.list_tags(), "total_materials": db.stats()["materials"]}


@app.get("/api/materials")
def api_list_materials(
    keyword: str = Query("", description="在标题或正文里搜这个词"),
    tag: str = Query("", description="只看挂了这个标签的素材"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """素材列表（不含正文，正文点开才取，列表才快）"""
    return db.list_materials(keyword=keyword, tag=tag, limit=limit, offset=offset)


@app.get("/api/materials/{mid}")
def api_get_material(mid: int):
    """单条素材详情（含正文）"""
    m = db.get_material(mid)
    if not m:
        raise HTTPException(status_code=404, detail="没有这条素材")
    return m


# ----------------------------------------------------------------------
# 素材：写
# ----------------------------------------------------------------------

@app.post("/api/materials")
def api_paste(req: PasteIn):
    """粘贴一段文字直接入库（没带标题就用正文前 20 字当标题）"""
    text = (req.content or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="正文是空的，没什么可存的")

    title = (req.title or "").strip()
    if not title:
        title = text[:20].replace("\n", " ")

    saved = db.save_material(title=title, text=text, ext=".txt",
                             source_path="（手动粘贴）", note="手动粘贴导入",
                             tags=req.tags)
    if saved["status"] == "same":
        return {"ok": True, "status": "same", "id": saved["id"],
                "message": "这段内容库里已经有了，没有重复存"}
    return {"ok": True, "status": "new", "id": saved["id"],
            "message": "已存入素材库"}


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    """浏览器里选文件 / 拖文件上传。

    注意上传走的是"文件内容"（浏览器拿不到你磁盘路径），
    所以这种方式不会自动带文件夹名标签，标签要自己补。
    """
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in parsers.PARSERS:
        raise HTTPException(
            status_code=400,
            detail=f"暂不支持 {ext or '（无扩展名）'}，目前支持："
                   f"{'、'.join(parsers.SUPPORTED_EXTS)}",
        )

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件是空的")

    # 先落到临时目录，再复用已有的解析逻辑（避免为上传单写一套解析）
    import tempfile
    tmp_dir = os.path.join(BASE_DIR, "data", "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_path = os.path.join(tmp_dir, file.filename or ("upload" + ext))
    with open(tmp_path, "wb") as f:
        f.write(raw)

    try:
        r = parsers.parse_file(tmp_path)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    if not r["ok"]:
        raise HTTPException(status_code=400, detail=r["note"])
    if not r["text"].strip():
        raise HTTPException(status_code=400,
                            detail=r["note"] or "这个文件里没有可读的文字")

    title = os.path.splitext(file.filename or "未命名")[0]
    saved = db.save_material(title=title, text=r["text"], ext=r["ext"],
                             source_path="（上传：" + (file.filename or "") + "）",
                             note=r["note"], tags=[title])
    return {"ok": True, "status": saved["status"], "id": saved["id"],
            "chars": saved["chars"], "title": title}


@app.patch("/api/materials/{mid}")
def api_update_material(mid: int, req: UpdateIn):
    """改标题 / 改标签 / 切换「仅本地」"""
    ok = db.update_material(mid, title=req.title, tags=req.tags,
                            local_only=req.local_only)
    if not ok:
        raise HTTPException(status_code=404, detail="没有这条素材")
    return {"ok": True, "material": db.get_material(mid)}


@app.delete("/api/materials/{mid}")
def api_delete_material(mid: int):
    """删掉一条素材"""
    if not db.delete_material(mid):
        raise HTTPException(status_code=404, detail="没有这条素材")
    return {"ok": True, "message": "已删除"}


# ----------------------------------------------------------------------
# 导入：文件夹
#
# 分成两步是刻意的：先 scan（只报告，不写库），确认了再 run（真写库）。
# 批量导入不可逆，多按一下确认键，比事后清理划算。
# ----------------------------------------------------------------------

@app.post("/api/import/scan")
def api_import_scan(req: PathIn):
    """第一步：扫一遍文件夹，报告会导入什么（不写数据库）"""
    path = (req.path or "").strip().strip('"')
    if not path:
        raise HTTPException(status_code=400, detail="请填写文件夹或文件路径")
    if not os.path.exists(path):
        raise HTTPException(status_code=400, detail="路径不存在：" + path)

    if os.path.isfile(path):
        one = importer.import_file(path, dry_run=True)
        return {"ok": True, "is_file": True, "folder": os.path.dirname(path),
                "summary": importer._summarize([one]), "items": [one]}

    res = importer.import_folder(path, recursive=req.recursive, dry_run=True)
    res["is_file"] = False
    return res


@app.post("/api/import/run")
def api_import_run(req: PathIn):
    """第二步：真的导入"""
    path = (req.path or "").strip().strip('"')
    if not path:
        raise HTTPException(status_code=400, detail="请填写文件夹或文件路径")
    if not os.path.exists(path):
        raise HTTPException(status_code=400, detail="路径不存在：" + path)

    if os.path.isfile(path):
        one = importer.import_file(path, local_only=req.local_only)
        return {"ok": True, "is_file": True,
                "summary": importer._summarize([one]), "items": [one]}

    res = importer.import_folder(path, recursive=req.recursive,
                                 local_only=req.local_only)
    res["is_file"] = False
    return res
