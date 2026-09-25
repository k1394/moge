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
from typing import Dict, List, Optional

from fastapi import (Depends, FastAPI, File, HTTPException, Query, Request,
                     UploadFile)
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    from backend import auth, db, importer, parsers
    from backend import classify_db as cls
    from backend import classification as auto
    from backend import plots_db as plots
    from backend import plots_ai as pai
    from backend import segmentation as sg
    from backend import llm
except ImportError:                                   # pragma: no cover
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from backend import auth, db, importer, parsers
    from backend import classify_db as cls
    from backend import classification as auto
    from backend import plots_db as plots
    from backend import plots_ai as pai
    from backend import segmentation as sg
    from backend import llm


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
INDEX_HTML = os.path.join(FRONTEND_DIR, "index.html")


# lifespan = 应用启动和关闭时要做的事。
# 在这里建表：服务每次启动都确保表存在，反复启动也不会出错（IF NOT EXISTS）。
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # 分类库的建表与迁移：反复启动是安全的（IF NOT EXISTS + 查过再加列）
    cls.migrate()
    # 自动分类任务的建表。必须在 cls.migrate() 之后 ——
    # 它要给 cls 建的 ai_judgements 补两列，那张表得先存在。
    auto.migrate()
    # 剧情内化库的建表（plots / plot_cards / plot_versions）。
    # 纯新增三张空表，没有迁移、不碰任何已有表，所以随时跑都安全。
    plots.migrate()
    # AI 内化那一步的四张表（plot_runs / plot_run_cards / plot_items /
    # plot_candidates）。同样是纯新增空表。必须在 plots.migrate() 之后 ——
    # 落库时要往 plots / plot_cards 里写，那两张得先存在。
    pai.migrate()
    # 把上次运行留下的"还在跑"的任务收尾。
    # 必须放在启动时做：后台任务跑在进程内的线程里，进程一没线程就没了，
    # 但任务表里还写着 running —— 界面上会永远显示"分类中"而进度不动。
    auto.reap_orphan_runs()
    # 内化任务同理。它会顺手把"没轮到的"卡片记成失败，
    # 所以点「重试」只会补这些，已经跑过的那几十批不会重花钱。
    pai.reap_orphan_runs()
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


# ======================================================================
# 素材分类库
# ======================================================================
#
# 这一整块回答一个问题：怎么把一份文件变成"一条一条能看能改的素材卡片"。
#
# 分成三步，每一步都能停下来看：
#     1. 来源列表   —— 有哪些文件、切了没有、能切多少条
#     2. 切分预览   —— 先看切得对不对（这一步不写任何数据）
#     3. 确认生成   —— 真的切、真的建卡片
#
# 之后就是卡片流的增删改查，以及"任何批量操作都能撤销"。
#
# 安全约定（和素材接口完全一致）：
#     每个接口都挂 Depends(auth.current_user)，
#     所有查询都带 owner=user["owner"] ——
#     别人的卡片、切分、变更记录一个字都看不到。
#     绝不接受前端传来的用户身份。
#
# 路由顺序有个坑：/api/cards/merge 和 /api/cards/batch-update 这种
# 固定路径，必须写在 /api/cards/{cid} 前面。
# 否则 FastAPI 会先拿 "merge" 去当卡片 id 解析，报 422。
# ----------------------------------------------------------------------

class SplitIn(BaseModel):
    """确认切分"""
    rule: Optional[str] = None      # None = 用自动判断的结果
    force: bool = False             # 换切法时是否强制


class RestoreIn(BaseModel):
    """把被自动排除的噪音段恢复成卡片"""
    run_id: int
    segment_ids: Optional[List[int]] = None


class CardPatchIn(BaseModel):
    """改单张卡片。没传的字段不动；clear_category=True 表示清空主类。"""
    category_id: Optional[int] = None
    clear_category: bool = False
    sub_tags: Optional[List[str]] = None
    status: Optional[str] = None
    note: Optional[str] = None


class BatchPatchIn(BaseModel):
    """批量改卡片"""
    card_ids: List[int] = []
    category_id: Optional[int] = None
    clear_category: bool = False
    sub_tags: Optional[List[str]] = None
    status: Optional[str] = None
    note: Optional[str] = None
    sample_ids: List[int] = []
    sample_result: str = ""


class SplitCardIn(BaseModel):
    """拆分一张卡。cuts 是原文里的绝对偏移（卡内部的位置）"""
    cuts: List[int] = []


class MergeIn(BaseModel):
    card_ids: List[int] = []


class SourceMapIn(BaseModel):
    """改来源映射表的一行"""
    source_collection: str = ""
    category_id: Optional[int] = None
    clear_category: bool = False
    note: Optional[str] = None
    confirmed: Optional[bool] = None


class SourceApplyIn(BaseModel):
    """按来源批量采纳。

    confirm=False 只写"建议"，状态保持待确认；
    confirm=True  才标记为已确认（只该用于来源明确的那几个，比如"神态"）。
    """
    source_collection: str = ""
    category_id: int
    confirm: bool = False
    material_id: Optional[int] = None
    sample_ids: List[int] = []
    sample_result: str = ""


class SubTagIn(BaseModel):
    """副标签的增删停用"""
    name: str = ""
    action: str = "add"          # add / rename / deactivate / activate / merge
    new_name: str = ""
    merge_into: str = ""


def _patch_from_body(body) -> dict:
    """把请求体转成 classify_db 要的 patch 字典。

    规则：字段"传了"才进 patch。
    clear_category=True 是一种"显式清空"的表达方式
    （因为 JSON 里传 null 和"没传"很难区分）。
    """
    patch = {}
    if body.clear_category:
        patch["primary_category_id"] = None
    elif body.category_id is not None:
        patch["primary_category_id"] = body.category_id
    if body.sub_tags is not None:
        patch["sub_tags"] = body.sub_tags
    if body.status is not None:
        patch["status"] = body.status
    if body.note is not None:
        patch["note"] = body.note
    return patch


@app.get("/api/material-classification/overview")
def api_cls_overview(user: dict = Depends(auth.current_user)):
    """分类库顶部的统计条"""
    return cls.overview(user["owner"])


@app.get("/api/material-classification/sources")
def api_cls_sources(user: dict = Depends(auth.current_user)):
    """左栏：文件 / 来源列表"""
    return {"items": cls.source_list(user["owner"])}


@app.get("/api/categories")
def api_categories(user: dict = Depends(auth.current_user)):
    """十一个正式主类 + 判据 + 副标签 + 状态清单 + 切分规则说明。

    一次全给前端：这三样都是"选项菜单"，页面初始化时取一次就够。
    """
    return {
        "categories": cls.list_categories(user["owner"]),
        "set_version": cls.CATEGORY_SET_VERSION,
        "sub_tags": cls.list_sub_tags(user["owner"], active_only=True),
        "sub_tags_all": cls.list_sub_tags(user["owner"]),
        "statuses": list(sg.ALL_STATUS),
        "sources": [x["source_collection"] for x in cls.source_list(user["owner"])],
        "rules": [{"key": k, "name": v["name"], "desc": v["desc"]}
                  for k, v in sg.RULES.items()],
        "norm_version": sg.NORM_VERSION,
        "rule_version": sg.RULE_VERSION,
    }


# ----------------------------------------------------------------------
# 主类 / 副标签：她自己加的那几个
#
# 【为什么给这个口子】
#   那 11 个主类是照着她手上素材的名字拟的 —— 本质是"我替她猜的一版"。
#   她写着写着一定会冒出新类（「打斗」就是她后加的），与其每次都来改代码，
#   不如让她自己加。
#
# 【加出来的类只有她自己看得见】
#   owner_id 存她的账号。别人的下拉框、别人的提示词里都不会出现 ——
#   分类体系是一个人的私人语法，她的「梗」和别人的「梗」未必是同一件事。
#   系统自带的那 11 个仍是全站共用。
#
# 【两个 delete 都是"停用"不是"删除"】
#   卡片上存着主类 id。物理删掉的话那些卡的主类会显示成空白，看着像数据丢了。
#   停用只是不再出现在"选主类"的清单里，老卡片照样认得出它，重新加同名还能复活。
# ----------------------------------------------------------------------

class CategoryIn(BaseModel):
    name: str
    description: str = ""
    suggested_tags: List[str] = []


