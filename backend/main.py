# -*- coding: utf-8 -*-
"""
墨阁 · 后端入口
========================================================
这是整个网站的"总机"：浏览器发出的每一个请求，都先到这里，
由它决定交给哪个函数处理。

FastAPI 里三个最基本的概念
    app                     应用本体，所有接口都挂在它身上
    @app.get("/xxx")        注册一个网址（装饰器 = 给函数贴标签）
    return                  返回什么，浏览器就看到什么

接口分成三组
    页面类   /                         返回前端页面
    账号类   /api/auth/...             注册、登录、退出、问"我是谁"
    素材类   /api/...                  素材库的增删改查、导入

--------------------------------------------------------
本版本新增：账号
--------------------------------------------------------
每个素材接口现在都挂了 Depends(auth.current_user)。
它的意思是"进这个接口之前，先确认来访者登录了没有"——
没登录直接返回 401，代码根本不会往下跑。

为什么这么做而不是让前端"传一个用户名过来"：
    前端传什么，用户就能改成什么。如果接口信任前端传来的用户名，
    那任何人都能把用户名改成别人的，直接读走别人的素材。
    所以"你是谁"这件事，必须由服务端从它自己发出的门票（Cookie）里认，
    一个字都不能听前端的。

启动方式（在 G:\\docker\\moge 目录下）：
    ./.venv/Scripts/python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
或者直接双击「启动墨阁.bat」。
"""

import os
import secrets
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import (Depends, FastAPI, File, HTTPException, Query, Request,
                     UploadFile)
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    from backend import auth, db, importer, parsers
except ImportError:                                   # pragma: no cover
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from backend import auth, db, importer, parsers


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
INDEX_HTML = os.path.join(FRONTEND_DIR, "index.html")


# lifespan = 应用启动和关闭时要做的事。
# 在这里建表：服务每次启动都确保表存在，反复启动也不会出错（IF NOT EXISTS）。
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    db.purge_expired_sessions()
    cleanup_tmp_dir()
    print("[墨阁] 数据库就绪：", db.DB_PATH)
    print("[墨阁] 已有账号数：", db.count_users())
    yield


def cleanup_tmp_dir():
    """清掉上次运行遗留的上传临时文件。

    正常情况下上传用完就把临时文件删了，这里应该是空的。
    但如果某次删除没成功（比如被系统策略拦住），文件会留下来；
    素材正文属于隐私，不该在磁盘上越积越多，所以每次启动扫一遍。
    """
    tmp_dir = os.path.join(BASE_DIR, "data", "tmp")
    if not os.path.isdir(tmp_dir):
        return
    try:
        for name in os.listdir(tmp_dir):
            try:
                os.remove(os.path.join(tmp_dir, name))
            except BaseException:
                pass
    except BaseException:
        pass


app = FastAPI(title="墨阁", version="0.2.0", lifespan=lifespan)


# ----------------------------------------------------------------------
# 请求体格式（前端传上来的 JSON 长什么样）
# BaseModel 是 Pydantic 的写法：把它当成"参数说明书 + 自动校验器"。
# 前端少传了字段，FastAPI 会直接用默认值；类型不对会返回 422 报错。
# ----------------------------------------------------------------------

class RegisterIn(BaseModel):
    """注册

    adopt_mine 是"要不要接收之前没有归属的素材"。
    默认 False —— 一件会影响已有数据的事，必须由用户明确点一下，
    不能因为"反正是第一个账号"就替他决定。
    """
    username: str = ""
    password: str = ""
    adopt_mine: bool = False


class LoginIn(BaseModel):
    """登录"""
    username: str = ""
    password: str = ""


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
    """打开 http://localhost:8000 —— 直接返回素材库页面。

    注意：这里不检查登录。页面本身是"空壳"，
    真正有没有登录由页面里的 JS 去问 /api/auth/me 才知道。
    这样页面可以被浏览器缓存，也不用为两种状态准备两个 HTML。
    """
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
# 现在页面只有一个 index.html（由上面的 home() 直接返回），
# 将来拆出 css / js / 图片时，就从 /static/xxx 取。
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/api/health")
def health():
    """不需要登录的体检接口。

    前端靠它回答两个问题：
        1. 服务活着吗
        2. 这个站已经有人注册过吗（一个都没有 → 界面上偏向"新用户注册"）
    """
    return {"ok": True, "db": db.DB_PATH,
            "users": db.count_users(),
            "unclaimed": db.stats(owner=db.DEFAULT_OWNER)["materials"],
            "stats": db.stats(owner=db.DEFAULT_OWNER),
            "supported": list(parsers.SUPPORTED_EXTS)}


