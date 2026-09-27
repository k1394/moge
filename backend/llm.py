# -*- coding: utf-8 -*-
"""
墨阁 · 大模型接入层
========================================================

这个文件只管一件事：**怎么跟大模型说话**。
（至于"跟它说什么" —— 提示词、判据、怎么解析结果 —— 那是
 classification.py 的事。分开是为了以后加新功能时不用重写这一层。）

--------------------------------------------------------
一、为什么一份代码能接好几家模型
--------------------------------------------------------
国内能直接用的几家（通义千问、DeepSeek、智谱、Kimi、硅基流动…）
都提供了 **OpenAI 兼容接口**：请求地址长得一样、请求体长得一样、
返回的 JSON 也长得一样，差别只有三处：

    base_url      服务地址
    model         模型名
    api_key       密钥

所以不需要为每家写一套代码，只要一份**清单**，每条写清这三样。
任务跑之前挑一条，跑完把用的是哪条记下来 —— 这样同一批素材
换个模型再跑一次，两次结果并排看，就知道哪个分得准。

--------------------------------------------------------
二、清单放在哪，为什么不放代码里
--------------------------------------------------------
    data/models.json

    - data/ 整块在 .gitignore 里，永远同步不到公开仓库。
      密钥放这儿，是"不进代码、不进公开仓库"这条底线的落点。
    - 首次运行时自动生成一份**不含任何密钥**的默认清单，
      她只要往里填 Key 就能用。
    - 她随时能改、能加（比如换一个自己有的模型）。

--------------------------------------------------------
三、三条安全底线（这个文件里最要紧的东西）
--------------------------------------------------------
    1. **密钥不进日志**。要打日志的地方一律用 mask_key() 打码。
    2. **报错信息里不能带密钥**。上游报错有时候会把整个
       Authorization 头回显出来，所以抛出前必须过一遍 _scrub()。
    3. 给界面的永远是打码版（public_models()），真钥匙不出这个文件。

--------------------------------------------------------
四、超时 / 重试 / 限流
--------------------------------------------------------
    一次性把 630 张卡发出去肯定不行（又慢又容易整体失败），
    所以调用方是分批的（见 classification.BATCH_SIZE）。
    这一层负责单次请求的可靠性：

    - 超时：单次 180 秒（默认值，调用方可以覆盖）。分类一批 25 条，
      正常十几秒就回来了，给到 180 秒是为了兜住模型偶发的慢。
      **生成大纲不吃这个默认值** —— 它一次要吐 8000 字，
      outline_ai 自己传 OUTLINE_TIMEOUT=600，理由见常量区的注释。
    - 重试：只有"重试有意义"的错才重试 ——
      429（限流）、5xx（服务端抽风）、网络断。
      401（密钥不对）、400（请求不合法）**不重试**，因为重试一百次
      还是同样的结果，白白等，还把真正的错因埋掉。
      次数默认 3 次，长任务可以传 max_retry 砍掉（大纲传 1 次）。
      超时算不算"重试有意义"要看任务：短问答重试划算，
      长生成不划算 —— 详见 MAX_RETRY 上面的注释。
    - 退避：等 2 秒、4 秒、8 秒。紧接着重试只会再撞一次限流。
"""

import inspect
import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request

from backend import db


# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------

MODELS_FILE = "models.json"

# 这三个默认值是照着「短回答」定的：分类、内化一次只吐几百字，
# 180 秒绰绰有余，失败了再试两次也划算。
#
# **长任务必须自己传 timeout / max_retry，别吃这套默认值。**
# 生成大纲一次要吐 8000 字，单个模型就要跑一两分钟，180 秒是掐着线过的
# （实测通义千问 108 秒，离超时只剩 72 秒）；一旦超时还会重试 3 次，
# 等于白等 9 分钟去等一个注定不回的请求。而且超时只是「我们不等了」，
# 服务端那边该算的已经算完了 —— 重试一次就可能多扣一次钱。
# 所以 outline_ai 显式传了 OUTLINE_TIMEOUT / max_retry=1。
#
# 改这里的数之前先算一遍：最坏等待 = timeout × max_retry + 退避总和。
TIMEOUT = 180          # 单次请求超时（秒）
MAX_RETRY = 3          # 最多试 3 次（含第一次）
RETRY_BACKOFF = 2.0    # 退避基数：第 n 次失败后等 2**n 秒


# ----------------------------------------------------------------------
# 请求自报的身份（User-Agent）
#
# 【为什么非要有这一行】2026-09-26 实测踩到的坑，她报「CCS 里能用，
# 填到你这里就报错」。查下来是这样：
#   有些中转站把接口挂在 Cloudflare 后面，并且开了「防机器人」规则 ——
#   它**按请求自己报上来的名字**放行或拦截。
#   Python 默认自报 "Python-urllib/3.13"，正好撞在它的黑名单上，
#   于是**连网站首页都回 403**（Cloudflare Error 1010: Access denied），
#   密钥对不对根本没轮到检查。界面上却只能看到一句「密钥不认」，
#   她就去反复折腾 Key —— 而问题压根不在 Key 上。
#   实测同一个地址：默认身份 → 403；换成下面这个名字 → 立刻通，
#   回的是正常的 401「要密钥」。
#
# 【为什么不去冒充浏览器】
# 不需要。实测 Moge/1.0、curl、node 这些**中性名字全都能过**，
# 只有 "Python-urllib/*" 这一串被点名封。既然自报真名就能进门，
# 就没有理由去假装自己是 Chrome。
# ----------------------------------------------------------------------
USER_AGENT = "Moge/1.0 (+local writing tool)"


# ----------------------------------------------------------------------
# 默认清单
#
# 【注意】这里面**一个密钥都没有**，也不可能有。
# 密钥只能由她在界面上填，填完存进 data/models.json。
#
# 每个字段什么意思：
#   key      内部用的短名字（记进任务表的就是它，以后改名会连不上历史记录）
#   label    界面上显示的名字
#   base_url 服务地址（OpenAI 兼容端点，注意结尾是 /v1 那一层）
#   model    模型名（这一项最容易过时 —— 各家改版会换名字。
#            报错说"模型不存在"就来这儿改成控制台里的名字）
#   api_key  密钥（默认空，等填）
#   enabled  关掉之后界面上就不显示它了（但不删，历史记录还认得出）
# ----------------------------------------------------------------------

DEFAULT_MODELS = [
    {
        "key": "qwen-plus",
        "label": "通义千问 Plus（阿里云百炼）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "api_key": "",
        "enabled": True,
        "note": "便宜、快。分类这种活够用，建议先拿它试。"
                "阿里巴巴旗下，国内直连不用梯子。",
    },
    {
        "key": "qwen-max",
        "label": "通义千问 Max（阿里云百炼）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-max",
        "api_key": "",
        "enabled": True,
        "note": "同一个阿里云账号、同一个 Key，判断更细，贵一截。"
                "如果 Plus 分不对，拿它再跑一遍对比。",
    },
    {
        "key": "deepseek-chat",
        "label": "DeepSeek Chat",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key": "",
        "enabled": True,
        "note": "另一家公司，便宜，中文语感好。"
                "https://platform.deepseek.com 申请 Key。",
    },
    {
        "key": "glm-4-flash",
        "label": "智谱 GLM-4-Flash",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "api_key": "",
        "enabled": True,
        "note": "有免费额度，适合先试水。"
                "https://open.bigmodel.cn 申请。",
    },
    {
        "key": "kimi",
        "label": "Kimi（月之暗面）",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "api_key": "",
        "enabled": True,
        "note": "长文处理好。模型名各家改版时会变，"
                "报错说找不到模型就来这儿按控制台改。",
    },
]

# 界面上允许她手填的字段。多出来的字段一律丢掉 ——
# 免得配置文件里被塞进奇怪的东西。
_ALLOWED_FIELDS = ("key", "label", "base_url", "model", "api_key",
                   "enabled", "note", "custom", "provider")


# ----------------------------------------------------------------------
# 错误
# ----------------------------------------------------------------------

class LlmError(Exception):
    """跟模型打交道出的错。

    message 一定已经过 _scrub() 抹过密钥，**可以直接给她看**。
    retryable 告诉上层"这个错再试一次有没有意义"。

    elapsed_ms 是"这一次请求从发出去到出错等了多久"。
    【为什么挂在错误上带出来】失败也是花过时间的（可能还花了钱），
    耗时表要连失败一起记 —— 只记成功的那几次，一个"十次里失败八次"
    的模型在对比表里反而显得又快又稳，结论正好反了。
    """

    def __init__(self, message, retryable=False, status=None, elapsed_ms=0):
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status = status
        self.elapsed_ms = elapsed_ms


# ----------------------------------------------------------------------
# 密钥打码
# ----------------------------------------------------------------------

def mask_key(k):
    """把密钥变成 sk-abc****wxyz 这样，用来显示。

    太短的干脆全打码 —— 短字符串露头露尾等于没打码。
    """
    k = (k or "").strip()
    if not k:
        return ""
    if len(k) <= 12:
        return "*" * len(k)
    return k[:6] + "****" + k[-4:]


def _scrub(text, key):
    """把文本里出现的密钥抹掉。

    【为什么必须有这个】
    上游报错的时候，有些服务会把整个请求头（含 Authorization: Bearer sk-…）
    原样回显在错误信息里。这个字符串如果一路冒到界面上、或者写进日志，
    密钥就等于贴出去了。所以**所有**抛出去的错误信息都要过这一遍。
    """
    text = str(text or "")
    k = (key or "").strip()
    if k and k in text:
        text = text.replace(k, mask_key(k))
    return text


# ----------------------------------------------------------------------
# 清单读写
# ----------------------------------------------------------------------