@app.post("/api/categories")
def api_create_category(req: CategoryIn, user: dict = Depends(auth.current_user)):
    """自己加一个主类。

    判据（description）是必填的，这不是形式主义：AI 判类时**只看这段说明**。
    空着它就只能照着名字猜 —— 分错了她还以为是"自己建的类不管用"。
    """
    try:
        cat = cls.create_category(user["owner"], req.name, req.description,
                                  req.suggested_tags)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "category": cat,
            # 顺手把整份清单回给前端：加完那一屏要立刻多出一个圆钮，
            # 不用再发一次请求（少一次请求就少一次"点了没反应"的机会）。
            "categories": cls.list_categories(user["owner"]),
            "message": "「%s」加好了" % cat["name"]}


@app.delete("/api/categories/{cid}")
def api_disable_category(cid: int, user: dict = Depends(auth.current_user)):
    """停用一个自己建的主类（系统自带的动不了）。"""
    cat = cls.set_category_active(user["owner"], cid, False)
    if not cat:
        raise HTTPException(status_code=404,
                            detail="没有这个主类，或者它不是你自己建的"
                                   "（系统自带的那几个不能停用）")
    return {"ok": True, "categories": cls.list_categories(user["owner"]),
            "message": "已停用「%s」" % cat["name"]}


# 小标签的新增/改名/停用/合并**本来就有**一条接口：POST /api/sub-tags
# （带 action 参数，见下面那一段）。所以这里不再另开一条 ——
# 曾经新开过一条同路径的，结果把老那条整个盖掉了（FastAPI 取先注册的那个），
# 连带把 SubTagIn 的定义也覆盖了。被 tests/test_classify_api.py 当场抓住。


@app.get("/api/material-classification/materials/{mid}/split-preview")
def api_split_preview(
    mid: int,
    user: dict = Depends(auth.current_user),
    rule: str = Query("", description="line / blank，留空则自动判断"),
    limit: int = Query(80, ge=0, le=800),
    offset: int = Query(0, ge=0),
):
    """切分预览：不写任何数据，只告诉你会切成什么样。"""
    pv = cls.split_preview(mid, user["owner"], rule=(rule or None),
                           text_limit=limit if limit else None,
                           offset=offset)
    if pv is None:
        raise HTTPException(status_code=404, detail="没有这份素材，或者它不属于你")
    if limit:
        pv["segments"] = pv["segments"][offset:offset + limit]
        pv["page"] = {"offset": offset, "limit": limit,
                      "returned": len(pv["segments"])}
    return pv


@app.post("/api/material-classification/materials/{mid}/segment-runs")
def api_apply_split(mid: int, req: SplitIn,
                    user: dict = Depends(auth.current_user)):
    """确认生成：真的切一遍，写 segment_runs + segments + cards。"""
    res = cls.apply_split(mid, user["owner"], rule=req.rule, force=req.force)
    if res is None:
        raise HTTPException(status_code=404, detail="没有这份素材，或者它不属于你")
    if not res.get("ok"):
        return res
    return res


@app.post("/api/segments/restore")
def api_restore_segments(req: RestoreIn, user: dict = Depends(auth.current_user)):
    """把被自动排除的噪音段恢复成卡片（识别不等于删除）"""
    res = cls.restore_segments_as_cards(req.run_id, user["owner"],
                                        req.segment_ids, operator_id=user["id"])
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("message", "恢复失败"))
    return res


# ---- 卡片：读 ---------------------------------------------------------

@app.get("/api/cards")
def api_list_cards(
    user: dict = Depends(auth.current_user),
    material_id: Optional[int] = None,
    source_collection: str = "",
    category_id: Optional[int] = None,
    sub_tag: str = "",
    status: str = "",
    include_excluded: bool = False,
    verify_error: bool = False,
    duplicate: bool = False,
    order: str = "seq",
    limit: int = Query(60, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """卡片流：分页 + 筛选

    筛选项对应界面上的那一排控件：
        按文件 / 按来源 / 按主类（category_id=0 表示"还没分类"）
        / 按副标签 / 按状态 / 是否显示已排除 / 只看看校验失败的
        / 只看有重复提示的
    """
    if source_collection:
        # 按来源筛选 = 这个来源名下的所有文件
        return cls.list_cards(user["owner"], source_collection=source_collection,
                              category_id=category_id,
                              sub_tag=sub_tag or None, status=status or None,
                              include_excluded=include_excluded,
                              only_verify_error=verify_error,
                              only_duplicate=duplicate, order=order,
                              limit=limit, offset=offset)

    return cls.list_cards(user["owner"], material_id=material_id,
                          category_id=category_id,
                          sub_tag=sub_tag or None, status=status or None,
                          include_excluded=include_excluded,
                          only_verify_error=verify_error,
                          only_duplicate=duplicate, order=order,
                          limit=limit, offset=offset)


@app.get("/api/cards/{cid}")
def api_get_card(cid: int, user: dict = Depends(auth.current_user)):
    """卡片详情：正文 + 前后文 + 原文位置 + 校验结果"""
    d = cls.get_card(cid, user["owner"])
    if not d:
        raise HTTPException(status_code=404, detail="没有这张卡片，或者它不属于你")
    return d


# ---- 卡片：写（批量相关的一定要放在 {cid} 前面）-----------------------

@app.post("/api/cards/batch-update")
def api_cards_batch_update(req: BatchPatchIn,
                           user: dict = Depends(auth.current_user)):
    """批量改卡片：主类 / 副标签 / 状态 / 备注。一定会留下可撤销的记录。"""
    if not req.card_ids:
        raise HTTPException(status_code=400, detail="没有选中任何卡片")
    patch = _patch_from_body(req)
    if not patch:
        raise HTTPException(status_code=400, detail="没有要修改的字段")
    res = cls.update_cards(user["owner"], req.card_ids, patch,
                           action_type="batch_update",
                           operator_id=user["id"],
                           sample_ids=req.sample_ids,
                           sample_result=req.sample_result)
    if not res:
        raise HTTPException(status_code=404, detail="选中的卡片一张都不属于你")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("message", "修改失败"))
    return res


@app.post("/api/cards/merge")
def api_cards_merge(req: MergeIn, user: dict = Depends(auth.current_user)):
    """合并相邻卡片"""
    res = cls.merge_cards(req.card_ids, user["owner"], operator_id=user["id"])
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("message", "合并失败"))
    return res


@app.patch("/api/cards/{cid}")
def api_patch_card(cid: int, req: CardPatchIn,
                   user: dict = Depends(auth.current_user)):
    """改单张卡片"""
    patch = _patch_from_body(req)
    if not patch:
        raise HTTPException(status_code=400, detail="没有要修改的字段")
    res = cls.update_cards(user["owner"], [cid], patch,
                           action_type="update_card", operator_id=user["id"])
    if not res:
        raise HTTPException(status_code=404, detail="没有这张卡片，或者它不属于你")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("message", "修改失败"))
    return {"ok": True, "change_id": res["change_id"],
            "card": cls.get_card(cid, user["owner"])}


@app.post("/api/cards/{cid}/split")
def api_split_card(cid: int, req: SplitCardIn,
                   user: dict = Depends(auth.current_user)):
    """拆分一张卡。原卡不删，标记为已排除（可恢复）。"""
    res = cls.split_card(cid, user["owner"], req.cuts, operator_id=user["id"])
    if not res:
        raise HTTPException(status_code=404, detail="没有这张卡片，或者它不属于你")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("message", "拆分失败"))
    return res


# ---- 变更记录与撤销 --------------------------------------------------

@app.get("/api/card-changes")
def api_list_changes(user: dict = Depends(auth.current_user),
                     material_id: Optional[int] = None,
                     limit: int = Query(50, ge=1, le=500)):
    """变更记录。界面上是一个"操作历史"面板，每条后面带一个撤销按钮。"""
    return {"items": cls.list_changes(user["owner"], limit=limit,
                                      material_id=material_id)}


@app.post("/api/card-changes/{cid}/undo")
def api_undo(cid: int, user: dict = Depends(auth.current_user)):
    """撤销一次变更。只恢复"本次改过、且之后没人再动过"的字段。"""
    res = cls.undo_change(user["owner"], cid)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("message", "撤销失败"))
    return res


# ---- 来源映射与抽样确认 ----------------------------------------------

