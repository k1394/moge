# -*- coding: utf-8 -*-
"""分阶段生成（A 规划 → B 节点分批 → C 检查 → D 只补缺失）的单元测试。

为什么不走 HTTP、不起真服务：_generate_staged 是纯编排，输入 ctx、
输出 payload。直接把 cls.llm.chat 换成假的，按调用顺序吐出预设响应，
就能验「分批、断号续写、只补缺失」这些逻辑，不碰真网络。

怎么跑：
    cd G:\\docker\\moge
    .venv\\Scripts\\python.exe -X utf8 tests\\test_outline_staged.py
"""

import os
import sys
import tempfile

# 数据目录要在 import 之前定（跟其它测试同一个规矩）
_tmp = tempfile.mkdtemp(prefix="moge_staged_")
os.environ["MOGE_DATA_DIR"] = _tmp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import classification as cls          # noqa: E402
from backend import outline_ai as oai             # noqa: E402
from backend import outline_db as odb             # noqa: E402


def check(name, cond, extra=""):
    if cond:
        print("  [通过] " + name + (("  " + extra) if extra else ""))
    else:
        print("  [失败] " + name + (("  " + extra) if extra else ""))
        raise AssertionError(name)


def _node(nid, title):
    """造一个写细后的完整节点。"""
    return {
        "node_id": nid, "node_title": title, "estimated_words": 600,
        "location_time": "夜里，城门口",
        "participating_roles": ["甲"],
        "purpose": "这一段的作用", "event": "他做了什么", 
        "character_action": "具体动作", "conflict": "冲突",
        "emotional_change": "从平静到紧绷",
        "information_revealed": "透露出一个信息",
        "connection_to_next": "引出下一段",
        "source_plot_ids": [1],
        "writing_notes": "注意",
    }


def _ctx():
    """一个最小的、能跑通 _system_block 的 ctx。"""
    return {
        "template": None,                 # 让 _system_block 用内置 GENERIC
        "worldview": "一个没有月亮的城池。",
        "characters": [{"name": "甲", "personality": "克制"}],
        "hook": "他来复仇，其实在求救。",
        "design": "结局留白。",
        "target_words": 6000,
        "tier": odb.word_tier(6000),
        "plots": [{"id": 1, "title": "雨夜送信", "ref_count": 0}],
        "user_prompt": "",
        "learning": [],
    }


def _plan(n_nodes):
    """规划阶段的响应。"""
    return {
        "story_core": "一个过期的承诺。",
        "character_functions": [{"role": "甲", "goal": "送到",
                                  "obstacle": "旧人拦路", "change": "承认还想见"}],
        "expected_node_count": n_nodes,
        "node_plan": [
            {"node_id": "x%d" % (i + 1), "purpose": "第%d段的目的" % (i + 1),
             "estimated_words": 600,
             "required_event": "第%d段必须发生的事" % (i + 1),
             "source_plot_ids": [1], "causal_link": "触发下一段"}
            for i in range(n_nodes)
        ],
    }


def _finalize():
    return {
        "title_candidates": ["雨夜的信"],
        "theme_tone": "冷、克制",
        "overview": "他受托送信，半路被旧同门截住。",
        "climax": "第二段当众对质",
        "ending": "烧信，回应核心",
        "logic_risks": [],
    }