def models_path():
    """模型清单文件的位置。跟着 MOGE_DATA_DIR 走（测试时用临时目录）。"""
    return os.path.join(db.DATA_DIR, MODELS_FILE)


def _clean(item):
    """把一条配置洗干净：只留认识的字段，类型摆正，key 去空格。"""
    if not isinstance(item, dict):
        return None
    out = {}
    for f in _ALLOWED_FIELDS:
        if f in item:
            out[f] = item[f]
    out["key"] = str(out.get("key") or "").strip()
    if not out["key"]:
        return None                       # 没名字的条目没法引用，丢掉
    out["label"] = str(out.get("label") or out["key"]).strip()
    out["base_url"] = str(out.get("base_url") or "").strip()
    out["model"] = str(out.get("model") or "").strip()
    out["api_key"] = str(out.get("api_key") or "").strip()
    out["note"] = str(out.get("note") or "").strip()
    out["enabled"] = bool(out.get("enabled", True))
    return out


# ----------------------------------------------------------------------
# 三之二、接入点（providers）
#
# 【为什么要有这一层】
# 她原话：「阿里云一个 api 可以调用那么多模型，网站能不能统一一下，
# 我想用其他模型的免费额度。」
# 实测：她那个阿里云 Key 的 /models 能拉到 261 个模型，
# 里面不光通义 —— glm-5.3、kimi/kimi-k2.8-preview、deepseek-v4.1-flash、
# stepfun/step-5-preview 全都能调。
#
# 按老结构（每条模型自带地址 + 密钥），要用 20 个模型就得把同一个 Key
# 抄 20 遍 —— 现在的文件里 qwen-plus 和 qwen-max 已经抄了两遍。
# 所以拆成两层：
#     接入点 = 「地址 + 密钥」，填一次
#     模型   = 挂在某个接入点下，只填模型名
#
# 【向后兼容怎么保证】
# providers 是**新增**的一节。老文件里没有它，代码不会因此读不出来：
# load_models() 的结果跟以前一字不差（模型自己填了地址/密钥就用自己的）。
# 接入点只在内存里推导，她主动保存时才落盘 —— 这样即便推导逻辑有问题，
# 也只是"接入点显示得不好看"，不会让正在跑的分类和大纲哑掉。
# ----------------------------------------------------------------------

PROVIDER_FIELDS = ("key", "label", "base_url", "api_key", "note")


def _clean_provider(item):
    """把一条接入点洗干净。字段少，规矩跟 _clean 一样。"""
    if not isinstance(item, dict):
        return None
    out = {}
    for f in PROVIDER_FIELDS:
        if f in item:
            out[f] = item[f]
    out["key"] = str(out.get("key") or "").strip()
    if not out["key"]:
        return None                       # 没名字的接入点没法被引用
    out["label"] = str(out.get("label") or out["key"]).strip()
    # 地址统一去掉末尾斜杠：她抄文档时常常带一个 "/v1/"，
    # 拼 "/models" 时会变成 "//models"，有的服务商因此 404。
    out["base_url"] = str(out.get("base_url") or "").strip().rstrip("/")
    out["api_key"] = str(out.get("api_key") or "").strip()
    out["note"] = str(out.get("note") or "").strip()
    return out


def _read_raw():
    """读整个 models.json，返回原始的两节 (providers, models)。

    【为什么要"整个读"】
    这个文件里现在住着两样东西：接入点和模型。
    以前 save_models() 是"直接覆盖整个文件"，加了 providers 之后
    那样写会把她的接入点**连同里面的密钥一起抹掉** —— 而且不报错。
    所以读写都改成"整个文件进出"。
    """
    path = models_path()
    if not os.path.isfile(path):
        return [], []
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        raise LlmError(
            "模型清单读不了：%s\n"
            "文件位置：%s\n"
            "（内容坏掉的话，把这个文件删掉重启，会重新生成一份默认的。）"
            % (_scrub(e, ""), path))

    if isinstance(raw, dict):
        provs = raw.get("providers") or []
        models = raw.get("models") or []
    else:
        # 更老的格式：整个文件就是一个数组
        provs, models = [], raw
    return (provs if isinstance(provs, list) else [],
            models if isinstance(models, list) else [])


def _write_raw(providers, models):
    """把两节一起写回去。**这是密钥唯一落地的地方。**"""
    path = models_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)

    pout, mout = [], []
    for p in providers or []:
        c = _clean_provider(p)
        if c:
            pout.append(c)
    for m in models or []:
        c = _clean(m)
        if c:
            mout.append(c)

    # ---- 让"照着接入点走"这件事真的成立 ----
    #
    # 【为什么写的时候要动这两下】
    # 老配置里 qwen-plus 和 qwen-max 各自抄了一份阿里云的 Key。
    # 这两份要是留着，她以后在接入点上换 Key，这两个模型**不会跟着变** ——
    # load_models 的回填规则是"模型自己填了就以它自己为准"。
    # 于是"统一"成了一句空话，而且不报错：某个模型悄悄还在用旧钥匙，
    # 表现是"我明明换了 Key 它还是 401"。
    #
    # 所以落盘时做两件事（只在这个唯一出口做，别处不许各写一套）：
    #   ① 没写 provider 的老条目，按「地址 + 密钥」认领一个接入点
    #   ② 跟接入点**完全相同**的地址/密钥是冗余，清掉（留空 = 跟着接入点走）
    #
    # 【边界：只清"两边一模一样"的】
    # 她要是单独把某个模型的地址改成了中转站，那两栏不一样，原样保留 ——
    # 那个模型就该用它自己的。
    if pout:
        sig_to_key = {}
        for p in pout:
            sig_to_key[(p.get("base_url") or "", p.get("api_key") or "")] = p["key"]
        pmap = {p["key"]: p for p in pout}
        for m in mout:
            if not m.get("provider"):
                hit = sig_to_key.get((m.get("base_url") or "",
                                      m.get("api_key") or ""))
                if hit:
                    m["provider"] = hit
            p = pmap.get(m.get("provider") or "")
            if not p:
                continue
            if m.get("base_url") and m["base_url"] == p.get("base_url"):
                m["base_url"] = ""
            if m.get("api_key") and m["api_key"] == p.get("api_key"):
                m["api_key"] = ""

    payload = {
        "_说明": "这个文件里存着 API 密钥，别往外发、别提交到 git。"
                 "data/ 目录已经在 .gitignore 里。",
        "_接入点": "providers = 一个「地址 + 密钥」对，填一次即可。"
                   "models 里的 provider 指向它，就不必每个模型抄一遍钥匙；"
                   "模型自己填了 base_url / api_key 的话以它自己为准。",
        "providers": pout,
        "models": mout,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)          # 先写临时文件再换，写一半断电不会留个坏文件
    return pout, mout


def _provider_key_from_url(base_url, taken):
    """按地址给接入点起个能看懂的名字（dashscope / deepseek / siliconflow…）。

    为什么不用 p1、p2：她要在界面上认出"这堆模型是哪家的"，
    叫 p1 等于没名字。重名时加个序号。
    """
    host = ""
    m = re.search(r"https?://([^/:]+)", base_url or "")
    if m:
        host = m.group(1)
    parts = [x for x in host.split(".") if x and x not in
             ("api", "www", "com", "cn", "net", "org")]
    # 取**最长**的那一段当名字：
    #   dashscope.aliyuncs.com → dashscope（而不是 aliyuncs）
    #   open.bigmodel.cn       → bigmodel（而不是 open）
    # 域名里品牌名一般是最长的一段；而 "open" / "api" / "gateway"
    # 这类前缀很通用，拿它当名字会冒出一堆叫 open 的接入点，认不出来。
    base = max(parts, key=len) if parts else "provider"
    base = re.sub(r"[^a-z0-9_-]", "", base.lower()) or "provider"
    key = base
    n = 2
    while key in taken:
        key = "%s%d" % (base, n)
        n += 1
    return key


def _derive_providers():
    """老文件（扁平结构）→ 在内存里推出接入点。

    【为什么不写成"迁移一次、直接改文件"】
    她的 qwen-plus 就躺在这个文件里，分类和大纲每天在用。
    迁移代码要是写坏一次，那两件事一起哑。所以这里只在内存里推、
    不改文件；只有她主动动过模型设置，才把新结构落盘。
    推导是纯函数，推错最坏就是"接入点名字难看"，不会影响 load_models()。
    """
    _, models = _read_raw()
    out, seen = [], {}
    for m in models:
        c = _clean(m)
        if not c:
            continue
        sig = (c["base_url"], c["api_key"])
        if not sig[0] and not sig[1]:
            continue                       # 地址和密钥都空，不配当接入点
        if sig in seen:
            continue
        key = _provider_key_from_url(c["base_url"], [p["key"] for p in out])
        seen[sig] = key
        out.append({"key": key,
                    "label": (c["base_url"].split("//")[-1].split("/")[0]
                              if c["base_url"] else key),
                    "base_url": c["base_url"],
                    "api_key": c["api_key"],
                    "note": "自动从已有的模型里认出来的"})
    return out


def load_providers():
    """读接入点清单。文件里没写这一节就从模型里推导。"""
    provs, _ = _read_raw()
    out = []
    for p in provs:
        c = _clean_provider(p)
        if c:
            out.append(c)
    return out if out else _derive_providers()


def save_providers(items):
    """只改接入点那一节，模型不动。"""
    _, models = _read_raw()
    pout, _ = _write_raw(items, models)
    return pout