@app.get("/api/source-mappings")
def api_source_mappings(user: dict = Depends(auth.current_user)):
    return {"items": cls.list_source_mappings(user["owner"])}


@app.patch("/api/source-mappings")
def api_set_source_mapping(req: SourceMapIn,
                           user: dict = Depends(auth.current_user)):
    """改一行来源映射（改建议、加备注、标记已确认）"""
    if not req.source_collection:
        raise HTTPException(status_code=400, detail="没有指定来源名")
    row = cls.set_source_mapping(
        user["owner"], req.source_collection,
        category_id=(None if req.clear_category else req.category_id),
        note=req.note, confirmed=req.confirmed)
    if not row:
        raise HTTPException(status_code=404,
                            detail="没有这个来源，或者它不属于你")
    return {"ok": True, "mapping": row}


@app.get("/api/source-mappings/sample")
def api_source_sample(
    user: dict = Depends(auth.current_user),
    source_collection: str = "",
    material_id: Optional[int] = None,
    size: int = Query(10, ge=1, le=50),
):
    """抽样检查。

    样本刻意不是纯随机：固定包含 最长 / 最短 / 含数字或表格残留 /
    极短 / 疑似重复，再用随机补齐。
    理由：最长的最容易出问题，重复的说明来源不纯 ——
    纯随机很可能一条都抽不到。
    """
    if not source_collection and not material_id:
        raise HTTPException(status_code=400, detail="要指定来源名或文件 id")
    return cls.sample_cards(user["owner"], material_id,
                            source_collection=source_collection, size=size)


@app.post("/api/source-mappings/apply")
def api_source_apply(req: SourceApplyIn,
                     user: dict = Depends(auth.current_user)):
    """按来源批量采纳。

    两件事分开：
        确认建议（confirm=False）→ 只写主类，状态仍是"待确认"
        批量确认结果（confirm=True）→ 主类 + 状态"已确认"

    为什么要分开：
        来源名本身就是一个内容类型的（比如来源名就叫「环境描写」），
        可以一次定下来；
        但来源名是"效果评价"或"写作状态"的那种（它和主类不是一回事），
        整批写"已确认"等于把不确定的东西装成确定的，以后很难查。
    """
    cards = cls.all_card_ids(user["owner"], material_id=req.material_id,
                             source_collection=req.source_collection)
    if not cards:
        raise HTTPException(status_code=400,
                            detail="这个来源下还没有卡片，先切分再采纳")

    patch = {"primary_category_id": req.category_id}
    if req.confirm:
        patch["status"] = sg.STATUS_CONFIRMED

    res = cls.update_cards(user["owner"], cards, patch,
                           action_type="source_apply" if req.confirm else "source_suggest",
                           operator_id=user["id"],
                           sample_ids=req.sample_ids,
                           sample_result=req.sample_result,
                           summary="来源「%s」批量%s %d 张卡片"
                                   % (req.source_collection,
                                      "确认" if req.confirm else "给出建议",
                                      len(cards)))
    if not res:
        raise HTTPException(status_code=404, detail="没有可改的卡片")
    # 批量没成功（比如有卡片校验不过）时，映射表也不能动 ——
    # 否则映射表写着"已确认"，卡片却还是待确认，两边就对不上了。
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("message", "批量采纳失败"))
    cls.set_source_mapping(user["owner"], req.source_collection,
                           category_id=req.category_id,
                           confirmed=True if req.confirm else None)
    return res


# ---- 副标签维护 ------------------------------------------------------

@app.post("/api/sub-tags")
def api_sub_tags(req: SubTagIn, user: dict = Depends(auth.current_user)):
    """副标签的新增 / 改名 / 停用 / 合并。

    一律不做物理删除（规格要求"旧标签不物理删除"）——
    停用之后旧卡片上挂着的记录还认得出它叫什么。
    """
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="标签名不能是空的")
    if len(name) > cls.SUB_TAG_NAME_MAX:
        # 上限跟"自己建主类"那套对齐：标签在界面上是个小圆钮，
        # 200 个字的名字会把归类面板那一行整个撑爆。
        raise HTTPException(
            status_code=400,
            detail="标签名最长 %d 个字，现在 %d 个。"
                   % (cls.SUB_TAG_NAME_MAX, len(name)))

    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM sub_tags WHERE owner_id=? AND name=?",
            (user["owner"], name)).fetchone()

        if req.action == "add":
            if row:
                if not row["active"]:
                    conn.execute("UPDATE sub_tags SET active=1 WHERE id=?", (row["id"],))
                    return {"ok": True, "message": "标签「%s」已重新启用" % name}
                return {"ok": True, "message": "标签「%s」已经有了" % name}
            conn.execute(
                "INSERT INTO sub_tags (owner_id, name, active, created_at) "
                "VALUES (?,?,1,?)", (user["owner"], name, cls.now_str()))
            return {"ok": True, "message": "已新增标签「%s」" % name}

        if not row:
            raise HTTPException(status_code=404, detail="没有这个标签")

        if req.action in ("deactivate", "activate"):
            conn.execute("UPDATE sub_tags SET active=? WHERE id=?",
                         (0 if req.action == "deactivate" else 1, row["id"]))
            return {"ok": True,
                    "message": "标签「%s」已%s"
                               % (name, "停用" if req.action == "deactivate" else "启用")}

        if req.action == "rename":
            new = (req.new_name or "").strip()
            if not new:
                raise HTTPException(status_code=400, detail="新名字不能是空的")
            if conn.execute("SELECT 1 FROM sub_tags WHERE owner_id=? AND name=?",
                            (user["owner"], new)).fetchone():
                raise HTTPException(status_code=400, detail="已经有一个叫「%s」的标签了" % new)
            conn.execute("UPDATE sub_tags SET name=? WHERE id=?", (new, row["id"]))
            return {"ok": True, "message": "「%s」已改名为「%s」" % (name, new)}

        if req.action == "merge":
            into = (req.merge_into or "").strip()
            tgt = conn.execute("SELECT * FROM sub_tags WHERE owner_id=? AND name=?",
                               (user["owner"], into)).fetchone()
            if not tgt:
                raise HTTPException(status_code=404, detail="找不到要并入的标签「%s」" % into)
            # 把挂在这个标签上的卡片改挂到目标标签，然后停用原标签
            conn.execute(
                """UPDATE OR IGNORE card_tags SET sub_tag_id=? WHERE sub_tag_id=?""",
                (tgt["id"], row["id"]))
            conn.execute("DELETE FROM card_tags WHERE sub_tag_id=?", (row["id"],))
            conn.execute("UPDATE sub_tags SET active=0 WHERE id=?", (row["id"],))
            return {"ok": True, "message": "「%s」已并入「%s」（原标签停用保留）" % (name, into)}

    raise HTTPException(status_code=400, detail="不认识的操作：%s" % req.action)


# ---- 近重复提示 ------------------------------------------------------

@app.get("/api/duplicates")
def api_duplicates(user: dict = Depends(auth.current_user),
                   material_id: Optional[int] = None,
                   source_collection: str = "",
                   limit: int = Query(200, ge=1, le=500)):
    """近重复检测。只提示，不自动删、不自动合并。"""
    return cls.duplicates(user["owner"], material_id=material_id,
                          source_collection=source_collection or None,
                          limit=limit)


# ======================================================================
# 自动分类任务（第二阶段）
# ======================================================================
#
# 这几个接口的共同点：**永远不信任前端**。
# 素材 id、任务 id 都是前端传上来的，每一次都要用"当前登录的人"去库里
# 核一遍"这东西是不是他的"。少核一次，别人就能改别人的稿子。
#
# 另一个共同点：**全都不阻塞**。
# 提交任务立刻返回一个任务号，真正的活在后台线程里跑，
# 前端拿着任务号轮询 classification-status 看进度。
#
# 现在用的分类器是「占位规则」—— 纯本地字符串判断，不联网、不花钱。
# 这一点必须让界面如实显示，不能让人以为这是 AI 判的。