# ----------------------------------------------------------------------
# 账号
#
# 一组约定，理解这四条，整个登录流程就通了：
#
#   1. 服务端谁都不信，只信自己发出去的门票（Cookie 里的 token）
#   2. 密码进数据库前必须先搅碎（auth.hash_password）
#   3. 登录成功 → 发一张新门票 → 塞进 Cookie
#   4. 退出登录 → 把那张门票作废
# ----------------------------------------------------------------------

@app.get("/api/auth/me")
def api_me(user: Optional[dict] = Depends(auth.optional_user)):
    """问"我现在是谁"。没登录不算错误，如实回答 logged_in=false。"""
    return {"logged_in": user is not None, "user": user}


@app.post("/api/auth/register")
def api_register(req: RegisterIn, request: Request):
    """注册。

    注册成功后直接就是登录状态（自动发一张票）——
    否则用户还要再输一遍密码，纯粹的麻烦。
    """
    username = (req.username or "").strip()
    password = req.password or ""

    err = auth.check_username(username) or auth.check_password(password)
    if err:
        raise HTTPException(status_code=400, detail=err)

    # 先搅碎再交给数据库。数据库那层永远见不到原始密码。
    salt = auth.make_salt()
    pwd_hash = auth.hash_password(password, salt)

    u = db.create_user(username, pwd_hash, salt)
    if not u:
        raise HTTPException(status_code=400, detail="这个用户名已经有人用了，换一个")

    # ---- 认领之前用命令行导入的素材 ----
    # 只在用户明确勾了"要"的时候才做。
    # db.adopt_owner 内部还有一道保险：目标账号名下必须一条素材都没有，
    # 否则不搬（避免两边的素材撞车）。
    adopted = 0
    if req.adopt_mine:
        adopted = db.adopt_owner(db.DEFAULT_OWNER, db.owner_of(u["id"]))

    token = auth.new_token()
    db.create_session(token, u["id"], auth.expires_at_str())
    db.touch_last_login(u["id"])

    return auth.json_with_cookie({
        "ok": True,
        "user": {"id": u["id"], "username": u["username"],
                 "owner": db.owner_of(u["id"])},
        "adopted": adopted,          # 认领过来多少条素材（0 表示没有）
        "message": "注册成功，已经登录",
    }, token)