def public_providers():
    """给界面看的接入点清单：密钥打码 + 标出能不能用 + 底下挂了几个模型。

    跟 public_models() 同一条规矩：真钥匙不出后端。
    """
    models = load_models()
    out = []
    for p in load_providers():
        d = dict(p)
        d["api_key_masked"] = mask_key(p.get("api_key"))
        d["has_key"] = bool((p.get("api_key") or "").strip())
        d.pop("api_key", None)
        d["model_count"] = len([m for m in models
                                if m.get("provider") == p["key"]])
        # 这个接入点现在有没有"因为地址+密钥相同而被认成它"的模型。
        # 老文件里模型没有 provider 字段，靠这个也能把归属算对。
        d["linked"] = len([m for m in models
                           if m.get("provider") == p["key"]
                           or (not m.get("provider")
                               and m.get("base_url") == p.get("base_url")
                               and m.get("api_key") == p.get("api_key"))])
        out.append(d)
    return out


def get_provider(key):
    key = (key or "").strip()
    for p in load_providers():
        if p["key"] == key:
            return p
    return None


def upsert_provider(item):
    """新增或改一条接入点。PATCH 语义跟 upsert_model 完全一致 ——
    包括 api_key 那条例外：空字符串 = "我没改密钥"，不是"清空"。

    【为什么要单独盯着 sent 这个集合】
    _clean_provider 会把没传的字段补成空串（它得保证返回的结构完整）。
    要是照着这份"补全过"的结果去覆盖，她只想改个名字，
    地址就会被空串抹掉 —— 而且不报错，只是那个接入点突然连不上了。
    所以这里按"请求里**真的出现过**哪些字段"来改。
    """
    c = _clean_provider(item)
    if not c:
        return None
    sent = set(item.keys()) if isinstance(item, dict) else set()
    provs = load_providers()
    for i, p in enumerate(provs):
        if p["key"] != c["key"]:
            continue
        for k, v in c.items():
            if k not in sent:
                continue                   # 没传 = 不改
            if k == "api_key" and not v:
                continue                   # 传了但是空 = 不改密钥（界面是打码版）
            # 地址同理：空串 = "我没改"，不是"把地址改成空"。
            # 接入点没有地址就等于废了，不存在"我要清空"这种意图。
            # 跟 upsert_model 保持同一条规矩 —— 两处口径不一样，坏的是数据。
            if k == "base_url" and not str(v or "").strip():
                continue
            provs[i][k] = v
        save_providers(provs)
        return provs[i]
    provs.append(c)
    save_providers(provs)
    return c


def clear_provider_key(key):
    """只把密钥抹掉，地址和名字留着。"""
    provs = load_providers()
    hit = None
    for p in provs:
        if p["key"] == (key or "").strip():
            p["api_key"] = ""
            hit = p
    if hit:
        save_providers(provs)
    return hit


def delete_provider(key):
    """删掉一个接入点。**挂在它下面的模型不跟着删** ——
    她只是不想再看见这个接入点，不代表那些模型不要了。
    那些模型会保留自己当前的地址和密钥（等于"就地独立"）。
    """
    key = (key or "").strip()
    provs = load_providers()
    keep = [p for p in provs if p["key"] != key]
    if len(keep) == len(provs):
        return False
    # 先把该接入点的地址/密钥写进它下面的模型里，再摘掉引用 ——
    # 顺序反了的话，模型会在一瞬间变成"没有地址也没有钥匙"的空壳。
    gone = [p for p in provs if p["key"] == key][0]
    models = load_models()
    for m in models:
        if m.get("provider") == key:
            if not m.get("base_url"):
                m["base_url"] = gone.get("base_url") or ""
            if not m.get("api_key"):
                m["api_key"] = gone.get("api_key") or ""
            m["provider"] = ""
    save_providers(keep)
    save_models(models)
    return True


def _looks_like_webpage(raw):
    """这段返回看着像网页而不是接口数据吗？

    用来把「地址少写了 /v1」这个坑单独认出来。
    为什么值得单认：她填中转站时最容易只填网站域名（https://xxx.shop），
    那样请求打到首页上，回来的是一整页 HTML —— 界面上的原话是
    「返回的不是 JSON，前 200 字：<!doctype html>…」，她看了只会
    以为"这家不支持"，然后去别处折腾，而问题其实就差一个 /v1。
    """
    s = (raw or "").lstrip()[:300].lower()
    return (s.startswith("<!doctype") or s.startswith("<html")
            or "<head>" in s[:300] or "<body" in s[:300])


def _looks_like_gate_block(err, detail):
    """这个 401/403 是不是 CDN / 防火墙挡下来的，而不是服务商在说密钥不对？

    【为什么必须单独认这一种】2026-09-26 她报的坑就是它。
    被 Cloudflare 之类挡下来时回的是 403，跟"密钥不对"同一个号码。
    如果笼统说成「密钥不认」，她会拿着一个好端端的 Key 反复折腾 ——
    因为真正的原因（请求被挡在门外）在 Key 上根本查不出来。
    实测：同一条 Key、同一个地址，CC Switch 里能用、程序里 403，
    差别只在请求自报的身份（Cloudflare Error 1010 按身份拦人）。

    【判据只能看正文，绝不能看响应头】
    走 Cloudflare 的站，**每一个**响应都带 `Server: cloudflare` 和
    `CF-RAY` —— 包括它正常转发的 401。拿响应头当判据的话，会把她
    一条好密钥也误判成"被拦"，那就从一种误导换成另一种误导。
    真正的区别在正文：被拦时回来的是 Cloudflare 的错误页
    （带 developers.cloudflare.com 链接 / Error 1010 / Just a moment），
    而服务商正常的拒绝是它自己格式的 JSON。
    """
    blob = (detail or "").lower()
    return ("developers.cloudflare.com" in blob
            or "cloudflare-1xxx-errors" in blob
            or "error 1010" in blob
            or "error 1020" in blob
            or "just a moment" in blob
            or "attention required" in blob
            or "<!doctype html" in blob)


def _gate_block_error(label, code, base, detail):
    """被网关拦下来时给她的报错 —— 要点是**先把责任摘清楚**：
    这不是 Key 的问题，别去折腾密钥。"""
    return LlmError(
        "「%s」的请求被它前面的 CDN 或防火墙挡下来了（HTTP %d）——\n"
        "还没轮到检查密钥，所以这不是 Key 的问题。\n"
        "多出现在中转站把接口挂在 Cloudflare 后面、又开了「防机器人」"
        "规则的时候：它会按请求自报的身份拦人。\n"
        "判断办法：如果同一个 Key 在别的客户端里能用（浏览器插件、"
        "Claude 客户端之类），那就是这一条 —— 那类客户端自报的身份正常。\n"
        "当前地址：%s\n上游原话：%s" % (label, code, base, detail),
        status=code)