class ClassifyIn(BaseModel):
    """提交一次自动分类（新建和重试共用）。

    classifier  用哪种方法：
                  留空          = 大模型（默认）
                  placeholder   = 本地占位规则
                为什么默认大模型：本地规则读不懂语义，实测分出来大半是错的。
                它只该在"还没注册模型账号"时用来把流程走通。

    model_key   用哪个模型。留空 = 清单里第一个填了 Key 的。

    user_prompt 这次要捎带的补充提示词（作者自己在界面上写的那几句）。
                留空/不传 = 用她存着的那份。
                为什么请求里也带一份：她在分类页写完那几句，
                最自然的动作是直接点「开始分类」，而不是先点保存再点开始。
                带上来的同时会替她存下来，下次进来还在。

    prompt_id   改用「提示词库」里的哪一条（快捷选项里挑的）。
                传了它就**以它为准**，user_prompt 会被忽略 ——
                见 classification.create_run 的 docstring。
                可以是别人公开出来的条目：那种情况下服务端自己取内容，
                前端全程拿不到原文（"公开但私密"）。

    两个都不传 = 不用补充提示词（空跑）。
    """
    classifier: Optional[str] = None
    model_key: Optional[str] = None
    user_prompt: Optional[str] = None
    prompt_id: Optional[int] = None


@app.post("/api/materials/{mid}/auto-classify")
def api_auto_classify(mid: int, req: Optional[ClassifyIn] = None,
                      user: dict = Depends(auth.current_user)):
    """提交一次自动分类任务。立刻返回，不等它跑完。"""
    req = req or ClassifyIn()
    # 她带上来的补充提示词：先存下来（下次进来还在），再拿这一份去建任务。
    # 顺序不能反 —— 先建任务再存的话，任务用的是旧的那份。
    #
    # 注意：这次是"选了提示词库里的某一条"时不存 ——
    # 那条内容可能是**别人的**，存进"我的补充提示词"会把别人的东西
    # 变成她自己的默认值，下次空跑就偷偷带着别人的提示词发出去了。
    user_prompt = req.user_prompt
    if user_prompt is not None and not req.prompt_id:
        try:
            user_prompt = auto.set_user_prompt(user["owner"], user_prompt)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    try:
        res, _ = auto.create_run(
            user["owner"], mid,
            classifier_name=req.classifier or auto.LlmClassifier.name,
            model_key=req.model_key,
            user_prompt=user_prompt,
            prompt_id=req.prompt_id,
            background=True)
    except ValueError as e:
        # 「没填 API Key」「没有这个模型」这类"配置还没弄好"的错，
        # 在这里就变成一句人话返回给她 —— 不要建完任务再失败。
        raise HTTPException(status_code=400, detail=str(e))
    if not res.get("ok"):
        # 「已经有一个在跑」不是她的错，用 409（冲突）比 400 更准；
        # 但前端只需要知道没成功 + 原因，所以统一 400 也能用。
        # 这里保持 400，免得前端要为一个分支多写一套处理。
        raise HTTPException(status_code=400, detail=res.get("message", "提交失败"))
    return res


@app.get("/api/materials/{mid}/classification-status")
def api_classification_status(mid: int, user: dict = Depends(auth.current_user)):
    """这个文件现在的分类状态：按钮该显示成什么、进度多少、各类各多少张。"""
    st = auto.material_state(user["owner"], mid)
    if st is None:
        raise HTTPException(status_code=404, detail="没有这份素材，或者它不属于你")
    return st


@app.post("/api/materials/{mid}/classification-retry")
def api_classification_retry(mid: int, req: Optional[ClassifyIn] = None,
                             user: dict = Depends(auth.current_user)):
    """重试。只重跑上次失败的那些卡片；上次没失败的才整份重来。

    默认**沿用上次用的模型**（理由见 classification.retry_run 的注释：
    中途换模型会让结果一半是 A 判的、一半是 B 判的，而她不会知道）。
    真要换，就在请求里带上 model_key。
    """
    req = req or ClassifyIn()
    try:
        res = auto.retry_run(mid, user["owner"],
                             classifier_name=req.classifier,
                             model_key=req.model_key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("message", "重试失败"))
    return res


@app.post("/api/materials/{mid}/classification-cancel")
def api_classification_cancel(mid: int, user: dict = Depends(auth.current_user)):
    """取消正在跑的任务。

    按素材 id 找，不按任务 id —— 界面上她是对着"这个文件"点的取消，
    不需要（也不该）知道当前任务是几号。
    """
    run = auto.active_run(user["owner"], mid)
    if not run:
        raise HTTPException(status_code=400, detail="这个文件现在没有在跑的任务")
    res = auto.cancel_run(run["id"], user["owner"])
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("message", "取消失败"))
    return res


@app.get("/api/classification-runs")
def api_classification_runs(user: dict = Depends(auth.current_user),
                            material_id: Optional[int] = None,
                            limit: int = Query(50, ge=1, le=200)):
    """任务历史。放的是"谁在什么时候对哪个文件做了什么"，出问题先看这里。"""
    return {"items": auto.list_runs(user["owner"], material_id=material_id,
                                    limit=limit)}


@app.get("/api/classification-runs/{run_id}")
def api_classification_run(run_id: int, user: dict = Depends(auth.current_user)):
    run = auto.get_run(run_id, user["owner"])
    if not run:
        raise HTTPException(status_code=404, detail="没有这个任务，或者它不属于你")
    return run


@app.get("/api/classification-runs/{run_id}/items")
def api_classification_run_items(run_id: int,
                                 user: dict = Depends(auth.current_user),
                                 status: str = Query(""),
                                 limit: int = Query(200, ge=1, le=1000),
                                 offset: int = Query(0, ge=0)):
    """任务明细：每一条卡片处理成什么样，失败的话原因是什么。"""
    d = auto.list_items(run_id, user["owner"], status=status or None,
                        limit=limit, offset=offset)
    if d is None:
        raise HTTPException(status_code=404, detail="没有这个任务，或者它不属于你")
    return d


@app.get("/api/classification-states")
def api_classification_states(user: dict = Depends(auth.current_user)):
    """总素材库里每一份素材的自动分类状态。

    为什么单开一个接口而不是塞进 /api/materials：
        /api/materials 是"素材库"那条线的核心接口，被好几处复用。
        只为了给按钮取个状态就去改它的返回结构，一旦出错影响面太大。
        单独一个接口，坏了也只坏这一个按钮。

    顺带把"现在能选哪些模型"也带上 —— 总素材库的按钮点下去之前要能选模型，
    为这个再多拉一次接口不值得。
    """
    items = db.list_materials(owner=user["owner"], limit=500)["items"]
    ids = [m["id"] for m in items]
    models = llm.public_models()
    usable = [m for m in models if m.get("enabled") and m.get("has_key")]

    # usable_models 给的是**精简后的对象**，不是光秃秃的 key 字符串。
    # 为什么：界面要在两处显示它的名字 —— "已配好 1 个（通义千问 Plus）"
    # 和下拉框的选项文字。只给 key 的话界面拿不到人话名字，
    # 要么显示成 qwen-plus（看着像机器），要么自己再去 models 里翻一遍。
    # 给对象最省事，也不会多带一个字段给别人（api_key 早在 public_models 里被摘掉了）。
    usable_brief = [{"key": m["key"], "label": m["label"],
                     "model": m["model"]} for m in usable]

    out = {"items": auto.material_states(user["owner"], ids),
           "models": models,
           "usable_models": usable_brief,
           "default_model": usable_brief[0]["key"] if usable_brief else "",
           "default_classifier": (auto.LlmClassifier.name if usable
                                  else auto.PlaceholderClassifier.name),
           "llm_ready": bool(usable)}

    # 界面上那句"当前用的是什么"必须如实说。
    # 分两种说法写，是为了别在她还没配 Key 的时候骗她说在用 AI。
    if usable:
        out["note"] = ("自动分类会调用你选的那个大模型 —— "
                       "素材片段会发到模型服务商的服务器上，"
                       "发之前界面上会先跟你确认一次。")
    else:
        out["note"] = ("还没配置任何大模型。现在只能跑本地占位规则："
                       "不联网、不花钱，但它读不懂语义，分出来大半是错的。"
                       "去「模型设置」填一个 API Key 就能用真模型。")
    return out


