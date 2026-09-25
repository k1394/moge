# -*- coding: utf-8 -*-
"""
墨阁 · 剧情内化（第 2 步：AI 批量内化）测试
========================================================
这一轮做的是"点一下 AI 内化之后发生的全部事情"。

**全程不联网、不花一分钱。** 手法跟 test_llm_classify.py 一样：
把 llm.chat 换成一个按剧本回话的假的。真发请求就得有 Key、要花钱、
要等几十秒，而这里要验的东西（提示词拼装、返回解析、校验、落库、
自动建零件、重试只补失败的）跟"谁来回答"无关。

这个文件守的是六件最容易出错、而且出了不错的事：

    ① **used_card_ids 只认本批输入**。模型一旦引用了本批之外的编号，
       整批作废 —— 来源错了比没有来源更糟（零件会挂到别人的素材上）。
    ② **AI 提的零件只能是「待确认」**。绝不能自动变成「已确认」，
       否则她分不清哪些是她认过的、哪些是机器塞进来的。
    ③ **AI 只新建、绝不改已有的零件**。重跑多少次都不许动到
       她已经确认过的东西（任务书验收第 17 条）。
    ④ **重试只补没跑成的**。已经跑过的那几十批绝不重花钱 ——
       这是真金白银，也是 reap_orphan_runs 把"没轮到的"记成失败的原因。
    ⑤ **跨账号隔离**。别人猜 id 也看不到、取消不了我的任务和素材。
    ⑥ **正文一个字都不许被模型改写**。落库的零件带上来源卡，
       点进去现算的正文必须跟卡片接口给的一模一样。

怎么跑（不需要先启动墨阁）：

    cd G:\\docker\\moge
    .venv\\Scripts\\python.exe tests\\test_plots_ai.py

分两段，前段自己起服务，后段直接调函数，**各自用独立的临时库**。
两段开头都有一道"用的不是临时库就拒绝继续"的保险。

**真实数据从头到尾不会被碰。**
"""

import hashlib
import io
import json
import os
import sys
import tempfile
import time

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 复用自动分类那套测试脚手架（起服务、随机端口、Cookie 客户端、等就绪、收日志）
import test_classification_api as H          # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OK = FAIL = 0


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  [通过] %s  %s" % (name, extra))
    else:
        FAIL += 1
        print("  [失败] %s  %s" % (name, extra))


# 造卡用的原文本。三行 → 按行切 → 三张卡。
# 完全中性的内容：这个文件会进公开仓库，里面不许出现她的稿件片段、角色名。
SAMPLE_TEXT = """天还没亮他就出了门，把刀藏在袖子里。
半路上撞见那人，两人谁也没先开口。
后来谁也没赢，各自散了，只是从此再没照过面。
"""


def sample_text(n):
    """另造一份**三行、且跟别人都不一样**的样本。

    【为什么必须不一样】db.save_material 按 content_hash 去重，
    内容一样就直接把**同一个 material_id** 还给你。测试里
    "另建一份素材再跑一遍"的地方就会踩上：拿回来的还是上一份，
    卡片的真实状态是"全都跑过了"，于是 create_run 拒绝空跑、返回
    run_id=None，报错却是 TypeError 而不是"素材没建成"。
    这个坑真的踩过一次，排查花了半天 —— 所以下面每个 save_material
    都要顺手断言 status == "new"。
    """
    return ("第%d份：天还没亮，檐下的水滴声没停过。\n"
            "第%d份：他把灯拨亮，摊开一张空白的纸，一个字也没落。\n"
            "第%d份：天亮时纸还是空的，他把它折好收进了怀里。\n"
            % (n, n, n))


# 假模型的地址。9 端口是 discard 服务，本机基本不会有人监听，
# 连上去立刻被拒 —— 用来测"模型连不上"这条失败路，不会真的等超时。
DEAD_URL = "http://127.0.0.1:9/v1"


def write_fake_models(data_dir, key="fake", base=DEAD_URL):
    """往临时库里写一份只有假模型的清单。

    【为什么不调 llm.upsert_model】那要先 import llm，而 import 的那一刻
    DATA_DIR 就定死了 —— 接口测试是跑在**另一个进程**里的，测试进程这边
    改了环境变量对它没用。直接按文件写最稳。
    """
    p = os.path.join(data_dir, "models.json")
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "_说明": "测试用，全是假的，别当成真配置",
            "models": [{
                "key": key, "label": "假模型（测试用）", "base_url": base,
                "model": "fake-1", "api_key": "sk-fake-0000aaaa1111",
                "enabled": True, "note": "",
            }],
        }, ensure_ascii=False, indent=1))
    return p


# ======================================================================
# 第一段：HTTP（接口、权限、失败路、补充提示词）
# ======================================================================

