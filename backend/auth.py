# -*- coding: utf-8 -*-
"""
墨阁 · 账号与登录
========================================================
这个文件只干两件事：

    一、把密码搅碎（不能直接存密码）
    二、认出"这次请求是谁发来的"（登录状态）

为什么密码不能直接存进数据库，是这一层要解决的核心问题。

假设直接把密码明文存下来，那么：
    · 数据库文件被人拷走 → 所有人的密码全泄露
    · 你是写小说的人，很多人在不同网站用同一个密码
      → 你这边泄露，别人的邮箱、支付账号跟着遭殃
    · 连你自己都不该看到用户的密码

所以真正存进去的是"密码搅碎后的结果"：

    密码 + 随机盐  →  [搅碎函数]  →  一串乱码
    存这串乱码。登录时把用户输入按同样方式搅碎，比对两串乱码是否相同。

为什么还要加"盐"：
    不加盐的话，两个人密码都是 123456，搅碎结果就一模一样，
    一眼能看出"这两个人密码相同"。
    盐是给每个账号单独生成的一小段随机字符，搅碎时掺进去 ——
    同样输入 123456，A 和 B 得到的结果完全不同。
    另外，加盐后黑客没法用"常见密码对照表"批量反查。

为什么用 pbkdf2 而不是一次 md5：
    搅碎函数可以被暴力试。一次 md5 一秒能试几十亿次，
    而 pbkdf2 是刻意"慢"的 —— 故意让它一次要花十万分之一秒。
    正常登录你感觉不到（就慢这一下），
    但想暴力穷举就慢了千万倍，不划算了。

这里用的都是 Python 自带的标准库（hashlib / hmac / secrets），
不用额外装东西。
"""

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta

from fastapi import Request
from fastapi.responses import JSONResponse

try:
    from backend import db
except ImportError:                                   # pragma: no cover
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from backend import db


# ----------------------------------------------------------------------
# 参数
# ----------------------------------------------------------------------

# 搅碎的轮数。数字越大越安全，但登录越慢。
# 20 万轮是目前的常见推荐值，本机测下来一次约 0.05 秒，人感觉不到。
ITERATIONS = 200_000

# 浏览器里那张"门票"的 Cookie 名
COOKIE_NAME = "moge_session"

# 门票有效期（天）。超过就要重新登录。
SESSION_DAYS = 30


# ----------------------------------------------------------------------
# 一、密码
# ----------------------------------------------------------------------

def make_salt():
    """生成一段随机盐。

    secrets 和 random 的区别：
        random 是给"抽奖、洗牌"用的，结果能被推算出规律
        secrets 是给"密钥、密码"用的，密码学安全
    沾安全的地方一律用 secrets。
    """
    return secrets.token_hex(16)          # 16 字节 → 32 位十六进制字符串


def hash_password(password, salt):
    """把密码搅碎。同一个密码 + 同一个盐，结果永远一样（登录时靠这点比对）。"""
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        ITERATIONS,
    ).hex()


def verify_password(password, salt, expected_hash):
    """比对密码对不对。

    为什么用 hmac.compare_digest 而不是直接 == ：
    两个字符串用 == 比较时，发现第一个字符不同就立刻返回，
    所以"第 1 位对"和"前 10 位都对"的耗时不一样。
    攻击者可以靠测量响应时间，一个字符一个字符地猜出正确答案（叫时序攻击）。
    compare_digest 从头到尾看完，耗时恒定，堵住这条路。
    """
    if not password or not salt or not expected_hash:
        return False
    actual = hash_password(password, salt)
    return hmac.compare_digest(actual, expected_hash)


# ----------------------------------------------------------------------
# 二、校验规则
#
# 校验放在后端，不能只靠前端 —— 前端的检查是给用户看的提示，
# 后端才是真正把关的地方（前端代码用户在浏览器里能改）。
# ----------------------------------------------------------------------

USERNAME_RE = re.compile(r"^[\w\u4e00-\u9fa5-]{2,20}$")
#   \w            字母数字下划线
#   \u4e00-\u9fa5 中文
#   -             连字符
#   2 到 20 个字符