# ======================================================================
# 补充提示词（作者自己写的那几句）
# ======================================================================
#
# 【为什么要有这一块】
#   调好的那版提示词在 prompts/classify.txt 里，动它要改文件、重启服务。
#   但有一类调整是**临时的、跟这次素材有关**的，比如
#   「这份稿子里的『他』都指师兄，别按第三人称叙述判」。
#   为这种事去改文件太麻烦，而且改完忘了改回来更麻烦。
#   所以单独给一个"这次想额外交代几句"的位置。
#
# 【为什么存库不存文件】
#   它跟着账号走（换个账号就是另一套口味），而且必须是能随时改、
#   改了立刻生效的。放文件里就又要重启。
#
# 【和那两个硬约束的关系】
#   它排在主类清单和 JSON 格式之后，提示词里明说了"不能推翻"。
#   见 classification.build_messages 里那段拼装。

class PromptIn(BaseModel):
    content: str = ""


@app.get("/api/classify-prompt")
def api_get_classify_prompt(user: dict = Depends(auth.current_user)):
    content = auto.get_user_prompt(user["owner"])
    return {"content": content, "max": auto.USER_PROMPT_MAX,
            "chars": len(content)}


@app.post("/api/classify-prompt")
def api_set_classify_prompt(req: PromptIn,
                            user: dict = Depends(auth.current_user)):
    """存下来。超长直接拒绝，不悄悄截断 ——
    截到一半的提示词会变成一句没头没尾的话，模型照样照做，
    而她以为自己那整段都发出去了。"""
    try:
        content = auto.set_user_prompt(user["owner"], req.content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "content": content, "chars": len(content),
            "max": auto.USER_PROMPT_MAX}


# ======================================================================
# 提示词库
# ======================================================================
#
# 【和上面那个「补充提示词」什么关系】
#   上面那个是**一个框**：她随手敲的几句，一对一的（一个账号一份）。
#   这里是**一个库**：她攒下来的成句提示词，能反复用、能改名、
#   能公开给别人用。快捷选项里挑的就是这里的东西。
#   两者不冲突：挑了库里的某条，就用那条；没挑，就用框里那几句。
#
# 【最要紧的一条约定，改这里的代码前先读三遍】
#   公开 ≠ 内容可见。
#   公开 = **别人可以拿它去跑分类，但永远看不到里面写了什么**。
#   所以：
#     · 自己的清单（GET /api/prompt-library 的 mine）—— 带 content
#     · 别人的公开清单（同接口的 public）—— **不带 content**
#     · 用时由服务端按 id 取内容直接塞进任务（create_run(prompt_id=...)）
#   谁要是想在这个文件里给 public 那批补一个 content 字段，
#   先回答一句："凭什么让别人看见她写的东西？"

class PromptLibIn(BaseModel):
    """新建 / 修改一条提示词。

    name        名称，≤30 字。快捷选项里就靠它认人。
    content     提示词正文（真正发给模型的那段），≤5000 字。
    usage_note  使用方法，≤50 字，一句话。
    summary     介绍，≤6000 字。
    visibility  private / public。public 的含义见上面那段注释 ——
                **不是**"内容公开"。
    """
    name: Optional[str] = None
    content: Optional[str] = None
    usage_note: Optional[str] = None
    summary: Optional[str] = None
    visibility: Optional[str] = None


@app.get("/api/prompt-library")
def api_prompt_library(user: dict = Depends(auth.current_user)):
    """我的全部 + 别人公开的那批（后者**不带内容**）。

    一次全给：快捷选项那个下拉要同时显示"我的"和"可以借用的"，
    分成两个请求只会让界面闪两下。
    """
    mine = auto.list_my_library_prompts(user["owner"])
    pub = auto.list_public_library_prompts(user["owner"])
    return {
        "mine": mine,
        "public": pub,
        "max": {"name": auto.PROMPT_NAME_MAX,
                "content": auto.PROMPT_CONTENT_MAX,
                "usage_note": auto.PROMPT_USAGE_MAX,
                "summary": auto.PROMPT_SUMMARY_MAX},
        # 前端要拿这个数去提示"还能写多少字"，别在 JS 里再抄一份常量
        "visibilities": list(auto.VISIBILITIES),
    }


@app.post("/api/prompt-library")
def api_create_prompt(req: PromptLibIn,
                      user: dict = Depends(auth.current_user)):
    """新建一条。同名会被拒（不覆盖）—— 覆盖会静默毁掉她之前写的那份。"""
    try:
        item = auto.create_library_prompt(
            user["owner"], req.name, req.content,
            usage_note=req.usage_note or "", summary=req.summary or "",
            visibility=req.visibility or auto.VIS_PRIVATE)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "item": item}


@app.patch("/api/prompt-library/{pid}")
def api_update_prompt(pid: int, req: PromptLibIn,
                      user: dict = Depends(auth.current_user)):
    """改一条。请求里**没出现的字段不动** —— 跟模型设置那边一个规矩。

    为什么不能让"字段传空"等于"清空"：
    界面上保存时是把整个表单发上来的，清空和"这个字段没填"分不清。
    想清空就显式传空串（""），不传（None）才是不动。
    靠 _body_dict(sent_only=True) 把"没传"和"传了空"分开，
    不然她只改个名字，介绍和正文会被一起抹掉。
    """
    patch = _body_dict(req, sent_only=True)
    patch = {k: v for k, v in patch.items() if v is not None}
    try:
        item = auto.update_library_prompt(user["owner"], pid, patch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not item:
        raise HTTPException(status_code=404,
                            detail="没有这条提示词，或者它不是你的")
    return {"ok": True, "item": item}


@app.delete("/api/prompt-library/{pid}")
def api_delete_prompt(pid: int, user: dict = Depends(auth.current_user)):
    """删一条（软删）。历史任务记录里还引用着它，物理删掉就查不到名字了。"""
    if not auto.delete_library_prompt(user["owner"], pid):
        raise HTTPException(status_code=404,
                            detail="没有这条提示词，或者它不是你的")
    return {"ok": True}


# ======================================================================
# 模型设置（多模型接入）
# ======================================================================
#
# 【为什么要有这一块】
# 她要拿同一批素材试不同的模型，看哪个分得准 ——
# 那"用哪个模型"就必须是界面上能改的东西，不能写死在代码里。
#
# 【密钥怎么保护的】
#   · 存 data/models.json。data/ 整块在 .gitignore 里，同步不出去
#   · 接口**只返回打码版**，真钥匙不出后端
#   · 回传空字符串 = "不改密钥"。界面上显示的是 sk-abc****wxyz，
#     她只改了个显示名就回传，不能因此把钥匙清掉

class ModelIn(BaseModel):
    """一条模型配置。字段含义见 backend/llm.py 的 DEFAULT_MODELS。

    clear_api_key 不是配置字段，是一个动作开关：
        勾上它 = "把这一条的密钥抹掉"。
        为什么不能靠 api_key="" 来表达：空字符串在保存那一步的含义是
        "我没改密钥"（界面显示的是打码版，回传不了原文）。
        两种意图共用一个值必然分不清，所以删密钥要有自己的开关。
    """
    key: str = ""
    label: str = ""
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    enabled: bool = True
    note: str = ""
    clear_api_key: bool = False


def _body_dict(m, sent_only=False):
    """Pydantic 新旧版都能用。

    为了一行取值去赌版本不值当 —— 项目里 .dict() 和 model_dump()
    的写法都出现过，这里两个都兜住。

    sent_only=True 表示"只要客户端真的发过来的字段"。
    为什么需要它：ModelIn 给每个字段都留了默认值（空串），
    于是不管客户端发了什么，转出来的字典都是齐全的一整份 ——
    后端的"没传就不改"逻辑（见 llm.upsert_model）就再也分不出
    "她没传中文名"和"她把中文名清空了"，一条配置的名字会被悄悄改掉。
    """
    if hasattr(m, "model_dump"):
        return m.model_dump(exclude_unset=sent_only)
    return m.dict(exclude_unset=sent_only)              # pragma: no cover


@app.get("/api/models")
def api_models(user: dict = Depends(auth.current_user)):
    """能选的模型清单。**密钥是打码的，真钥匙不出后端。**"""
    items = llm.public_models()
    return {"items": items,
            "usable": [m["key"] for m in items if m["enabled"] and m["has_key"]],
            "file": llm.models_path()}


@app.post("/api/models")
def api_save_model(req: ModelIn, user: dict = Depends(auth.current_user)):
    """改一条模型配置（按 key 认）。**不含"删密钥"那个动作**，见下面。

    为什么"改"和"加"分成两个接口：
        在她那侧它们是两个按钮。在【添加】里撞上已有的名字，
        正确反应是说"这个名字有了"，而不是默默覆盖掉原来那条。
    """
    want = _body_dict(req, sent_only=True)
    _k = (want.get("key") or "").strip()
    if _k and llm.get_model(_k) is None:
        # 【为什么这里不许"顺手新建"】
        # upsert 的底层行为是"没有就追加"。界面上这不合适：
        # 她在另一个标签页把某条删了，这边还开着旧表单，点保存 ——
        # 那条会被一个**没有地址、没有模型名的空壳**复活，
        # 而且看起来像"我明明删了"。新增必须走「添加一个模型」那条路。
        raise HTTPException(
            status_code=400,
            detail="清单里没有「%s」这一条了（可能刚被删掉）。"
                   "刷新一下页面；要新增请用「添加一个模型」。" % _k)
    try:
        # 顺手支持"保存 + 同时清空密钥"（她勾了清空又点保存的场景）
        if want.pop("clear_api_key", False):
            llm.clear_api_key(want.get("key"))
        llm.upsert_model(want)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "items": llm.public_models()}