def http_tests():
    data_dir = tempfile.mkdtemp(prefix="moge_plotsai_http_")
    proc = None
    try:
        port = H.free_port()
        base = "http://127.0.0.1:%d" % port
        print("=" * 66)
        print("第一段：HTTP 接口测试（AI 内化）")
        print("临时库目录：", data_dir)
        print("=" * 66)

        proc = H.start_server(data_dir, port)
        if not H.wait_ready(base):
            print("服务没起来，测试中止。")
            H.dump_server_log(data_dir)
            return

        probe = H.Client(base)
        _code, health = probe.call("GET", "/api/health")
        dbp = os.path.abspath(health.get("db", ""))
        if os.path.abspath(data_dir) not in dbp:
            print("！！服务连的不是临时库：", dbp)
            print("！！为避免动到真实数据，测试拒绝继续。")
            return
        check("服务连的是临时库（不是 data/moge.db）", True, dbp)

        suffix = hashlib.md5(tempfile.mkdtemp().encode()).hexdigest()[:6]
        A = H.Client(base)
        B = H.Client(base)
        A.ok("POST", "/api/auth/register",
             {"username": "甲" + suffix, "password": "pw123456",
              "password2": "pw123456"})
        B.ok("POST", "/api/auth/register",
             {"username": "乙" + suffix, "password": "pw123456",
              "password2": "pw123456"})
        check("两个账号各自登录（Cookie 互不串味）", True)

        # ---- 造素材与卡片 ------------------------------------------------
        print("\n【1】造一份素材并切分，拿到真卡片")
        A.ok("POST", "/api/materials",
             {"title": "样本甲", "content": SAMPLE_TEXT})
        mats = A.ok("GET", "/api/materials")["items"]
        mid = [m for m in mats if m["title"] == "样本甲"][0]["id"]
        A.ok("POST", "/api/material-classification/materials/%d/segment-runs" % mid,
             {"rule": "line"})
        ids = [c["id"] for c in A.ok("GET", "/api/cards?material_id=%d" % mid)["items"]]
        check("切出了 3 张卡片", len(ids) == 3, "得到 %d 张" % len(ids))

        # ---- 内化元信息 --------------------------------------------------
        print("\n【2】内化弹层要的元信息")
        meta = A.ok("GET", "/api/infuse-meta")
        check("下发了模型清单（密钥打码）",
              "models" in meta and all("api_key" not in m for m in meta["models"]))
        check("下发了补充提示词上限", meta["user_prompt_max"] == 5000,
              meta["user_prompt_max"])
        check("下发了置信度分界线（前端要跟它一致）",
              isinstance(meta["low_confidence"], float), meta["low_confidence"])
        check("下发了批次大小（界面上要如实告诉她）",
              meta["first_batch_size"] > 0 and meta["batch_size"] > 0,
              "首批 %s / 之后 %s" % (meta["first_batch_size"], meta["batch_size"]))
        check("下发了提示词版本", bool(meta["prompt_version"]), meta["prompt_version"])

        # ---- 送出去多少张的预览 -------------------------------------------
        print("\n【3】动手前先看见「要送多少张」")
        pv = A.ok("GET", "/api/materials/%d/infuse-preview" % mid)
        check("预览说这份素材会送 3 张", pv["will_send"] == 3, pv["will_send"])
        check("预览说还没跑过任何卡", pv["done_before"] == 0, pv["done_before"])
        check("★ 一张都没跑过时，不许报「跳过 N 张」",
              pv["skip_by_done"] == 0 and pv["cut_by_limit"] == 0,
              "跳过 %s / 被截 %s" % (pv["skip_by_done"], pv["cut_by_limit"]))
        check("★ 三个数能对上（总数 = 跳过 + 被截 + 真送）",
              pv["all_card_count"] == (pv["skip_by_done"] + pv["cut_by_limit"]
                                       + pv["will_send"]),
              "%s = %s + %s + %s" % (pv["all_card_count"], pv["skip_by_done"],
                                     pv["cut_by_limit"], pv["will_send"]))
        pv2 = A.ok("GET", "/api/materials/%d/infuse-preview?limit=2" % mid)
        check("加 limit=2 就只送 2 张", pv2["will_send"] == 2, pv2["will_send"])
        check("★ 被 limit 砍掉的那 1 张记在 cut_by_limit 上（不是算成跳过）",
              pv2["cut_by_limit"] == 1 and pv2["skip_by_done"] == 0
              and pv2["picked_count"] == 3,
              "cut=%s skip=%s picked=%s" % (pv2["cut_by_limit"],
                                            pv2["skip_by_done"],
                                            pv2["picked_count"]))
        code, _ = B.call("GET", "/api/materials/%d/infuse-preview" % mid)
        check("★ 别人的素材看不到预览", code == 404, "HTTP %s" % code)
        code, _ = A.call("GET", "/api/materials/999999/infuse-preview")
        check("不存在的素材给 404（不是 500）", code == 404, "HTTP %s" % code)

        # ---- 补充提示词 ---------------------------------------------------
        print("\n【4】补充提示词（跟素材分类共用一张表，换了个 kind）")
        A.ok("PUT", "/api/infuse-prompt", {"content": "重点提炼拉扯和冲突升级"})
        got = A.ok("GET", "/api/infuse-prompt")
        check("存进去能读回来", got["content"] == "重点提炼拉扯和冲突升级",
              got["content"])
        # 两份补充提示词住在同一张表（user_prompts）里，靠 kind 分家。
        # 这条就是在验"分家分对了" —— 串味的话她会以为分类那边的判据被改了。
        A.ok("POST", "/api/classify-prompt", {"content": "分类那边的判据"})
        c_code, c_prompt = A.call("GET", "/api/classify-prompt")
        check("★ 分类那份没被内化这份串味（kind 分家分对了）",
              c_code == 200
              and (c_prompt or {}).get("content") == "分类那边的判据",
              "%s / %s" % (c_code, json.dumps(c_prompt, ensure_ascii=False)[:60]))
        check("★ 反过来：内化那份也没被分类覆盖",
              A.ok("GET", "/api/infuse-prompt")["content"] == "重点提炼拉扯和冲突升级")
        code, _ = A.call("PUT", "/api/infuse-prompt", {"content": "字" * 5001})
        check("★ 超长要 400，不悄悄截断", code == 400, "HTTP %s" % code)
        A.ok("PUT", "/api/infuse-prompt", {"content": ""})
        check("清空后是空串", A.ok("GET", "/api/infuse-prompt")["content"] == "")

        # ---- 一个能用的模型都没有 → 当场拒绝，不许建空任务 ------------------
        print("\n【5】没配模型时要当场说清楚，别建一个跑不起来的任务")
        code, err = A.call("POST", "/api/materials/%d/infuse" % mid,
                           {"model_key": ""})
        check("★ 没模型 → 400", code == 400, "HTTP %s" % code)
        check("★ 而且话里说了去哪儿填",
              "Key" in json.dumps(err, ensure_ascii=False)
              or "模型" in json.dumps(err, ensure_ascii=False),
              json.dumps(err, ensure_ascii=False)[:90])
        runs = A.ok("GET", "/api/infuse-runs")["runs"]
        check("★ 没有留下一个空任务", len(runs) == 0, "%d 条" % len(runs))

        # ---- 配上假模型（地址指向一个连不上的端口）→ 走失败路 -------------
        print("\n【6】模型连不上时：任务要留痕，不能悄悄过去")
        write_fake_models(data_dir)
        res = A.ok("POST", "/api/materials/%d/infuse" % mid,
                   {"model_key": "fake", "user_prompt": ""})
        rid = res["run_id"]
        check("任务立刻返回了 run_id（没有阻塞在请求里）", rid > 0, rid)

        run = None
        for _ in range(60):
            run = A.ok("GET", "/api/infuse-runs/%d" % rid)["run"]
            if run["status"] not in ("排队中", "进行中"):
                break
            time.sleep(0.5)
        check("任务最后结束了（不是永远卡在「进行中」）",
              run["status"] not in ("排队中", "进行中"), run["status"])
        check("★ 结束状态是「失败」", run["status"] == "失败", run["status"])
        check("任务上记着用的是哪个模型", run["model_key"] == "fake", run["model_key"])
        check("任务上记着哪版提示词", bool(run["prompt_version"]),
              run["prompt_version"])

        items = A.ok("GET", "/api/infuse-runs/%d/items" % rid)["items"]
        check("3 张卡都留了痕", len(items) == 3, "%d 条" % len(items))
        check("★ 每一条都写了失败原因（不是空着）",
              all(i["error_message"] for i in items),
              (items[0]["error_message"] or "")[:70] if items else "")
        check("失败原因说人话（提到了模型或连接）",
              any(k in (items[0]["error_message"] or "")
                  for k in ("模型", "连接", "请求", "地址", "失败")),
              (items[0]["error_message"] or "")[:70] if items else "")

        runs = A.ok("GET", "/api/infuse-runs?material_id=%d" % mid)["runs"]
        check("按素材能查到任务", any(r["id"] == rid for r in runs))

        print("\n【7】重试只补没跑成的（不许重花钱）")
        r2 = A.ok("POST", "/api/infuse-runs/%d/retry" % rid)
        check("重试建出了新任务", r2["run_id"] != rid, r2["run_id"])
        new_run = A.ok("GET", "/api/infuse-runs/%d" % r2["run_id"])["run"]
        check("★ 新任务记着是从哪个任务重试来的",
              new_run["retry_of_run_id"] == rid, new_run["retry_of_run_id"])
        check("★ 重试只带上了失败的条目（没有把整份重跑）",
              new_run["input_card_count"] == 3, new_run["input_card_count"])

        print("\n【8】取消")
        code, _ = A.call("POST", "/api/infuse-runs/%d/cancel" % rid)
        check("已经结束的任务不能再取消，且说清了原因", code == 400, "HTTP %s" % code)

        print("\n【9】跨账号隔离（猜 id 也不行）")
        for name, method, path, body in (
                ("别人的任务详情", "GET", "/api/infuse-runs/%d" % rid, None),
                ("别人的任务条目", "GET", "/api/infuse-runs/%d/items" % rid, None),
                ("别人的候选", "GET", "/api/infuse-runs/%d/candidates" % rid, None),
                ("取消别人的任务", "POST", "/api/infuse-runs/%d/cancel" % rid, None),
                ("重试别人的任务", "POST", "/api/infuse-runs/%d/retry" % rid, None),
                ("对别人的素材发起内化", "POST",
                 "/api/materials/%d/infuse" % mid, {"model_key": "fake"})):
            code, _ = B.call(method, path, body)
            check("★ %s → 拒绝" % name, code in (400, 404), "HTTP %s" % code)

        print("\n【10】候选接口")
        cands = A.ok("GET", "/api/infuse-runs/%d/candidates" % rid)
        check("候选接口能打开（这批是空的有原因）", cands["count"] == 0,
              "%d 条" % cands["count"])
        code, _ = A.call("GET", "/api/infuse-runs/999999/candidates")
        check("不存在的任务给 404", code == 404, "HTTP %s" % code)

        # ---- 提示词库：按用途分档 -----------------------------------------
        # 这一块守的是"做内化时不该挑到一条讲怎么判主类的话"。
        # kind 写错不会报错、只会安静返回空列表 —— 界面上就表现为
        # "我存的提示词全没了"，而数据其实好端端躺在另一档里。
        print("\n【11】提示词库按用途分档（分类 / 内化互不串味）")
        A.ok("POST", "/api/materials",
             {"title": "样本乙", "content": sample_text(2)})
        m2 = [m for m in A.ok("GET", "/api/materials")["items"]
              if m["title"] == "样本乙"][0]["id"]
        A.ok("POST", "/api/material-classification/materials/%d/segment-runs" % m2,
             {"rule": "line"})

        code, err = A.call("GET", "/api/prompt-library?kind=outline")
        check("★ 不认识的用途要 400（不能安静返回空列表）",
              code == 400, "HTTP %s %s" % (code, json.dumps(err, ensure_ascii=False)[:60]))

        pi = A.ok("POST", "/api/prompt-library",
                  {"name": "内化话术", "content": "提炼换成别人名也成立的剧情",
                   "usage_note": "长篇专用", "kind": "infuse"})
        pid = pi["item"]["id"]
        check("内化档建出了一条", pid > 0, pid)
        A.ok("POST", "/api/prompt-library",
             {"name": "分类话术", "content": "按主类判据判", "kind": "classify"})

        inf_lib = A.ok("GET", "/api/prompt-library?kind=infuse")
        cls_lib = A.ok("GET", "/api/prompt-library?kind=classify")
        inf_names = [p["name"] for p in inf_lib["mine"]]
        cls_names = [p["name"] for p in cls_lib["mine"]]
        check("★ 内化档只看得见内化那几条", inf_names == ["内化话术"], inf_names)
        check("★ 分类档只看得见分类那几条", cls_names == ["分类话术"], cls_names)
        check("★ 不传 kind 时默认分类那档（老前端不能坏）",
              [p["name"] for p in A.ok("GET", "/api/prompt-library")["mine"]]
              == ["分类话术"])
        check("自己的条目带正文（她要能接着改）",
              inf_lib["mine"][0].get("content") == "提炼换成别人名也成立的剧情",
              inf_lib["mine"][0].get("content"))

        # ---- 别人的公开条目：能用，但看不到内容 ---------------------------
        print("\n【12】别人公开的提示词：能用，但内容不下发")
        pub = B.ok("POST", "/api/prompt-library",
                   {"name": "乙的内化话术", "content": "乙的独门要求（不该被甲看到）",
                    "visibility": "public", "kind": "infuse"})
        pub_id = pub["item"]["id"]
        lib2 = A.ok("GET", "/api/prompt-library?kind=infuse")
        pub_names = [p["name"] for p in lib2["public"]]
        check("★ 甲能看到乙公开的那条", pub_names == ["乙的内化话术"], pub_names)
        p0 = lib2["public"][0]
        check("★ 但正文一个字都没下发",
              "content" not in p0 and "content_length" in p0,
              json.dumps(p0, ensure_ascii=False)[:110])
        check("★ 只给了字数（够她判断长短，不够她抄走）",
              p0["content_length"] == len("乙的独门要求（不该被甲看到）"),
              p0["content_length"])

        # ---- 用库里的提示词跑一次：记快照 ---------------------------------
        print("\n【13】挑库里一条跑内化 → 任务上要记下用的是哪条")
        r3 = A.ok("POST", "/api/materials/%d/infuse" % m2,
                  {"model_key": "fake", "prompt_id": pid})
        run3 = A.ok("GET", "/api/infuse-runs/%d" % r3["run_id"])["run"]
        check("★ 任务上记着用的是库里哪一条",
              run3["prompt_ref_id"] == pid, run3["prompt_ref_id"])
        check("★ 记着那条的名字（删了也能答出用过什么）",
              run3["prompt_name"] == "内化话术", run3["prompt_name"])
        check("★ 存的是**那一刻的正文快照**（以后改了也不影响这次）",
              run3["user_prompt"] == "提炼换成别人名也成立的剧情",
              json.dumps(run3["user_prompt"], ensure_ascii=False)[:40])
        check("★ 标记成「自己存的」",
              run3["prompt_owner_label"] == ("甲" + suffix),
              "owner=%s label=%s" % (run3.get("prompt_owner"),
                                     run3.get("prompt_owner_label")))

        # 改掉库里那条 → 老任务的快照**不能跟着变**
        A.ok("PATCH", "/api/prompt-library/%d" % pid,
             {"content": "改过之后的要求"})
        old = A.ok("GET", "/api/infuse-runs/%d" % r3["run_id"])["run"]
        check("★ 库里改了，老任务的快照还是原来那句（不回头篡改历史）",
              old["user_prompt"] == "提炼换成别人名也成立的剧情",
              json.dumps(old["user_prompt"], ensure_ascii=False)[:40])
        check("★ 内化档改完能按自己的 kind 取回来（不是静默丢失）",
              any(p["name"] == "内化话术"
                  and p.get("content") == "改过之后的要求"
                  for p in A.ok("GET", "/api/prompt-library?kind=infuse")["mine"]))

        # ---- 别人的 prompt_id 不能拿来跑我的任务 ---------------------------
        code, _ = A.call("POST", "/api/materials/%d/infuse" % m2,
                         {"model_key": "fake", "prompt_id": 999999})
        check("★ 用不存在的提示词 id → 400（不是静默当成没提示词）",
              code == 400, "HTTP %s" % code)

        # 乙公开的那条：甲**能用**（服务端自己取正文），界面上从没见过原文。
        # 另起一份素材 —— 同一个文件不许并发两个任务（这是接口的规定，
        # 也是对的：两个任务同时在改同一批卡片的状态，谁也不准）。
        A.ok("POST", "/api/materials",
             {"title": "样本丙", "content": sample_text(3)})
        m3 = [m for m in A.ok("GET", "/api/materials")["items"]
              if m["title"] == "样本丙"][0]["id"]
        A.ok("POST", "/api/material-classification/materials/%d/segment-runs" % m3,
             {"rule": "line"})
        r4 = A.ok("POST", "/api/materials/%d/infuse" % m3,
                  {"model_key": "fake", "prompt_id": pub_id})
        run4 = A.ok("GET", "/api/infuse-runs/%d" % r4["run_id"])["run"]
        check("★ 别人的公开条目也能用（任务建起来了）",
              run4["prompt_ref_id"] == pub_id, run4["prompt_ref_id"])
        check("★ 名字照给（她要知道这轮用的是哪条）",
              run4["prompt_name"] == "乙的内化话术", run4["prompt_name"])
        check("★ 任务上标明这是别人提的（不是我的）",
              run4["prompt_owner_label"] == ("乙" + suffix),
              run4["prompt_owner_label"])
        # 【为什么正文是空的】公开的含义是"她可以拿去用"，**不是**
        # "她能看见里面写了什么"。正文进了库（任务自己要用），
        # 但不下发到浏览器 —— 一旦下发就等于公开了。
        check("★ 别人的正文一个字都不下发（这是有意的，不是丢了）",
              run4["user_prompt"] == "" and run4["user_prompt_hidden"] is True,
              "hidden=%s" % run4.get("user_prompt_hidden"))
        check("★ 但字数照给（她靠这个判断值不值得用）",
              run4["user_prompt_len"] == len("乙的独门要求（不该被甲看到）"),
              run4["user_prompt_len"])

        # 别人猜我的私有条目 → 看不见也用不了
        code, _ = B.call("PATCH", "/api/prompt-library/%d" % pid, {"name": "篡改"})
        check("★ 别人改不了我私有的提示词", code in (400, 403, 404),
              "HTTP %s" % code)
        b_lib = B.ok("GET", "/api/prompt-library?kind=infuse")
        check("★ 我的私有条目不会出现在别人的库里",
              [p["name"] for p in b_lib["mine"]] == ["乙的内化话术"],
              [p["name"] for p in b_lib["mine"]])
    finally:
        if proc is not None:
            H.stop_server(proc)
        _rm(data_dir)


