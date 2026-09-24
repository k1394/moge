# -*- coding: utf-8 -*-
"""
墨阁 · 素材分类库 接口测试
========================================================
测什么：把「素材分类」这一整套接口，从粘贴入库 → 切分预览 → 确认切分 →
        卡片列表 → 改分类 → 批量改 → 撤销 → 拆分 → 合并 → 重复提示 →
        换切法重切，完整跑一遍；再验证两件不能出错的事：
        ① 未登录进不去，别人的卡片看不到也改不了
        ② 原文一个字都没被改过

怎么跑（不需要先启动墨阁）：

    cd G:\\docker\\moge
    .venv\\Scripts\\python.exe tests\\test_classify_api.py

它自己会：
    1. 建一个空的临时数据库（不在你的 data/ 目录里）
    2. 用这个临时库在随机端口上起一个墨阁服务
    3. 注册测试账号、粘贴一段**测试用假文本**（不是她小说里的任何内容）
    4. 跑完整流程
    5. 关掉服务、删掉临时库

**她的真实数据从头到尾不会被碰。**
而且下面 verify_isolated() 那道保险一旦不通过，脚本会直接拒绝运行，
而不是硬着头皮测下去 —— 这条规矩是被"测试误删过 12 条真实素材"换来的。
"""

import http.cookiejar
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

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


# ----------------------------------------------------------------------
# 测试用的假文本
#
# 刻意做成小说划线笔记的形态（书名行 + 作者行 + 统计行 + 章节 + 划线段落），
# 但内容全部是现编的句子 —— 不引用任何真实素材里的句子。
# 里面故意放了两行一模一样的，用来测「近重复提示」。
# ----------------------------------------------------------------------

TEXT = "\n".join([
    "《测试稿》",
    "",
    "测试作者",
    "",
    "638个笔记",
    "",
    "第一章 起",
    "",
    "「你来了。」他把杯子搁在桌上。",
    "他捏碎了手里的茶杯，仍然笑着说没事。",
    "那条河在夜里像一条黑绸子，慢慢地流。",
    "他捏碎了手里的茶杯，仍然笑着说没事。",
    "",
    "第二章 承",
    "",
    "她笑起来的时候，眼睛弯成两道月牙。",
    "他本该把自己拔出来，结束这场荒唐。",
    "「别说了。」她抬手打断他。",
    "窗外下起了雨，雨点砸在瓦上，一声接着一声。",
    "那年冬天特别长，长到谁都以为不会再有春天。",
    "他站在原地，像一截被雷劈过的树，半天没动。",
    "",
    "第三章 转",
    "",
    "孙朗瞅着他笑了一声，说咦，兄台，你又怎么了。",
    "反派死于话多，但这位教主话多，也依然活蹦乱跳。",
    "黑白虾第一次见盐，眼神就一直跟着他转。",
    "开馆开到军阀自己爬起来，这事说出去谁信。",
    "",
    "第四章 合",
    "",
    "黎钺有些难堪地闭上了眼。",
    "他本该讲句笑话把这一页翻过去，却一个字也没说。",
    "雨停了，屋檐上最后一滴水落在青石板上。",
    "他把那半块玉佩塞回袖子里，转身走了。",
])

# 这份假文本的预期切分结果（按非空行切）
WANT_TOTAL = 25          # 非空行数
WANT_NOISE = 7           # 3 条头部元信息 + 4 个章节标题
WANT_CARDS = WANT_TOTAL - WANT_NOISE