@app.post("/api/models/add")
def api_add_model(req: ModelIn, user: dict = Depends(auth.current_user)):
    """新增一条（比如往中转站、或硅基流动这类第三方接）。

    一般不用她手填 base_url：界面上的「添加」会按选的服务商预填好，
    她只要粘 Key。但预填只是省事，四个字段都能自己改 ——
    中转站的地址和模型名千奇百怪，写死一套必然有一半人填不进去。
    """
    want = _body_dict(req, sent_only=True)
    want.pop("clear_api_key", None)
    try:
        item = llm.add_model(want)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "key": item["key"], "items": llm.public_models()}


@app.post("/api/models/clear-key")
def api_clear_model_key(req: ModelIn, user: dict = Depends(auth.current_user)):
    """把某一条的密钥抹掉（配置留着）。

    她要的就是这个：填错了要能撤。以前只能把整条删掉重加，
    地址、模型名、备注一起丢。
    """
    try:
        llm.clear_api_key((req.key or "").strip())
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "items": llm.public_models()}


@app.post("/api/models/test")
def api_test_model(req: ModelIn, user: dict = Depends(auth.current_user)):
    """拿这条配置真发一句话，看连不连得通。

    【为什么值得单开一个接口】
    她填完 Key 的第一件事一定是"这样行不行"。让她跑一次 630 张卡的
    自动分类来试，又慢又费钱 —— 这里只发一句「在吗」，
    顺带把用量也带回来（让她对"跑一遍要花多少"有个直觉）。
    """
    want = _body_dict(req, sent_only=True)
    base = llm.get_model(want.get("key")) or {}
    cfg = {}
    for f in ("key", "label", "base_url", "model", "api_key", "note"):
        v = (want.get(f) or "").strip()
        # 空的一律用已存的 —— 界面回传的是打码版，本来也回传不了原文
        cfg[f] = v or (base.get(f) or "")

    try:
        r = llm.chat(cfg, [{"role": "user", "content": "在吗？回我一个字就行。"}],
                     temperature=0.0, timeout=30)
    except llm.LlmError as e:
        raise HTTPException(status_code=400, detail=e.message)
    return {"ok": True, "model": r.get("model"),
            "reply": (r.get("content") or "")[:60],
            "usage": r.get("usage") or {}}


@app.post("/api/models/delete")
def api_delete_model(req: ModelIn, user: dict = Depends(auth.current_user)):
    """把一条配置从清单里移走。

    只是移走，不是"抹掉痕迹" —— 历史任务记录里还留着它的 key 和模型名，
    所以以前跑过的结果照样能回答"那次用的是谁"。
    """
    key = (req.key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="没说是哪一条")
    items = [m for m in llm.load_models() if m["key"] != key]
    llm.save_models(items)
    return {"ok": True, "items": llm.public_models()}


# ══════════════════════════════════════════════════════════════════════
# 剧情内化库：剧情零件
#
# 【它和上面那一大段的区别】
#   上面全是"素材"——原文、切片、卡片、分类。原文一个字不许动。
#   这一段是"成品"——把素材抽象成"换个角色名还成立"的通用剧情。
#   零件将来要直接喂给大纲生成，所以它必须自足到"光看它就能用"。
#
# 【这一轮（第 1 步）只做人工零件】
#   新建 / 编辑 / 版本 / 恢复 / 排除 / 来源定位。
#   一个 AI 都不调 —— AI 内化、多模型候选、延迟反馈学习是第 2~5 步的事。
#   先把"她自己手写的零件能存能改能追版本"这条路走通，
#   再加 AI 才有地方放它的产出。
#
# 【两条不许破的规矩】
#   一、改内容一定产生新版本（见 plots_db._write_version 的说明）。
#      她改零件不是审一遍就完，是用很久以后还会回来改。
#   二、来源关系只存在 plot_cards 里。零件和卡片都不存正文副本，
#      正文永远现算 materials.content[start:end]。
# ══════════════════════════════════════════════════════════════════════

def _given(req):
    """只取"前端这次真的传了"的字段 —— PATCH 语义靠它。

    【为什么必须这样】「字段在不在这次传来的东西里」决定改不改，
    「值是不是空」决定改成什么。不区分的话，
    她只想改个标题，回传的 JSON 里没带 summary，
    就会被当成"把摘要清空"。这种 bug 不报错，只是内容悄悄没了。
    （和模型配置那套 PATCH 语义是同一条规矩，见 llm.upsert_model）
    """
    if hasattr(req, "model_dump"):
        return req.model_dump(exclude_unset=True)
    return req.dict(exclude_unset=True)


class PlotIn(BaseModel):
    """手工新建一条剧情零件。"""
    title: str
    summary: str = ""
    plot_type: str = ""
    usage_hints: List[str] = []
    beats: Dict[str, str] = {}
    role_slots: List[str] = []
    category_id: Optional[int] = None
    # 手工建的默认 human；第 2 步"采用 AI 候选"时会传 ai
    source: str = "human"
    status: Optional[str] = None
    card_ids: List[int] = []
    change_note: str = ""


class PlotPatch(BaseModel):
    """改一条零件。字段全是可选的 —— 传哪个改哪个。"""
    title: Optional[str] = None
    summary: Optional[str] = None
    plot_type: Optional[str] = None
    usage_hints: Optional[List[str]] = None
    beats: Optional[Dict[str, str]] = None
    role_slots: Optional[List[str]] = None
    # 想清空分类就显式传 null（不是不传）
    category_id: Optional[int] = None
    status: Optional[str] = None
    change_note: str = ""


class PlotStatusIn(BaseModel):
    status: str = ""
    change_note: str = ""


class PlotVersionIn(BaseModel):
    """恢复历史版本。version_id 是必填的 —— 不填就没法知道要恢复哪一版。

    【为什么单独一个模型，不塞进 PlotStatusIn】
    恢复版本和"改状态"是两件事。混在一个模型里，
    前端传 {"status": "已排除"} 去恢复版本时，version_id 会静默是 0，
    接口只能报"没说是要恢复哪一版" —— 报得对，但错在入口就该拦住。
    """
    version_id: int = 0
    change_note: str = ""


@app.get("/api/plot-meta")
def api_plot_meta(user: dict = Depends(auth.current_user)):
    """剧情内化页初始化要用的所有"选项菜单"，一次全给。

    【为什么把中文标签也从后端发下去】
    剧情类型、使用场景、零件状态、beats 的六个点位、来源标记 ——
    这些值的**唯一定义处**都在 backend/plots_db.py 里。
    前端要是自己抄一份中文标签，我这边改一个字（比如"前提"→"起手"），
    前端就永远显示老的了，而且不会有任何报错。
    这和"状态轴只在 segmentation.py 定义一处、前端从接口拿"是同一个规矩。
    """
    cats = cls.list_categories(user["owner"])
    return {
        "plot_types": list(plots.PLOT_TYPES),
        "usage_hints": list(plots.USAGE_HINTS),
        "statuses": list(plots.ALL_PLOT_STATUS),
        "sources": dict(plots.SOURCE_LABELS),
        "beats": [{"key": k, "label": plots.BEAT_LABELS[k]}
                  for k in plots.BEAT_KEYS],
        "card_states": dict(plots.CARD_INFUSE_LABELS),
        "limits": {
            "title_max": plots.TITLE_MAX,
            "summary_max": plots.SUMMARY_MAX,
            "note_max": plots.NOTE_MAX,
            "role_slots_max": plots.ROLE_SLOTS_MAX,
            "usage_hints_max": plots.USAGE_HINTS_MAX,
        },
        "categories": cats,
    }