# ======================================================================
# 第二段：数据层（直接调函数 + 假的 llm.chat，把开心路走通）
# ======================================================================

def data_layer_tests():
    data_dir = tempfile.mkdtemp(prefix="moge_plotsai_dl_")
    old_env = os.environ.get("MOGE_DATA_DIR")
    try:
        # 环境变量必须在 import backend 之前设 —— db.py 是在 import 那一刻
        # 把 DATA_DIR 定死的。晚一步设，db.DB_PATH 就还指着真实库，
        # 下面那道保险会拦住（而不是把真实数据写坏）。
        os.environ["MOGE_DATA_DIR"] = data_dir
        print()
        print("=" * 66)
        print("第二段：数据层测试（假模型，跑通整条内化流水线）")
        print("临时库目录：", data_dir)
        print("=" * 66)

        # 模块不能在文件顶层 import —— 那会在设环境变量之前
        # 就把 DATA_DIR 定死到真实库上（跟 test_plots_base 同一个理由）。
        # 别名照抄 main.py：cls=classify_db（分类库/卡片/来源），
        # auto=classification（user_prompts / ai_judgements / 调模型）。
        # 这两个模块的别名在别的文件里正好是反的（plots_ai 里 cls 指
        # classification），最容易搞混 —— 认 main.py 这份。
        from backend import db, classify_db as cls, classification as auto
        from backend import plots_db as P
        from backend import plots_ai as pai, llm, segmentation as sg

        if os.path.abspath(data_dir) not in os.path.abspath(db.DB_PATH):
            print("！！用的不是临时库：", db.DB_PATH)
            print("！！为避免动到真实数据，测试拒绝继续。")
            return
        check("数据层连的是临时库", True, db.DB_PATH)

        # 建表顺序照抄 main.py 启动时那一串，一个都不能少、也不能换序：
        #   cls.migrate()  classify_db —— 分类库 / 卡片 / 来源 / ai_judgements
        #   auto.migrate() classification —— 要给上面那张 ai_judgements 补列，
        #                  表不存在就报 no such table；user_prompts 也在这儿建
        #   P.migrate()    plots_db
        #   pai.migrate()  plots_ai 那四张（要往 plots / plot_cards 里写）
        # 少调一个，任务一跑就报 no such table —— 而且是在**后台线程**里报，
        # 界面上只看到"任务失败"，看不出是漏建了表。
        db.init_db()
        cls.migrate()
        auto.migrate()
        P.migrate()
        pai.migrate()
        write_fake_models(data_dir)

        owner = "__u1"
        other = "__u2"

        # ---- 四张新表 -----------------------------------------------------
        print("\n【11】四张新表建出来了")
        import sqlite3
        c = sqlite3.connect(db.DB_PATH)
        tabs = {r[0] for r in c.execute(
            "select name from sqlite_master where type='table'")}
        for t in ("plot_runs", "plot_run_cards", "plot_items", "plot_candidates"):
            check("建出表 %s" % t, t in tabs)
        sql_items = c.execute("select sql from sqlite_master where name='plot_items'"
                              ).fetchone()[0]
        check("plot_items 上 (run_id, card_id) 唯一",
              "UNIQUE (run_id, card_id)" in sql_items)
        c.close()

        # ---- 造一份素材并切分 ----------------------------------------------
        print("\n【12】造素材、切分、拿真卡片")
        mid = db.save_material("内化样本", SAMPLE_TEXT, owner=owner)["id"]
        # 必须显式指定按行切。不传 rule 会走 auto_rule，它把这三行短句
        # 归成"一整段"，只切出 1 张卡 —— 下面所有"3 张卡"的断言就全塌了。
        # （HTTP 段那边是接口带 rule=line 进来的，行为一致。）
        cls.apply_split(mid, owner, rule="line")
        card_ids = [r["id"] for r in _q(
            db, "SELECT id FROM cards WHERE material_id=? ORDER BY start_offset",
            (mid,))]
        check("切出 3 张卡", len(card_ids) == 3, "%d 张" % len(card_ids))

        # 给第 2 张卡挂个主类，用来验"AI 没判分类时回退到原素材分类"
        cats = {x["name"]: x["id"] for x in cls.list_categories(owner)}
        _exec(db, "UPDATE cards SET primary_category_id=?, status=? WHERE id=?",
              (cats["神态"], sg.STATUS_CONFIRMED, card_ids[1]))

        # ---- 假模型 --------------------------------------------------------
        calls = []
        script = {}          # card_id → 剧本
        mode = {"text": None}   # 非 None 就直接回这段原文（测脏返回）

        def card_ids_in(messages):
            import re
            text = "\n".join(m.get("content") or "" for m in messages)
            return [int(x) for x in re.findall(r"card_id=(\d+)", text)]

        def fake_chat(cfg, messages, temperature=0.0, json_mode=False, timeout=None):
            calls.append({"model": cfg.get("model"), "json_mode": json_mode,
                          "messages": messages})
            if mode["text"] is not None:
                return {"content": mode["text"], "usage": {},
                        "model": cfg.get("model")}
            seen = card_ids_in(messages)
            items = []
            for cid in seen:
                r = script.get(cid)
                if r is not None:
                    items.append(r)
            used = sorted({c for it in items for c in it.get("used_card_ids", [])})
            payload = {
                "items": items,
                "input_assessment": {
                    "input_card_ids": seen,
                    "usable_card_ids": used,
                    "not_used_card_ids": [x for x in seen if x not in used],
                    "overall_note": "测试",
                },
                "warnings": [],
            }
            return {"content": json.dumps(payload, ensure_ascii=False),
                    "usage": {"total_tokens": 100},
                    "model": cfg.get("model")}

        real_chat = llm.chat
        llm.chat = fake_chat
        try:
            # 第 1 张卡 → 一个零件（用两张卡当来源）
            script[card_ids[0]] = {
                "result": "generate",
                "used_card_ids": [card_ids[0], card_ids[1]],
                "title": "以罚代护",
                "summary": "一方借责罚把人留在身边，名义是惩戒，实为护他周全。",
                "primary_category": None,      # 故意留空 → 该回退到原素材分类
                "plot_type": "保护救援",
                "usage_hint": ["初遇", "关系升温"],
                "tags": ["拉扯"],
                "role_slots": ["保护者", "被保护者"],
                "beats": {"setup": "关系冷淡", "trigger": "一件小事",
                          "motivation": "想护住对方", "乱写的键": "该被丢掉"},
                "transferable_core": "以惩罚为名行保护之实",
                "optional_variations": ["惩罚可以换成责骂"],
                "reason": "两张卡共同构成保护结构。",
                "confidence": 0.92,
                "unsure_points": [],
            }

            print("\n【13】跑一整条内化（单批 3 张）")
            res, run_id = pai.create_run(owner, mid, model_key="fake",
                                         user_prompt="重点提炼拉扯",
                                         background=False)
            check("任务建起来了", res.get("ok"), True)
            run = pai.get_run(run_id, owner)
            check("任务状态是已完成", run["status"] == "已完成", run["status"])
            check("任务记下了模型", run["model_key"] == "fake", run["model_key"])
            check("任务记下了提示词版本", bool(run["prompt_version"]),
                  run["prompt_version"])
            check("任务记下了模板来源", run["template_source"] in ("file", "builtin"),
                  run["template_source"])
            check("任务记下了补充提示词长度", run["user_prompt_len"] == 6,
                  run["user_prompt_len"])
            check("总条目 = 3", run["total_items"] == 3, run["total_items"])
            check("3 张都处理完了", run["done_items"] == 3, run["done_items"])
            check("落成 1 条零件", run["created_plot_count"] == 1,
                  run["created_plot_count"])

            check("真的调了模型（1 批）", len(calls) == 1, "%d 次" % len(calls))
            check("json_mode 是开着的（有服务端兜底）", calls[0]["json_mode"] is True)
            sent = calls[0]["messages"][0]["content"]
            check("★ 正文是按偏移现算出来的原文",
                  SAMPLE_TEXT.strip().split("\n")[0] in sent)
            check("★ 提示词里带上了六个槽位的内容（主类判据在）",
                  "判据" in sent)
            check("★ 补充提示词进了提示词", "重点提炼拉扯" in sent)
            check("★ 没有出现没被替换掉的占位符",
                  "{" not in sent.replace("{\"", "@").replace("}", "")
                  or "{card_blocks}" not in sent and "{user_prompt}" not in sent)

            print("\n【14】AI 提的零件：状态、来源、置信度、理由")
            rows = P.list_plots(owner)["plots"]
            check("库里多了 1 条零件", len(rows) == 1, "%d 条" % len(rows))
            p = P.get_plot(owner, rows[0]["id"])
            check("★ 状态是「待确认」（绝不自动变成已确认）",
                  p["status"] == "待确认", p["status"])
            check("★ 来源标成 ai（界面上要跟人工写的长得不一样）",
                  p["source"] == "ai", p["source"])
            check("置信度存下来了", p["ai_confidence"] == 0.92, p["ai_confidence"])
            check("理由存下来了", "保护结构" in (p["ai_reason"] or ""),
                  (p["ai_reason"] or "")[:40])
            check("标题对", p["title"] == "以罚代护", p["title"])
            check("剧情类型对", p["plot_type"] == "保护救援", p["plot_type"])
            check("★ 七个点位里的「动机」没被丢掉（第 1 步只定义了六个）",
                  p["beats"].get("motivation") == "想护住对方",
                  json.dumps(p["beats"], ensure_ascii=False))
            check("模型编的键被丢掉了（不是全盘照收）",
                  "乱写的键" not in p["beats"])
            check("使用场景进了 2 个", len(p["usage_hints"]) == 2, p["usage_hints"])

            print("\n【15】「按原素材标签分类」—— AI 没判分类时回退到来源卡")
            check("★ 回退到了来源卡的主类（神态）",
                  p["primary_category_id"] == cats["神态"],
                  "%s / 期望 %s" % (p["primary_category_id"], cats["神态"]))

            print("\n【16】来源关系：多对多、双向、能定位回原文")
            src = P.list_sources(owner, p["id"])
            check("★ 2 张卡 → 1 个零件", len(src) == 2, "%d 条" % len(src))
            check("来源带偏移（不是只存个 id）",
                  all(s["start_offset"] is not None for s in src))
            back = P.plots_of_card(owner, card_ids[0])
            check("★ 反向：这张卡能查出它内化成了哪个零件",
                  any(x["id"] == p["id"] for x in back), "%d 条" % len(back))
            check("没被用到的第 3 张卡，反向是空的",
                  len(P.plots_of_card(owner, card_ids[2])) == 0)

            print("\n【17】卡片的内化状态是从关联表算出来的")
            st = P.card_infuse_states(owner, card_ids)
            check("★ 被用到的 2 张：状态是「已内化」",
                  st.get(card_ids[0]) == P.CARD_INFUSE_HAS_PLOT
                  and st.get(card_ids[1]) == P.CARD_INFUSE_HAS_PLOT,
                  "%s / %s" % (st.get(card_ids[0]), st.get(card_ids[1])))
            check("没被用到的第 3 张不是已内化",
                  st.get(card_ids[2]) != P.CARD_INFUSE_HAS_PLOT, st.get(card_ids[2]))

            print("\n【17b】预览的三档数量分得清（别自相矛盾）")
            # 这两条守的是弹层上那行字。她看到"已跑过 0 张，跳过 3 张"
            # 会立刻觉得这个功能是坏的 —— 而那种数字确实出现过一次。
            pv = pai.target_preview(owner, mid, skip_infused=True)
            check("★ 跑过的 3 张：done_before 与 skip_by_done 都是 3",
                  pv["done_before"] == 3 and pv["skip_by_done"] == 3,
                  "跑过 %s / 跳过 %s" % (pv["done_before"], pv["skip_by_done"]))
            check("★ 跑过的里面分得清：2 张出过零件、1 张看过没用",
                  pv["has_plot_count"] == 2 and pv["not_suitable_count"] == 1,
                  "出零件 %s / 看过没用 %s" % (pv["has_plot_count"],
                                            pv["not_suitable_count"]))
            check("★ 总数 = 跳过 + 被 limit 截掉 + 真送",
                  pv["all_card_count"] == (pv["skip_by_done"]
                                           + pv["cut_by_limit"] + pv["will_send"]),
                  "%s = %s + %s + %s" % (pv["all_card_count"],
                                         pv["skip_by_done"],
                                         pv["cut_by_limit"], pv["will_send"]))
            check("★ 全跑过了 → 默认一张都不送（不重复花钱）",
                  pv["will_send"] == 0, pv["will_send"])
            pv_ns = pai.target_preview(owner, mid, skip_infused=False)
            check("★ 不勾「跳过跑过的」时一张都不砍，3 张全送",
                  pv_ns["skip_by_done"] == 0 and pv_ns["will_send"] == 3,
                  "跳过 %s / 要送 %s" % (pv_ns["skip_by_done"],
                                        pv_ns["will_send"]))
            pv_lim = pai.target_preview(owner, mid, skip_infused=False, limit=1)
            check("不勾跳过 + limit=1：被截的 2 张记在 cut_by_limit 上",
                  pv_lim["cut_by_limit"] == 2 and pv_lim["will_send"] == 1
                  and pv_lim["skip_by_done"] == 0,
                  "cut=%s will=%s" % (pv_lim["cut_by_limit"], pv_lim["will_send"]))
            # 预览说送几张，点下去就必须送几张 —— 两处口径不一致的话，
            # 她按这个数估的 token 钱会全错。
            picked = pai._target_cards(owner, mid, skip_infused=False, limit=1)
            check("★ 预览的 will_send 跟真正送出去的张数一致",
                  len(picked) == pv_lim["will_send"],
                  "%d vs %s" % (len(picked), pv_lim["will_send"]))

            print("\n【18】候选全部留底（含原始返回，方便以后溯源）")
            cands = pai.list_candidates(run_id, owner)
            check("候选存下来了", len(cands) >= 1, "%d 条" % len(cands))
            check("候标记了被采用", cands[0]["adopted"] == 1, cands[0]["adopted"])
            check("候选指向了落成的零件", cands[0]["plot_id"] == p["id"],
                  cands[0]["plot_id"])
            with db.connect() as conn:
                raw = conn.execute("SELECT raw_response FROM plot_candidates"
                                   " WHERE id=?", (cands[0]["id"],)).fetchone()[0]
            check("★ 模型的原始返回原样存着", "以罚代护" in (raw or ""))
            check("候标记了分组键（第 3 步多模型比较要用）",
                  bool(cands[0]["candidate_group_key"]))

            print("\n【19】★ 重跑不许动到已有的零件（任务书验收第 17 条）")
            # 先把她"确认"过这条零件，看重跑会不会把它退回待确认。
            # 用 set_plot_status，不是 update_plot —— update_plot 是"改内容"，
            # 它压根不认 status 这个字段，只会顺手把「待确认」升成「已编辑」。
            P.set_plot_status(owner, p["id"], P.PLOT_STATUS_CONFIRMED)
            before = P.get_plot(owner, p["id"])
            n_ver_before = len(P.list_versions(owner, p["id"]))
            res2, run2 = pai.create_run(owner, mid, model_key="fake",
                                        skip_infused=False, background=False)
            check("重跑建起来了（关掉跳过）", res2.get("ok"), True)
            after = P.get_plot(owner, p["id"])
            check("★ 她确认过的零件状态没被改回待确认",
                  after["status"] == "已确认", after["status"])
            check("★ 重跑没有给已有零件加版本",
                  len(P.list_versions(owner, p["id"])) == n_ver_before,
                  "%d → %d" % (n_ver_before,
                               len(P.list_versions(owner, p["id"]))))
            check("★ 重跑是**新建**了一条，不是覆盖",
                  len(P.list_plots(owner)["plots"]) == 2,
                  len(P.list_plots(owner)["plots"]))
            check("新那条还是「待确认」",
                  [x for x in P.list_plots(owner)["plots"]
                   if x["id"] != p["id"]][0]["status"] == "待确认")

            print("\n【20】跳过已内化的卡（默认行为）")
            res3, _ = pai.create_run(owner, mid, model_key="fake",
                                     skip_infused=True, background=False)
            check("★ 全跑过的素材，默认会拒绝空跑并说清原因",
                  res3.get("ok") is False
                  and res3.get("reason") == "nothing_to_do",
                  res3.get("message", "")[:60])
            check("话里解释了为什么（提到了已内化）",
                  "内化" in (res3.get("message") or ""),
                  (res3.get("message") or "")[:60])

            print("\n【21】★ used_card_ids 引用了本批之外的卡 → 整批作废")
            got2 = db.save_material("内化样本二", SAMPLE_TEXT + "第四行。",
                                    owner=owner)
            check("【21】用的是新素材（不是去重拿回上面那份）",
                  got2["status"] == "new", got2["status"])
            mid2 = got2["id"]
            cls.apply_split(mid2, owner, rule="line")
            ids2 = [r["id"] for r in _q(
                db, "SELECT id FROM cards WHERE material_id=? ORDER BY start_offset",
                (mid2,))]
            n_plots_before = len(P.list_plots(owner)["plots"])
            script.clear()
            script[ids2[0]] = {
                "result": "generate", "used_card_ids": [999999],   # 本批之外
                "title": "不该落库", "summary": "来源错位",
                "plot_type": "保护救援", "usage_hint": [], "tags": [],
                "role_slots": [], "beats": {}, "transferable_core": "",
                "optional_variations": [], "reason": "x", "confidence": 0.9,
                "unsure_points": [],
            }
            res4, run4 = pai.create_run(owner, mid2, model_key="fake",
                                        background=False)
            r4 = pai.get_run(run4, owner)
            check("★ 任务记成失败（不是悄悄当成功）",
                  r4["status"] in ("失败", "部分失败"), r4["status"])
            check("★ 一条零件都没落库（来源错位宁可不要）",
                  len(P.list_plots(owner)["plots"]) == n_plots_before,
                  "%d → %d" % (n_plots_before,
                               len(P.list_plots(owner)["plots"])))
            items4 = pai.list_items(run4, owner)
            check("失败原因写着「本批之外」",
                  any("本批之外" in (i["error_message"] or "") for i in items4),
                  (items4[0]["error_message"] or "")[:70] if items4 else "")
            cands4 = pai.list_candidates(run4, owner)
            check("★ 整批作废时连候选都不留（免得她以为有结果）",
                  len(cands4) == 0, "%d 条" % len(cands4))

            print("\n【22】unsure / unsuitable 不落库，但要留候选给她看")
            got3 = db.save_material("内化样本三", sample_text(3), owner=owner)
            check("【22】用的是新素材（不是去重拿回上面那份）",
                  got3["status"] == "new", got3["status"])
            mid3 = got3["id"]
            cls.apply_split(mid3, owner, rule="line")
            ids3 = [r["id"] for r in _q(
                db, "SELECT id FROM cards WHERE material_id=? ORDER BY start_offset",
                (mid3,))]
            n_plots = len(P.list_plots(owner)["plots"])
            script.clear()
            script[ids3[0]] = {
                "result": "unsure", "used_card_ids": [ids3[0]],
                "title": "", "summary": "", "primary_category": None,
                "plot_type": None, "usage_hint": [], "tags": [],
                "role_slots": ["主动提出者"], "beats": {"trigger": "提了一句"},
                "transferable_core": "", "optional_variations": [],
                "reason": "缺前后文", "confidence": 0.52,
                "unsure_points": ["缺少对方反应"],
            }
            script[ids3[1]] = {
                "result": "unsuitable", "used_card_ids": [ids3[1]],
                "title": "", "summary": "", "primary_category": "外貌",
                "plot_type": None, "usage_hint": [], "tags": ["好词好句"],
                "role_slots": [], "beats": {}, "transferable_core": "",
                "optional_variations": [], "reason": "只是神态描写",
                "confidence": 0.95, "unsure_points": [],
            }
            _res5, run5 = pai.create_run(owner, mid3, model_key="fake",
                                         background=False)
            check("★ 两种「不生成」都不落库",
                  len(P.list_plots(owner)["plots"]) == n_plots,
                  "%d → %d" % (n_plots, len(P.list_plots(owner)["plots"])))
            c5 = pai.list_candidates(run5, owner)
            check("候选留了 2 条", len(c5) == 2, "%d 条" % len(c5))
            check("★ 候选里能看到 unsure 的理由",
                  any("缺前后文" in (x["reason"] or "") for x in c5))
            check("★ 候选里带着 unsure_points（她要知道缺什么）",
                  any(x["unsure_points"] for x in c5))
            check("候选都标了没被采用",
                  all(x["adopted"] == 0 for x in c5))
            r5 = pai.get_run(run5, owner)
            check("任务本身是成功的（没生成零件 ≠ 失败）",
                  r5["status"] == "已完成", r5["status"])

            print("\n【23】手工建的零件不受影响")
            pm = P.create_plot(owner, "我自己写的", summary="手写",
                               category_id=cats["情节"])
            check("手工建的还是「已确认」", pm["status"] == "已确认", pm["status"])
            check("手工建的来源不是 ai", pm["source"] == "human", pm["source"])

            print("\n【24】僵尸任务收尾（服务重启后）")
            with db.connect() as conn:
                conn.execute(
                    "INSERT INTO plot_runs (owner_id, material_id, status,"
                    " total_items, done_items, created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (owner, mid3, "进行中", 5, 2, "2026-01-01 00:00:00"))
                zid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    "INSERT INTO plot_items (run_id, card_id, status,"
                    " created_at, updated_at) VALUES (?,?,?,?,?)",
                    (zid, ids3[0], "待处理", "2026-01-01 00:00:00",
                     "2026-01-01 00:00:00"))
            n = pai.reap_orphan_runs("测试重启")
            check("收尾了 1 条僵尸任务", n == 1, "%d 条" % n)
            zr = pai.get_run(zid, owner)
            check("僵尸任务被标成失败", zr["status"] == "失败", zr["status"])
            check("并且告诉她重试能接着跑",
                  "重试" in (zr["note"] or ""), (zr["note"] or "")[:50])
            zi = pai.list_items(zid, owner)
            check("没轮到的那条记成失败（重试正好补它）",
                  zi[0]["status"] == "失败", zi[0]["status"])

            print("\n【25】脏返回要留痕，不能崩")
            got4 = db.save_material("内化样本四", sample_text(4), owner=owner)
            check("【25】用的是新素材（不是去重拿回上面那份）",
                  got4["status"] == "new", got4["status"])
            mid4 = got4["id"]
            cls.apply_split(mid4, owner, rule="line")
            mode["text"] = "我什么都不会说，抱歉。"
            _res6, run6 = pai.create_run(owner, mid4, model_key="fake",
                                         background=False)
            r6 = pai.get_run(run6, owner)
            check("★ 模型胡言乱语 → 任务失败而不是崩掉",
                  r6["status"] == "失败", r6["status"])
            i6 = pai.list_items(run6, owner)
            check("★ 失败原因把模型说的前 200 字留下来了",
                  "抱歉" in (i6[0]["error_message"] or ""),
                  (i6[0]["error_message"] or "")[:60])
            mode["text"] = "```json\n" + json.dumps({
                "items": [{"result": "unsuitable", "used_card_ids": [],
                           "title": "", "summary": "",
                           "confidence": 0.9, "unsure_points": []}],
                "input_assessment": {}, "warnings": []}) + "\n```"
            _res7, run7 = pai.create_run(owner, mid4, model_key="fake",
                                         background=False)
            check("★ 带 ```json 围栏也能解析（这是常态不是极端情况）",
                  pai.get_run(run7, owner)["status"] == "已完成",
                  pai.get_run(run7, owner)["status"])
            mode["text"] = None

            print("\n【26】跨账号：__u2 看不见 __u1 的任务")
            _res8, run8 = pai.create_run(owner, mid4, model_key="fake",
                                         background=False)
            check("别人查不到这个任务", pai.get_run(run8, other) is None)
            check("别人查不到这个任务的条目",
                  len(pai.list_items(run8, other)) == 0)
            check("别人查不到这个任务的候选",
                  len(pai.list_candidates(run8, other)) == 0)
            try:
                pai.cancel_run(run8, other)
                check("别人不能取消我的任务", False)
            except ValueError:
                check("别人不能取消我的任务", True)
        finally:
            llm.chat = real_chat
    finally:
        if old_env is None:
            os.environ.pop("MOGE_DATA_DIR", None)
        else:
            os.environ["MOGE_DATA_DIR"] = old_env
        _rm(data_dir)


def _q(db, sql, args=()):
    with db.connect() as conn:
        return conn.execute(sql, args).fetchall()


def _exec(db, sql, args=()):
    with db.connect() as conn:
        conn.execute(sql, args)


def _rm(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def main():
    http_tests()
    data_layer_tests()
    print()
    print("=" * 66)
    print("通过 %d 项，失败 %d 项" % (OK, FAIL))
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