# ----------------------------------------------------------------------
# 隔离：自己起一个用临时数据库的服务
# ----------------------------------------------------------------------

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_server(data_dir, port):
    env = dict(os.environ)
    env["MOGE_DATA_DIR"] = data_dir
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=PROJECT_DIR, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def wait_ready(base, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(base + "/api/health"), timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def stop_server(proc):
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass


# ----------------------------------------------------------------------
# 一个"浏览器" = 一个 Cookie 罐 = 一个账号
#
# 为什么要用类而不是全局一个 OPENER：
#   要验"另一个账号看不到你的卡片"，就必须同时有两个互不串味的登录状态。
#   浏览器里这是两个无痕窗口，测试里就是两个 CookieJar。
# ----------------------------------------------------------------------

class Client(object):
    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.jar),
        )

    def call(self, method, path, data=None):
        """发请求。返回 (状态码, 解析后的 JSON 或原始文字)。

        刻意不抛异常：401 / 404 / 400 都是**要被断言的结果**，
        不是"出错"。所以一律把状态码原样返回，交给调用方判断。
        """
        p = urllib.parse.quote(path, safe="/?=&#")
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(self.base + p, data=body, method=method)
        if body:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=120) as r:
                raw = r.read()
                code = r.status
        except urllib.error.HTTPError as e:
            raw = e.read()
            code = e.code
        try:
            return code, json.loads(raw.decode("utf-8"))
        except Exception:
            return code, raw.decode("utf-8", "replace")

    def ok(self, method, path, data=None):
        """只接受 2xx 的调用。测试里绝大多数请求属于这一类。"""
        code, data2 = self.call(method, path, data)
        if code >= 300:
            raise AssertionError("%s %s → HTTP %s %s" % (method, path, code, data2))
        return data2


# ----------------------------------------------------------------------