@app.get("/api/plots")
def api_plots(user: dict = Depends(auth.current_user),
              category_id: Optional[str] = Query(None),
              status: Optional[str] = Query(None),
              plot_type: Optional[str] = Query(None),
              keyword: Optional[str] = Query(None),
              order: str = Query("category"),
              limit: int = Query(300, ge=1, le=1000),
              offset: int = Query(0, ge=0)):
    """零件列表。默认按主分类分段（跟素材分类库一个排法，未分类排最后）。"""
    return plots.list_plots(user["owner"], category_id=category_id, status=status,
                            plot_type=plot_type, keyword=keyword, order=order,
                            limit=limit, offset=offset)


@app.post("/api/plots")
def api_create_plot(req: PlotIn, user: dict = Depends(auth.current_user)):
    """手工新建一条剧情零件。

    可以一张来源卡都不挂（先记个想法，以后再补来源）——
    所以 card_ids 不是必填。
    """
    try:
        p = plots.create_plot(
            user["owner"], req.title, summary=req.summary,
            plot_type=req.plot_type, usage_hints=req.usage_hints,
            beats=req.beats, role_slots=req.role_slots,
            category_id=req.category_id, source=req.source, status=req.status,
            card_ids=req.card_ids, change_note=req.change_note,
            created_by=user.get("name") or user.get("owner") or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "plot": p, "message": "零件建好了"}


@app.get("/api/plots/{pid}")
def api_get_plot(pid: int, user: dict = Depends(auth.current_user)):
    """一条零件的详情（带来源清单和版本数）。不是自己的，一律说"没有这个"。"""
    p = plots.get_plot(user["owner"], pid)
    if not p:
        raise HTTPException(status_code=404, detail="没有这条剧情零件")
    return p


@app.patch("/api/plots/{pid}")
def api_patch_plot(pid: int, req: PlotPatch,
                   user: dict = Depends(auth.current_user)):
    """改一条零件。

    【两类改动走两条路，别混】
      · 传了内容字段（标题/摘要/beats/…）→ 产生一个新版本
      · 只传 status → 只改状态，**不产生版本**
    任务书第 168 行：「用户确认不产生新内容版本，只改变零件状态。」
    内容一个字没动却建个版本，版本历史里会堆满一模一样的东西，
    以后翻起来更累。
    """
    given = _given(req)
    body = {k: v for k, v in given.items()
            if k not in ("status", "change_note")}
    out = None
    ver = None

    if body:
        try:
            out, ver = plots.update_plot(
                user["owner"], pid, body,
                change_note=given.get("change_note", ""),
                created_by=user.get("name") or user.get("owner") or "")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if out is None:
            raise HTTPException(status_code=404, detail="没有这条剧情零件")

    if "status" in given:
        try:
            r = plots.set_plot_status(user["owner"], pid, given["status"])
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if r is None:
            raise HTTPException(status_code=404, detail="没有这条剧情零件")
        out = r

    if out is None:
        # 只传了 change_note 之类，什么都没改 —— 别假装改了
        out = plots.get_plot(user["owner"], pid)
        if not out:
            raise HTTPException(status_code=404, detail="没有这条剧情零件")
        return {"ok": True, "plot": out, "version_no": None,
                "message": "这次没有要改的东西"}

    return {"ok": True, "plot": out, "version_no": ver,
            "message": ("已存成 v%d" % ver) if ver else "改好了"}


@app.get("/api/plots/{pid}/versions")
def api_plot_versions(pid: int, user: dict = Depends(auth.current_user)):
    """版本历史（新的在前）。"""
    v = plots.list_versions(user["owner"], pid)
    if v is None:
        raise HTTPException(status_code=404, detail="没有这条剧情零件")
    return {"versions": v, "count": len(v)}


@app.post("/api/plots/{pid}/restore-version")
def api_restore_version(pid: int, req: PlotVersionIn,
                        user: dict = Depends(auth.current_user)):
    """把一个历史版本的内容恢复出来。

    恢复成功**会产生一个新版本**（内容跟那个老版本一样），
    老版本一条都不删 —— 这样"我什么时候恢复过什么"也留了痕。
    """
    if not req.version_id:
        raise HTTPException(status_code=400, detail="没说是要恢复哪一版")
    out, ver = plots.restore_version(
        user["owner"], pid, req.version_id, change_note=req.change_note,
        created_by=user.get("name") or user.get("owner") or "")
    if out is None:
        raise HTTPException(status_code=404, detail="没有这条剧情零件，或者没有这个版本")
    return {"ok": True, "plot": out, "version_no": ver,
            "message": "恢复好了，存成 v%d（老版本都还在）" % ver}


@app.post("/api/plots/{pid}/exclude")
def api_exclude_plot(pid: int, req: PlotStatusIn,
                     user: dict = Depends(auth.current_user)):
    """排除一条零件。

    【为什么不物理删除】引用过它的大纲（将来的功能）还得认得它，
    而且她可能是手滑点错了。排除是状态，随时能恢复。
    """
    given = _given(req)
    r = plots.set_plot_status(user["owner"], pid, plots.PLOT_STATUS_EXCLUDED)
    if r is None:
        raise HTTPException(status_code=404, detail="没有这条剧情零件")
    return {"ok": True, "plot": r,
            "message": (given.get("change_note") or "").strip() or "排除了，随时能恢复"}


@app.post("/api/plots/{pid}/restore")
def api_restore_plot(pid: int, req: PlotStatusIn,
                     user: dict = Depends(auth.current_user)):
    """把排除掉的零件恢复回来。"""
    given = _given(req)
    want = given.get("status") or plots.PLOT_STATUS_CONFIRMED
    r = plots.set_plot_status(user["owner"], pid, want)
    if r is None:
        raise HTTPException(status_code=404, detail="没有这条剧情零件")
    return {"ok": True, "plot": r, "message": "恢复了"}


@app.get("/api/plots/{pid}/sources")
def api_plot_sources(pid: int, user: dict = Depends(auth.current_user)):
    """这条零件的来源素材清单。

    每条都带一个 locate 字段：
        ok       原文还在原地，点开就能看
        moved    卡片被拆分/合并过，或者这段原文后来被改过
        missing  卡片没了
    界面拿它决定是"点开看原文"还是"提醒她已经变了"——
    总比悄悄给她看一段错的内容强。
    """
    s = plots.list_sources(user["owner"], pid)
    if s is None:
        raise HTTPException(status_code=404, detail="没有这条剧情零件")
    return {"sources": s, "count": len(s)}


@app.get("/api/cards/{cid}/plots")
def api_card_plots(cid: int, user: dict = Depends(auth.current_user)):
    """反向链接：这张素材卡片被哪些剧情零件用到了。

    素材分类库的卡片上那句「已内化 · 看零件 →」就靠它。
    没有这个的话，她分不清哪些卡白放过了、哪些还没处理。
    """
    rows = plots.plots_of_card(user["owner"], cid)
    return {"plots": rows, "count": len(rows)}


@app.get("/api/plot-card-states")
def api_plot_card_states(card_ids: str = "",
                         user: dict = Depends(auth.current_user)):
    """批量取卡片的内化状态，给「素材分类」页每张卡上那个标记用。

    【为什么不一张卡发一次】
    素材分类页一屏 30 张、她自己拉到几百张，
    逐张问就是几百个请求；而且这只是一句"这张内化过没有"的提示，
    不值得为它把页面拖慢。

    参数 card_ids 用逗号分隔（不传 = 全部）。状态值全部由关联表算出来，
    cards 表里没有、也不该有这一列，理由见 plots_db.card_infuse_states()。
    """
    ids = [x.strip() for x in card_ids.split(",") if x.strip()]
    states = plots.card_infuse_states(user["owner"], ids or None)
    return {
        "states": {str(k): v for k, v in states.items()},
        "labels": plots.CARD_INFUSE_LABELS,
    }


