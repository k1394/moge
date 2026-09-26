# -*- coding: utf-8 -*-
"""接入点（providers）的数据层测试。

为什么单独盯这一层：
    她的诉求是「阿里云一个 api 可以调用那么多模型，网站能不能统一一下，
    我想用其他模型的免费额度」。而"统一"这件事**全押在同一个文件上** ——
    data/models.json 里同时住着接入点和模型，而且里面存着她正在用的
    API 密钥（分类和大纲每天都在读它）。

    所以有两条底线必须被测试盯死：

      1. save_models() **不许**把 providers 连密钥一起抹掉。
         改动之前这个函数是"直接覆盖整个文件"的写法 ——
         加了 providers 之后，那样写会把接入点悄悄删掉，还不报错。

      2. 老配置（扁平结构）读出来必须一字不差。
         迁移不能改坏她现有的东西 —— 她现在跑着的 qwen-plus 就在里面。

    （补一个背景：她现在的文件里 qwen-plus 和 qwen-max 各自抄了一份
     阿里云 Key。所以这里刻意照着那个样子造数据。）

怎么跑：
    cd G:\\docker\\moge
    .venv\\Scripts\\python.exe -X utf8 tests/test_providers.py
"""

import json
import os
import shutil
import sys
import tempfile

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

# 【顺序要紧】先定临时目录，再 import —— import 的瞬间 llm 就定下了数据目录
data_dir = tempfile.mkdtemp(prefix="moge_prov_")
os.environ["MOGE_DATA_DIR"] = data_dir

from backend import llm   # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OK = 0
FAIL = 0


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  [通过] %s  %s" % (name, extra))
    else:
        FAIL += 1
        print("  [失败] %s  %s" % (name, extra))


def same(name, got, want):
    """显式比一遍期望值。

    【为什么不写 check(name, got, want)】
    第三个参数是"补充说明"，不是期望值 —— got 是个非空字符串/非空元组时
    它**永远为真**，断言等于没写。这个坑在另一个测试文件里踩过。
    """
    check(name, got == want, "得到 %r，期望 %r" % (got, want))


DASH = "https://dashscope.aliyuncs.com/compatible-mode/v1"
BIG = "https://open.bigmodel.cn/api/paas/v4"
SK_A = "sk-aaaa1111"
SK_B = "sk-bbbb2222"


def write_old_file():
    """写一份**老格式**的清单（只有 models，没有 providers）。

    照着她迁移前那台机器上的真实样子造：同一个阿里云 Key 被
    qwen-plus 和 qwen-max 各抄了一遍。
    """
    path = llm.models_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"_说明": "老格式（没有 providers 这一节）",
                   "models": [
                       {"key": "qwen-plus", "label": "通义千问 Plus",
                        "base_url": DASH, "model": "qwen-plus",
                        "api_key": SK_A, "enabled": True},
                       {"key": "qwen-max", "label": "通义千问 Max",
                        "base_url": DASH, "model": "qwen-max",
                        "api_key": SK_A, "enabled": True},
                       {"key": "glm", "label": "智谱 GLM",
                        "base_url": BIG, "model": "glm-4-flash",
                        "api_key": SK_B, "enabled": True},
                   ]}, f, ensure_ascii=False)


def read_file():
    with open(llm.models_path(), encoding="utf-8") as f:
        return json.load(f)