class _FakeChat:
    """按顺序吐出预设响应的假 chat。

    返回体跟真 llm.chat 一致：{"content", "usage", "finish_reason"}。
    content 是 JSON 字符串（json_mode 下模型吐的就是字符串）。
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, cfg, msgs, json_mode=False, **opts):
        self.calls.append(msgs)
        if not self.responses:
            raise RuntimeError("假 chat 被多调了（没预备那么多响应）")
        r = self.responses.pop(0)
        import json as _j
        if isinstance(r, dict) and "content" in r:
            return r
        return {"content": _j.dumps(r, ensure_ascii=False),
                "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                          "total_tokens": 150},
                "finish_reason": "stop"}


def main():
    print("=" * 66)
    print("分阶段生成单元测试")
    print("=" * 66)

    cfg = {"key": "fake", "label": "假", "base_url": "http://x/v1",
           "model": "fake-1", "api_key": "sk-x"}
    opts = {"temperature": 0.7, "timeout": 30, "max_retry": 1}
    _real = cls.llm.chat

    # --------------------------------------------------------------
    print("\n【1】正常全流程：规划 4 节点 → 2 批写完 → 收尾")
    # --------------------------------------------------------------
    fake = _FakeChat([
        _plan(4),                                   # A 规划
        {"nodes": [_node("n1", "起"), _node("n2", "承")]},   # B 批1（n1,n2）
        {"nodes": [_node("n3", "转"), _node("n4", "合")]},   # B 批2（n3,n4）
        _finalize(),                                # C 收尾
    ])
    cls.llm.chat = fake
    payload, meta = oai._generate_staged(_ctx(), cfg, opts)
    cls.llm.chat = _real

    check("规划后节点数 = 4", len(payload["nodes"]) == 4,
          "实际 %d" % len(payload["nodes"]))
    ids = [n.get("node_id") for n in payload["nodes"]]
    check("节点编号连续 n1..n4", ids == ["n1", "n2", "n3", "n4"],
          "实际 %s" % ids)
    check("有高潮", bool(payload["climax"]))
    check("有结局", bool(payload["ending"]))
    check("有故事核心", bool(payload["story_core"]))
    check("总共 4 次调用（规划+2批+收尾）", len(fake.calls) == 4,
          "实际 %d" % len(fake.calls))
    check("stage_notes 记录了批次", "第 2 批" in " ".join(meta["stage_notes"]))

    # --------------------------------------------------------------
    print("\n【2】断号续写：节点阶段少写一个 → D 补缺")
    # --------------------------------------------------------------
    fake = _FakeChat([
        _plan(4),
        {"nodes": [_node("n1", "起"), _node("n2", "承")]},   # 批1
        {"nodes": [_node("n3", "转")]},                      # 批2 少了 n4
        _finalize(),
        {"nodes": [_node("n4", "合")]},                      # D 补缺
    ])
    cls.llm.chat = fake
    payload, meta = oai._generate_staged(_ctx(), cfg, opts)
    cls.llm.chat = _real

    ids = [n.get("node_id") for n in payload["nodes"]]
    check("补缺后仍是 4 节点", len(ids) == 4, "实际 %d" % len(ids))
    check("补缺后编号连续", sorted(ids) == ["n1", "n2", "n3", "n4"],
          "实际 %s" % ids)
    check("触发了补缺（5 次调用）", len(fake.calls) == 5,
          "实际 %d" % len(fake.calls))
    check("meta 警告里说了缺节点",
          any("缺 1 个节点" in w for w in meta["warns"]))

    # --------------------------------------------------------------
    print("\n【3】节点一批被字数掐断，但节点都在 → 不补，只提醒")
    # --------------------------------------------------------------
    fake = _FakeChat([
        _plan(3),
        {"nodes": [_node("n1", "起"), _node("n2", "承"), _node("n3", "合")]},
        _finalize(),
    ])
    # 让批 1 的 finish_reason 是 length（但节点其实给全了）
    orig_responses = list(fake.responses)
    fake.responses = orig_responses
    # 直接改 _FakeChat 的第一个节点批响应，标记 length
    fake.responses[1] = {"content": '{"nodes": ['
                         + '{"node_id":"n1","node_title":"起","event":"x"},'
                         + '{"node_id":"n2","node_title":"承","event":"x"},'
                         + '{"node_id":"n3","node_title":"合","event":"x"}'
                         + ']}',
                         "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                   "total_tokens": 2},
                         "finish_reason": "length"}
    cls.llm.chat = fake
    payload, meta = oai._generate_staged(_ctx(), cfg, opts)
    cls.llm.chat = _real
    check("节点都在（3 个）", len(payload["nodes"]) == 3)
    check("掐断被记进警告",
          any("掐断" in w for w in meta["warns"]))

    # --------------------------------------------------------------
    print("\n【4】规划阶段失败 → 抛错，不硬造")
    # --------------------------------------------------------------
    fake = _FakeChat([None])   # 第一个响应是 None（解析失败）
    cls.llm.chat = fake
    raised = False
    try:
        oai._generate_staged(_ctx(), cfg, opts)
    except ValueError:
        raised = True
    cls.llm.chat = _real
    check("规划失败抛 ValueError", raised)

    # --------------------------------------------------------------
    print("\n【5】规划没给 node_plan → 抛错")
    # --------------------------------------------------------------
    fake = _FakeChat([{"story_core": "x"}])   # 没有 node_plan
    cls.llm.chat = fake
    raised = False
    try:
        oai._generate_staged(_ctx(), cfg, opts)
    except ValueError:
        raised = True
    cls.llm.chat = _real
    check("没 node_plan 抛 ValueError", raised)

    print("\n" + "=" * 66)
    print("全部通过")
    print("=" * 66)


if __name__ == "__main__":
    main()