# ══════════════════════════════════════════════════════════════════════
# 剧情内化 · AI 那一半（第 2 步）
#
# 【它和上面第 1 步那段的区别】
#   上面全是"零件本身"：手工建、改、版本、排除、来源定位。一个 AI 都不调。
#   这一段是"让 AI 批量把卡片抽象成零件"：发起任务、看进度、取消、重试。
#   真正的编排逻辑全在 backend/plots_ai.py，这里只是把开关接到网页上。
#
# 【为什么发起之后立刻返回】
#   任务书第十五节禁止"把任务执行放在 HTTP 请求里长时间阻塞页面"。
#   663 张卡按 12 张一批是 56 批、每批等模型几十秒 —— 放在请求里页面就死了。
#   所以这里只建任务记录 + 起后台线程，进度靠前端轮询 /api/infuse-runs/{id}。
#
# 【AI 提的零件落在哪儿】
#   直接进【剧情内化库】，status='待确认' + source='ai' + 带置信度和理由。
#   她逐条确认／改／排除。「已确认」永远只能由人点出来。
# ══════════════════════════════════════════════════════════════════════


class InfuseIn(BaseModel):
    """发起一次 AI 内化。

    user_prompt 的三种含义（跟 PATCH 语义一脉相承）：
      不传  → 用她存着的那份补充提示词
      传 ""  → 这次不要补充提示词
      传文字 → 这次就用这段
    """
    model_key: str = ""
    user_prompt: Optional[str] = None
    skip_infused: bool = True
    # 只跑前 N 张（0 = 不限）。663 张的稿子先跑 20 张试水用得上。
    limit: int = 0


class InfusePromptIn(BaseModel):
    content: str = ""


@app.get("/api/infuse-meta")
def api_infuse_meta(user: dict = Depends(auth.current_user)):
    """内化弹层初始化要用的东西，一次全给齐。

    为什么连批次大小都下发：界面上要跟她说"首批先发 3 张、之后每批 12 张"。
    硬编码在前端的话，后端调了批次大小、界面上那句提示就成了假话。
    """
    return {
        "models": llm.public_models(),
        "usable_model_keys": [m["key"] for m in llm.usable_models()],
        "user_prompt": pai.get_user_prompt(user["owner"]),
        "user_prompt_max": pai.USER_PROMPT_MAX,
        "low_confidence": pai.CONFIDENCE_LOW,
        "first_batch_size": pai.FIRST_BATCH_SIZE,
        "batch_size": pai.BATCH_SIZE,
        "max_cards_per_run": pai.MAX_CARDS_PER_RUN,
        "prompt_version": pai.prompt_version(),
        "result_labels": pai.RESULT_LABELS,
        "skipped_statuses": list(pai.SKIP_STATUS),
        "active_statuses": list(pai.RUN_ACTIVE),
    }


@app.get("/api/materials/{mid}/infuse-preview")
def api_infuse_preview(mid: int,
                       skip_infused: int = 1, limit: int = 0,
                       user: dict = Depends(auth.current_user)):
    """这份素材会送出去多少张卡。一个字都不写库。

    弹层打开时先调它 —— 663 张的稿子和 20 张的稿子，
    她要花的钱和要等的时间差两个数量级，动手前必须先看见这个数。
    """
    pv = pai.target_preview(user["owner"], mid, bool(skip_infused), limit)
    if not pv.get("ok"):
        raise HTTPException(status_code=404,
                            detail=pv.get("message") or "没有这份素材")
    return pv


@app.get("/api/infuse-states")
def api_infuse_states(user: dict = Depends(auth.current_user)):
    """总素材库里每一份素材的内化状态（含"还有几张没跑过"）。

    为什么单开一个接口而不是塞进 /api/materials：
        跟 /api/classification-states 同一个理由 —— /api/materials 是
        素材库那条线的核心接口，好几处在用；为了给按钮取个状态就动它，
        一旦出错影响面太大。单独一个接口，坏了也只坏这一个状态条。
    """
    return pai.states_for_owner(user["owner"])


@app.post("/api/materials/{mid}/infuse")
def api_infuse_start(mid: int, req: InfuseIn,
                     user: dict = Depends(auth.current_user)):
    """发起 AI 内化。立刻返回 run_id，活儿在后台线程里干。"""
    body = _given(req)
    res, _run_id = pai.create_run(
        user["owner"], mid,
        model_key=body.get("model_key") or "",
        user_prompt=body.get("user_prompt"),
        skip_infused=bool(body.get("skip_infused", True)),
        limit=int(body.get("limit") or 0),
        background=True)
    if not res.get("ok"):
        raise HTTPException(status_code=400,
                            detail=res.get("message") or "发起内化失败")
    return res


@app.get("/api/infuse-runs")
def api_infuse_runs(material_id: int = 0, limit: int = 20,
                    user: dict = Depends(auth.current_user)):
    """最近的内化任务。进度条和"这个文件跑到第几批了"都靠它。"""
    rows = pai.list_runs(user["owner"], material_id or None, limit)
    return {"runs": rows, "count": len(rows)}


@app.get("/api/infuse-runs/{rid}")
def api_infuse_run(rid: int, user: dict = Depends(auth.current_user)):
    """一个任务的进度。不是自己的，一律说"没有这个"。"""
    r = pai.get_run(rid, user["owner"])
    if not r:
        raise HTTPException(status_code=404, detail="没有这个内化任务")
    return {"run": r}


@app.get("/api/infuse-runs/{rid}/items")
def api_infuse_items(rid: int, status: str = "", outcome: str = "",
                     limit: int = Query(1000, ge=1, le=3000),
                     offset: int = Query(0, ge=0),
                     user: dict = Depends(auth.current_user)):
    """逐张卡片的处理结果。失败的那些她要看得到"哪一张、为什么"。"""
    if not pai.get_run(rid, user["owner"]):
        raise HTTPException(status_code=404, detail="没有这个内化任务")
    rows = pai.list_items(rid, user["owner"], status or None,
                          outcome or None, limit, offset)
    return {"items": rows, "count": len(rows)}


@app.get("/api/infuse-runs/{rid}/candidates")
def api_infuse_candidates(rid: int, result: str = "",
                          limit: int = Query(200, ge=1, le=1000),
                          user: dict = Depends(auth.current_user)):
    """AI 提出的候选（含原始返回）。

    【为什么候选也要给前端】零件已经直接落库了，但候选里还有
    "拿不准"和"不适合"的那些 —— 她要能看到模型对这批素材的整体判断，
    才知道"为什么这份稿子一条零件都没出"。
    raw_response **不下发**（又长又乱，她看的是结构化那几个字段）。
    """
    if not pai.get_run(rid, user["owner"]):
        raise HTTPException(status_code=404, detail="没有这个内化任务")
    rows = pai.list_candidates(rid, user["owner"], result or None, limit)
    for r in rows:
        r.pop("raw_response", None)
    return {"candidates": rows, "count": len(rows),
            "result_labels": pai.RESULT_LABELS}


@app.post("/api/infuse-runs/{rid}/cancel")
def api_infuse_cancel(rid: int, user: dict = Depends(auth.current_user)):
    """取消。已经跑好的批次留着，不删。"""
    try:
        pai.cancel_run(rid, user["owner"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "message": "已取消。跑好的部分都留着。"}


@app.post("/api/infuse-runs/{rid}/retry")
def api_infuse_retry(rid: int, user: dict = Depends(auth.current_user)):
    """重试。**只补没跑成的那几张**，跑好的绝不重花钱。"""
    try:
        res, _new_id = pai.retry_run(rid, user["owner"], background=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not res.get("ok"):
        raise HTTPException(status_code=400,
                            detail=res.get("message") or "重试失败")
    return res


@app.get("/api/infuse-prompt")
def api_infuse_prompt_get(user: dict = Depends(auth.current_user)):
    """读她给内化写的补充提示词。

    跟自动分类那个框共用同一张表（user_prompts），靠 kind 区分，
    所以她在这两个框里踩的是同一套规则，不用学两遍。
    """
    return {"content": pai.get_user_prompt(user["owner"]),
            "max": pai.USER_PROMPT_MAX}


@app.put("/api/infuse-prompt")
def api_infuse_prompt_put(req: InfusePromptIn,
                          user: dict = Depends(auth.current_user)):
    """存她给内化写的补充提示词。超长直接 400，不悄悄截断。"""
    try:
        c = pai.set_user_prompt(user["owner"], req.content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "length": len(c), "max": pai.USER_PROMPT_MAX}