def main():
    print("=" * 66)
    print("墨阁 · 接入点（一个 Key 调一堆模型）· 数据层测试")
    print("=" * 66)

    # ---------------- 【1】 ----------------
    print("\n【1】老格式（扁平结构）：读出来必须跟以前一样")
    write_old_file()
    ms = llm.load_models()
    same("三条模型都在", len(ms), 3)
    by = {m["key"]: m for m in ms}
    same("qwen-plus 的地址没被改", by["qwen-plus"]["base_url"], DASH)
    same("qwen-plus 的密钥没被动过", by["qwen-plus"]["api_key"], SK_A)
    same("glm 的密钥也没被动过", by["glm"]["api_key"], SK_B)
    check("老文件里本来确实没有 providers 这一节",
          "providers" not in read_file())

    # ---------------- 【2】 ----------------
    print("\n【2】老配置也能推导出接入点（同一个 Key 合成一个）")
    ps = llm.public_providers()
    by2 = {p["key"]: p for p in ps}
    same("推出来两个接入点", len(ps), 2)
    check("阿里云那个被认出来了（名字从域名取）",
          "dashscope" in by2, "得到的：" + str(sorted(by2)))
    check("qwen-plus 和 qwen-max 合并到同一个接入点下",
          by2["dashscope"]["linked"] == 2,
          "linked=%d" % by2["dashscope"]["linked"])
    same("智谱那个也认出来了", by2["bigmodel"]["linked"], 1)
    check("推导出来的接入点是带密钥的", by2["dashscope"]["has_key"])
    check("但给到界面的密钥是打码的，真钥匙不出后端",
          ("api_key" not in by2["dashscope"]) and
          by2["dashscope"]["api_key_masked"] != SK_A,
          by2["dashscope"]["api_key_masked"])

    # ---------------- 【3】最危险的一条 ----------------
    print("\n【3】只改模型时，接入点（含密钥）不许被抹掉")
    llm.save_providers(llm.load_providers())
    same("接入点已落盘", len(read_file().get("providers") or []), 2)

    ms = llm.load_models()
    ms[0]["note"] = "改了个备注"
    llm.save_models(ms)
    raw = read_file()
    same("接入点还在（没被覆盖掉）", len(raw.get("providers") or []), 2)
    dashes = [p for p in raw["providers"] if p["key"] == "dashscope"]
    same("接入点里的密钥还在", dashes[0]["api_key"], SK_A)
    check("改的那条备注写进去了",
          [m for m in raw["models"] if m["key"] == "qwen-plus"][0]["note"]
          == "改了个备注")

    # ---------------- 【4】核心承诺 ----------------
    print("\n【4】统一生效：换一次接入点的密钥，底下模型一起用新钥匙")
    qp = [m for m in raw["models"] if m["key"] == "qwen-plus"][0]
    same("qwen-plus 已认领到接入点（不再各抄一份）", qp["provider"], "dashscope")
    same("它那份冗余密钥被清掉了（清了才谈得上统一）", qp["api_key"], "")
    llm.upsert_provider({"key": "dashscope", "api_key": "sk-cccc3333"})
    by3 = {m["key"]: m for m in llm.load_models()}
    same("换完之后 qwen-plus 用新钥匙", by3["qwen-plus"]["api_key"], "sk-cccc3333")
    same("qwen-max 也是新钥匙（两个共用一把）",
         by3["qwen-max"]["api_key"], "sk-cccc3333")
    same("另一个接入点底下没被牵连", by3["glm"]["api_key"], SK_B)

    # ---------------- 【5】 ----------------
    print("\n【5】她单独改过地址的模型，保留它自己的")
    ms = llm.load_models()
    for m in ms:
        if m["key"] == "qwen-max":
            m["base_url"] = "https://my-relay.example.com/v1"
    llm.save_models(ms)
    llm.upsert_provider({"key": "dashscope", "api_key": "sk-dddd4444"})
    by4 = {m["key"]: m for m in llm.load_models()}
    same("单独改过的地址没被接入点覆盖",
         by4["qwen-max"]["base_url"], "https://my-relay.example.com/v1")
    same("但钥匙还是跟着接入点（它没单独填过钥匙）",
         by4["qwen-max"]["api_key"], "sk-dddd4444")
    same("没改过的那条地址照旧跟着接入点",
         by4["qwen-plus"]["base_url"], DASH)

    # ---------------- 【6】 ----------------
    print("\n【6】从接入点批量加模型（地址密钥跟着接入点，不重复抄）")
    added, skipped = llm.add_models_from_provider(
        "dashscope", ["qwen3.7-plus", "qwen-plus", "glm-5.3"])
    same("加进来 2 个", added, 2)
    same("跳过 1 个（qwen-plus 已经在清单里）", skipped, 1)
    ms = {m["model"]: m for m in llm.load_models()}
    check("新加的那两个在清单里（按模型名认，不是按显示名）",
          "qwen3.7-plus" in ms and "glm-5.3" in ms,
          "现在有：" + str(sorted(ms)))
    # 【要看文件里存的，不是 load_models() 的结果】
    # load_models() 会把接入点的地址/密钥**回填**进来 —— 那正是"统一"生效的
    # 地方，所以从它那儿看一定是有值的。真正要盯的是文件里有没有多存一份：
    # 多存了就意味着"以后改接入点它不跟着变"。
    raw6 = read_file()
    fresh = [m for m in raw6["models"] if m["model"] == "qwen3.7-plus"][0]
    same("文件里没给它单独存地址（跟着接入点）", fresh["base_url"], "")
    same("文件里没给它单独存密钥（跟着接入点）", fresh["api_key"], "")
    same("它指向的接入点是对的", ms["qwen3.7-plus"]["provider"], "dashscope")
    same("读出来时地址由接入点补上", ms["qwen3.7-plus"]["base_url"], DASH)
    same("密钥也由接入点补上", ms["qwen3.7-plus"]["api_key"], "sk-dddd4444")
    check("同一个模型加两次不会变成两条",
          len([m for m in llm.load_models() if m["model"] == "qwen-plus"]) == 1)

    # ---------------- 【7】 ----------------
    print("\n【7】删掉接入点，它底下的模型不跟着消失")
    before = len(llm.load_models())
    check("删掉了", llm.delete_provider("dashscope"))
    ms = llm.load_models()
    same("模型一个都没少", len(ms), before)
    by5 = {m["key"]: m for m in ms}
    check("qwen-plus 还在", "qwen-plus" in by5)
    same("它把地址就地留下了（不至于变成没地址的空壳）",
         by5["qwen-plus"]["base_url"], DASH)
    same("密钥也留下了", by5["qwen-plus"]["api_key"], "sk-dddd4444")
    same("引用被摘掉了", by5["qwen-plus"]["provider"], "")
    same("接入点少了一个", len(llm.load_providers()), 1)

    # ---------------- 【8】 ----------------
    print("\n【8】PATCH 语义：没传的字段不许被清掉")
    llm.upsert_provider({"key": "bigmodel", "label": "改个名字"})
    p = llm.get_provider("bigmodel")
    same("名字改了", p["label"], "改个名字")
    same("地址没被空串抹掉（只传了名字）", p["base_url"], BIG)
    same("密钥也还在", p["api_key"], SK_B)
    llm.upsert_provider({"key": "bigmodel", "api_key": ""})
    same("密钥传空 = 不改，还是原来那把",
         llm.get_provider("bigmodel")["api_key"], SK_B)
    llm.clear_provider_key("bigmodel")
    same("明确清空才真的清掉", llm.get_provider("bigmodel")["api_key"], "")

    print("\n" + "=" * 66)
    print("通过 %d 项，失败 %d 项" % (OK, FAIL))
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)