@app.post("/api/auth/login")
def api_login(req: LoginIn):
    """登录"""
    username = (req.username or "").strip()
    password = req.password or ""
    if not username or not password:
        raise HTTPException(status_code=400, detail="用户名和密码都要填")

    u = db.get_user_by_name(username)

    # 注意这里故意不区分"用户不存在"和"密码不对"，统一说一句话。
    # 如果分开提示，别人就能拿它当探测工具：
    # 一直试用户名，看到"密码不对"就说明这个用户名是存在的。
    # 这叫"用户名枚举"，是很常见的一种信息泄露。
    if not u or not auth.verify_password(password, u["salt"], u["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码不对")

    db.purge_expired_sessions()           # 顺手清掉过期的票

    token = auth.new_token()
    db.create_session(token, u["id"], auth.expires_at_str())
    db.touch_last_login(u["id"])

    return auth.json_with_cookie({
        "ok": True,
        "user": {"id": u["id"], "username": u["username"],
                 "owner": db.owner_of(u["id"])},
        "message": "登录成功",
    }, token)


@app.post("/api/auth/logout")
def api_logout(request: Request):
    """退出登录：作废门票 + 让浏览器把票删掉。

    两步都要做。只删数据库里的记录，浏览器里那张废票还留着，
    看着像没退出；只删浏览器的，数据库里那张票还能被人拿去用。
    """
    db.delete_session(auth.token_from(request))
    from fastapi.responses import JSONResponse
    resp = JSONResponse({"ok": True, "message": "已退出登录"})
    auth.clear_login_cookie(resp)
    return resp


@app.post("/api/auth/adopt")
def api_adopt(user: dict = Depends(auth.current_user)):
    """把"还没有归属的素材"接到当前账号名下。

    这些素材是你早期用命令行导入的（那时还没有账号，owner_id 是 'local'）。
    注册时没接、后来想接了，就走这里。
    """
    n = db.adopt_owner(db.DEFAULT_OWNER, user["owner"])
    if n == 0:
        return {"ok": True, "adopted": 0,
                "message": "没有可接收的素材（已经没有了，或你名下已经有素材了）"}
    return {"ok": True, "adopted": n, "message": "已接收 %d 条素材" % n}


# ----------------------------------------------------------------------
# 素材：读
#
# 从这一行往下，每个接口都多了一个 user 参数：
#     user: dict = Depends(auth.current_user)
# 它不是前端传上来的，是"进门安检"的结果。
# 下面所有 db.xxx(owner=user["owner"]) 里的 owner 都来自它 ——
# 于是每个账号只能看到自己的素材，这件事在数据层面就成立了。
# ----------------------------------------------------------------------

@app.get("/api/stats")
def api_stats(user: dict = Depends(auth.current_user)):
    """首页统计条：素材数 / 总字数 / 标签数"""
    return db.stats(owner=user["owner"])


@app.get("/api/tags")
def api_tags(user: dict = Depends(auth.current_user)):
    """标签栏：每个标签 + 它下面有几份素材"""
    return {"items": db.list_tags(owner=user["owner"]),
            "total_materials": db.stats(owner=user["owner"])["materials"]}


@app.get("/api/materials")
def api_list_materials(
    user: dict = Depends(auth.current_user),
    keyword: str = Query("", description="在标题或正文里搜这个词"),
    tag: str = Query("", description="只看挂了这个标签的素材"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """素材列表（不含正文，正文点开才取，列表才快）"""
    return db.list_materials(keyword=keyword, tag=tag, owner=user["owner"],
                             limit=limit, offset=offset)


@app.get("/api/materials/{mid}")
def api_get_material(mid: int, user: dict = Depends(auth.current_user)):
    """单条素材详情（含正文）"""
    m = db.get_material(mid, owner=user["owner"])
    if not m:
        raise HTTPException(status_code=404, detail="没有这条素材")
    return m


# ----------------------------------------------------------------------
# 素材：写
# ----------------------------------------------------------------------

@app.post("/api/materials")
def api_paste(req: PasteIn, user: dict = Depends(auth.current_user)):
    """粘贴一段文字直接入库（没带标题就用正文前 20 字当标题）"""
    text = (req.content or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="正文是空的，没什么可存的")

    title = (req.title or "").strip()
    if not title:
        title = text[:20].replace("\n", " ")

    saved = db.save_material(title=title, text=text, ext=".txt",
                             source_path="（手动粘贴）", note="手动粘贴导入",
                             tags=req.tags, owner=user["owner"])
    if saved["status"] == "same":
        return {"ok": True, "status": "same", "id": saved["id"],
                "message": "这段内容库里已经有了，没有重复存"}
    return {"ok": True, "status": "new", "id": saved["id"],
            "message": "已存入素材库"}


def _safe_name(name):
    """从浏览器传来的文件名里，只取最后一段。

    为什么必须做这件事：
    浏览器（和一些工具）传来的文件名可能带路径，甚至是
    "..\\..\\Windows\\System32\\xxx.txt" 这种东西。
    如果直接拿它拼落盘路径，就等于让别人决定往你磁盘的哪里写文件。
    这叫"路径穿越"，是最经典的攻击之一。
    只取最后一段文件名，路径部分全部丢掉。
    """
    name = (name or "").replace("\\", "/").split("/")[-1].strip()
    # 顺手挡掉几个在 Windows 上不合法或会被系统特殊对待的字符
    for ch in '<>:"|?*':
        name = name.replace(ch, "_")
    return name or "未命名"


@app.post("/api/upload")
async def api_upload(
    user: dict = Depends(auth.current_user),
    files: List[UploadFile] = File(...),
):
    """浏览器里选文件 / 拖文件上传。支持一次多个。

    上传走的是"文件内容"（浏览器出于安全，拿不到你磁盘的真实路径），
    所以这种方式不会自动带文件夹名标签 —— 标签就用文件名自己。

    返回结构和文件夹导入保持一致，前端可以复用同一套渲染：
        {"ok": true, "summary": {...}, "items": [...]}
    """
    tmp_dir = os.path.join(BASE_DIR, "data", "tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    items = []
    for f in files:
        shown = _safe_name(f.filename)
        ext = os.path.splitext(shown)[1].lower()

        if ext not in parsers.PARSERS:
            items.append({"name": shown, "status": "fail", "chars": 0, "tags": [],
                          "message": f"暂不支持 {ext or '（无扩展名）'} 格式"})
            continue

        raw = await f.read()
        if not raw:
            items.append({"name": shown, "status": "fail", "chars": 0, "tags": [],
                          "message": "文件是空的"})
            continue

        # 落到临时文件时用一个随机名，不沿用用户给的文件名。
        # 这样即使文件名很奇怪，也不会影响磁盘上的路径。
        # 落盘的原因：parsers.py 是按路径解析的（docx/pdf/xlsx 都需要真文件），
        # 复用它能避免为"上传"再写一套解析逻辑。
        tmp_path = os.path.join(tmp_dir, secrets.token_hex(8) + ext)
        try:
            with open(tmp_path, "wb") as fh:
                fh.write(raw)
            r = parsers.parse_file(tmp_path)
        finally:
            # 删掉临时文件。这里刻意连 BaseException 一起兜住，而不是只 catch Exception。
            #
            # 原因：素材内容和解析结果此时已经拿到手了，入库也马上就完成 ——
            # 「删不掉一个临时文件」是无关紧要的小事，
            # 绝不能因为它把整个上传打成失败。
            # 而且某些运行环境会给 os.remove 套一层安全策略，
            # 拦下来时抛的甚至不是 Exception，而是 SystemExit（BaseException 的子类），
            # 单 catch Exception 是接不住的。
            try:
                os.remove(tmp_path)
            except BaseException:
                pass

        if not r["ok"]:
            items.append({"name": shown, "status": "fail", "chars": 0, "tags": [],
                          "message": r["note"]})
            continue

        if not r["text"].strip():
            items.append({"name": shown, "status": "empty", "chars": 0, "tags": [],
                          "message": r["note"] or "文件里没有可读的文字"})
            continue

        title = os.path.splitext(shown)[0]
        saved = db.save_material(title=title, text=r["text"], ext=r["ext"],
                                 source_path="（上传：" + shown + "）",
                                 note=r["note"], tags=[title],
                                 owner=user["owner"])
        items.append({"name": shown, "status": saved["status"],
                      "id": saved["id"], "chars": saved["chars"],
                      "tags": [title], "message": r["note"]})

    summary = importer._summarize(items)
    summary["scanned"] = len(items)
    return {"ok": True, "summary": summary, "items": items}


@app.patch("/api/materials/{mid}")
def api_update_material(mid: int, req: UpdateIn,
                        user: dict = Depends(auth.current_user)):
    """改标题 / 改标签 / 切换「仅本地」"""
    ok = db.update_material(mid, owner=user["owner"], title=req.title,
                            tags=req.tags, local_only=req.local_only)
    if not ok:
        raise HTTPException(status_code=404, detail="没有这条素材")
    return {"ok": True, "material": db.get_material(mid, user["owner"])}


@app.delete("/api/materials/{mid}")
def api_delete_material(mid: int, user: dict = Depends(auth.current_user)):
    """删掉一条素材"""
    if not db.delete_material(mid, owner=user["owner"]):
        raise HTTPException(status_code=404, detail="没有这条素材")
    return {"ok": True, "message": "已删除"}


# ----------------------------------------------------------------------
# 导入：文件夹
#
# 分成两步是刻意的：先 scan（只报告，不写库），确认了再 run（真写库）。
# 批量导入不可逆，多按一下确认键，比事后清理划算。
# ----------------------------------------------------------------------

@app.post("/api/import/scan")
def api_import_scan(req: PathIn, user: dict = Depends(auth.current_user)):
    """第一步：扫一遍文件夹，报告会导入什么（不写数据库）"""
    path = (req.path or "").strip().strip('"')
    if not path:
        raise HTTPException(status_code=400, detail="请填写文件夹或文件路径")
    if not os.path.exists(path):
        raise HTTPException(status_code=400, detail="路径不存在：" + path)

    if os.path.isfile(path):
        one = importer.import_file(path, dry_run=True, owner=user["owner"])
        return {"ok": True, "is_file": True, "folder": os.path.dirname(path),
                "summary": importer._summarize([one]), "items": [one]}

    res = importer.import_folder(path, recursive=req.recursive,
                                 dry_run=True, owner=user["owner"])
    res["is_file"] = False
    return res


@app.post("/api/import/run")
def api_import_run(req: PathIn, user: dict = Depends(auth.current_user)):
    """第二步：真的导入"""
    path = (req.path or "").strip().strip('"')
    if not path:
        raise HTTPException(status_code=400, detail="请填写文件夹或文件路径")
    if not os.path.exists(path):
        raise HTTPException(status_code=400, detail="路径不存在：" + path)

    if os.path.isfile(path):
        one = importer.import_file(path, local_only=req.local_only,
                                   owner=user["owner"])
        return {"ok": True, "is_file": True,
                "summary": importer._summarize([one]), "items": [one]}

    res = importer.import_folder(path, recursive=req.recursive,
                                 local_only=req.local_only, owner=user["owner"])
    res["is_file"] = False
    return res
