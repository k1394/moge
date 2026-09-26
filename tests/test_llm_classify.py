# -*- coding: utf-8 -*-
"""
墨阁 · 大模型分类（全程不联网）
========================================================

测的是「接上真模型之后」那条路，但**一次都不真发请求** ——
把 llm.chat 换成一个假的，让它按剧本回话。

为什么这么做：
    · 真发请求就得有 API Key，测试不该依赖这个
    · 真发请求要花钱、要等，跑一次几十秒
    · 关键逻辑（提示词拼装、返回解析、校验、落库）跟"谁来回答"无关

真模型的行为（限流、超时、各家报错格式不一样）没法在这儿测全，
那些靠 llm.py 自己的自测，加上第一次真跑时盯着看。

--------------------------------------------------------
隔离（本项目的铁律）
--------------------------------------------------------
    开头就把 MOGE_DATA_DIR 指到临时目录，并且**核对一遍确实进去了**。
    会写卡片的测试必须隔离 —— 跑测试把真实素材搞坏过一次，
    不能再有第二次。
--------------------------------------------------------
"""

import json
import os
import re
import shutil
import sys
import tempfile

# ---- 铁律一：先把数据目录指到临时目录，再 import 项目模块 ----
# 为什么顺序不能反：db.py 在 import 的那一刻就把 DATA_DIR 定死了，
# import 之后再改环境变量是没用的。
_TMP = tempfile.mkdtemp(prefix="moge_llmtest_")
os.environ["MOGE_DATA_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import db                                          # noqa: E402
from backend import classification as auto                      # noqa: E402
from backend import classify_db as cls                          # noqa: E402
from backend import llm                                         # noqa: E402
from backend import segmentation as sg                          # noqa: E402

# ---- 铁律二：核对隔离真的生效了，不对就立刻停 ----
if os.path.abspath(db.DATA_DIR) != os.path.abspath(_TMP):
    print("！！数据目录没隔离成功（%s），拒绝运行" % db.DATA_DIR)
    sys.exit(1)


PASS = 0
FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("  [v] %s" % name)
    else:
        FAIL += 1
        print("  [x] %s\n        得到：%r\n        期望：%r" % (name, got, want))


# 5 行 → 按行切 → 5 张卡
TEXT = "\n".join([
    "那少年生得极冷，一双眼睛像结了冰，偏偏嘴角又有一点软。",
    "他手里捏着杯子，指节发白，半晌没说话。",
    "耿苍怀却道：“当年东京上元节的烟火，想来比这要远胜了。”",
    "他心里想，这一趟本不该来。",
    "两人对视一眼，谁也没先开口。",
])
LINES = 5


def main():
    global PASS, FAIL

    print("数据目录：%s" % db.DATA_DIR)
    print("=" * 66)

    # ------------------------------------------------------------------
    # 准备：建表、建素材、切分
    # ------------------------------------------------------------------
    db.init_db()
    cls.migrate()
    auto.migrate()

    owner = db.owner_of(1)
    other = db.owner_of(2)
    mid = db.save_material("大模型测试稿", TEXT, owner=owner)["id"]
    other_mid = db.save_material("别人的稿子甲", TEXT + "换个字", owner=other)["id"]

    # 再 migrate 一次：这会儿库里才有 materials，
    # 副标签的播种是"按 materials 里出现过的 owner 走的"（见 classify_db._seed_owner）
    cls.migrate()
    auto.migrate()

    cls.apply_split(mid, owner)
    cls.apply_split(other_mid, other)

    with db.connect() as conn:
        n_cards = conn.execute("SELECT COUNT(*) FROM cards WHERE material_id=?",
                               (mid,)).fetchone()[0]
    check("切出 %d 张卡片" % LINES, n_cards, LINES)

    cats = cls.list_categories()
    check("当前是 v2 那 11 个主类", len(cats), 11)
    check("主类名是她的那套",
          [c["name"] for c in cats],
          ["外貌", "神态", "动作", "打斗", "环境", "梗", "暧昧拉扯",
           "心理", "搞笑情节", "对话台词", "情节"])

    # ------------------------------------------------------------------
    print("\n【A】提示词：她的 11 类判据有没有真的发出去")
    # ------------------------------------------------------------------
    # 素材名一律用假的。她的真实稿件名不能出现在 tests/ 里 ——
    # tests/ 在同步白名单里，写在这里等于把稿名送到公开仓库去
    #（推送前的私人词检查会拦下来，但更该一开始就不写）。
    FAKE_TITLE = "测试素材甲"
    msgs, _src, _warn = auto.build_messages(cats, ["好磕", "吻"], FAKE_TITLE,
                                            [{"card_id": 3, "text": "随便一句"}])
    sysmsg = msgs[0]["content"]
    usermsg = msgs[1]["content"]

    check("11 个主类名都在", all(c["name"] in sysmsg for c in cats), True)
    check("判据原文也发了（不是光发类名）",
          all((c["description"] or "")[:16] in sysmsg for c in cats), True)
    check("「神态 / 心理」的分界那句话发过去了",
          "神态看得见" in sysmsg, True)
    check("建议副标签发过去了", "直接描写" in sysmsg, True)
    check("副标签清单发过去了", "好磕" in sysmsg, True)
    check("明确要求只输出 JSON 对象", "只输出一个 JSON 对象" in sysmsg, True)
    check("同时要它给出该合看的组（逻辑素材组）", "groups" in sysmsg, True)
    check("明确说了判不出填 null", "null" in sysmsg, True)
    check("明确说了编号要原样照抄", "原样照抄" in sysmsg, True)
    check("要求理由具体（不许写空话）", "空话" in sysmsg, True)
    check("user 部分每条都带编号", "[3]" in usermsg, True)
    check("user 部分带了素材名当背景", FAKE_TITLE in usermsg, True)
    check("占位符都被换掉了（没有 {categories} 这种残留）",
          not any(s in sysmsg for s in
                  ("{categories}", "{sub_tags}", "{user_prompt}")), True)

    # ------------------------------------------------------------------
    print("\n【A2】提示词是从文件读的，代码里不许藏真提示词")
    # ------------------------------------------------------------------
    tpl, tsrc, twarn = auto.prompt_template()
    check("能拿到模板，来源是 file 或 builtin",
          tsrc in ("file", "builtin"), True)
    check("模板里有必需的占位符",
          all(s in tpl for s in auto.REQUIRED_SLOTS), True)
    check("读文件成功时不该有警告",
          (tsrc != "file") or not twarn, True)
    # 这条是「开源边界」的自动化防线：
    # 代码里的通用模板必须是**通用**的，不能是她调好的那版。
    # 判据：通用模板里不该出现她那份文件才有的强调句。
    gen = auto.GENERIC_CLASSIFY_PROMPT
    check("代码里的通用模板不含调好版特有的强调句",
          "这个工作对她很重要" not in gen, True)
    if tsrc == "file":
        check("文件那版和代码里的通用版确实不是同一份",
              tpl.strip() != gen.strip(), True)

    # 模板被写坏 → 必须退回通用版并报警（静默丢类目清单最难查）
    bad_path = os.path.join(auto.ROOT_DIR, "prompts", "_test_bad_tpl.txt")
    _saved = auto.PROMPT_FILE
    try:
        with open(bad_path, "w", encoding="utf-8") as f:
            f.write("我少写了占位符")
        auto.PROMPT_FILE = "_test_bad_tpl.txt"
        t2, s2, w2 = auto.prompt_template()
        check("模板缺占位符 → 退回通用版", s2, "builtin")
        check("退回时给出警告，不静默", "占位符" in (w2 or ""), True)
        check("退回的那份确实是通用模板", t2.strip(), gen.strip())
    finally:
        auto.PROMPT_FILE = _saved
        if os.path.isfile(bad_path):
            os.remove(bad_path)

    # 文件不存在 → 用通用版，且不该报错
    try:
        auto.PROMPT_FILE = "_test_no_such_file.txt"
        t3, s3, w3 = auto.prompt_template()
        check("文件不存在 → 用通用版", s3, "builtin")
        check("文件不存在不算错误（不报警告）", w3, "")
    finally:
        auto.PROMPT_FILE = _saved

    # ------------------------------------------------------------------
    print("\n【A3】补充提示词：写了就发出去，没写就不占地方")
    # ------------------------------------------------------------------
    MINE = "带引号的短句优先算对话台词"
    m2, _s, _w = auto.build_messages(cats, [], "", 
                                     [{"card_id": 1, "text": "x"}],
                                     user_prompt=MINE)
    sys2 = m2[0]["content"]
    check("补充提示词进提示词了", MINE in sys2, True)
    check("它被标成「作者本人的补充要求」", "作者本人的补充要求" in sys2, True)
    check("它被明确限定不能推翻硬规则", "不能推翻" in sys2, True)
    check("没写补充提示词时不留空标题",
          "作者本人的补充要求" not in auto.build_messages(
              cats, [], "", [{"card_id": 1, "text": "x"}])[0][0]["content"], True)

    check("补充提示词上限是 5000 字", auto.USER_PROMPT_MAX, 5000)
    try:
        auto.set_user_prompt(owner, "字" * 5001)
        check("超 5000 字要被拒绝", False, "居然存进去了")
    except ValueError as e:
        check("超 5000 字被拒绝，且说清超了多少", "超了 1 字" in str(e), True)
    auto.set_user_prompt(owner, MINE)
    check("存进去的能读回来", auto.get_user_prompt(owner), MINE)
    auto.set_user_prompt(owner, "")
    check("清空之后就没了", auto.get_user_prompt(owner), "")

    # ------------------------------------------------------------------
    print("\n【A4】提示词库：能攒、能挑、**公开但内容私密**")
    # ------------------------------------------------------------------
    # 这一段的重点是最后一条。她的规矩是反直觉的：
    #   公开 = 别人可以拿它去跑分类，但**永远看不到里面写了什么**。
    # 所以这里不验"某个字段在不在"，而是验**整个响应体里搜不到原文** ——
    # 只要内容从任何一条缝里漏出去，那一条断言就会红。
    SECRET = "SECRET-LIB-CONTENT-99887"

    # 造一个"别人"。
    # 注意 owner 是 db.owner_of(1)（见上面 main 的开头），
    # 所以得先占住 id=1，再建第二个账号才拿得到**不同**的归属 ——
    # 第一次写这段时没占住，结果"别人"跟自己是同一个 owner，
    # "改不了别人的条目"那条就成了假绿。
    db.create_user("测试甲账号", "x", "y")            # id=1
    u2 = db.create_user("测试乙账号", "x", "y")        # id=2
    owner2 = db.owner_of(u2["id"])
    check("两个账号的归属确实不一样（不然这段测试全是白测）",
          owner2 == owner, False)

    check("上限：名称 30 / 使用方法 50 / 介绍 6000",
          (auto.PROMPT_NAME_MAX, auto.PROMPT_USAGE_MAX,
           auto.PROMPT_SUMMARY_MAX), (30, 50, 6000))
    check("正文上限跟补充提示词一致（都得每批重发）",
          auto.PROMPT_CONTENT_MAX, auto.USER_PROMPT_MAX)

    mine_row = auto.create_library_prompt(
        owner, "我的私货", "只有我能看", visibility=auto.VIS_PRIVATE)
    pub_row = auto.create_library_prompt(
        owner2, "别人公开的", SECRET, usage_note="直接使用",
        visibility=auto.VIS_PUBLIC)
    check("默认可见性是私有",
          auto.create_library_prompt(owner, "默认私有", "x")["visibility"],
          auto.VIS_PRIVATE)

    mylist = auto.list_my_library_prompts(owner)
    check("我的清单里只有我自己的",
          sorted(x["name"] for x in mylist), ["我的私货", "默认私有"])
    check("我的清单里**带内容**（界面要能编辑）",
          all("content" in x for x in mylist), True)

    publist = auto.list_public_library_prompts(owner)
    check("公开清单里没有我自己的条目",
          all(x["name"] != "我的私货" for x in publist), True)
    check("公开清单里能看到别人公开的那条",
          any(x["name"] == "别人公开的" for x in publist), True)
    check("★ 公开清单的每一条都**不带 content 字段**",
          all("content" not in x for x in publist), True)
    check("★ 公开清单整个 json 里搜不到原文",
          SECRET not in json.dumps(publist, ensure_ascii=False), True)
    check("但给了字数（字数不算内容）",
          [x["content_length"] for x in publist
           if x["name"] == "别人公开的"], [len(SECRET)])
    check("也给了作者名",
          [x["owner_label"] for x in publist
           if x["name"] == "别人公开的"], ["测试乙账号"])
    check("匿名归属显示成「未归属」而不是空白",
          auto._owner_label("local"), "未归属")
    check("查不到的账号原样返回，不瞎猜",
          auto._owner_label("__u999999"), "__u999999")

    # ---- 用：我自己的 + 别人公开的 ----
    _rid, name, content, _ro, is_mine = auto.resolve_prompt_for_use(
        owner, mine_row["id"])
    check("取自己的条目：内容、归属都对",
          (name, content, is_mine), ("我的私货", "只有我能看", True))
    _rid, name, content, _ro, is_mine = auto.resolve_prompt_for_use(
        owner, pub_row["id"])
    check("★ 取别人公开的条目：内容能拿到（要用它去发请求）",
          (content, is_mine), (SECRET, False))
    try:
        auto.resolve_prompt_for_use(owner2, mine_row["id"])
        check("★ 别人取我私有的条目 → 必须拒绝", "居然拿到了", "应该被拒")
    except ValueError as e:
        check("★ 别人取我私有的条目 → 拒绝，且说清是私有",
              "私有" in str(e), True)
    try:
        auto.resolve_prompt_for_use(owner, 999999)
        check("取不存在的条目 → 拒绝", "居然拿到了", "应该被拒")
    except ValueError as e:
        check("取不存在的条目 → 拒绝", "不存在" in str(e), True)

    # ---- 改 / 删 ----
    check("只传 name 时正文不动",
          auto.update_library_prompt(owner, mine_row["id"],
                                     {"name": "改名了"})["content"],
          "只有我能看")
    try:
        auto.create_library_prompt(owner, "改名了", "撞名")
        check("同名不许覆盖", "居然建成功了", "应该被拒")
    except ValueError as e:
        check("同名不许覆盖，且提示怎么改", "换个名字" in str(e), True)
    check("改不了别人的条目（返回 None = 不是我的一律不动）",
          auto.update_library_prompt(owner, pub_row["id"], {"name": "我改"}),
          None)
    check("删不了别人的条目",
          auto.delete_library_prompt(owner, pub_row["id"]), False)
    check("删自己的能删掉",
          auto.delete_library_prompt(owner, mine_row["id"]), True)
    check("删掉之后我的清单里没了",
          all(x["id"] != mine_row["id"]
              for x in auto.list_my_library_prompts(owner)), True)
    try:
        auto.resolve_prompt_for_use(owner, mine_row["id"])
        check("删掉之后取它 → 拒绝（不是静默返回空内容）",
              "居然拿到了", "应该被拒")
    except ValueError:
        check("删掉之后取它 → 拒绝（不是静默返回空内容）", True, True)

    # 删掉之后同名能重建：软删的老条目会先把自己的名字让开。
    # 不让开的话 UNIQUE(owner_id,kind,name) 会一直挡着，
    # 她"删了想重写一遍"就会莫名其妙报"已经有一条叫这个名字的了"。
    again = auto.create_library_prompt(owner, "改名了", "重写一遍")
    check("删掉之后同名能重建（老条目让开了 UNIQUE 位）",
          (again["name"], again["content"]), ("改名了", "重写一遍"))

    # 历史记录查得到名字：软删之后老条目还在库里
    with db.connect() as conn:
        gone = conn.execute(
            "SELECT name, active FROM prompt_library WHERE id=?",
            (mine_row["id"],)).fetchone()
    check("软删（active=0）而不是物理删 —— 历史任务还认得出它",
          gone is not None and gone["active"] == 0, True)

    # ------------------------------------------------------------------
    print("\n【B】模型返回的各种脏格式都要能读")
    # ------------------------------------------------------------------
    good = ('[{"card_id": 1, "primary_category": "外貌", "tags": ["直接描写"], '
            '"reason": "写的是长相", "confidence": 0.9}]')
    check("纯 JSON 数组", len(auto._extract_json_array(good)), 1)
    check("```json 围栏",
          len(auto._extract_json_array("```json\n" + good + "\n```")), 1)
    check("前面有客套话",
          len(auto._extract_json_array("好的，结果如下：\n" + good)), 1)
    check("末尾有补充说明",
          len(auto._extract_json_array(good + "\n以上共 1 条。")), 1)
    check("对象包着数组（items 键）",
          len(auto._extract_json_array('{"items": %s}' % good)), 1)
    check("对象包着数组（results 键）",
          len(auto._extract_json_array('{"results": %s}' % good)), 1)
    check("只给了一个对象（没包数组）",
          len(auto._extract_json_array('{"card_id": 1, "primary_category": "外貌"}')), 1)

    raised = False
    try:
        auto._extract_json_array("模型今天不想说话，啥也没给")
    except auto.SuggestionError:
        raised = True
    check("完全不是 JSON → 报错（不静默返回空）", raised, True)

    # ------------------------------------------------------------------
    print("\n【C】没配 Key 的时候，建任务当场就该失败")
    # ------------------------------------------------------------------
    check("清单里有 5 个预置模型", len(llm.load_models()) >= 5, True)
    check("预置模型一个都没带密钥",
          any((m.get("api_key") or "").strip() for m in llm.load_models()), False)

    raised, msg = False, ""
    try:
        auto.create_run(owner, mid, classifier_name="llm", background=False)
    except ValueError as e:
        raised, msg = True, str(e)
    check("没配 Key → 建任务就被拒（不是跑起来再失败）", raised, True)
    check("并且说清了去哪儿填", "模型设置" in msg, True)

    raised = False
    try:
        auto.create_run(owner, mid, classifier_name="不存在的分类器",
                        background=False)
    except ValueError as e:
        raised = "没有这个分类器" in str(e)
    check("分类器名字写错也当场报", raised, True)

    # ------------------------------------------------------------------
    print("\n【D】密钥安全：打码 + 报错里不留密钥")
    # ------------------------------------------------------------------
    check("长密钥留头留尾", llm.mask_key("sk-1234567890abcdef"), "sk-123****cdef")
    check("短密钥全打码", llm.mask_key("abc"), "***")
    k = "sk-supersecret-abcdefghij"
    check("上游报错里如果带了密钥，会被抹掉",
          k in llm._scrub("Authorization: Bearer " + k + " 无效", k), False)

    # 配一个假模型（真请求会被 mock 掉，不会真的发出去）
    # 假密钥里掺字母、不留长数字串 —— 否则推送前的安全检查会把它
    # 当成银行卡号报警（那是误报，但不如一开始就不长得像）。
    FAKE_KEY = "sk-fake-0000aaaa1111bbbb"
    llm.upsert_model({"key": "fake", "label": "假模型（测试用）",
                      "base_url": "http://127.0.0.1:9/v1", "model": "fake-1",
                      "api_key": FAKE_KEY, "enabled": True})
    pub = llm.public_models()
    fake_pub = [m for m in pub if m["key"] == "fake"][0]
    check("给界面的清单里没有明文密钥", "api_key" in fake_pub, False)
    check("给界面的是打码版", fake_pub["api_key_masked"], "sk-fak****bbbb")
    check("打码后仍能看出填过 Key", fake_pub["has_key"], True)

    # 回传打码版（或空）不能把密钥清掉。
    # 注意这里其他字段要一起带上 —— 界面上就是整条传回来的，
    # 只有 api_key 是例外（界面拿到的是打码版，传空 = "不改钥匙"）。
    llm.upsert_model({"key": "fake", "label": "改了名字",
                      "base_url": "http://127.0.0.1:9/v1", "model": "fake-1",
                      "api_key": "", "enabled": True, "note": "测试用"})
    check("只改名字、密钥回传空 → 密钥还在",
          bool(llm.get_model("fake")["api_key"]), True)
    check("名字确实改了", llm.get_model("fake")["label"], "改了名字")

    # ------------------------------------------------------------------
    print("\n【E】真链路（模型是假的）：主类 + 副标签一起落库")
    # ------------------------------------------------------------------
    calls = []

    def ids_in(messages):
        return [int(x) for x in re.findall(r"^\[(\d+)\]",
                                          messages[-1]["content"], re.M)]

    # 剧本：card_id → 判定。没写进剧本的，一律"判不出"。
    script = {}
    reply_mode = {"text": None}      # 非 None 时直接按这个文本回（用来测脏格式）

    def fake_chat(cfg, messages, temperature=0.0, json_mode=False, timeout=None):
        calls.append({"model": cfg.get("model"), "json_mode": json_mode,
                      "messages": messages})
        if reply_mode["text"] is not None:
            return {"content": reply_mode["text"], "usage": {}, "model": cfg.get("model")}
        out = []
        for cid in ids_in(messages):
            r = script.get(cid)
            if r is None:
                out.append({"card_id": cid, "primary_category": None, "tags": [],
                            "reason": "这一条我判不出来", "confidence": None})
            else:
                r = dict(r)
                r["card_id"] = cid
                out.append(r)
        return {"content": json.dumps(out, ensure_ascii=False),
                "usage": {"total_tokens": 321}, "model": cfg.get("model")}

    real_chat = llm.chat
    llm.chat = fake_chat
    try:
        # 取出前两张卡的 id 用来写剧本
        with db.connect() as conn:
            card_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM cards WHERE material_id=? ORDER BY start_offset",
                (mid,)).fetchall()]

        script[card_ids[0]] = {"primary_category": "外貌",
                               "tags": ["直接描写", "气质"],
                               "reason": "写的是长相和气质", "confidence": 0.92}
        script[card_ids[1]] = {"primary_category": "神态",
                               "tags": ["心动"],
                               "reason": "捏杯子、指节发白，是外面看得见的反应",
                               "confidence": 0.81}
        script[card_ids[2]] = {"primary_category": "对话台词", "tags": [],
                               "reason": "有说话人和引号", "confidence": 0.95}

        res, run_id = auto.create_run(
            owner, mid, classifier_name="llm", model_key="fake",
            background=False)

        check("任务建起来了", res.get("ok"), True)
        check("返回里说了用的是哪个模型", res.get("model_key"), "fake")
        check("返回里说了模型版本", res.get("model_version"), "fake-1")

        run = auto.get_run(run_id, owner)
        check("任务记录：方法记成 llm", run["classifier"], "llm")
        check("任务记录：模型 key 记下来了", run["model_key"], "fake")
        check("任务记录：模型名记下来了", run["model_version"], "fake-1")
        check("任务记录：提示词版本记下来了", run["prompt_version"], auto.PROMPT_VERSION)
        check("任务状态是已完成", run["status"], auto.RUN_COMPLETED)

        with db.connect() as conn:
            def card(cid):
                return conn.execute("SELECT * FROM cards WHERE id=?",
                                    (cid,)).fetchone()

            c0 = card(card_ids[0])
            cat_id = {c["name"]: c["id"] for c in cats}
            check("第 1 张：主类写对了", c0["primary_category_id"], cat_id["外貌"])
            check("第 1 张：来源标成 ai", c0["source"], sg.SOURCE_AI)
            check("第 1 张：状态是待确认（不是已确认）", c0["status"], sg.STATUS_PENDING)
            check("第 1 张：置信度存下来了", c0["ai_confidence"], 0.92)
            check("第 1 张：理由存下来了", "长相和气质" in c0["ai_reason"], True)

            tags = [r["name"] for r in conn.execute(
                "SELECT t.name FROM card_tags ct JOIN sub_tags t ON t.id=ct.sub_tag_id"
                " WHERE ct.card_id=? ORDER BY t.name", (card_ids[0],)).fetchall()]
            check("第 1 张：两个副标签都挂上了", tags, ["气质", "直接描写"])
            ct = conn.execute("SELECT * FROM card_tags WHERE card_id=?",
                              (card_ids[0],)).fetchone()
            check("副标签来源是 ai", ct["source"], sg.SOURCE_AI)
            check("副标签还没被确认", ct["confirmed"], 0)

            c1 = card(card_ids[1])
            check("第 2 张：主类是神态", c1["primary_category_id"], cat_id["神态"])

            # 剧本里没写的两张 → 模型说判不出
            c3 = card(card_ids[3])
            check("判不出的那张：主类还是空的", c3["primary_category_id"], None)
            check("判不出的那张：状态是「分类失败」（不是待确认）",
                  c3["status"], sg.STATUS_FAILED)

            n_judge = conn.execute(
                "SELECT COUNT(*) FROM ai_judgements WHERE classification_run_id=?",
                (run_id,)).fetchone()[0]
            check("AI 判断记录写了 %d 条" % LINES, n_judge, LINES)
            j = conn.execute("SELECT * FROM ai_judgements WHERE card_id=?",
                             (card_ids[0],)).fetchone()
            check("判断记录里记的是真模型名（不是占位规则的号）",
                  j["model_version"], "fake-1")
            check("判断记录里存了它当时给的副标签",
                  "直接描写" in j["suggested_tags"], True)

        check("请求确实发出去了（发了 1 批）", len(calls), 1)
        check("请求里带的是「原样照抄编号」的那套提示词",
              "只输出一个 JSON 对象" in calls[0]["messages"][0]["content"], True)
        check("默认开着 json_mode", calls[0]["json_mode"], True)

        # ------------------------------------------------------------------
        print("\n【F】AI 乱说话的时候：整批拒收，卡片标成分类失败")
        # ------------------------------------------------------------------
        # 先清空一次，让所有卡回到可处理的起点
        with db.connect() as conn:
            conn.execute("UPDATE cards SET primary_category_id=NULL, source=?,"
                         " status=? WHERE material_id=? AND source<>?",
                         (sg.SOURCE_INHERITED, sg.STATUS_PENDING, mid, sg.SOURCE_HUMAN))

        # F1：主类不存在
        calls.clear()
        reply_mode["text"] = json.dumps(
            [{"card_id": card_ids[0], "primary_category": "乱七八糟类",
              "tags": [], "reason": "x", "confidence": 0.9}], ensure_ascii=False)
        res, run_id = auto.create_run(owner, mid, classifier_name="llm",
                                      model_key="fake", background=False)
        run = auto.get_run(run_id, owner)
        with db.connect() as conn:
            c0 = conn.execute("SELECT * FROM cards WHERE id=?",
                              (card_ids[0],)).fetchone()
        check("编出不存在的类 → 卡片没被写脏", c0["primary_category_id"], None)
        check("编出不存在的类 → 标成「分类失败」", c0["status"], sg.STATUS_FAILED)
        # ↓ 这三条在 2026-09-24 变了。
        #   以前"不认识的主类名"会让整批拒收、任务记成失败；
        #   现在只把这一条降级成"判不出"，任务照常算完成。
        #   理由见 classification._validate_suggestions 的 docstring：
        #   模型偶尔编个类名是常态（它可能把「外貌」写成「外貌描写」），
        #   为一条小毛病把另外 19 条对的也扔掉，代价太大 —— 重跑还得再花钱。
        #   而且这条以前在真实数据上炸过：占位规则还在输出旧体系的类名，
        #   于是整份素材 0/15 全失败，看着像功能坏了。
        # 这里想知道的是"这条不认识的类名有没有把整批拖垮"。
        # 这个假模型只回了 1 条、另外 4 条漏了，所以任务状态是「部分完成」
        # 而不是「全部完成」—— 那是"漏回复"的正常结果，与类名无关。
        check("编出不存在的类 → 没有走「整批拒收」",
              "格式不合规" in (run["error"] or ""), False)
        check("任务状态是「部分完成」，不是「失败」",
              run["status"], auto.RUN_PARTIAL)
        _note = run["note"] if "note" in run.keys() else ""
        check("任务备注里点名了那个类名（她得看得见）",
              "乱七八糟类" in (_note or ""), True)
        check("卡片理由里写了「类名不认」",
              "类名不认" in (c0["ai_reason"] or ""), True)

        # F2：把正文一起返回（这是任务书里明令禁止的）
        with db.connect() as conn:
            conn.execute("UPDATE cards SET primary_category_id=NULL, source=?,"
                         " status=? WHERE material_id=? AND source<>?",
                         (sg.SOURCE_INHERITED, sg.STATUS_PENDING, mid, sg.SOURCE_HUMAN))
        reply_mode["text"] = json.dumps(
            [{"card_id": card_ids[0], "primary_category": "外貌", "tags": [],
              "reason": "x", "confidence": 0.9, "text": "我顺手把原文改了一遍"}],
            ensure_ascii=False)
        res, run_id = auto.create_run(owner, mid, classifier_name="llm",
                                      model_key="fake", background=False)
        run = auto.get_run(run_id, owner)
        with db.connect() as conn:
            c0 = conn.execute("SELECT * FROM cards WHERE id=?",
                              (card_ids[0],)).fetchone()
        check("返回里带正文 → 整批拒收，一个字没写", c0["primary_category_id"], None)
        check("返回里带正文 → 失败原因直说了带正文",
              "带了正文" in (run["error"] or ""), True)

        # F3：返回了别人的卡片 id
        with db.connect() as conn:
            conn.execute("UPDATE cards SET primary_category_id=NULL, source=?,"
                         " status=? WHERE material_id=? AND source<>?",
                         (sg.SOURCE_INHERITED, sg.STATUS_PENDING, mid, sg.SOURCE_HUMAN))
            other_card = conn.execute(
                "SELECT id FROM cards WHERE material_id=? LIMIT 1",
                (other_mid,)).fetchone()["id"]
        reply_mode["text"] = json.dumps(
            [{"card_id": other_card, "primary_category": "外貌", "tags": [],
              "reason": "x", "confidence": 0.9}], ensure_ascii=False)
        res, run_id = auto.create_run(owner, mid, classifier_name="llm",
                                      model_key="fake", background=False)
        run = auto.get_run(run_id, owner)
        check("返回别人卡的 id → 整批拒收",
              "没让它判的 card_id" in (run["error"] or ""), True)
        with db.connect() as conn:
            still = conn.execute("SELECT primary_category_id FROM cards WHERE id=?",
                                 (other_card,)).fetchone()[0]
        check("别人的卡一个字没动", still, None)

        # F4：模型返回一坨不是 JSON 的东西
        with db.connect() as conn:
            conn.execute("UPDATE cards SET primary_category_id=NULL, source=?,"
                         " status=? WHERE material_id=? AND source<>?",
                         (sg.SOURCE_INHERITED, sg.STATUS_PENDING, mid, sg.SOURCE_HUMAN))
        reply_mode["text"] = "我觉得这些素材都挺好的，你说呢？"
        res, run_id = auto.create_run(owner, mid, classifier_name="llm",
                                      model_key="fake", background=False)
        run = auto.get_run(run_id, owner)
        check("模型胡言乱语 → 任务记成失败，不是静默成功",
              run["status"], auto.RUN_FAILED)
        check("失败原因里带上了它说的话",
              "都挺好的" in (run["error"] or ""), True)

        # ------------------------------------------------------------------
        print("\n【G】补一批：判断记录和副标签的分工")
        # ------------------------------------------------------------------
        reply_mode["text"] = None
        with db.connect() as conn:
            conn.execute("UPDATE cards SET primary_category_id=NULL, source=?,"
                         " status=? WHERE material_id=? AND source<>?",
                         (sg.SOURCE_INHERITED, sg.STATUS_PENDING, mid, sg.SOURCE_HUMAN))
            conn.execute("DELETE FROM card_tags WHERE card_id IN"
                         " (SELECT id FROM cards WHERE material_id=?)", (mid,))

        # 先手工给这张卡挂一个人工标签，验证自动分类不会把它抹掉
        manual_tag = None
        with db.connect() as conn:
            row = conn.execute("SELECT id FROM sub_tags WHERE owner_id=? AND name=?",
                               (owner, "好磕")).fetchone()
            manual_tag = row["id"]
            conn.execute("INSERT OR IGNORE INTO card_tags"
                         " (card_id, sub_tag_id, source, confirmed) VALUES (?,?,?,1)",
                         (card_ids[0], manual_tag, sg.SOURCE_HUMAN))

        script[card_ids[0]] = {"primary_category": "外貌", "tags": ["直接描写"],
                               "reason": "长相", "confidence": 0.9}
        script[card_ids[1]] = {"primary_category": None, "tags": [],
                               "reason": "拿不准", "confidence": None}
        script[card_ids[2]] = {"primary_category": None, "tags": [],
                               "reason": "拿不准", "confidence": None}
        script[card_ids[3]] = {"primary_category": None, "tags": [],
                               "reason": "拿不准", "confidence": None}
        script[card_ids[4]] = {"primary_category": "梗", "tags": ["不存在的标签"],
                               "reason": "玩梗", "confidence": 0.7}

        res, run_id = auto.create_run(owner, mid, classifier_name="llm",
                                      model_key="fake", background=False)
        with db.connect() as conn:
            names = [r["name"] for r in conn.execute(
                "SELECT t.name FROM card_tags ct JOIN sub_tags t ON t.id=ct.sub_tag_id"
                " WHERE ct.card_id=? ORDER BY t.name", (card_ids[0],)).fetchall()]
        check("人工挂的标签没被 AI 抹掉", "好磕" in names, True)
        check("AI 的标签也挂上了", "直接描写" in names, True)

        with db.connect() as conn:
            c4 = conn.execute("SELECT * FROM cards WHERE id=?",
                              (card_ids[4],)).fetchone()
            tags4 = conn.execute("SELECT COUNT(*) FROM card_tags WHERE card_id=?",
                                 (card_ids[4],)).fetchone()[0]
        check("AI 编了个不存在的标签 → 跳过它，但主类照样写",
              c4["primary_category_id"] is not None, True)
        check("不存在的标签没被写进去", tags4, 0)

        # ------------------------------------------------------------------
        print("\n【H】重试沿用上次用的模型")
        # ------------------------------------------------------------------
        # 造一个「部分完成」的任务
        with db.connect() as conn:
            conn.execute("UPDATE cards SET primary_category_id=NULL, source=?,"
                         " status=? WHERE material_id=? AND source<>?",
                         (sg.SOURCE_INHERITED, sg.STATUS_PENDING, mid, sg.SOURCE_HUMAN))

        boom = {"on": True}

        def flaky_chat(cfg, messages, temperature=0.0, json_mode=False, timeout=None):
            ids = ids_in(messages)
            if boom["on"]:
                # 第一次：整批失败
                from backend.llm import LlmError
                raise LlmError("假装的限流", retryable=True, status=429)
            return fake_chat(cfg, messages)

        llm.chat = flaky_chat
        res, run_id = auto.create_run(owner, mid, classifier_name="llm",
                                      model_key="fake", background=False)
        run = auto.get_run(run_id, owner)
        check("上游 429 → 任务失败", run["status"], auto.RUN_FAILED)
        with db.connect() as conn:
            n_failed = conn.execute(
                "SELECT COUNT(*) FROM classification_items WHERE run_id=? AND status=?",
                (run_id, auto.ITEM_FAILED)).fetchone()[0]
        check("每一条都记了失败（%d 张）" % LINES, n_failed, LINES)

        boom["on"] = False
        llm.chat = fake_chat
        retry_res = auto.retry_run(mid, owner)
        check("重试建起来了", retry_res.get("ok"), True)
        check("重试返回里带的还是同一个模型", retry_res.get("model_key"), "fake")
        check("重试只重跑上次失败的那些", retry_res.get("retried_only_failed"), True)

    finally:
        llm.chat = real_chat

    # ------------------------------------------------------------------
    # 重试次数：长任务只发一次，短任务照旧 3 次
    # ------------------------------------------------------------------
    # 这一段不联网：把 urlopen 换成"每次都失败"，然后数它被调了几次。
    #
    # 为什么值得单独钉死：生成大纲一次要吐 8000 字，超时后重试等于把
    # 等待时间翻倍 —— 她 2026-09-26 真踩过一次，跑了 9 分钟（180×3 + 退避）
    # 拿回一个必然失败。而且超时只是"我们这边不等了"，服务端那边可能
    # 已经把字写完了，重试一次就多扣一笔钱。所以大纲传 max_retry=1。
    # 分类/内化那种短活吃默认的 3 次是对的，也要一起钉住，别被误改。
    print()
    print("---- 重试次数（长任务 vs 短任务）----")
    _orig_urlopen = llm.urllib.request.urlopen
    _orig_sleep = llm.time.sleep
    calls = {"n": 0}

    def _dead_urlopen(req, timeout=None):
        calls["n"] += 1
        raise llm.urllib.error.URLError("假装连不上")

    llm.urllib.request.urlopen = _dead_urlopen
    llm.time.sleep = lambda s: None          # 别真等 2 + 4 秒
    try:
        cfg = {"key": "t", "label": "假模型", "api_key": "sk-x",
               "base_url": "http://127.0.0.1:9/v1", "model": "m"}

        err1 = ""
        calls["n"] = 0
        try:
            llm.chat(cfg, [{"role": "user", "content": "hi"}], max_retry=1)
        except Exception as e:
            err1 = str(e)
        check("max_retry=1 时只发一次（大纲就是这么用的）", calls["n"], 1)
        check("只发一次时，报错不说「一共试了」", "一共试了" in err1, False)
        check("报错仍然说清是什么毛病（连不上）", "连不上" in err1, True)

        err3 = ""
        calls["n"] = 0
        try:
            llm.chat(cfg, [{"role": "user", "content": "hi"}])
        except Exception as e:
            err3 = str(e)
        check("不传 max_retry 时还是 3 次（分类/内化照旧）", calls["n"], 3)
        check("试了不止一次时，报错里写明试了几次",
              "一共试了 3 次" in err3, True)

        # 再让 urlopen 抛 TimeoutError，看报错里写的是不是我传进去的那个数。
        # 这条才真正证明 timeout 传到底了 —— 光看签名上有这个参数不算数。
        seen = {}

        def _slow_urlopen(req, timeout=None):
            calls["n"] += 1
            seen["timeout"] = timeout
            raise TimeoutError("假装超时")

        llm.urllib.request.urlopen = _slow_urlopen
        err_to = ""
        calls["n"] = 0
        try:
            llm.chat(cfg, [{"role": "user", "content": "hi"}],
                     timeout=600, max_retry=1)
        except Exception as e:
            err_to = str(e)
        check("urlopen 收到的是 600 秒，不是写死的 180",
              seen.get("timeout"), 600)
        check("超时报错里写的也是 600 秒", "超过 600 秒" in err_to, True)
        check("超时之后没有再试一次", calls["n"], 1)
    finally:
        llm.urllib.request.urlopen = _orig_urlopen
        llm.time.sleep = _orig_sleep

    # ------------------------------------------------------------------
    print("\n" + "=" * 66)
    print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return FAIL == 0


if __name__ == "__main__":
    try:
        ok = main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0 if ok else 1)