def check_username(name):
    """返回错误提示；没问题返回空字符串。

    「返回提示语而不是抛异常」是一种风格：
    校验失败是预料之中的正常情况，不是程序出了故障，
    所以用返回值表达，让上层的接口层决定怎么告诉用户。
    """
    name = (name or "").strip()
    if not name:
        return "请填写用户名"
    if len(name) < 2 or len(name) > 20:
        return "用户名要 2 到 20 个字符"
    if not USERNAME_RE.match(name):
        return "用户名只能用中文、字母、数字、下划线或连字符"
    return ""


def check_password(pwd):
    """密码规则：只要求长度，不强制符号组合。

    原因：强制"必须含大写+数字+符号"实际效果不好 ——
    用户会写成 Abc123! 这类好猜的，或者贴在纸上。
    长度才是真正管用的（每多一位，穷举难度翻很多倍）。
    """
    pwd = pwd or ""
    if not pwd:
        return "请填写密码"
    if len(pwd) < 6:
        return "密码至少 6 位"
    if len(pwd) > 128:
        return "密码太长了"
    return ""


# ----------------------------------------------------------------------
# 三、门票（登录状态）
# ----------------------------------------------------------------------

def new_token():
    """生成一张门票。

    token_urlsafe(32) 生成 32 字节随机数，转成 URL 安全的字符串。
    这个长度下猜中概率约等于零 —— 比"猜中宇宙里某个原子"还难。
    """
    return secrets.token_urlsafe(32)


def expires_at_str():
    """门票到期时间（和数据层用同一种时间格式，方便直接比大小）"""
    return (datetime.now() + timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")


def set_login_cookie(response, token):
    """把门票塞进浏览器 Cookie。

    每个参数都是在防一件事：
        httponly=True   网页里的 JS 读不到它 —— 万一页面被注入恶意脚本，
                        脚本也偷不走这张票
        samesite="lax"  别的网站替你发请求时，浏览器不会带上这张票
                        （防 CSRF：防止别人做个假页面骗你点一下删光素材）
        max_age         到期自动失效，不用服务器记着删
        path="/"        整个站点都带上它

    secure=True 的作用是"只在 https 下才发这张票"，
    但我们现在是 http://localhost 本地跑，开了会导致登录成功却始终没登录。
    将来真的上线换了 https，记得把它打开。
    """
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_DAYS * 24 * 3600,
        path="/",
    )


def clear_login_cookie(response):
    """删掉浏览器里的票（退出登录时）"""
    response.delete_cookie(key=COOKIE_NAME, path="/")


# ----------------------------------------------------------------------
# 四、认出请求是谁发的
#
# 下面两个函数是给 main.py 用的"依赖"。
# FastAPI 的依赖机制：接口函数的参数里写 current_user: dict = Depends(current_user)，
# FastAPI 就会先调这个函数拿结果，再进来执行接口本身。
# 好处是"必须登录"这件事只写一遍，所有接口自动都有了。
# ----------------------------------------------------------------------

def token_from(request: Request):
    """从请求的 Cookie 里取门票"""
    return request.cookies.get(COOKIE_NAME, "")


def optional_user(request: Request):
    """取当前用户；没登录返回 None。用于"登录与否都能访问"的接口。"""
    sess = db.get_session(token_from(request))
    if not sess:
        return None
    u = db.get_user(sess["user_id"])
    if not u:
        return None
    return {"id": u["id"], "username": u["username"],
            "owner": db.owner_of(u["id"]), "expires_at": sess["expires_at"]}


def current_user(request: Request):
    """必须登录。没登录就抛 401，前端看到 401 会弹回登录页。

    抛的必须是 HTTPException —— FastAPI 才认识它，
    会把它变成正确的 HTTP 响应，而不是当成程序崩溃。
    """
    from fastapi import HTTPException

    u = optional_user(request)
    if not u:
        raise HTTPException(status_code=401, detail="还没登录，或登录已过期")
    return u


def json_with_cookie(payload, token):
    """返回 JSON，同时顺手把门票写进 Cookie。

    为什么要自己拼响应对象：FastAPI 里给普通返回值加 Cookie 不方便，
    直接构造 JSONResponse 更直白。
    """
    resp = JSONResponse(payload)
    set_login_cookie(resp, token)
    return resp