def main():
    data_dir = tempfile.mkdtemp(prefix="moge_cls_")
    proc = None
    try:
        port = free_port()
        base = "http://127.0.0.1:%d" % port
        print("=" * 64)
        print("墨阁 · 素材分类库 接口测试")
        print("临时库目录：", data_dir)
        print("=" * 64)

        proc = start_server(data_dir, port)
        if not wait_ready(base):
            print("服务没起来，测试中止。")
            return

        probe = Client(base)

        # ---- 保险：确认这个服务用的确实是临时库 --------------------
        print("\n【0】隔离保险")
        code, health = probe.call("GET", "/api/health")
        dbp = os.path.abspath(health.get("db", ""))
        if os.path.abspath(data_dir) not in dbp:
            print("！！服务连的不是临时库：", dbp)
            print("！！为避免动到真实数据，测试拒绝继续。")
            return
        check("服务连的是临时库（不是 data/moge.db）", True, dbp)

        # ---- 账号 -------------------------------------------------
        print("\n【1】账号与权限")
        code, _ = probe.call("GET", "/api/cards")
        check("未登录读卡片列表 → 401", code == 401, "HTTP %s" % code)
        code, _ = probe.call("GET", "/api/material-classification/sources")
        check("未登录读来源列表 → 401", code == 401, "HTTP %s" % code)
        code, _ = probe.call("POST", "/api/cards/batch-update", {"card_ids": [1]})
        check("未登录批量改卡片 → 401", code == 401, "HTTP %s" % code)

        A = Client(base)
        ua = "test_a_" + secrets.token_hex(3)
        A.ok("POST", "/api/auth/register", {"username": ua, "password": "pw123456"})
        me = A.ok("GET", "/api/auth/me")
        check("注册后能认出自己是谁",
              me.get("logged_in") is True and
              (me.get("user") or {}).get("username") == ua,
              str(me.get("user", {}).get("username")))

        # ---- 入库 -------------------------------------------------
        print("\n【2】粘贴入库")
        m = A.ok("POST", "/api/materials", {"title": "测试稿", "content": TEXT})
        mid = m["id"]
        before_content = A.ok("GET", "/api/materials/%d" % mid)["content"]
        check("入库后取回的正文与粘进去的一致",
              before_content == TEXT, "%d 字" % len(before_content))

        # ---- 选项菜单 ---------------------------------------------
        print("\n【3】主类 / 状态 / 规则清单")
        opt = A.ok("GET", "/api/categories")
        # v2 那套（她自己定的那套，换过一版了；2026-09-24 晚加了动作/打斗/环境）。
        # 数量写死在这儿是有意的：主类数目变了就该有人注意到 ——
        # 前端那个"选主类"的下拉框、以及所有按类筛的界面都跟着它走。
        check("11 个正式主类", len(opt["categories"]) == 11,
              "得到 %d" % len(opt["categories"]))
        check("新加的三类都在",
              all(n in [c["name"] for c in opt["categories"]]
                  for n in ("动作", "打斗", "环境")),
              str([c["name"] for c in opt["categories"]]))
        check("每个主类都带判据说明",
              all(c.get("description") for c in opt["categories"]))
        check("6 个状态（含自动分类新增的「分类失败」）",
              len(opt["statuses"]) == 6, str(opt["statuses"]))
        cats = {c["name"]: c["id"] for c in opt["categories"]}

        # ---- 切分预览 ---------------------------------------------
        print("\n【4】切分预览（不写任何数据）")
        pv = A.ok("GET", "/api/material-classification/materials/%d/split-preview"
                         "?limit=5" % mid)
        check("自动推荐了一种切法，并附了理由",
              pv["rule"] in ("line", "blank") and len(pv["rule_reason"]) > 10,
              "%s：%s" % (pv["rule_name"], pv["rule_reason"][:40]))
        check("两种切法的条数都算出来了（行 25 / 空行 11）",
              pv["alternatives"]["line"]["count"] == 25 and
              pv["alternatives"]["blank"]["count"] == 11,
              str({k: v["count"] for k, v in pv["alternatives"].items()}))
        check("预览报的条数与它推荐的那种切法一致",
              pv["counts"]["total"] == pv["alternatives"][pv["rule"]]["count"],
              "%d vs %d" % (pv["counts"]["total"],
                            pv["alternatives"][pv["rule"]]["count"]))
        check("预览分页只回前 5 条正文（不撑爆页面）",
              len(pv["segments"]) == 5 and
              all("text" in s for s in pv["segments"]) and
              pv["page"]["returned"] == 5,
              "整份 %d 条，本页回 %d 条" % (pv["counts"]["total"],
                                          len(pv["segments"])))
        check("预览没写数据（卡片数还是 0）",
              A.ok("GET", "/api/cards")["total"] == 0)

        # 手动改选切法 —— 规格要求"用户可以手动改选，页面要写明判断理由"
        pv_line = A.ok("GET", "/api/material-classification/materials/%d/split-preview"
                              "?limit=3&rule=line" % mid)
        check("能手动改选为按非空行切（%d 条）" % WANT_TOTAL,
              pv_line["rule"] == "line" and pv_line["counts"]["total"] == WANT_TOTAL,
              "%s / %d 条" % (pv_line["rule_name"], pv_line["counts"]["total"]))
        check("改选后理由里写清楚这是手动改的",
              "手动" in pv_line["rule_reason"], pv_line["rule_reason"][:34])
        check("按非空行切能认出 %d 条噪音" % WANT_NOISE,
              pv_line["counts"]["noise_high"] == WANT_NOISE,
              "得到 %d，明细 %s" % (pv_line["counts"]["noise_high"],
                                    pv_line["noise_summary"]))
        check("预计生成 %d 张卡片" % WANT_CARDS,
              pv_line["counts"]["will_create_cards"] == WANT_CARDS,
              "得到 %d" % pv_line["counts"]["will_create_cards"])

        # ---- 确认切分 ---------------------------------------------
        print("\n【5】确认切分")
        r = A.ok("POST", "/api/material-classification/materials/%d/segment-runs" % mid,
                 {"rule": "line"})
        check("切分成功", r.get("ok") is True, r.get("message", ""))
        ov = A.ok("GET", "/api/material-classification/overview")
        check("总览里卡片数 = %d" % WANT_CARDS, ov["cards"] == WANT_CARDS,
              "得到 %d" % ov["cards"])
        check("总览里统计了被自动排除的噪音段",
              ov.get("noise_segments") == WANT_NOISE,
              "得到 %s" % ov.get("noise_segments"))

        # ---- 卡片列表 ---------------------------------------------
        print("\n【6】卡片流")
        lst = A.ok("GET", "/api/cards?material_id=%d&limit=100" % mid)
        check("可见卡片 %d 张" % WANT_CARDS, lst["total"] == WANT_CARDS,
              "得到 %d" % lst["total"])
        check("每张卡都带正文", all(x["text"] for x in lst["items"]))
        check("每张卡都标了原文位置",
              all(x["start_offset"] < x["end_offset"] for x in lst["items"]))
        check("每张卡的正文都能对上原文（校验通过）",
              all(x["verify_ok"] for x in lst["items"]),
              "失败 %d 张" % sum(1 for x in lst["items"] if not x["verify_ok"]))
        check("新卡片默认是「待确认」",
              all(x["status"] == "待确认" for x in lst["items"]))
        check("新卡片默认没有主类",
              all(x["primary_category_id"] is None for x in lst["items"]))
        check("卡片带来源名", all(x["source_collection"] == "测试稿"
                                  for x in lst["items"]),
              str(lst["items"][0]["source_collection"]))
        check("卡片按原文顺序排列",
              [x["start_offset"] for x in lst["items"]] ==
              sorted(x["start_offset"] for x in lst["items"]))
        check("相邻卡片不重叠",
              all(lst["items"][i]["end_offset"] <= lst["items"][i + 1]["start_offset"]
                  for i in range(len(lst["items"]) - 1)))

        # ---- 卡片详情 ---------------------------------------------
        print("\n【7】卡片详情（前后文 + 原文位置）")
        c0 = lst["items"][5]
        d = A.ok("GET", "/api/cards/%d" % c0["id"])
        check("详情带正文", d["text"] == c0["text"])
        check("详情带前文", isinstance(d.get("before_text"), str))
        check("详情带后文", isinstance(d.get("after_text"), str))
        check("前文接在正文前面（能对上原文）",
              before_content[:d["start_offset"]].endswith(d["before_text"]))
        check("后文接在正文后面（能对上原文）",
              before_content[d["end_offset"]:].startswith(d["after_text"]))
        check("详情带校验结果", d["verify_ok"] is True, d.get("verify_reason", ""))

        # ---- 单张改分类 -------------------------------------------
        print("\n【8】改一张卡：主类 + 副标签 + 状态")
        r = A.ok("PATCH", "/api/cards/%d" % c0["id"],
                 {"category_id": cats["神态"], "status": "已确认",
                  "sub_tags": ["好磕", "适合冲突"]})
        check("改完返回了卡片", r["card"]["category_name"] == "神态",
              r["card"]["category_name"])
        check("副标签写进去了", sorted(r["card"]["sub_tags"]) == sorted(["好磕", "适合冲突"]),
              str(r["card"]["sub_tags"]))
        check("状态改成已确认", r["card"]["status"] == "已确认")
        check("能按副标签筛出来",
              A.ok("GET", "/api/cards?material_id=%d&sub_tag=好磕" % mid)["total"] == 1)
        check("能按主类筛出来",
              A.ok("GET", "/api/cards?material_id=%d&category_id=%d"
                   % (mid, cats["神态"]))["total"] == 1)
        check("能按「未分类」筛出来（还有 %d 张）" % (WANT_CARDS - 1),
              A.ok("GET", "/api/cards?material_id=%d&category_id=0" % mid)["total"]
              == WANT_CARDS - 1)
        check("能按状态筛出来",
              A.ok("GET", "/api/cards?material_id=%d&status=已确认" % mid)["total"] == 1)

        # ---- 各类各装了多少张（界面上那排类目按钮的数字）----------------
        print("\n【8b】类目导航条的计数")
        r = A.ok("GET", "/api/cards?material_id=%d&limit=100" % mid)
        cc = r.get("cat_counts") or {}
        check("卡片列表顺带把各类计数带回来了", isinstance(cc, dict) and bool(cc),
              str(cc)[:70])
        check("「神态」的那 1 张被数进去了", cc.get(str(cats["神态"])) == 1, str(cc))
        check("其余的都算在「未分类」格子里", cc.get("") == WANT_CARDS - 1, str(cc))
        # 这一条是它存在的理由：她点了「神态」之后，其他类的数字不能跟着变成 0，
        # 否则看起来像刚才归的类全丢了，其实只是视角被筛窄了。
        r2 = A.ok("GET", "/api/cards?material_id=%d&category_id=%d"
                  % (mid, cats["神态"]))
        check("点了某一类之后，别的类的计数照样在",
              (r2.get("cat_counts") or {}).get("") == WANT_CARDS - 1,
              str(r2.get("cat_counts")))

        # ---- 批量改 + 撤销 ----------------------------------------
        print("\n【9】批量改 3 张，再撤销")
        ids = [x["id"] for x in lst["items"][:3]]
        r = A.ok("POST", "/api/cards/batch-update",
                 {"card_ids": ids, "category_id": cats["对话台词"],
                  "sample_ids": ids[:2], "sample_result": "抽样 2 条全对"})
        ch = r["change_id"]
        check("批量改返回影响条数", r["affected"] == 3, "得到 %s" % r["affected"])
        check("3 张都改成了对话台词",
              all(A.ok("GET", "/api/cards/%d" % i)["category_name"] == "对话台词"
                  for i in ids))
        changes = A.ok("GET", "/api/card-changes")["items"]
        check("变更记录里有这一条", any(c["id"] == ch for c in changes))
        one = [c for c in changes if c["id"] == ch][0]
        check("变更记录带影响条数与抽样样本",
              one["affected_count"] == 3 and one["sample_ids"],
              "样本 %s" % one["sample_ids"])
        check("变更记录标了可撤销", one["undoable"] is True)

        # 先手动改一条（模拟"撤销之前她自己又动过一张"）
        A.ok("PATCH", "/api/cards/%d" % ids[1], {"category_id": cats["心理"]})
        r = A.ok("POST", "/api/card-changes/%d/undo" % ch)
        check("撤销时只回滚没被动过的那两张",
              r["restored"] == 2 and r["skipped"] == 1,
              "恢复 %s，跳过 %s" % (r["restored"], r["skipped"]))
        check("第 1 张退回未分类",
              A.ok("GET", "/api/cards/%d" % ids[0])["primary_category_id"] is None)
        check("第 2 张保留她后来改的「心理」",
              A.ok("GET", "/api/cards/%d" % ids[1])["category_name"] == "心理")

        # ---- 合并 -------------------------------------------------
        print("\n【10】合并相邻两张，再撤销")
        base_total = A.ok("GET", "/api/cards?material_id=%d" % mid)["total"]
        cur = A.ok("GET", "/api/cards?material_id=%d&limit=100&order=seq" % mid)["items"]
        pair = [cur[8]["id"], cur[9]["id"]]
        r = A.ok("POST", "/api/cards/merge", {"card_ids": pair})
        mg_ch = r["change_id"]
        after_merge = A.ok("GET", "/api/cards?material_id=%d" % mid)["total"]
        check("合并 2 张 → 可见数少 1", after_merge == base_total - 1,
              "%d → %d" % (base_total, after_merge))
        vis = A.ok("GET", "/api/cards?material_id=%d&limit=100" % mid)["items"]
        check("合并后可见卡片两两不重叠",
              all(vis[i]["end_offset"] <= vis[i + 1]["start_offset"]
                  for i in range(len(vis) - 1)))
        check("合并产生的新卡覆盖了两张的范围",
              any(x["start_offset"] == cur[8]["start_offset"] and
                  x["end_offset"] == cur[9]["end_offset"] for x in vis))
        r = A.ok("POST", "/api/card-changes/%d/undo" % mg_ch)
        check("撤销合并后可见数回到 %d" % base_total,
              A.ok("GET", "/api/cards?material_id=%d" % mid)["total"] == base_total)

        print("\n【11】合并中间夹着别的卡（应被拒绝）")
        cur = A.ok("GET", "/api/cards?material_id=%d&limit=100" % mid)["items"]
        code, body = A.call("POST", "/api/cards/merge",
                            {"card_ids": [cur[0]["id"], cur[3]["id"]]})
        check("不相邻的两张不能合并 → 400", code == 400,
              "HTTP %s %s" % (code, body))

        # ---- 拆分 -------------------------------------------------
        print("\n【12】拆分最长的一张，再撤销")
        big = A.ok("GET", "/api/cards?material_id=%d&order=chars&limit=1" % mid)["items"][0]
        cut = (big["start_offset"] + big["end_offset"]) // 2
        r = A.ok("POST", "/api/cards/%d/split" % big["id"], {"cuts": [cut]})
        sp_ch = r["change_id"]
        check("拆成 2 张", len(r["new_card_ids"]) == 2, str(r["new_card_ids"]))
        after_split = A.ok("GET", "/api/cards?material_id=%d" % mid)["total"]
        check("拆分 1 张 → 可见数多 1", after_split == base_total + 1,
              "%d → %d" % (base_total, after_split))
        vis = A.ok("GET", "/api/cards?material_id=%d&limit=100" % mid)["items"]
        check("拆分后可见卡片两两不重叠",
              all(vis[i]["end_offset"] <= vis[i + 1]["start_offset"]
                  for i in range(len(vis) - 1)))
        check("两张新卡首尾相接，正好覆盖原来那张",
              sum(x["end_offset"] - x["start_offset"]
                  for x in vis
                  if big["start_offset"] <= x["start_offset"] and
                  x["end_offset"] <= big["end_offset"]) == big["chars"])
        r = A.ok("POST", "/api/card-changes/%d/undo" % sp_ch)
        check("撤销拆分后可见数回到 %d" % base_total,
              A.ok("GET", "/api/cards?material_id=%d" % mid)["total"] == base_total)

        # ---- 恢复噪音段 -------------------------------------------
        print("\n【13】把被自动排除的噪音段恢复成卡片")
        pv2 = A.ok("GET", "/api/material-classification/materials/%d/split-preview"
                          "?limit=0" % mid)
        run_id = pv2.get("run_id")
        if run_id:
            r = A.ok("POST", "/api/segments/restore", {"run_id": run_id})
            check("恢复成功", r.get("ok") is True, r.get("message", ""))
            check("恢复后可见数变多",
                  A.ok("GET", "/api/cards?material_id=%d" % mid)["total"] > base_total,
                  "得到 %d" % A.ok("GET", "/api/cards?material_id=%d" % mid)["total"])
            A.ok("POST", "/api/card-changes/%d/undo" % r["change_id"])
            check("撤销恢复后回到 %d" % base_total,
                  A.ok("GET", "/api/cards?material_id=%d" % mid)["total"] == base_total)
        else:
            check("预览里带 run_id", False, "预览没给 run_id")

        # ---- 重复提示 ---------------------------------------------
        print("\n【14】近重复提示（同两行内容出现两次）")
        dup = A.ok("GET", "/api/duplicates?material_id=%d" % mid)
        check("找出了重复对", dup["total"] >= 1, "找到 %d 对" % dup["total"])
        if dup["total"]:
            check("重复对带判定原因和两条正文",
                  bool(dup["items"][0].get("kind")) and
                  bool(dup["items"][0].get("a_text")) and
                  bool(dup["items"][0].get("b_text")),
                  str(dup["items"][0].get("kind")))
        check("能按「有重复」筛选卡片",
              A.ok("GET", "/api/cards?material_id=%d&duplicate=true" % mid)["total"] >= 2)

        # ---- 换切法重切 -------------------------------------------
        print("\n【15】换成按空行切重切（force），已有卡片不能失效")
        code, body = A.call("POST",
                            "/api/material-classification/materials/%d/segment-runs" % mid,
                            {"rule": "blank"})
        check("换切法不加 force → 被拦住", body.get("ok") is False,
              str(body.get("message", ""))[:40])
        r = A.ok("POST", "/api/material-classification/materials/%d/segment-runs" % mid,
                 {"rule": "blank", "force": True})
        check("force 之后重切成功", r.get("ok") is True, r.get("message", ""))
        vis = A.ok("GET", "/api/cards?material_id=%d&limit=100" % mid)
        check("重切之后卡片一张不少", vis["total"] == base_total,
              "得到 %d" % vis["total"])
        check("重切之后每张卡仍然对得上原文",
              all(x["verify_ok"] for x in vis["items"]),
              "失败 %d 张" % sum(1 for x in vis["items"] if not x["verify_ok"]))
        check("按「校验失败」筛出来是 0 张",
              A.ok("GET", "/api/cards?material_id=%d&verify_error=true" % mid)["total"] == 0)

        # ---- 原文没有被改过 ---------------------------------------
        print("\n【16】原文一个字都没被改")
        after_content = A.ok("GET", "/api/materials/%d" % mid)["content"]
        check("跑完整套流程后，原文仍与入库时逐字相同",
              after_content == before_content,
              "%d 字" % len(after_content))

        # ---- 账号隔离 ---------------------------------------------
        print("\n【17】另一个账号看不到、也改不了")
        B = Client(base)
        ub = "test_b_" + secrets.token_hex(3)
        B.ok("POST", "/api/auth/register", {"username": ub, "password": "pw123456"})
        check("B 的卡片列表是空的", B.ok("GET", "/api/cards")["total"] == 0)
        check("B 的来源列表是空的",
              B.ok("GET", "/api/material-classification/sources")["items"] == [])
        code, _ = B.call("GET", "/api/cards/%d" % c0["id"])
        check("B 读 A 的卡片 → 404", code == 404, "HTTP %s" % code)
        code, _ = B.call("PATCH", "/api/cards/%d" % c0["id"],
                         {"category_id": cats["心理"]})
        check("B 改 A 的卡片 → 404", code == 404, "HTTP %s" % code)
        code, _ = B.call("POST",
                         "/api/material-classification/materials/%d/segment-runs" % mid, {})
        check("B 切 A 的文件 → 404", code == 404, "HTTP %s" % code)
        code, _ = B.call("GET",
                         "/api/material-classification/materials/%d/split-preview" % mid)
        check("B 预览 A 的文件 → 404", code == 404, "HTTP %s" % code)
        code, _ = B.call("POST", "/api/cards/batch-update",
                         {"card_ids": [c0["id"]], "category_id": cats["心理"]})
        check("B 批量改 A 的卡片 → 404", code == 404, "HTTP %s" % code)
        check("A 的卡片没被 B 改过",
              A.ok("GET", "/api/cards/%d" % c0["id"])["category_name"] == "神态")

        # ---- 来源映射 ---------------------------------------------
        print("\n【18】来源映射表")
        sm = A.ok("GET", "/api/source-mappings")["items"]
        check("映射表里有这份来源", any(x["source_collection"] == "测试稿" for x in sm))
        r = A.ok("PATCH", "/api/source-mappings",
                 {"source_collection": "测试稿",
                  "category_id": cats["情节"], "confirmed": True})
        check("能改映射建议", r.get("ok") is True, str(r))
        r = A.ok("GET", "/api/source-mappings/sample?source_collection=测试稿&size=8")
        check("抽样能取到样本", len(r["items"]) >= 1, "抽出 %d 条" % len(r["items"]))
        check("抽样不是纯随机（带构成说明）",
              bool(r.get("composition")), str(r.get("composition")))

        # ---- 副标签维护 -------------------------------------------
        print("\n【19】副标签的新增 / 停用 / 合并")
        r = A.ok("POST", "/api/sub-tags", {"name": "测试标签", "action": "add"})
        check("能新增副标签", r.get("ok") is True, str(r.get("message", "")))
        names = [t["name"] for t in A.ok("GET", "/api/categories")["sub_tags"]]
        check("新标签出现在可选清单里", "测试标签" in names, str(names[-3:]))
        r = A.ok("POST", "/api/sub-tags", {"name": "测试标签", "action": "deactivate"})
        check("能停用副标签", r.get("ok") is True, str(r.get("message", "")))
        names = [t["name"] for t in A.ok("GET", "/api/categories")["sub_tags"]]
        check("停用后默认不出现在可选标签里", "测试标签" not in names, str(names[-3:]))
        r = A.ok("POST", "/api/sub-tags",
                 {"name": "好磕", "action": "rename", "new_name": "好嗑"})
        check("能改标签名", r.get("ok") is True, str(r.get("message", "")))
        tags_now = A.ok("GET", "/api/cards/%d" % c0["id"])["sub_tags"]
        check("改名后旧卡片上挂的标签也认得出新名字",
              "好嗑" in tags_now and "好磕" not in tags_now, str(tags_now))

    finally:
        stop_server(proc)
        shutil.rmtree(data_dir, ignore_errors=True)

    print("\n" + "=" * 64)
    print("通过 %d 项，失败 %d 项" % (OK, FAIL))
    print("=" * 64)
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
