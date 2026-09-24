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

    - 超时：单次 180 秒。分类一批 25 条，正常十几秒就回来了，
      给到 180 秒是为了兜住模型偶发的慢。
    - 重试：只有"重试有意义"的错才重试 ——
      429（限流）、5xx（服务端抽风）、网络断。
      401（密钥不对）、400（请求不合法）**不重试**，因为重试一百次
      还是同样的结果，白白等，还把真正的错因埋掉。
    - 退避：等 2 秒、4 秒、8 秒。紧接着重试只会再撞一次限流。
"""

import json
import os
import time
import urllib.error
import urllib.request

from backend import db


# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------

MODELS_FILE = "models.json"

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
                   "enabled", "note", "custom")


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


def load_models(create=True):
    """读清单。文件不存在就生成一份默认的（不含密钥）。

    【为什么不把清单写死在代码里】
    因为密钥得有个地方放，而代码要进公开仓库。
    清单文件放 data/（不同步），代码里只留默认模板。
    """
    path = models_path()
    if not os.path.isfile(path):
        if not create:
            return []
        items = [_clean(m) for m in DEFAULT_MODELS]
        save_models([m for m in items if m])
        return [m for m in items if m]

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
        raw = raw.get("models") or []
    if not isinstance(raw, list):
        raise LlmError("模型清单格式不对：应该是一个列表。位置：%s" % path)

    out = []
    for it in raw:
        c = _clean(it)
        if c:
            out.append(c)
    return out


def save_models(items):
    """把清单写回去。**这是密钥唯一落地的地方。**"""
    path = models_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cleaned = []
    for it in items or []:
        c = _clean(it)
        if c:
            cleaned.append(c)
    payload = {"_说明": "这个文件里存着 API 密钥，别往外发、别提交到 git。"
                        "data/ 目录已经在 .gitignore 里。",
               "models": cleaned}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)          # 先写临时文件再换，写一半断电不会留个坏文件
    return cleaned


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

def _pick_content(raw, key):
    """从返回的 JSON 里挑出模型说的话。

    为什么单独写：不同家的返回结构大同小异但细节有差
    （有的 content 是 None + reasoning_content，有的 choices 可能为空），
    挑不出来的时候要给一句人能看懂的话，而不是 KeyError。
    """
    try:
        d = json.loads(raw)
    except Exception:
        raise LlmError("模型返回的不是 JSON，前 200 字：%s" % raw[:200])

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
    return text, d.get("usage") or {}


def chat(cfg, messages, temperature=0.0, json_mode=False, timeout=None):
    """发一次对话请求。

    返回 {"content": 模型说的话, "usage": 用量, "model": 实际用的模型名}

    cfg 就是清单里的一条（含明文 api_key）。
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
            "要填服务商文档里那个**接口地址**，一般以 /v1 结尾，"
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

    for attempt in range(1, MAX_RETRY + 1):
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + key)
        try:
            with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", "replace")
            text, usage = _pick_content(raw, key)
            return {"content": text, "usage": usage, "model": model}

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
                    "  ② **Key 不是这家的** —— 比如充的是第三方（硅基流动之类的）"
                    "或中转站的额度，却填进了官方那一条。"
                    "这种要去「模型设置」把「API 地址」也一起改成第三方的，"
                    "模型名通常也要改（如 deepseek-ai/DeepSeek-V3.2）\n"
                    "  ③ 这个 Key 没开通当前模型（现在填的是「%s」）\n"
                    "当前地址：%s\n上游原话：%s"
                    % (label, e.code, model, base, detail), status=e.code)

            if e.code == 404:
                raise LlmError(
                    "「%s」找不到这个模型（HTTP 404，模型名：%s）。"
                    "多半是模型名过时了 —— 去「模型设置」里改成控制台上的名字。\n"
                    "上游原话：%s" % (label, model, detail), status=e.code)

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

        if attempt < MAX_RETRY:
            time.sleep(RETRY_BACKOFF ** attempt)

    raise last or LlmError("「%s」试了 %d 次都没成功。" % (label, MAX_RETRY))


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