def _fetch_models_from(base_url, api_key, timeout=30):
    """拿一份「地址 + 密钥」去问它有哪些模型（OpenAI 兼容的 GET /models）。

    【为什么在服务端拉，不让她浏览器直接拉】
    两个理由，缺一不可：
      · 密钥：浏览器里发请求得先把 Key 交给前端 —— 那等于公开
        （截图、开发者工具、缓存都会留痕）。这条规矩整个项目都在守。
      · 跨域：各家服务商不会为我们的网页开 CORS，浏览器直拉必被拦。
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise LlmError("还没填 API 地址 —— 填上再拉模型列表。")
    if not (api_key or "").strip():
        raise LlmError("这一行还没有密钥 —— 先填上 Key，再拉模型列表。")

    req = urllib.request.Request(base + "/models", headers={
        "Authorization": "Bearer " + api_key,
        "Accept": "application/json",
        # 见文件上方 USER_AGENT 的说明：不带这一行，挂 Cloudflare
        # 防机器人规则的中转站会把请求整条拦掉（403），与密钥无关。
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:                                  # pragma: no cover
            pass
        if e.code in (401, 403):
            detail = _scrub(detail, api_key)
            # 先摘清楚：是"被网关挡了"还是"密钥不对"。两者都是 403，
            # 但给她的下一步完全不同。见 _looks_like_gate_block 的说明。
            if _looks_like_gate_block(e, detail):
                raise _gate_block_error("这个地址", e.code, base, detail)
            raise LlmError("这个地址不认这个密钥（HTTP %s）。"
                           "检查一下 Key 有没有复制全。" % e.code)
        raise LlmError("拉模型列表失败（HTTP %s）：%s"
                       % (e.code, detail or "服务商没给原因"))
    except Exception as e:
        raise LlmError("连不上这个地址：%s" % _scrub(e, api_key))

    try:
        d = json.loads(raw)
    except Exception:
        if _looks_like_webpage(raw):
            raise LlmError(
                "这个地址返回的是一整个网页，不是接口数据。\n"
                "多半是「API 地址」少写了 /v1 —— 中转站尤其容易这样：\n"
                "  现在填的：%s\n"
                "  应该填成：%s/v1\n"
                "（也检查一下是不是把控制台首页地址粘过来了。）" % (base, base))
        raise LlmError("服务商返回的不是 JSON，前 200 字：%s"
                       % _scrub(raw[:200], api_key))

    rows = d.get("data") if isinstance(d, dict) else d
    if not isinstance(rows, list):
        raise LlmError("返回里没有模型列表（应该是一个 data 数组）—— "
                       "有些服务商不提供这个接口，那就只好手填模型名。")
    out = []
    for x in rows:
        if isinstance(x, dict):
            mid = str(x.get("id") or "").strip()
            if mid:
                out.append({"id": mid, "owned_by": str(x.get("owned_by") or "")})
        elif isinstance(x, str) and x.strip():
            out.append({"id": x.strip(), "owned_by": ""})
    return out


def fetch_remote_models(provider_key, timeout=30):
    """按接入点拉它的模型列表。"""
    p = get_provider(provider_key)
    if not p:
        raise LlmError("没有「%s」这个接入点。" % (provider_key or ""))
    return _fetch_models_from(p.get("base_url"), p.get("api_key"), timeout)


def _key_for_base(base_url):
    """这个地址在清单里有没有钥匙？有就借来用。

    【为什么需要】
    她填一个新中转站时，地址和 Key 是刚敲进输入框的、还没保存 ——
    这时候"模型列表"正是最需要的东西（不然不知道模型名叫什么）。
    但空输入框回传不了密钥（界面上显示的是打码版），
    所以往回找一层：同名地址的接入点、或者某条模型存过的。
    找不到也没关系，调用方会给出"先去填 Key"的提示。
    """
    want = (base_url or "").strip().rstrip("/").lower()
    if not want:
        return ""
    for p in load_providers():
        if (p.get("base_url") or "").strip().rstrip("/").lower() == want:
            k = (p.get("api_key") or "").strip()
            if k:
                return k
    for m in load_models():
        if (m.get("base_url") or "").strip().rstrip("/").lower() == want:
            k = (m.get("api_key") or "").strip()
            if k:
                return k
    return ""


def fetch_models_for_cfg(cfg, timeout=30):
    """按一份**还没保存**的地址/密钥拉模型列表。

    存在的理由就是那场死锁：
      不知道模型名 → 想拉列表 → 拉列表要先有接入点 →
      接入点靠"保存模型"时才顺便认领 → 模型名填不对就测不过、她不敢保存。
    破法就是允许拿输入框里当下的值直接拉。
    """
    base = (cfg.get("base_url") or "").strip()
    key = (cfg.get("api_key") or "").strip()
    if not key:
        key = _key_for_base(base)
    return _fetch_models_from(base, key, timeout)


def add_models_from_provider(provider_key, codes):
    """把从这个接入点勾来的模型批量加进清单。

    返回 (加了几条, 跳过了几条)。跳过的原因只有一种：清单里已经有同名 model。
    为什么要按 model 去重、而不是按 key：她要的是"同一个模型别出现两次"，
    而 key 是她能改的显示名（改个名字再勾一次，不该变成两条）。
    """
    p = get_provider(provider_key)
    if not p:
        raise LlmError("没有「%s」这个接入点。" % (provider_key or ""))
    models = load_models()
    have_model = {(m.get("model") or "").strip() for m in models}
    used_keys = {m["key"] for m in models}

    added = skipped = 0
    for code in codes or []:
        code = str(code or "").strip()
        if not code or code in have_model:
            skipped += 1
            continue
        # key 直接就用模型名（qwen3.7-plus 这种）—— 它本来就是全站引用名，
        # 而且她一眼能认出来。撞名了才加序号。
        key, n = code, 2
        while key in used_keys:
            key = "%s-%d" % (code, n)
            n += 1
        used_keys.add(key)
        have_model.add(code)
        models.append({
            "key": key,
            "label": code,
            "provider": p["key"],
            "model": code,
            "base_url": "",            # 留空 = 跟着接入点走
            "api_key": "",             # 同上（这正是"统一"的意义）
            "enabled": True,
            "note": "从「%s」拉进来的" % p["label"],
        })
        added += 1
    if added:
        save_models(models)
    return added, skipped


def load_models(create=True):
    """读清单。文件不存在就生成一份默认的（不含密钥）。

    【为什么不把清单写死在代码里】
    因为密钥得有个地方放，而代码要进公开仓库。
    清单文件放 data/（不同步），代码里只留默认模板。

    【接入点回填】
    模型自己没填地址/密钥时，用它的接入点（provider）那两条填上。
    这就是"填一次 Key，下面所有模型都能用"的实现处 ——
    也是老配置一字不改还能跑的原因：老条目自己带着地址和密钥，
    回填这一步不会碰它们。
    """
    provs, raw_models = _read_raw()

    if not raw_models and not provs:
        if not create:
            return []
        items = [_clean(m) for m in DEFAULT_MODELS]
        items = [m for m in items if m]
        save_models(items)
        return items

    by_key = {}
    for x in provs:
        c = _clean_provider(x)
        if c:
            by_key[c["key"]] = c

    out = []
    for it in raw_models:
        c = _clean(it)
        if not c:
            continue
        p = by_key.get(c.get("provider") or "")
        if p:
            if not c["base_url"]:
                c["base_url"] = p["base_url"]
            if not c["api_key"]:
                c["api_key"] = p["api_key"]
            if not c["label"] or c["label"] == c["key"]:
                c["label"] = c["model"] or c["key"]
        out.append(c)
    return out


def save_models(items):
    """把模型清单写回去（接入点那一节原样保留）。

    【为什么这里要读一遍再写】
    见 _read_raw 的说明：直接覆盖整个文件会把她的接入点连密钥一起抹掉。
    """
    provs, _ = _read_raw()
    _, mout = _write_raw(provs, items)
    return mout


def public_models():
    """给界面的清单：密钥打码，并标出"这个能不能用"。

    【为什么不能直接把 load_models() 丢给前端】
    那样密钥就跟着 JSON 跑到浏览器里了。浏览器里出现过的东西，
    本质上等于公开 —— 截图、开发者工具、缓存都会留痕。
    """
    out = []
    for m in load_models():
        d = dict(m)
        d["api_key_masked"] = mask_key(m.get("api_key"))
        d["has_key"] = bool((m.get("api_key") or "").strip())
        d.pop("api_key", None)
        out.append(d)
    return out


def get_model(key):
    """按 key 拿一条。找不到返回 None。"""
    key = (key or "").strip()
    for m in load_models():
        if m["key"] == key:
            return m
    return None


def merge_model_cfg(want):
    """把「界面上刚填的」和「已经存过的」合成一份能用的配置。

    【为什么必须合】
    界面上的密钥框，她没改的时候回传的是空值（打码版只当占位符显示，
    不是输入框的值）。只按请求里传的字段走，一条存好的模型会变成
    "没填密钥"—— 测试和拉模型列表都会莫名其妙地失败。

    PATCH 语义在这儿的落点：请求里没出现的字段用已存的兜底；
    出现了但是空串的，也当"没改"处理 —— 空串在界面上没法表达
    "我要清空"（清空有专门的开关，见 ModelIn.clear_api_key）。
    """
    w = want or {}
    base = get_model(w.get("key")) or {}
    cfg = {}
    for f in ("key", "label", "base_url", "model", "api_key", "note"):
        v = (w.get(f) or "").strip()
        cfg[f] = v or (base.get(f) or "")
    return cfg


def usable_models():
    """能真正拿去用的：没被停用 + 填了密钥。"""
    return [m for m in load_models()
            if m.get("enabled") and (m.get("api_key") or "").strip()]


def upsert_model(item):
    """新增或改一条。按 key 认。

    【改一条时用 PATCH 语义，不是整体替换】
    分清两件事：
        「这个字段在不在这次传来的 JSON 里」 → 决定改不改
        「这个字段的值是不是空」            → 决定改成什么
    为什么必须分开：
        界面上"只填个 Key"是很常见的操作，那种情况她不会把中文名、
        请求地址一起回传；如果按"没传 = 用默认值"处理，
        一条叫「通义千问 Plus」的配置会被改名成「qwen-plus」——
        等于她只是填了个钥匙，名字就被吃掉了。
        这类 bug 不会报错，只是名字悄悄变了，最难发现。
    唯一的例外是 api_key：
        界面上显示的是打码版（sk-fak****test），回传时是空字符串。
        空 = "我没改密钥"，不能真当成"把密钥清空"。
        真要撤掉密钥，走 clear_api_key()。
    """
    c = _clean(item)
    if not c:
        raise LlmError("这条模型配置没有 key（内部短名字），没法保存")
    given = {f for f in _ALLOWED_FIELDS if isinstance(item, dict) and f in item}
    items = load_models()
    for i, m in enumerate(items):
        if m["key"] == c["key"]:
            merged = dict(m)
            for f in given:
                if f == "api_key" and not str(c.get("api_key") or "").strip():
                    continue                  # 打码版回传 → 不动密钥
                # ---- 地址和模型名也一样：空串 = "我没改"，不是"改成空" ----
                #
                # 【为什么非拦不可】界面上地址那一栏空着只有一种含义：
                # "跟着接入点走"（老配置里每条模型自带地址，前端会把它显示成
                # 跟着接入点，输入框是空的）。这时候请求里带一个空串过来，
                # 照字面执行就会把库里那条好好的地址**改成空** ——
                # 而 providers 那一节在老配置里是空的，等于地址彻底丢了，
                # 表现是"点了一下保存，这条模型就再也发不出去请求了"。
                # 2026-09-27 实测复现过。
                #
                # 反过来说"我要清空地址"这种意图不存在：没有地址的模型
                # 根本用不了。所以这两栏跟 api_key 同一条规矩。
                # （merge_model_cfg 早就写着"出现了但是空串的，也当没改处理"，
                #   只有这里没跟上 —— 两处口径不一致，坏的是数据。）
                if f in ("base_url", "model") and not str(c.get(f) or "").strip():
                    continue
                merged[f] = c.get(f)
            merged["key"] = c["key"]          # 它同时是查找键，永远以这次为准
            items[i] = _clean(merged) or c
            save_models(items)
            return items[i]
    items.append(c)
    save_models(items)
    return c


def clear_api_key(key):
    """把一条的密钥抹掉，其余字段原样留着。

    【为什么单开一个函数，而不是"保存时传个空字符串"】
    空字符串在保存那一步有专门含义 —— "我没改密钥"
    （因为界面上显示的是打码版，回传的是空）。
    两种意图共用同一个值，就必然分不清她到底想干什么。
    所以"我要删掉密钥"必须有自己的一条路。

    【为什么不干脆把整条删掉重加】
    删了的话，地址、模型名、备注这些她调过的东西也一起没了。
    而且这条还挂着历史任务记录里的引用。抹掉钥匙、留住配置，才是她要的。
    """
    key = (key or "").strip()
    if not key:
        raise LlmError("没说是哪一条")
    items = load_models()
    hit = False
    for m in items:
        if m["key"] == key:
            m["api_key"] = ""
            hit = True
    if not hit:
        raise LlmError("清单里没有「%s」这一条。" % key)
    save_models(items)
    return True


def add_model(item):
    """新增一条。key 已经存在就报错，不悄悄覆盖。

    为什么不走 upsert：
        "添加"和"修改"在她那侧是两个不同的按钮。
        在【添加】里撞上一个已有的名字，正确反应是告诉她"这个名字有了"，
        而不是默默把人家原来那条配置盖掉 —— 那等于删数据。
    """
    c = _clean(item)
    if not c:
        raise LlmError("新条目没有内部短名字（key）")
    items = load_models()
    if any(m["key"] == c["key"] for m in items):
        raise LlmError("已经有一条叫「%s」的了。换个名字，"
                       "或者直接改那一条。" % c["key"])
    items.append(c)
    save_models(items)
    return c


# ----------------------------------------------------------------------
# 调用
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# 这次生成是怎么结束的（finish_reason）
#
# 【为什么必须留它】OpenAI 兼容接口会在 choices[0].finish_reason 里回一个词，
# 说清这次是"说完了"还是"被掐断的"：
#     stop            正常说完了，可以当完整结果用
#     length          撞到 max_tokens 上限被硬掐断，**内容只有一半**
#     content_filter  被内容策略拦下
#     tool_calls      模型要调工具（我们不接工具，出现就是异常）
#
# 不留它的后果是**静默的**：被掐断的大纲看起来和完整的一模一样 ——
# 模型照旧填着"预计 3000 字"（那是它计划要写的），只有正文末尾缺了半句，
# 而界面上正好看不到末尾。她 2026-09-26 截图那次"看着正常、其实没写完"
# 就踩在这里。所以这个字段一路要留到界面上，不能在中途解包时丢掉。
# ----------------------------------------------------------------------

FINISH_STOP = "stop"
FINISH_LENGTH = "length"
FINISH_FILTER = "content_filter"

# 我们自己的哨兵值：确实没拿到结束原因（补列之前的老数据、或上游没回这个字段）。
# 【为什么用一个真实的值、而不是留空】"空"没法跟"这一列还没补过"区分开 ——
# 老库补列后每一行都是空，回填逻辑就会把它们一遍遍重算，永远补不完。
# 写成一个明确的值，回填才有个"补过了"的凭据。
FINISH_UNKNOWN = "unknown"

FINISH_LABELS = {
    FINISH_STOP: "正常写完",
    FINISH_LENGTH: "被字数上限掐断",
    FINISH_FILTER: "被内容策略拦下",
    FINISH_UNKNOWN: "当时没记结束原因",
}


def finish_label(reason):
    """把 finish_reason 翻成一句人能看懂的话。

    【为什么空值要单独说】2026-09-26 之前的任务没记这个字段，
    空字符串必须说成"当时没记"，绝不能含混成"正常写完"——
    那等于把"不知道"当成"没问题"，正好是这次要修的病。
    """
    r = (reason or "").strip()
    if not r:
        return "当时没记结束原因"
    return FINISH_LABELS.get(r, "结束了（%s）" % r)


def _pick_content(raw, key, label="", base=""):
    """从返回的 JSON 里挑出模型说的话，**连同这次是怎么结束的**。

    为什么单独写：不同家的返回结构大同小异但细节有差
    （有的 content 是 None + reasoning_content，有的 choices 可能为空），
    挑不出来的时候要给一句人能看懂的话，而不是 KeyError。

    label / base 只是给报错用的（"哪一条、打的哪个地址"）。不传也行，
    那种时候退回笼统的说法 —— 测试里就是这么调的。

    返回 (正文, 用量, finish_reason)。第三个值见上面常量区的说明 ——
    它决定"这篇是不是写完了"，不能丢。
    """
    try:
        d = json.loads(raw)
    except Exception:
        if _looks_like_webpage(raw):
            # 这一条她真会碰到：中转站地址只填了域名、漏了 /v1，
            # 请求打到首页上，回来的是一整页 HTML。
            raise LlmError(
                "%s这个地址返回的是一整个网页，不是接口数据。\n"
                "多半是「API 地址」少写了 /v1 —— 中转站尤其容易这样：\n"
                "  现在填的：%s\n"
                "  应该填成：%s/v1\n"
                "（也检查一下是不是把控制台首页地址粘过来了。）"
                % (("「%s」的 " % label) if label else "", base, base))
        raise LlmError("模型返回的不是 JSON，前 200 字：%s"
                       % _scrub(raw[:200], key))

    choices = d.get("choices") or []
    if not choices:
        msg = d.get("error") or d.get("message") or d
        raise LlmError("模型没返回内容：%s" % _scrub(json.dumps(
            msg, ensure_ascii=False)[:300], key))

    msg = choices[0].get("message") or {}
    text = msg.get("content")
    if text is None:
        text = msg.get("reasoning_content") or ""
    if not isinstance(text, str) or not text.strip():
        raise LlmError("模型返回了空内容")
    return text, d.get("usage") or {}, (choices[0].get("finish_reason") or "").strip()


# ----------------------------------------------------------------------
# 流式收（stream）
#
# 【为什么非要用它，不只是为了"打字机效果"】
# 一次性收的请求，程序只知道"整段到齐了"，**量不出第一个字是第几秒到的**。
# 而"首字慢"和"总耗时慢"是两种完全不同的毛病：
#     首字慢   = 这家在"想"，或者上游在排队（推理模型尤其慢）
#     总耗时慢 = 它写得太长，或者中途卡住了
# 她想在模型设置页分清这两件事，就只能按流式收。打字机效果是顺带的。
#
# 【两种"不支持流式"，本质不同，都要兜住】
#   ① 服务商直接拒绝（400，正文里写着 stream unsupported）——
#      认得出，换非流式把这次重发一遍就行，不该把整批任务判成失败。
#   ② 服务商**假装没看见** stream，照样回一整份 JSON。
# ②更阴：它不报错，你按 SSE 去解就一个字也解不出来，最后表现成
# 「模型返回了空内容」—— 那个报错把方向完全指错了，
# 她会去查模型名、查 Key，而问题只是这家不认这个参数。
# 所以这里**不靠"要不要"决定怎么解，靠 Content-Type**：
# 带 event-stream 才逐行按 SSE 解，否则老老实实把整份读完按普通返回解，
# 并记下"这家忽略了流式"，让界面上能说出来。
# ----------------------------------------------------------------------

DONE_SENTINEL = "[DONE]"
_BIG_LINE = 4 * 1024 * 1024      # 一行 4MB 还不见换行，那就不是 SSE
_DONE = object()                 # 内部哨兵：这一帧是 [DONE]
_EOF_FRAME = object()            # 内部哨兵：这是一行空行（= 一帧结束）
_END = object()                  # 内部哨兵：chat_stream 的队列到底了


def _sse_data(line):
    """从 SSE 的一行里读出它的意思。

    返回四种之一：
        _DONE       这一帧是 [DONE] → 流到此为止
        _EOF_FRAME  这是一行空行 → 一帧结束
        None        这行没用（注释心跳、event:、id: …）
        字符串      一行数据（同一帧里多行要用换行拼起来才是完整内容）
    """
    s = (line or "").strip()
    if not s:
        return _EOF_FRAME
    if s.startswith(":"):
        return None                  # 注释行：有些服务商拿它当心跳
    if not s.startswith("data:"):
        return None                  # event: / id: / retry: 这几个我们用不上
    d = s[5:].strip()
    if d == DONE_SENTINEL:
        return _DONE
    return d or None


def _iter_sse(resp):
    """把一个 SSE 响应**逐帧**读出来：读满一帧就 yield 一帧，不许攒。

    一帧 = 若干行 data:（用换行拼起来）+ 一个空行。

    【为什么不用 resp.read()】
    read() 不带参数 = 一直读到连接关闭 —— 那正是"攒完整段再给你"的行为，
    等于把流式又做成了非流式，前面所有的功夫全白费。

    【为什么要按"帧"而不是按"行"】
    SSE 允许一帧里的数据拆成好几行 data:（合起来才是完整的 JSON）。
    按行独立解析的话，那几行都解不出 JSON，于是一整块内容被**静默丢掉**：
    不报错、不报警，就是少了一截。而"少了一截"正是这个项目里最不能忍的事。

    【为什么要检查 resp.length】
    声明了 Content-Length 却读不满，只有一种可能：连接被掐断在半路。
    这时候 read(1) 只是安静地返回空字节，**不会**抛异常 ——
    不检查的话，半截内容会被当成"完整收到了"。查一下这三个字节的代价，
    换的是"不会拿半截数据当成品"。
    """
    buf = b""
    data_lines = []
    while True:
        b = resp.read(1)             # 读 1 个字节就返回，绝不多等
        if not b:
            break
        if b == b"\n":
            got = _sse_data(buf.decode("utf-8", "replace"))
            buf = b""
            if got is None:
                continue
            if got is _DONE:
                return
            if got is not _EOF_FRAME:      # 空行 = 一帧结束
                data_lines.append(got)
                continue
            if data_lines:
                joined = "\n".join(data_lines)
                data_lines = []
                if joined.strip():
                    yield joined
            continue
        buf += b
        if len(buf) > _BIG_LINE:
            raise LlmError("流式返回的样子不像 SSE（一行超过 4MB 还没结束）。")

    if getattr(resp, "length", 0):
        raise LlmError("流式连接在收完之前断了（还差 %d 字节没收到）。"
                       % resp.length)

    # 有的服务商最后那帧不发空行，直接关连接 —— 别把最后一块丢了。
    got = _sse_data(buf.decode("utf-8", "replace")) if buf else None
    if got is not None and got is not _DONE and got is not _EOF_FRAME:
        data_lines.append(got)
    if data_lines:
        joined = "\n".join(data_lines)
        if joined.strip():
            yield joined


def _pick_delta(payload):
    """从一个流式数据块里挑出**这一块新增的字**。

    返回 (新增文本, usage, finish_reason)。

    【注意 finish_reason 常常在"内容为空"的那一块里】
    只挑有字的块会把它漏掉 —— 而它是"这一篇到底是被正常写完、
    还是撞到字数上限被掐断"的唯一硬证据（见上面常量区的说明）。
    """
    try:
        d = json.loads(payload)
    except Exception:
        return "", {}, ""
    if not isinstance(d, dict):
        return "", {}, ""
    if d.get("error"):
        # 有的服务商 HTTP 回 200，错却写在流里。不认它的话，
        # 她会看到"返回了空内容"这种指错方向的报错。
        raise LlmError("上游在流里回了错：%s"
                       % json.dumps(d.get("error"), ensure_ascii=False)[:300])
    choices = d.get("choices") or []
    text, finish = "", ""
    if choices and isinstance(choices[0], dict):
        ch = choices[0]
        delta = ch.get("delta")
        if not isinstance(delta, dict):
            delta = {}
        text = delta.get("content")
        if text is None:
            # 有些实现不填 content 而用 reasoning_content（推理模型的思考过程）
            text = delta.get("reasoning_content") or ""
        if not isinstance(text, str):
            text = ""
        finish = (ch.get("finish_reason") or "").strip()
    usage = d.get("usage")
    return text, (usage if isinstance(usage, dict) else {}), finish


# 报错里出现这些词，才认"_stream_not_supported"。
# 要求同时命中 "stream"，是为了不把别的 400（比如模型名不对）误认成它 ——
# 误认的代价是多发一次请求，漏认的代价是整批任务失败。两害相权，这里偏向前者。
_STREAM_MAYBE = (400, 404, 405, 415, 422, 500, 501)


def _stream_not_supported(code, detail):
    """这个报错是不是"这家不认 stream"？

    判据只看**正文**，不看响应头 —— 跟 _looks_like_gate_block 同一条规矩。
    """
    if code not in _STREAM_MAYBE:
        return False
    return "stream" in (detail or "").lower()


def _build_req(url, body, key, use_stream):
    """拼一次请求。stream 是写在**请求体**里的，不是请求头。"""
    b = dict(body)
    if use_stream:
        b["stream"] = True
    payload = json.dumps(b, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + key)
    # 见文件上方 USER_AGENT：不带这一行会被 Cloudflare 防机器人规则拦掉。
    req.add_header("User-Agent", USER_AGENT)
    if use_stream:
        req.add_header("Accept", "text/event-stream")
    return req


def _read_stream(resp, t0, key, on_chunk, model):
    """逐块读 SSE，**每读到一块就立刻交出去**。"""
    parts, usage, finish = [], {}, ""
    ttft = 0
    broke = None
    try:
        for data in _iter_sse(resp):
            text, u, f = _pick_delta(data)
            if u:
                usage = u
            if f:
                finish = f
            if not text:
                continue             # 只有结束原因、没有字的块，不算"第一个字"
            if not ttft:
                # 首字耗时。max(1, ...) 是为了别把"立刻就来了"记成 0 ——
                # 0 在我们这儿的意思是"这次没量到"，两者不能混。
                ttft = max(1, int((time.time() - t0) * 1000))
            parts.append(text)
            if on_chunk:
                try:
                    on_chunk(text)
                except BaseException:
                    # 看板出问题，绝不许把这次生成搞挂 —— 字都已经收到了。
                    pass
    except LlmError as e:
        if not "".join(parts).strip():
            raise                    # 一个字都没有 + 已经是我们自己的干净报错
        broke = e
    except Exception as e:
        broke = e

    content = "".join(parts)
    elapsed = int((time.time() - t0) * 1000)
    if broke is not None:
        if content.strip():
            # 【为什么收到一半也要算失败】断在半路的内容结构一定不完整
            # （JSON 缺个括号就是废的），拿它去解析只会得到一句让人摸不着
            # 头脑的"不是合法 JSON"。不如直说断了，让上层重试。
            raise LlmError(
                "流式收到一半断了：已经收到 %d 字，但连接中途断掉，"
                "这半截没法用（结构不完整）。\n原因：%s"
                % (len(content), _scrub(broke, key)), retryable=True)
        raise LlmError("流式连接断了，一个字都没收到：%s"
                       % _scrub(broke, key), retryable=True)
    if not content.strip():
        raise LlmError("模型返回了空内容")
    return {"content": content, "usage": usage, "model": model,
            "finish_reason": finish, "ttft_ms": ttft,
            "elapsed_ms": elapsed, "streamed": True, "fallback": ""}


def _send(req, use_stream, key, label, base, model, timeout, on_chunk):
    """把请求发出去、把返回收下来。**一次请求，不含重试。**

    返回 {content, usage, model, finish_reason, ttft_ms, elapsed_ms,
          streamed, fallback}。

    失败一律抛出去：urllib 那三种（HTTPError / URLError / TimeoutError）
    原样往上抛，好让 chat() 里那套报错文案继续管用；
    LlmError 则已经抹过密钥，可以直接给她看。
    """
    t0 = time.time()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        with resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if use_stream and "event-stream" in ctype:
                return _read_stream(resp, t0, key, on_chunk, model)
            # 走到这儿只有两种可能：
            #   ① 本来就没要流式
            #   ② 这家**假装没看见** stream，照样回了一整份 JSON（见文件上方说明）
            raw = resp.read().decode("utf-8", "replace")
        text, usage, finish = _pick_content(raw, key, label, base)
        return {"content": text, "usage": usage, "model": model,
                "finish_reason": finish, "ttft_ms": 0,
                "elapsed_ms": int((time.time() - t0) * 1000),
                "streamed": False,
                "fallback": "server_ignored_stream" if use_stream else ""}
    except BaseException as e:
        # 把"这一次等了多少"挂在错误上 —— 失败也要记进耗时表。
        try:
            e.elapsed_ms = int((time.time() - t0) * 1000)
        except BaseException:
            pass
        raise


def _record_call(cfg, purpose, r=None, err=None, use_stream=False,
                 fallback=""):
    """把这一次的耗时记进 llm_calls。**成功和失败都记。**

    记的是"性能数据"，不是"业务数据"：任何异常都不许冒出去
    （db.record_llm_call 里面已经把异常吃掉了，这里再兜一层，
    是因为它还要读 cfg 里几个可能不存在的字段）。
    """
    try:
        if r is not None:
            db.record_llm_call(
                purpose=purpose,
                model_key=(cfg.get("key") or ""),
                model_name=(r.get("model") or cfg.get("model") or ""),
                provider=(cfg.get("provider") or ""),
                streamed=r.get("streamed"),
                ttft_ms=r.get("ttft_ms"),
                elapsed_ms=r.get("elapsed_ms"),
                ok=True,
                usage=r.get("usage"),
                chars=len(r.get("content") or ""),
                finish_reason=r.get("finish_reason"),
                fallback=(r.get("fallback") or fallback))
            return
        db.record_llm_call(
            purpose=purpose,
            model_key=(cfg.get("key") or ""),
            model_name=(cfg.get("model") or ""),
            provider=(cfg.get("provider") or ""),
            streamed=bool(use_stream),
            ttft_ms=0,
            elapsed_ms=getattr(err, "elapsed_ms", 0),
            ok=False,
            error=(getattr(err, "message", "") or str(err or "")),
            fallback=fallback)
    except BaseException:
        pass


def chat(cfg, messages, temperature=0.0, json_mode=False, timeout=None,
         max_retry=None, stream=True, on_chunk=None, purpose=""):
    """发一次对话请求。

    返回 {"content": 模型说的话, "usage": 用量, "model": 实际用的模型名,
          "finish_reason": 这次是怎么结束的}。
    finish_reason 的取值与含义见上面常量区 —— 短任务（分类、内化一次几百字）
    用不上它；生成大纲这种一次要吐几千字的长任务，必须靠它分辨
    "正常写完"和"撞到字数上限被掐断"。

    cfg 就是清单里的一条（含明文 api_key）。

    timeout 不传吃 TIMEOUT(180)，max_retry 不传吃 MAX_RETRY(3)。
    **一次要吐几千字的长任务（生成大纲）必须自己传**，理由见上面常量区的注释：
    默认值是按"短回答"定的，用在大纲上会稳定超时，还会把等待时间乘三倍。

    stream    默认 True（按流式收）。为什么默认就是它：非流式量不出首字耗时，
              而"首字慢"和"总耗时慢"是两种毛病。这家不认流式时会**自动**
              换非流式重发（结果里记 fallback），所以默认开着不会有坏处。
    on_chunk  每收到一块字就回调一次。任务线程靠它把字实时推到页面上。
              回调里抛异常**不会**影响这次生成（见 _read_stream）。
    purpose   这次调用是干什么用的（classify / infuse / outline / probe），
              只用来给耗时表分组，缺省也能跑。

    返回里比原来多三项：ttft_ms（首字耗时）、elapsed_ms（这次等了多久）、
    streamed（是不是流式收的）。非流式收时 ttft_ms = 0，
    意思是"这次没量到"，跟"首字 0 毫秒"不是一回事。
    """
    if not isinstance(cfg, dict):
        raise LlmError("模型配置不对（应该是一条字典）")

    key = (cfg.get("api_key") or "").strip()
    label = cfg.get("label") or cfg.get("key") or "这个模型"
    if not key:
        raise LlmError("「%s」还没填 API Key。去「模型设置」里填上再跑。" % label)

    base = (cfg.get("base_url") or "").strip().rstrip("/")
    model = (cfg.get("model") or "").strip()
    if not base or not model:
        raise LlmError("「%s」的地址或模型名是空的，去「模型设置」里补上。" % label)

    # 地址抄错时，报错必须说人话。
    # 为什么单独校验：她很可能把控制台主页地址（不带 /v1）粘进来，
    # 那样请求会打到 https://xxx.com/chat/completions 上，返回一个
    # 跟"接口"无关的 HTML 或 404 —— 而她的第一反应会是"Key 又不对了"。
    if not base.lower().startswith(("http://", "https://")):
        raise LlmError(
            "「%s」的 API 地址不像网址（现在填的是「%s」）。\n"
            "要填服务商文档里那个「接口地址」，一般以 /v1 结尾，"
            "比如 https://api.siliconflow.cn/v1\n"
            "注意不是控制台首页地址。" % (label, base))
    if base.endswith("/chat/completions"):
        raise LlmError(
            "「%s」的 API 地址填多了：只要填到 /v1 那一段就行，"
            "末尾的 /chat/completions 程序会自己接上。\n"
            "现在填的是「%s」" % (label, base))

    url = base + "/chat/completions"
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        # OpenAI 的"强制返回 JSON"。不是所有兼容实现都支持，
        # 真报 400 就由上层关掉这个开关重试（见 classification.py）。
        body["response_format"] = {"type": "json_object"}

    last = None

    # 这次按不按流式收。默认是 —— 非流式量不出首字耗时（见上面流式那一节）。
    # 这家明确拒绝流式时，下面会把它翻成 False 并把这一次重发一遍。
    use_stream = bool(stream) if stream is not None else True
    fallback = ""                    # 退回非流式的原因（'' = 没退）

    def _fatal(err, from_err=None):
        """非重试类的失败：先记进耗时表，再抛出去。

        【为什么单独留一个出口】400 / 401 / 404 这些是"请求本身不对"，
        再试一百次也是同样结果，所以立刻抛。但它们同样是"花过时间的一次
        尝试"，不能因为抛得早就漏记 —— 她拿一条填错的 Key 点「测试」，
        对比表里要是什么都不显示，她会以为"这里根本没记录"。

        from_err 传那个原始的 urllib 错误，用来把"等了多久"抄过来
        （等待时长是在 _send 里量好挂在错误上的）。
        """
        if not getattr(err, "elapsed_ms", 0):
            err.elapsed_ms = getattr(from_err, "elapsed_ms", 0)
        _record_call(cfg, purpose, err=err, use_stream=use_stream,
                     fallback=fallback)
        raise err

    tries = max(1, int(max_retry or MAX_RETRY))
    attempt = 0
    # 【为什么是 while 不是 for】"这家不认流式"要能把**同一次尝试**重发一遍，
    # 而重发不该算掉一次重试机会（它不是失败，是这条路走不通）。
    # while 才能把 attempt 退回去，让"第 N 次尝试"这个说法对得上。
    while attempt < tries:
        attempt += 1
        req = _build_req(url, body, key, use_stream)
        try:
            r = _send(req, use_stream=use_stream, key=key, label=label,
                      base=base, model=model, timeout=timeout or TIMEOUT,
                      on_chunk=on_chunk)
            r["fallback"] = r.get("fallback") or fallback
            _record_call(cfg, purpose, r=r)
            return r

        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            detail = _scrub(detail, key)

            # ---- 这家不认 stream？换成非流式把这一次重发一遍 ----
            # 【为什么单独认这一种】不认流式是**兼容性问题，不是故障**：
            # 换条路立刻就能成。要是不认它，她会看到整批分类全失败，
            # 而原因只是"这家不支持一个可选的参数"，报错还指不到这儿。
            if use_stream and _stream_not_supported(e.code, detail):
                use_stream = False
                fallback = "stream_rejected"
                attempt -= 1         # 抵消循环开头那次 +=1：重发不算重试
                continue

            if e.code in (401, 403):
                # 先认一下"被网关挡了"这种：它也是 403，但跟"密钥不对"
                # 完全是两回事，混在一起说会让她抱着好 Key 白折腾。
                if _looks_like_gate_block(e, detail):
                    _fatal(_gate_block_error(label, e.code, base, detail), e)
                # 这条报错是**真会被看到**的一条：填第三方 Key 是最常见的坑。
                # 她充了硅基流动的额度，却填进"DeepSeek 官方"那一条 ——
                # 官方当然说这个密钥没注册过（401）。如果只说"密钥不对"，
                # 她会在 Key 上反复折腾，而问题其实在地址那一栏。
                _fatal(LlmError(
                    "「%s」说密钥不认（HTTP %d）。三个常见原因，挨个看一眼：\n"
                    "  ① Key 抄错了（前后有没有多空格、有没有漏字符）\n"
                    "  ② 「Key 不是这家的」 —— 比如充的是第三方（硅基流动之类的）"
                    "或中转站的额度，却填进了官方那一条。"
                    "这种要去「模型设置」把「API 地址」也一起改成第三方的，"
                    "模型名通常也要改（如 deepseek-ai/DeepSeek-V3.2）\n"
                    "  ③ 这个 Key 没开通当前模型（现在填的是「%s」）\n"
                    "当前地址：%s\n上游原话：%s"
                    % (label, e.code, model, base, detail), status=e.code), e)

            if e.code == 404:
                # 中转站特有的大坑，单独写清楚：
                # 它们后台有一列叫「分组」或「套餐」，写着 GPT PLUS / default
                # 这种看着很像模型名的东西 —— 那是**计费分组**，不是模型名。
                # 她照抄过去，上游就回一句"没有这个模型"。
                _fatal(LlmError(
                    "「%s」说它没有这个模型（HTTP 404，模型名：%s）。\n"
                    "两个常见原因，对一下：\n"
                    "  ① 填的是「分组 / 套餐」名，不是模型名 —— "
                    "中转站后台那列写的 GPT PLUS、default 之类是计费分组，"
                    "模型名长这样：gpt-4o、gpt-4o-mini、claude-sonnet-4。\n"
                    "  ② 模型名过时了，服务商改版换过名字。\n"
                    "怎么办：点这一行旁边那个「拉模型列表」——"
                    "它会把这家真正认的名字列出来，点一个就填进模型名。\n"
                    "当前地址：%s\n上游原话：%s" % (label, model, base, detail),
                    status=e.code), e)

            if e.code == 429:
                # 中转站的 429 原话是"All available accounts are currently
                # rate-limited" —— 是**服务商自己的账号池**被限流了，
                # 跟她配的 Key 没关系。不说清这一点，她只会去反复折腾 Key。
                last = LlmError(
                    "「%s」说服务商那边被限流了（HTTP 429），第 %d 次尝试。\n"
                    "这是服务商账号池的事，不是你配错了 Key。\n"
                    "下一步：等几分钟再试；或者换一家模型。\n上游原话：%s"
                    % (label, attempt, detail), retryable=True, status=e.code)
            elif e.code >= 500:
                last = LlmError(
                    "「%s」的上游出错了（HTTP %d），第 %d 次尝试。\n"
                    "多半是这家服务当前不稳定。\n"
                    "下一步：等一会儿再试；或者换一家模型。\n上游原话：%s"
                    % (label, e.code, attempt, detail),
                    retryable=True, status=e.code)
            else:
                # 400 之类：请求本身有问题，再试也是一样的结果，直接抛
                _fatal(LlmError(
                    "「%s」拒绝了这次请求（HTTP %d）。\n上游原话：%s"
                    % (label, e.code, detail), status=e.code), e)

        except urllib.error.URLError as e:
            last = LlmError("连不上「%s」（%s）。检查网络，或者地址填错了。"
                            % (label, _scrub(e.reason, key)), retryable=True)

        except TimeoutError:
            # 【为什么要把话说这么细】这次是"中转站 600 秒一个字都没回"。
            # 只说"超过 N 秒没回话"，她不知道是自己的问题还是服务商的问题，
            # 下一次还会照着原样再跑一遍、再白等十分钟。
            last = LlmError(
                "「%s」超过 %d 秒没回话（上游一个字都没返回）。\n"
                "多半是这家服务当前不稳或被限流（中转站尤其常见），"
                "也可能是这个模型属于「要想很久」的推理模型，长任务容易等不到。\n"
                "下一步：换个模型再试一次（直连的服务通常更稳，比如通义千问）；"
                "这一次一个字都没拿到，等于白等，没有额外花销。"
                % (label, timeout or TIMEOUT), retryable=True)

        except LlmError as e:
            # 已经是干净的错（比如返回不是 JSON）。上游模型服务偶尔会
            # 吐一半就断，这种也值得再试一次。
            last = LlmError(e.message, retryable=True, status=e.status,
                            elapsed_ms=getattr(e, "elapsed_ms", 0))

        if attempt < tries:
            time.sleep(RETRY_BACKOFF ** attempt)

    if last is None:
        last = LlmError("「%s」试了 %d 次都没成功。" % (label, tries))
    elif tries > 1:
        # 她最终只在任务备注里看到最后这一条错误。不写明"试了几轮"，
        # 她会以为只发了一次请求 —— 而实际上可能已经扣了好几笔钱。
        last = LlmError("%s（一共试了 %d 次）" % (last.message, tries),
                        retryable=last.retryable, status=last.status,
                        elapsed_ms=getattr(last, "elapsed_ms", 0))
    # 失败也是花过时间的 —— 记进耗时表，别让对比表里只剩成功那几次。
    _record_call(cfg, purpose, err=last, use_stream=use_stream,
                 fallback=fallback)
    raise last


def chat_stream(cfg, messages, temperature=0.0, json_mode=False, timeout=None,
                max_retry=None, purpose=""):
    """流式版：一边收一边把增量吐出来。

        for ev in llm.chat_stream(cfg, msgs, purpose="probe"):
            if ev["t"] == "chunk":
                ...把 ev["text"] 画到页面上...

    事件只有三种：
        {"t": "chunk", "text": "…"}        模型刚吐出来的一块字
        {"t": "done",  "result": {...}}    跑完了。result 就是 chat() 的返回
        {"t": "error", "message": "…"}     没成。message 抹过密钥，可以直接给她看

    【为什么不重新写一遍重试逻辑】
    重试、退避、各种报错的文案、"不认流式就换一条路"—— 这些 chat() 里
    已经有一套，而且是调了很久才调准的。所以这里只是**换一种交付方式**：
    把 chat() 放到一个线程里跑，它每收到一块字就丢进队列，这边取出来往外吐。
    重新实现一遍等于把那些坑再踩一遍。
    """
    q = queue.Queue()

    def _worker():
        try:
            r = chat(cfg, messages, temperature=temperature,
                     json_mode=json_mode, timeout=timeout, max_retry=max_retry,
                     stream=True, on_chunk=lambda t: q.put({"t": "chunk",
                                                            "text": t}),
                     purpose=purpose)
            q.put({"t": "done", "result": r})
        except BaseException as e:
            q.put({"t": "error",
                   "message": (getattr(e, "message", None) or str(e)),
                   "retryable": bool(getattr(e, "retryable", False)),
                   "status": getattr(e, "status", None)})
        finally:
            q.put(_END)

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    while True:
        ev = q.get()
        if ev is _END:
            break
        yield ev


# ----------------------------------------------------------------------
# 自测：只测纯函数，不联网、不碰数据库
#
# 为什么不在这里跑真请求：这个文件被 import 的时候就会执行到这儿，
# 联网自测会让"启动服务"变成一件依赖外网的事。
# ----------------------------------------------------------------------

def _self_check():
    ok = True

    def check(name, got, want):
        nonlocal ok
        if got != want:
            ok = False
            print("  [x] %s：得到 %r，期望 %r" % (name, got, want))
        else:
            print("  [v] %s" % name)

    check("短密钥全打码", mask_key("abc"), "***")
    check("空密钥还是空", mask_key(""), "")
    check("长密钥留头留尾",
          mask_key("sk-1234567890abcdef"), "sk-123****cdef")
    check("刚好 12 位全打码", mask_key("123456789012"), "************")

    # 长任务靠 max_retry 砍掉重试（生成大纲传 1）。这两个是**按关键字传**的，
    # 名字被改掉会静默失效 —— 所以在这里钉一下。
    _params = inspect.signature(chat).parameters
    check("chat 支持 timeout 参数（长任务要自己传）",
          "timeout" in _params, True)
    check("chat 支持 max_retry 参数（长任务要能不重试）",
          "max_retry" in _params, True)

    # _scrub 是安全的关键：报错里的密钥必须被抹掉
    k = "sk-secret-abcdefghijklmn"
    scrubbed = _scrub("Authorization: Bearer " + k + " 失败了", k)
    check("报错里的密钥被抹掉", k in scrubbed, False)
    check("抹掉后还能看出是哪个 Key", "sk-sec****klmn" in scrubbed, True)
    check("没有密钥时原样返回", _scrub("普通错误", ""), "普通错误")

    # 洗配置
    c = _clean({"key": "  a  ", "label": "", "enabled": 0, "乱七八糟": 1})
    check("key 去空格", c["key"], "a")
    check("label 空了就用 key 顶上", c["label"], "a")
    check("不认识字段被丢掉", "乱七八糟" in c, False)
    check("enabled 转成布尔", c["enabled"], False)
    check("没 key 的条目被丢掉", _clean({"label": "x"}), None)

    # 默认清单本身要自洽
    keys = [m["key"] for m in DEFAULT_MODELS]
    check("默认清单 key 不重复", len(keys), len(set(keys)))
    check("默认清单一个密钥都没带",
          any((m.get("api_key") or "").strip() for m in DEFAULT_MODELS), False)
    check("默认清单都有地址和模型名",
          all(m["base_url"] and m["model"] for m in DEFAULT_MODELS), True)

    # ---- 改配置的语义（这一段会写 models.json）------------------------
    # 只在明确隔离到临时目录时才跑：免得哪天真在项目目录下手滑跑一次自测，
    # 把她的模型清单给改了。环境变量没设就跳过，不冒这个险。
    if os.environ.get("MOGE_DATA_DIR"):
        def _m():
            return get_model("qwen-plus") or {}

        upsert_model({"key": "qwen-plus", "api_key": "sk-secret-abcdefg"})
        check("只填 Key 也能存上", bool(_m().get("api_key")), True)
        check("只填 Key 不会把中文名吃掉", _m().get("label"),
              "通义千问 Plus（阿里云百炼）")
        check("只填 Key 不会把请求地址清空",
              _m().get("base_url"),
              "https://dashscope.aliyuncs.com/compatible-mode/v1")

        upsert_model({"key": "qwen-plus", "api_key": ""})
        check("界面回传的空密钥 = 不改密钥",
              _m().get("api_key"), "sk-secret-abcdefg")

        upsert_model({"key": "qwen-plus", "note": "自己写的备注"})
        check("只改备注时，密钥和名字都还在",
              (bool(_m().get("api_key")), _m().get("label"),
               _m().get("note")),
              (True, "通义千问 Plus（阿里云百炼）", "自己写的备注"))

        # ---- 换地址 / 换模型名（接第三方和中转站靠的就是这个）----
        upsert_model({"key": "qwen-plus",
                      "base_url": "https://api.siliconflow.cn/v1",
                      "model": "deepseek-ai/DeepSeek-V3.2"})
        check("能改 API 地址（第三方 / 中转站要用）",
              _m().get("base_url"), "https://api.siliconflow.cn/v1")
        check("能改模型名（第三方的名字带厂商前缀）",
              _m().get("model"), "deepseek-ai/DeepSeek-V3.2")
        check("只改地址不会把密钥弄丢",
              _m().get("api_key"), "sk-secret-abcdefg")
        # 改回去，别让后面看的人以为默认清单长这样
        upsert_model({"key": "qwen-plus",
                      "base_url": DEFAULT_MODELS[0]["base_url"],
                      "model": DEFAULT_MODELS[0]["model"]})

        # ---- 清空密钥：和"没改密钥"必须分得开 ----
        # 这两个意图如果共用一个空字符串，她就永远说不清"我到底想干嘛"。
        check("清空密钥前，这条是有钥匙的", bool(_m().get("api_key")), True)
        clear_api_key("qwen-plus")
        check("清空密钥之后钥匙没了", _m().get("api_key"), "")
        check("清空密钥不会动地址（配置要留着）",
              _m().get("base_url"),
              "https://dashscope.aliyuncs.com/compatible-mode/v1")
        check("清空密钥不会动中文名", _m().get("label"),
              "通义千问 Plus（阿里云百炼）")
        check("清空密钥之后它就不能用了",
              "qwen-plus" in [m["key"] for m in usable_models()], False)
        try:
            clear_api_key("根本没有这条")
            check("给不存在的条目清密钥要报错", False, True)
        except LlmError:
            check("给不存在的条目清密钥要报错", True, True)

        # ---- 新增一条（撞名不能覆盖）----
        add_model({"key": "relay", "label": "我的中转站",
                   "base_url": "https://relay.example.com/v1",
                   "model": "gpt-4o-mini", "api_key": "sk-relay-123"})
        check("能新增一条自定义的", bool(get_model("relay")), True)
        check("新增的那条能直接拿来用",
              "relay" in [m["key"] for m in usable_models()], True)
        try:
            add_model({"key": "relay", "label": "改个名"})
            check("撞名时新增要报错，不许覆盖", False, True)
        except LlmError:
            check("撞名时新增要报错，不许覆盖", True, True)
        check("撞名之后原来那条没被改掉",
              get_model("relay").get("label"), "我的中转站")

        # ---- 地址填错的报错要说人话 ----
        # 她这次踩的就是这个：把控制台地址或整串接口地址粘进来，
        # 然后以为是 Key 的问题，在 Key 上反复折腾。
        for bad, why in (("api.siliconflow.cn/v1", "少了 https://"),
                         ("https://api.deepseek.com/v1/chat/completions",
                          "多填了 /chat/completions"),
                         ("", "空地址")):
            try:
                chat({"key": "x", "label": "试", "base_url": bad,
                      "model": "m", "api_key": "sk-a"}, [])
                check("地址%s 要给说得清的错" % why, False, True)
            except LlmError as e:
                check("地址%s 要给说得清的错" % why,
                      "地址" in str(e), True)

        print("  （这段动了 models.json，但跑在临时目录：%s）"
              % os.environ.get("MOGE_DATA_DIR"))

    print()
    print("自测：%s" % ("全部通过" if ok else "有失败"))
    return ok


if __name__ == "__main__":                                   # pragma: no cover
    import sys
    sys.exit(0 if _self_check() else 1)
