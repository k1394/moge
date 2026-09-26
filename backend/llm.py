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
import re
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
    """

    def __init__(self, message, retryable=False, status=None):
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status = status


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


def chat(cfg, messages, temperature=0.0, json_mode=False, timeout=None,
         max_retry=None):
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

    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last = None

    tries = max(1, int(max_retry or MAX_RETRY))
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + key)
        try:
            with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", "replace")
            text, usage, finish = _pick_content(raw, key, label, base)
            return {"content": text, "usage": usage, "model": model,
                    "finish_reason": finish}

        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            detail = _scrub(detail, key)

            if e.code in (401, 403):
                # 这条报错是**真会被看到**的一条：填第三方 Key 是最常见的坑。
                # 她充了硅基流动的额度，却填进"DeepSeek 官方"那一条 ——
                # 官方当然说这个密钥没注册过（401）。如果只说"密钥不对"，
                # 她会在 Key 上反复折腾，而问题其实在地址那一栏。
                raise LlmError(
                    "「%s」说密钥不认（HTTP %d）。三个常见原因，挨个看一眼：\n"
                    "  ① Key 抄错了（前后有没有多空格、有没有漏字符）\n"
                    "  ② 「Key 不是这家的」 —— 比如充的是第三方（硅基流动之类的）"
                    "或中转站的额度，却填进了官方那一条。"
                    "这种要去「模型设置」把「API 地址」也一起改成第三方的，"
                    "模型名通常也要改（如 deepseek-ai/DeepSeek-V3.2）\n"
                    "  ③ 这个 Key 没开通当前模型（现在填的是「%s」）\n"
                    "当前地址：%s\n上游原话：%s"
                    % (label, e.code, model, base, detail), status=e.code)

            if e.code == 404:
                # 中转站特有的大坑，单独写清楚：
                # 它们后台有一列叫「分组」或「套餐」，写着 GPT PLUS / default
                # 这种看着很像模型名的东西 —— 那是**计费分组**，不是模型名。
                # 她照抄过去，上游就回一句"没有这个模型"。
                raise LlmError(
                    "「%s」说它没有这个模型（HTTP 404，模型名：%s）。\n"
                    "两个常见原因，对一下：\n"
                    "  ① 填的是「分组 / 套餐」名，不是模型名 —— "
                    "中转站后台那列写的 GPT PLUS、default 之类是计费分组，"
                    "模型名长这样：gpt-4o、gpt-4o-mini、claude-sonnet-4。\n"
                    "  ② 模型名过时了，服务商改版换过名字。\n"
                    "怎么办：点这一行旁边那个「拉模型列表」——"
                    "它会把这家真正认的名字列出来，点一个就填进模型名。\n"
                    "当前地址：%s\n上游原话：%s" % (label, model, base, detail),
                    status=e.code)

            if e.code == 429 or e.code >= 500:
                last = LlmError(
                    "「%s」暂时不可用（HTTP %d），第 %d 次尝试。\n上游原话：%s"
                    % (label, e.code, attempt, detail),
                    retryable=True, status=e.code)
            else:
                # 400 之类：请求本身有问题，再试也是一样的结果，直接抛
                raise LlmError(
                    "「%s」拒绝了这次请求（HTTP %d）。\n上游原话：%s"
                    % (label, e.code, detail), status=e.code)

        except urllib.error.URLError as e:
            last = LlmError("连不上「%s」（%s）。检查网络，或者地址填错了。"
                            % (label, _scrub(e.reason, key)), retryable=True)

        except TimeoutError:
            last = LlmError("「%s」超过 %d 秒没回话。"
                            % (label, timeout or TIMEOUT), retryable=True)

        except LlmError as e:
            # 已经是干净的错（比如返回不是 JSON）。上游模型服务偶尔会
            # 吐一半就断，这种也值得再试一次。
            last = LlmError(e.message, retryable=True, status=e.status)

        if attempt < tries:
            time.sleep(RETRY_BACKOFF ** attempt)

    if last is None:
        raise LlmError("「%s」试了 %d 次都没成功。" % (label, tries))
    if tries > 1:
        # 她最终只在任务备注里看到最后这一条错误。不写明"试了几轮"，
        # 她会以为只发了一次请求 —— 而实际上可能已经扣了好几笔钱。
        raise LlmError("%s（一共试了 %d 次）" % (last.message, tries),
                       retryable=last.retryable, status=last.status)
    raise last


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
