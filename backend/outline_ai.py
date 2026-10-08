# -*- coding: utf-8 -*-
"""
墨阁 · 大纲生成 · 编排层
========================================================

这个文件管"怎么问模型、任务怎么排队、结果怎么收回来"。
表结构那一层在 outline_db.py，两边不互相越界。

--------------------------------------------------------
一、跟内化那边不一样的地方（先说清楚，免得照着抄错）
--------------------------------------------------------

内化是"几百张卡 → 分几十批 → 每批出几条零件"，慢工细活，可以跑十分钟。
大纲生成是"一份输入 → 一次长回答"，所以：

    内化：分批 + 逐批落库 + 断点续跑
    大纲：一次调用 + 每个模型一行候选 + 失败就重试那一个模型

多模型不是"多发几条"，而是**同一份输入分别问几个模型**，
每个模型的回答各自独立存一行（计划第五.5：`不同模型不能互相读取结果`）。
所以这里的并发是"模型之间并发"，不是"把一次请求拆开"。

--------------------------------------------------------
二、候选零件为什么要先筛一道
--------------------------------------------------------
计划第四节让系统"从内化剧情库选择逻辑连贯且尽量少重复使用的零件"。
字面上的做法是把**全部**可用零件摆给模型挑，但那不成立：
她的零件库迟早会有几百条，一条几百字，光零件就十几万字，
一次请求直接超出上下文。
所以后端先筛一个池子（默认 40 条），模型在这个池子里按因果链组合。

【筛法为什么是"分类轮转 + 池内按新鲜度排"，而不是"直接取次数最少的 40 条"】
她的零件是按主类归档的（外貌 / 神态 / 打斗 / 暧昧拉扯…）。
直接按次数取 40 条，很可能 38 条都落在同一两个类里 ——
因为那几个类她用得少，次数自然低。
大纲要的是"能串成一条因果链"的零件，得**跨类**才串得起来。
所以先按主类轮转（每轮每类取一条），类内再按次数少的先取。

【她也可能自己指定池子】payload 里带了 plot_ids 就用她的，一个字都不筛。
计划第 1 步明确要求"用户手动选择、添加和移除剧情零件"。

--------------------------------------------------------
三、取消到底能取消到什么程度
--------------------------------------------------------
不能。模型调用是一次阻塞的 HTTP 请求（大纲最长等 600 秒，而且只发一次），
Python 线程没法从外面把它掐断。所以"取消"的真实语义是：

    · 还在排队、还没发出去的模型  → 不发了
    · 已经在路上的                → 会跑完，结果照样落库

界面上必须把这句话说明白，否则她以为点了取消就不会花钱了。
（这也正是"取消/失败/重试不能造成重复结果"这条要求要小心的地方：
  跑完的那个模型结果留着，重试只补真正没跑成的那些，不会重花钱。）
"""

import json
import os
import re
import threading
import time
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from backend import db
from backend import livestream
from backend import classification as cls
from backend import outline_db as odb
from backend import plots_db as pdb


# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROMPT_FILE = "outline.txt"
PROMPT_VERSION = "v1"
PROMPT_VERSION_GENERIC = "v1-generic"

# 改写对比（2026-10-08 折腰要的）用的另一套模板。
# 它跟大纲生成是**两件完全不同的事**（一个"写"，一个"看别人怎么写"），
# 塞进同一个模板只会两边都别扭，所以另开一档、另存一个文件。
# 三层规矩照旧：常量（公开）/ prompts/rewrite.txt（真货）/ example.rewrite.txt（公开占位）
PROMPT_FILE_REWRITE = "rewrite.txt"
PROMPT_VERSION_REWRITE = "rw1"
PROMPT_VERSION_REWRITE_GENERIC = "rw1-generic"

# 模板里必须存在的槽位。少一个就退回通用模板 + 把警告写进任务备注，
# 绝不静默丢内容（跟分类/内化同一条规矩）。
REQUIRED_SLOTS = ("worldview", "characters", "plots", "constraints",
                  "target_words", "learning_examples", "user_prompt")

# 可选槽位：认它、会填它，但模板里**没有它不算错**。
#
# 【为什么 reference_cards 是可选】
# 真实模板（prompts/outline.txt）是她调过的版本，里面多半还没有这个占位符。
# 要是把它放进必填清单，老模板立刻"缺占位符"而退回通用版 ——
# 她调的那一版被整个顶掉，只为加一个新功能，得不偿失。
# 老模板照样能吃到素材：_system_block 会检测模板里有没有这个槽位，
# 没有就把素材块追加到 system 末尾（见那边的注释）。
OPTIONAL_SLOTS = ("reference_cards",)

# 改写对比模板的槽位。**跟大纲那套完全独立** ——
# 用同一个 _SLOT_RE 拼的话，两边的槽位名会互相认（改写模板里写个
# {plots} 会被当成大纲的零件块填进去），那种错很隐蔽。
#
# 【阶段一加了 align】折腰第 4 条要"逐段对齐要允许不确定、
# 不能用复杂结果掩盖错误"。只给模型一份汇总统计（{diff}）时，
# 它看不出"这两段其实是同一段被拆开的"，于是把拆分报成删+加。
# {align} 是逐段的对齐明细，每行标了"程序认的""这条不确定"。
# 加它必须同步改 prompts/rewrite.txt 与 prompts/example.rewrite.txt ——
# 真实模板少一个槽位就静默退回通用模板，只在任务记录里留一行警告。
REWRITE_SLOTS = ("ai_outline", "user_outline", "align", "diff",
                 "worldview", "characters")

_REWRITE_SLOT_RE = re.compile(
    r"\{(" + "|".join(REWRITE_SLOTS) + r")\}")

_SLOT_RE = re.compile(
    r"\{(" + "|".join(REQUIRED_SLOTS + OPTIONAL_SLOTS) + r")\}")

# 补充提示词也归提示词库管，用自己的一档。
USER_PROMPT_KIND_OUTLINE = "outline"
USER_PROMPT_MAX = cls.USER_PROMPT_MAX

# ---- 任务状态 ----
RUN_QUEUED = "排队中"
RUN_RUNNING = "进行中"
RUN_COMPLETED = "已完成"
RUN_PARTIAL = "部分失败"
RUN_FAILED = "失败"
RUN_CANCELLED = "已取消"
ALL_RUN_STATUS = (RUN_QUEUED, RUN_RUNNING, RUN_COMPLETED, RUN_PARTIAL,
                  RUN_FAILED, RUN_CANCELLED)
RUN_ACTIVE = (RUN_QUEUED, RUN_RUNNING)

# ---- 单个模型的候选状态 ----
CAND_QUEUED = "排队中"
CAND_RUNNING = "生成中"
CAND_DONE = "已完成"
CAND_FAILED = "失败"

# 候选池默认多大。40 条 × 每条几百字 ≈ 一万多字，加上世界观和角色卡，
# 一次请求大概两三万字 —— 主流模型都吃得下，也不至于贵得离谱。
#
# 【这两个数是"上限"，不是"目标"】
# 库里可用零件不够时，有多少用多少（见 plan_candidate_plots 的轮转循环）。
# 所以 21 条零件 + 默认 40，实际就是 21 条全部进池子。
# 上限只在零件堆得比它多时才起作用。
DEFAULT_POOL = 40
# 上限从 80 提到 150：她的原话是"要求 AI 尽可能多地参考素材内化库"。
# 150 条 × 每条几百字 ≈ 四五万字，加世界观角色卡约五万字，
# DeepSeek / 硅基流动这一档的模型（128K 上下文）完全吃得下，
# 一次生成的钱也就几毛。超过 BIG_INPUT_WARN 时预览页会主动提醒她。
MAX_POOL = 150

# 送出去的总字数超过这个数就提醒她（不拦，但要说）。
BIG_INPUT_WARN = 60000

# 单次生成最多挑几个模型（odb.MAX_MODELS_PER_RUN 是同一个数的定义处）。
MAX_MODELS = odb.MAX_MODELS_PER_RUN

# 生成一次最多等多久（秒）。**必须比 llm.TIMEOUT(180) 大得多。**
#
# 为什么：llm 那套默认值是按"短回答"定的（分类、内化一次只吐几百字）。
# 大纲一次要吐 8000 字，单个模型跑一两分钟是常态 ——
# 实测通义千问 108 秒才写完，离 180 秒只剩 72 秒；
# 硅基流动那个 DeepSeek-V3.2（推理模型）稳定超过 180 秒，
# 结果就是每次生成都白等 9 分钟（180×3 次 + 退避）拿个必然失败。
#
# 600 秒 = 10 分钟，给慢模型留够写 8000 字 + 思考头寸。
# 超时会明确写进任务备注，界面也会显示"已等多久 / 最长等多久"，
# 不会让她对着一个不知道还要多久的转圈干等。
OUTLINE_TIMEOUT = 600

# 超时后还重试几次。**大纲设成 1，也就是不重试。**
#
# 理由有两层：
# ① 一个 10 分钟都没回话的请求，再发一次大概率还是 10 分钟没回话 ——
#    重试等于把等待时间翻倍，而她看到的还只是"在跑"。
# ② 超时是"我们这边不等了"，不是"服务端没算"。服务端很可能已经
#    把 8000 字生成完了，我们断线不影响它算完 —— 重试一次就多扣一次钱。
# 与其白花钱等一个不确定的结果，不如早点告诉她"这个模型这次不行，
# 换个快的"。
OUTLINE_MAX_RETRY = 1

_worker_lock = threading.Lock()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------
# 框架级通用模板（**这一份是可以公开的**）
#
# 三层提示词，别混（跟分类、内化完全一样的规矩）：
#   ① 这个常量        写在代码里，框架级通用版，公开仓库里靠它就能跑
#   ② prompts/outline.txt  她自己调的那版（真货），永不出仓库
#   ③ prompts/example.outline.txt  公开仓库里的占位示例
# ----------------------------------------------------------------------

GENERIC_OUTLINE_PROMPT = """你是"墨阁"的同人短篇大纲助手。

作者要写一篇同人短篇。你的任务是根据她给的世界观、角色卡、要求，
以及一批**从她自己的素材库里提炼出来的"剧情零件"**，
设计一份她可以直接照着动笔的细纲。

============================================================
一、四条硬边界（违反了这份大纲就作废）
============================================================

1. 世界观是硬约束。
   零件只能换人物和外壳，绝不能改变世界观规则。
   原作设定、时代、制度、能力规则，一个字都不许动。

2. 角色卡是硬约束。
   角色的行为必须符合她的性格、身份、目标、关系和能力。
   如果情节要求角色发生改变，必须写出**触发这个改变的过程**，
   不能让他"忽然就变成另一个人"。
   角色卡里"必须遵守"和"禁止出现"那两栏是绝对的，不许违反。

3. 一句话梗是核心方向。
   写了就必须围绕它组织故事，**不许换成另一个故事**。
   没写的话，你可以从世界观和角色关系里推一个核心出来，
   但要老实说明这是你推的。

4. 情节设计是她的要求，能满足的必须满足。
   跟世界观或角色卡打架、没法同时满足的，**列出来告诉她**，
   不许静默舍弃，也不许偷偷改掉她的要求。

============================================================
二、剧情零件怎么用
============================================================

零件是结构参考，不是内容。**绝对不许照搬零件里的原句、专有名词、
具体人名地名** —— 那些是别人作品里的东西，换成这个世界观里的人重新写。

零件要串成一条因果链，不是几个不相干的高潮片段。
推荐的组合逻辑（不要每样都用，但缺了因果链一定不行）：

    核心冲突 → 触发事件 → 升级 / 误会 / 阻碍 / 代价
    → 转折或真相揭露 → 收束 / 关系确认 / 开放式余味

【来源必须诚实】某个节点如果用了某条零件，就在那个节点的
source_plot_ids 里写上它的 plot_id。没用就别挂 ——
来源不明的零件比没有来源更糟。
**只准用本次给你的零件编号**，不许自己编号码。

**新鲜度**：给你的零件里有一部分标注了"已被用过 N 次"。
在同样合适的前提下，优先用用得少的。
但**绝不能为了用次数少的而牺牲逻辑** —— 一条次数很低却不合适的零件，
跳过它，并且在 logic_risks 里说明你为什么跳过。

============================================================
二点五、分类素材怎么用（有才给，都是摘要）
============================================================

{reference_cards}

============================================================
三、结构规模必须跟预期字数匹配
============================================================

{target_words}

字数不是最后显示一个数字就完事，它决定结构密度：

· 短篇**不许**出现十几个空洞小节；
· 8000 字比 6000 字多出来的那一两个节点，必须有实际作用
  （多一次转折、多一层代价），而不是把每段字数写大。
· 各节点 estimated_words 加起来要接近预期字数。

============================================================
四、每一段都要写到"能直接动笔"的程度
============================================================

**禁止**用「他们经历了一系列事件」「关系逐渐升温」这种话代替具体情节。
读者不知道发生了什么，作者也不知道该写什么。

每个节点至少说清：在哪、谁在、谁做了什么、冲突是什么、
情绪怎么变的、透露出什么信息、怎么接到下一段。

每一个高潮都要有前置铺垫，每一个转折都要有原因或信息来源。
结局必须回应故事核心，不能突然结束。

预计字数分配跟情节重要程度相称：开场紧一点，中段展开，
高潮和结局留够篇幅。

============================================================
五、输出格式（**只输出一个合法 JSON 对象，不要任何别的话**）
============================================================

{
  "title_candidates": ["标题一", "标题二"],
  "story_core": "一句话说明这篇真正讲什么",
  "theme_tone": "主题和情绪基调",
  "character_functions": [
    {"role": "角色名", "goal": "他想要什么", "obstacle": "什么挡着他",
     "change": "到结尾他变成了什么样"}
  ],
  "overview": "一段完整的因果链，把整篇串起来",
  "nodes": [
    {
      "node_id": "n1",
      "node_title": "这一段叫什么",
      "estimated_words": 900,
      "location_time": "在哪、什么时候",
      "participating_roles": ["角色名"],
      "purpose": "这一段在整篇里的作用",
      "event": "具体发生了什么",
      "character_action": "角色做了什么",
      "conflict": "冲突是什么",
      "emotional_change": "情绪怎么变的",
      "information_revealed": "透露出什么信息",
      "connection_to_next": "怎么接到下一段",
      "source_plot_ids": [12],
      "reference_card_ids": [],
      "reference_use": "参考了哪条素材的什么写作功能（没用素材就写空串）",
      "writing_notes": "写的时候要注意什么"
    }
  ],
  "climax": "高潮和转折是哪一段，为什么",
  "ending": "结局，以及它怎么回应故事核心",
  "logic_risks": ["需要作者确认的地方"]
}

字段说明：
· node_id 从 n1 往后编，**每一段的 node_id 必须不一样**。
· title_candidates 给 1～3 个。
· nodes 的顺序就是正文顺序。
· source_plot_ids 里只能出现本次给你的零件编号。
· reference_card_ids 里只能出现本次给你的分类素材编号（素材块里写的 ref_card_id）。
· logic_risks 里写：你自己觉得可能有问题的地方、跳过某条零件的原因、
  她的要求和世界观/角色卡打架的地方。没有就写空数组。

============================================================
六、作者的世界观与角色卡
============================================================

【世界观】
{worldview}

【角色卡】
{characters}

============================================================
七、本次可以用的剧情零件
============================================================

{plots}

============================================================
八、她的其他要求
============================================================

{constraints}

============================================================
九、她过去的修改习惯（只作参考，不许照抄任何具体内容）
============================================================

{learning_examples}

============================================================
十、她这次特意交代的
============================================================

{user_prompt}

记住最后一遍：**只输出一个合法 JSON 对象**，前后不要写任何解释。

另外：生成会被分成几步（先规划节点骨架，再分批写节点，最后补高潮结局）。
每一步的指令会告诉你要输出哪一块，你只输出那一块，字段名按上面的来。
"""


# ----------------------------------------------------------------------
# 改写对比：她交一份成品，让模型看"她把 AI 的那版改成了什么样"
# ----------------------------------------------------------------------
#
# 【这件事为什么值得单开一套提示词】
# 大纲生成是"写"，成败看她照着能不能动笔。
# 改写对比是"看"——输入是两份已经写完的东西，输出是**关于她的规律**。
# 同一个模板干两件事，只会把两边的措辞都写糊。
#
# 【最要紧的一条约束】
# 归纳出来的必须是"她怎么写"的规律，不是"这篇写了什么"的内容。
# 一旦模型把具体人物、具体桥段写进结论，下次生成就会照着搬 ——
# 她要的是"AI 学她的手法"，不是"AI 抄她这一篇"。这条在提示词里要说死。
GENERIC_REWRITE_PROMPT = """你是"墨阁"的写作习惯分析师。

作者用 AI 生成过一版大纲，然后**自己改写了一遍**（或者干脆推倒重写）。
现在请你对照这两份，找出她**怎么改的**，并总结成可供以后参考的习惯。

【你要学的是什么】
写作层面的东西：人物目标和行动动机、因果关系、情节推进、
场景怎么推动故事、信息什么时候揭露、铺垫有没有回收、转折有没有依据、
结局有没有把主线收完、细纲有没有给出足够具体可写的行动。
措辞习惯也看，但不是重点。

============================================================
一、你要输出的东西（只这一段，别的都别写）
============================================================

一条条**可复用的写作方法**，而不是对这两份大纲的评价。
每条都要写清三件事：**在什么情况下**、**采取什么方法**、**解决什么问题**。
再补上**什么时候适用**、**什么时候不适用** ——
不适用条件不是客套，是防止这条被不分场合地硬套。

判断标准：
    · 好：「她习惯在高潮前把铺垫拉长，最后一两三段的篇幅明显加厚」
      → 说的是"怎么做"，下次生成照着做有用
    · 坏：「这篇的第 4 段写得不错」
      → 说的是"这一篇"，下次没法用

============================================================
二、分类：kind
============================================================

只能填这九个之一：
新增事件 / 删除事件 / 顺序调整 / 动机改变 / 冲突处理改变 /
信息揭露时机改变 / 铺垫回收变化 / 结局关系变化 / 仅措辞格式标题

前八个是"改了什么"，最后一个专给"只动了措辞、没动内容"的情况。

============================================================
三、硬边界（不许越）
============================================================

1. **绝对不许把具体内容带进结论。**
   角色名、地名、门派名、专有名词、具体桥段、原句、比喻 ——
   一个都不许出现在你的总结里。结论只谈手法：
   删了什么类型的东西、加了什么类型的东西、把什么改成了什么方向、
   节奏往哪边挪、篇幅怎么重新分配。

2. **不许替她脑补理由、也不许武断评价她。**
   至于"她为什么删" —— 除非她在备注里写了，否则只能写成
   **带证据的推测**，而且要写明这是推测。
   猜出来的动机最容易被当成事实，然后一路错下去。

3. **她的版本不是"更好"的代名词。**
   她改了不等于她改对了，"跟她不一样"也不等于"AI 错了"。
   **不许**把"人工稿不同"一律解释成"人工稿更好"。

============================================================
四、但要敢指出具体问题（有证据才行）
============================================================

上面第 2、3 条不是让你和稀泥。**原稿哪里不足、她怎么解决的** ——
这正是最该学的东西，写不出来这套学习就白做了。

有**原文证据**的质量问题，要直接指出来。例如：
  · 原稿这一段只有结果、没有行动动机，她补了一个触发事件
  · 原稿结尾没有回应前面的铺垫，她让最后一段把那条线收了
  · 原稿某段违反角色卡里定的禁忌，她改成别的处理方式
  · 原稿两段之间缺因果连接，她加了一句过渡

写这类话时必须带上证据：**原稿的哪一段、什么问题、她改成了什么**。
证据落在 problem / evidence / improved 三个字段里。

· **审美判断**（"这样更抓人"）→ 可以写，但要标出来这是主观判断。
· **不确定的推测** → 只能写成推测，写进 uncertain 字段。

============================================================
五、两份大纲
============================================================

【AI 生成的那一版】
{ai_outline}

============================================================
【她自己改写后的成品】
{user_outline}

============================================================
六、程序自动做的逐段对齐（辅助线索，可能认错）
============================================================

下面的对齐是程序按段落顺序自动认出来的，**只是线索，不是结论**。
程序认段会出错（她改动大、或者段落被合并拆分、大段移位时尤其容易错）。
它标"不确定"的地方是真的不确定 —— **不要**因为表格里给它配了一行
就当成事实。**以你读到的两份全文和创作要求为准**；
对齐结果跟你自己读出来的结论打架时，**信你自己**。

{align}

============================================================
七、程序算的结构差异统计（辅助线索）
============================================================

同一件事的另一个角度，只有汇总数字。一样只是线索。

{diff}

============================================================
八、当时的设定（帮你理解她在什么前提下改的）
============================================================

【世界观】
{worldview}

【角色卡】
{characters}

============================================================
九、输出格式：只输出一个合法 JSON 对象，前后不要任何解释
============================================================

{
  "summary": "一句话说清她这次改写最明显的特点（不超过 60 字）",
  "points": [
    {
      "kind": "这条属于哪一类。只能是：新增事件、删除事件、顺序调整、动机改变、冲突处理改变、信息揭露时机改变、铺垫回收变化、结局关系变化、仅措辞格式标题",
      "point": "她做了什么。用『她习惯…』『她倾向…』这种说法，不许出现具体人名地名。",
      "problem": "原稿的问题。有原文证据才写，写清是原稿哪一处、缺了什么；没有就填空字符串。",
      "change": "她这一处的具体做法（说手法，不带具体人名地名）。",
      "evidence": "你根据什么这么判断 —— 指出是 AI 版第几段 / 她那版第几段之间发生了什么。",
      "improved": "这个改法解决了什么问题。解决不了就别写、别硬夸。",
      "method": "以后生成时具体该怎么做。要能直接照着执行，比如『开场不要一次给全背景，留一半到第二段再说』。",
      "applies_when": "什么时候适用（写清条件）。",
      "not_when": "什么时候不适用（不加这句这条会被不分场合地硬套）。",
      "scope": "适用范围。只能填：长期偏好 / 情境适用 / 仅本篇。拿不准就填「仅本篇」。",
      "confidence": "你这条判断的把握。只能填：充分 / 一般 / 不足。",
      "uncertain": "有没有拿不准的地方（对齐不确定、或这只是推测）。确定就填空字符串。"
    }
  ]
}

【scope 三个值】
· 长期偏好 —— 跟具体题材无关、是她一贯的写法，以后每次都该参考。
· 情境适用 —— 只在某类情境下成立（把情境写进 applies_when）。
· 仅本篇 —— 只在这一次的设定下成立，换篇就不该套用。
**拿不准就填「仅本篇」** —— 宁可这条先不外扩，也不能把一次性的改动
当成她的长期习惯。

【confidence 跟 scope 是两件事】
scope 说的是"这条以后用在哪"，confidence 说的是"你现在有多确定"。
一条完全可能是"仅本篇 + 判断充分"，也可能是"长期偏好 + 判断一般"。
如果你觉得"这条还需要她解释一下才说得清"，
就把 confidence 填「不足」，并在 uncertain 里写明你想问什么。

· points 最多 8 条，**只写你真有把握的**。
  两份大纲看下来只看出 3 条，就写 3 条 —— 凑数的结论比没有更坏，
  它会一路影响以后每一次生成，而且她一直不知道是哪条在捣乱。
· 实在看不出来（比如两份几乎一样、或者她那版太短），
  就 points 给空数组 []，并在 summary 里如实写明"这次没有明显的改写规律"。
· 数组里没有内容就给空数组 []，不要省略字段。
· 不确定的字段一律给空字符串 ""，不要写"无""暂无""N/A"。
"""


def rewrite_template():
    """取改写对比要用的模板。返回 (模板文本, 来源, 警告语)。

    跟 prompt_template() 同一套规矩：来源只有 file / builtin，
    少了槽位就退回内置模板并把缺哪个写进警告 —— 绝不静默丢内容。
    """
    path = os.path.join(ROOT_DIR, "prompts", PROMPT_FILE_REWRITE)
    if not os.path.isfile(path):
        return GENERIC_REWRITE_PROMPT, "builtin", ""
    try:
        with open(path, encoding="utf-8") as f:
            txt = f.read().strip()
    except Exception as e:                                  # pragma: no cover
        return (GENERIC_REWRITE_PROMPT, "builtin",
                "prompts/%s 读不了（%s），这次用通用模板" % (PROMPT_FILE_REWRITE, e))
    if not txt:
        return (GENERIC_REWRITE_PROMPT, "builtin",
                "prompts/%s 是空的，这次用通用模板" % PROMPT_FILE_REWRITE)
    lost = [s for s in REWRITE_SLOTS if ("{%s}" % s) not in txt]
    if lost:
        return (GENERIC_REWRITE_PROMPT, "builtin",
                "prompts/%s 少了占位符 %s（那几处内容会被丢掉），"
                "这次改用通用模板" % (PROMPT_FILE_REWRITE, "、".join(lost)))
    return txt, "file", ""


def rewrite_prompt_version():
    _, src, _ = rewrite_template()
    return (PROMPT_VERSION_REWRITE if src == "file"
            else PROMPT_VERSION_REWRITE_GENERIC)


def prompt_template():
    """取这次要用的模板。返回 (模板文本, 来源, 警告语)。

    跟分类、内化同一个设计：来源只有 "file" / "builtin" 两种，
    三样一起返回，"这次到底跑的哪一版"必须可追溯。
    """
    path = os.path.join(ROOT_DIR, "prompts", PROMPT_FILE)
    if not os.path.isfile(path):
        return GENERIC_OUTLINE_PROMPT, "builtin", ""
    try:
        with open(path, encoding="utf-8") as f:
            txt = f.read().strip()
    except Exception as e:                                  # pragma: no cover
        return (GENERIC_OUTLINE_PROMPT, "builtin",
                "prompts/%s 读不了（%s），这次用通用模板" % (PROMPT_FILE, e))
    if not txt:
        return (GENERIC_OUTLINE_PROMPT, "builtin",
                "prompts/%s 是空的，这次用通用模板" % PROMPT_FILE)
    lost = [s for s in REQUIRED_SLOTS if ("{%s}" % s) not in txt]
    if lost:
        return (GENERIC_OUTLINE_PROMPT, "builtin",
                "prompts/%s 少了占位符 %s（那几处内容会被丢掉），"
                "这次改用通用模板" % (PROMPT_FILE, "、".join(lost)))
    return txt, "file", ""


def prompt_version():
    """这次会用哪一版提示词（任务记录里要存它，重跑两次要能对得上）。"""
    _, src, _ = prompt_template()
    return PROMPT_VERSION if src == "file" else PROMPT_VERSION_GENERIC


def _fill_slots(tpl, **slots):
    """把 {xxx} 换成实际内容。**单遍扫描**，不是逐个 replace。

    【为什么必须单遍】填进去的内容里有**她的小说世界观原文**和
    零件正文，里面出现花括号（写个算式、代码片段）完全不奇怪。
    先替换 worldview 再 replace user_prompt 的话，世界观里恰好写着
    "{user_prompt}" 那几个字的地方会被再替换一次 ——
    把别人的补充要求塞进正文中间，而且谁也不会发现。
    单遍正则只认自己定义的槽位名，每个位置只替换一次。
    （也不能用 str.format：模板正文里有 JSON 示例，大括号会打架。）
    """
    def _sub(m):
        return slots.get(m.group(1), m.group(0))
    return _SLOT_RE.sub(_sub, tpl)


# ----------------------------------------------------------------------
# 补充提示词（复用分类那条线的存储，换一个 kind）
# ----------------------------------------------------------------------

def get_user_prompt(owner):
    return cls.get_user_prompt(owner, kind=USER_PROMPT_KIND_OUTLINE)


def set_user_prompt(owner, content):
    """存她给大纲写的补充提示词。超长直接拒绝，不悄悄截断。"""
    return cls.set_user_prompt(owner, content, kind=USER_PROMPT_KIND_OUTLINE)


# ----------------------------------------------------------------------
# 候选零件：后端筛池
# ----------------------------------------------------------------------

def plan_candidate_plots(owner, data):
    """决定这次摆给模型看的零件池。

    返回 {"items": [...], "blocked": [...], "source": "auto"/"manual",
          "stuck": [...], "status_counts": {...}}

    auto：按主类轮转 + 类内按参考次数升序（理由见文件头第二节）
    manual：她自己在界面上勾的，一个字都不筛（但仅本地的那几条仍然拦）

    【stuck 是什么、为什么非要有】
    候选池只收「已确认 / 已编辑」。别的状态（最主要就是 AI 内化产出的
    「待确认」）在 list_candidate_plots 的 WHERE 里就被滤掉了 ——
    不进 items、不进 blocked、不出现在任何地方。所以她 AI 内化出 21 条零件、
    去生成大纲时看到的是"没有可用的剧情零件"，却完全不知道那 21 条
    就躺在库里，只差一个"确认"。stuck 就是把这批看不见的零件捞出来，
    让预览页能具体说出是哪几条、卡在哪一步。
    """
    data = data or {}
    # 状态统计与"被状态挡住的清单"：只为让她看得见，不参与筛池逻辑。
    # 两条分支都要带上，所以在最前面算一次。
    status_counts = odb.plot_status_counts(owner)
    stuck = odb.list_blocked_by_status(owner)
    manual = data.get("plot_ids")
    if isinstance(manual, list) and manual:
        ids = odb._int_list(manual, MAX_POOL, "剧情零件")
        items = odb.plot_blocks(owner, ids)
        # 状态不对的（她排除掉的）要挡回去，不能因为手动传了就放行 ——
        # 计划第四.1 节的条件跟"谁来选"无关。
        usable = {p["id"]: p for p in odb.list_candidate_plots(
            owner, include_blocked=True, limit=0)}
        bad = [p["id"] for p in items if p["id"] not in usable
               or usable[p["id"]]["status"] not in odb.PLOT_USABLE_STATUS]
        items = [p for p in items if p["id"] not in bad]
        blocked = [p for p in items if usable.get(p["id"], {}).get("local_only")]
        items = [p for p in items if not usable.get(p["id"], {}).get("local_only")]
        for p in items:
            p["ref_count"] = usable.get(p["id"], {}).get("ref_count", 0)
        return {"items": items, "blocked": blocked, "source": "manual",
                "dropped": bad, "stuck": stuck, "status_counts": status_counts}

    allp = odb.list_candidate_plots(owner, include_blocked=True, limit=0)
    blocked = [p for p in allp if p.get("local_only")]
    pool = [p for p in allp if not p.get("local_only")]

    want = int(data.get("pool_size") or DEFAULT_POOL)
    want = max(5, min(want, MAX_POOL))

    # ---- 按主类轮转 ----
    # 同一类里按"参考次数少的先"排；类的顺序按"这个类里最新那条的 id"倒序，
    # 所以她最近在用的类会先被轮到。
    by_cat = {}
    for p in pool:
        key = p.get("primary_category_id") or 0
        by_cat.setdefault(key, []).append(p)
    for k in by_cat:
        by_cat[k].sort(key=lambda x: (x.get("ref_count") or 0, -x["id"]))
    order = sorted(by_cat.keys(),
                   key=lambda k: -max(x["id"] for x in by_cat[k]))

    picked = []
    round_no = 0
    while len(picked) < want:
        added = False
        for k in order:
            lst = by_cat[k]
            if round_no < len(lst):
                picked.append(lst[round_no])
                added = True
                if len(picked) >= want:
                    break
        if not added:
            break
        round_no += 1

    return {"items": picked, "blocked": blocked, "source": "auto",
            "dropped": [], "stuck": stuck, "status_counts": status_counts}


# ----------------------------------------------------------------------
# 拼提示词
# ----------------------------------------------------------------------

def _worldview_block(text):
    t = (text or "").strip()
    return t if t else "（她没写世界观 —— 这种情况不该发生生成，请先让她补上。）"


def _characters_block(chars):
    """角色卡块。八项一项不落地摊开 —— 少一项模型就多猜一样。"""
    if not chars:
        return "（一张角色卡都没关联。）"
    parts = []
    for i, c in enumerate(chars, 1):
        lines = ["--- 角色 %d：%s ---" % (i, c.get("name") or "（没名字）")]
        for f in ("identity", "personality", "goal", "fear", "relations",
                  "speech"):
            v = (c.get(f) or "").strip()
            if v:
                lines.append("%s：%s" % (odb.CHAR_FIELD_LABELS[f], v))
        must = (c.get("must_do") or "").strip()
        never = (c.get("never_do") or "").strip()
        if must:
            lines.append("必须遵守（绝对不能违反）：%s" % must)
        if never:
            lines.append("禁止出现（绝对不能出现）：%s" % never)
        if len(lines) == 1:
            lines.append("（这张卡只有名字，其他都没填 —— 你不知道的就别编。）")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _plots_block(items):
    """零件块。每条带上"已被用过几次"，让模型自己权衡新鲜度。"""
    if not items:
        return ("（这次没有可用的剧情零件。请完全依据世界观、角色卡和她的要求"
                "设计结构，并在 logic_risks 里说明这一点。）")
    parts = []
    for i, p in enumerate(items, 1):
        n = p.get("ref_count") or 0
        lines = ["--- 剧情零件 %d（plot_id=%s）---" % (i, p.get("id"))]
        if p.get("title"):
            lines.append("名字：%s" % p["title"])
        if p.get("plot_type"):
            lines.append("类型：%s" % p["plot_type"])
        if p.get("category_name"):
            lines.append("归类：%s" % p["category_name"])
        if p.get("summary"):
            lines.append("讲的是什么：%s" % p["summary"])
        beats = p.get("beats") or {}
        if isinstance(beats, dict) and beats:
            bl = []
            for k in pdb.BEAT_KEYS:
                v = (beats.get(k) or "").strip()
                if v:
                    bl.append("    %s：%s" % (pdb.BEAT_LABELS.get(k, k), v))
            if bl:
                lines.append("情节节点：\n" + "\n".join(bl))
        slots = p.get("role_slots") or []
        if slots:
            lines.append("角色位：%s" % "、".join(slots))
        hints = p.get("usage_hints") or []
        if hints:
            lines.append("适合用在：%s" % "、".join(hints))
        lines.append("已被用过 %d 次。" % n)
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _reference_block(items):
    """「分类素材参考」那一节的正文。

    【这些是摘要，不是原文】检索层只发摘要 —— 任务书补充二.4：
    第一轮只给编号、短摘要和标签；只有模型明确要哪条细节时才给原文
    （那个"要原文"的通道这一版还没做，先让摘要够具体）。

    【防抄袭说明必须跟着素材走】素材块发到哪儿，防抄袭规矩就跟到哪儿，
    不依赖她记得改提示词。这是任务书补充三写死的。
    """
    items = [x for x in (items or []) if isinstance(x, dict)]
    if not items:
        return ("（这次没有参考分类素材。节点里 reference_card_ids 一律空数组、"
                "reference_use 写空串。）")
    parts = [
        "下面是从她素材库里筛出来的几条「分类素材摘要」。它们只是写作功能"
        "和结构的参考，不是待改写的正文：",
        "· 只能借它的触发方式、人物反应模式、互动机制、氛围构成、"
        "对白功能、情绪路径、信息揭示节奏。",
        "· 禁止照抄连续原句、独特比喻、人名地名、专有名词、组织名、"
        "独特道具组合、原文特有连续动作和可识别句式。",
        "· 必须用当前世界观和角色重新设计事件，用全新的表达和因果链。",
        "· 用了哪条，就在那个节点的 reference_card_ids 里写它的 ref_card_id，"
        "并在 reference_use 里用一句话说清借的是它的什么功能。没用的别挂。",
        "",
    ]
    for x in items:
        meta = x.get("summary_meta") or {}
        lines = ["--- 分类素材（ref_card_id=%s）---" % x.get("ref_card_id")]
        if x.get("category_name"):
            lines.append("归类：%s" % x["category_name"])
        if x.get("tags"):
            lines.append("标签：%s" % "、".join(x["tags"]))
        if x.get("summary"):
            lines.append("摘要：%s" % x["summary"])
        for key, label in (("use", "适合写"), ("character_action", "人物动作"),
                           ("emotion_path", "情绪路径"),
                           ("scene_function", "场景作用")):
            v = (meta.get(key) or "").strip()
            if v:
                lines.append("%s：%s" % (label, v))
        safe = (meta.get("safe_note") or "").strip()
        if safe:
            lines.append("要避开：%s" % safe)
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _target_words_block(target_words, tier):
    """「结构规模」那一段：这一档多少字、几个节点、每段多长。

    【口径只有这一个出口】节点数和每段字数都在这里说给模型。
    以前本文件和一个 outline_db.word_budget_hint() 都在说同一件事，
    措辞还不一样，改一处忘一处就会给模型两套说法 —— 2026-09-26 合并。

    关键是**把每段字数摆在前面**：以前是先说"建议 5～8 个节点"，
    每段字数由它倒推成 1000～1600 字，于是模型每段都写肥，
    十个字段摊进去平均一个才一百来字，全是概括。
    """
    try:
        n = int(target_words or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return "预期正文字数：没填。"
    lo, hi = tier["nodes"]
    head = ("预期正文字数：%d 字（%s 这一档：%s）。"
            % (n, tier["label"], tier["hint"]))
    per = n // max(1, hi)
    if per > odb.NODE_WORDS_MAX:
        # 撞到节点数上限（字数很大）。这时**不能**再报"每段 450～800 字"——
        # 那是凑不满的谎话，模型为了对上会硬拆出几百个节点。
        # 老实说清代价：每段会比理想长，或者干脆拆篇。
        return (head + "\n节点数已压到上限 %d 个，每段大约 %d 字，超过 %d～%d "
                "的理想区间 —— 建议拆成多篇写，或者接受每段写长一点。"
                % (odb.NODE_COUNT_MAX, per,
                   odb.NODE_WORDS_MIN, odb.NODE_WORDS_MAX))
    return (head + "\n按每段 %d～%d 字算，应该是 %d～%d 个情节节点。"
            "宁可多切几段，也不要让一段里塞下两件事。" % (
                odb.NODE_WORDS_MIN, odb.NODE_WORDS_MAX, lo, hi))


def _constraints_block(hook, design, hook_ai_derived=False):
    parts = []
    h = (hook or "").strip()
    if h:
        parts.append("【一句话梗】必须围绕它组织故事：\n%s" % h)
    else:
        parts.append("【一句话梗】她没给。你可以从世界观和角色关系里推一个核心出来，"
                     "但要在 story_core 里说清楚这是你推的，不要假装是她给的。")
    d = (design or "").strip()
    if d:
        parts.append("【情节设计】她的额外要求，能满足的必须满足；"
                     "实在跟世界观或角色卡打架的，写进 logic_risks：\n%s" % d)
    else:
        parts.append("【情节设计】她没给额外要求。")
    return "\n\n".join(parts)


def _learning_block(examples, rewrites=None):
    """两块拼一起，因为它们说的事是同一件：她想要什么样的东西。

    刻意**分成两段**而不是合成一列 ——
      · 第一块是"别写什么"（她标过不可用的毛病）
      · 第二块是"要写成什么样"（她改写时表现出来的取向）
    只给第一块，模型会一直躲着毛病写，却不知道要往哪边靠；
    只给第二块，它又会照着她喜欢的样子写、同时踩她讨厌的坑。
    """
    parts = []
    if not examples:
        parts.append("【她标过「不可用」的地方】\n（她还没有标过。）")
    else:
        lines = ["【她标过「不可用」的地方】写的时候避开同样的毛病："]
        for e in examples:
            line = "· %s" % (e.get("problem") or "（没写原因）")
            if e.get("note"):
                line += " —— %s" % e["note"]
            lines.append(line)
        lines.append("注意：这只是「她不满意什么」的概括，"
                     "**不要**把任何具体人物、作品或情节搬过来。")
        parts.append("\n".join(lines))

    rw = [r for r in (rewrites or []) if r.get("points")]
    if not rw:
        parts.append("【她的改写取向】\n"
                     "（她还没交过改写对比，这一块暂时是空的。）")
    else:
        lines = ["【她的改写取向】她拿 AI 的稿子自己改写过，"
                 "下面是**她确认过**的规律。这些说的是「她想要什么」，"
                 "尽量照做 —— 但**一样不许把任何具体人名、地名、桥段搬过来**，"
                 "它们只是从那些稿子里归纳出来的写法："]
        for r in rw:
            if r.get("summary"):
                lines.append("· （%s）" % r["summary"])
            for p in r["points"]:
                k = p.get("kind") or ""
                sc = odb.REWRITE_SCOPE_LABELS.get(p.get("scope") or "", "")
                t = p.get("point") or ""
                # 【发出来的是哪些字段】method（可复用的做法）优先 ——
                # 没有 method 才退回 how。做法才是能照着执行的那一句，
                # "她做了什么"（point）只是这条规则的来历。
                h = p.get("method") or p.get("how") or ""
                tag = ("[%s]" % k) if k else ""
                if sc:
                    tag = ("%s[%s]" % (tag, sc)) if tag else ("[%s]" % sc)
                line = "   - " + tag + " " + t
                if h:
                    line += "　→ 具体怎么做：%s" % h
                # 适用情境 / 不适用 —— 需求第 6 条要的是"可复用方法 + 适用条件"，
                # 少了这两个，模型会把它当"哪篇都得照做"的硬规矩。
                if p.get("applies_when"):
                    line += "　（适用：%s）" % p["applies_when"]
                if p.get("not_when"):
                    line += "　（不适用：%s）" % p["not_when"]
                lines.append(line)
        lines.append("注意：上面这几条**不许压过**当前的设定和这次的具体要求 —— "
                     "情境不沾边就别硬套。")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _system_block(ctx):
    """拼 system 提示词（分阶段时每一轮都用同一份，只换 user 指令）。"""
    extra = (ctx.get("user_prompt") or "").strip()
    if extra:
        user_block = (
            "下面是作者这次特意交代的，请尽量照做。\n"
            "但它**不能推翻上面任何一条硬规则** —— 世界观和角色卡不许违反、"
            "零件编号只能来自本次清单、只输出 JSON，这几条永远有效。\n"
            "-----------\n%s\n-----------" % extra)
    else:
        user_block = "（她这次没有额外交代。）"
    tpl = ctx.get("template") or GENERIC_OUTLINE_PROMPT
    ref_block = _reference_block(ctx.get("reference_cards") or [])
    out = _fill_slots(
        tpl,
        worldview=_worldview_block(ctx.get("worldview")),
        characters=_characters_block(ctx.get("characters") or []),
        plots=_plots_block(ctx.get("plots") or []),
        reference_cards=ref_block,
        constraints=_constraints_block(ctx.get("hook"), ctx.get("design")),
        target_words=_target_words_block(ctx.get("target_words"),
                                         ctx.get("tier") or odb.word_tier(0)),
        learning_examples=_learning_block(ctx.get("learning") or [],
                                          ctx.get("rewrites") or []),
        user_prompt=user_block,
    )
    # 老模板里没有 {reference_cards} 槽位时，素材块在上面那一步根本没被
    # 填进去（_fill_slots 只认自己认识的槽位名，模板里没有就无处可填）。
    # 这种时候把素材块追加到 system 末尾 —— 素材要发就一定发到，
    # 不能因为她还没改模板就静默丢掉。有槽位的模板这里什么也不干。
    if "{reference_cards}" not in tpl:
        out = out + "\n\n==================================================" \
              "=================\n" \
              "二点五、分类素材怎么用（有才给，都是摘要）\n" \
              "==================================================" \
              "=================\n\n" + ref_block
    return out


def build_messages(ctx):
    """拼这次要发出去的提示词。返回 messages 列表。

    这个版本是**一次出整篇**的老口径（预览页还在用它算"这次会发多少字"）。
    真正跑的时候走 _generate_staged() 的分阶段路径，system 块跟这里同源
    （都走 _system_block），所以预览的字数口径和实际一致。
    """
    return [
        {"role": "system", "content": _system_block(ctx)},
        {"role": "user", "content":
            "请按上面的全部要求，给这一篇同人短篇出一份可以直接动笔的细纲，"
            "只输出一个合法 JSON 对象。"},
    ]


def _staged_messages(ctx, kind, extra_user=""):
    """分阶段路径的消息。system 永远是同一份，user 按阶段变。

    kind: "plan" / "nodes" / "finalize" / "repair"
    """
    system = _system_block(ctx)
    directive = {
        "plan": (
            "现在只做「规划」这一步，不要写任何节点正文。\n"
            "输出一个 JSON 对象，包含：\n"
            "  story_core（这篇真正讲什么）\n"
            "  character_functions（每个角色的目标/阻碍/变化）\n"
            "  node_plan（数组，每个元素是你要写的每一个节点的骨架：\n"
            "     node_id、purpose、estimated_words、required_event（这一段必须发生什么）、\n"
            "     source_plot_ids（拟用哪几条零件）、causal_link（这段怎么触发下一段）、\n"
            "     reference_needs（这段需要什么细节才写得具体，用几个词概括，\n"
            "       比如对峙的对白、雨夜的氛围、动作打斗的节奏；\n"
            "       如果上面给了分类素材摘要，就从里面挑你会参考的，写它的 ref_card_id；\n"
            "       没有合适素材就写空串））\n"
            "  expected_node_count（一共几个节点）\n"
            "node_plan 里的节点数必须符合上面「结构规模」那一节给的范围。\n"
            "只输出一个合法 JSON 对象，不要别的。"
        ),
        "nodes": (
            "接着写节点正文。这一步只写下面指定的这一批节点，\n"
            "每个节点写到能直接动笔的完整程度（在哪、谁、做什么、冲突、\n"
            "情绪变化、透露的信息、怎么接下一段），字段要跟 node_plan 一致。\n"
            "上一批已经写好的节点会附在后面，只当上下文参考，不要重写，\n"
            "只要让这一批的第一段接住上一批最后一段的因果。\n"
            "输出一个 JSON 对象：{\"nodes\": [这一批的完整节点数组],\n"
            "  \"complete\": 是否全部节点都写完了, \"next_action\": \"done\"/\"continue\",\n"
            "  \"last_node_id\": \"这一批最后写到的节点\", \"has_ending\": false,\n"
            "  \"continuation_cursor\": \"没写完就写接下来该写哪个节点，写完了就空串\"}\n"
            "只输出一个合法 JSON 对象。"
        ),
        "finalize": (
            "节点正文都写完了。这一步只做收尾：\n"
            "根据已经写好的所有节点，补上 title_candidates、theme_tone、\n"
            "overview（一段完整因果链把整篇串起来）、climax（高潮和转折是哪一段、\n"
            "为什么）、ending（结局，以及它怎么回应故事核心）、logic_risks。\n"
            "输出一个 JSON 对象，字段名就是这六个，别重复输出节点。\n"
            "只输出一个合法 JSON 对象。"
        ),
        "repair": (
            "上一步检查发现这份大纲还缺东西。只补下面列出的缺失部分，\n"
            "不要重写已有的内容：\n"
            "  · 缺某几个节点（断号）：只补这几个节点的完整正文\n"
            "  · 缺高潮/结局/总览：只补这些字段\n"
            "输出一个 JSON 对象，只包含要补的那部分。只输出合法 JSON。"
        ),
    }[kind]
    if extra_user:
        directive = directive + "\n\n" + extra_user
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": directive},
    ]


# ----------------------------------------------------------------------
# 发送内容预览
# ----------------------------------------------------------------------

def build_ctx(owner, data):
    """把一次请求要用的全部上下文准备好（不写库、不调模型）。

    preview 和真正跑的时候**共用这一个函数** —— 两处口径必须同源，
    否则会出现"预览说会发 2 万字、实际发了 5 万字"这种账不对的事。
    """
    data = data or {}
    world = (data.get("worldview") or "").strip()
    char_ids = odb._int_list(data.get("character_ids"), 50, "角色卡")
    chars = odb.get_characters(owner, char_ids)
    try:
        target = int(data.get("target_words") or 0)
    except (TypeError, ValueError):
        raise ValueError("预期字数得是一个整数。")

    plan = plan_candidate_plots(owner, data)
    pool_ids = [p["id"] for p in plan["items"]]
    learning = odb.learning_examples(owner, limit=3) if data.get("use_learning", True) else []
    # 她交过的改写对比（"她会把东西改成什么样"）。跟上面那块共用同一个开关 ——
    # 界面上那个勾选写的是「参考以前标过的『不可用』」，但它真实的作用是
    # "这次要不要参考你过去的东西"。两样都归它管，不然屏幕上会多出
    # 一个说不清差别的开关。
    #
    # ★ 阶段二：不再"固定挑最近 2 条"，改成**按这次要写什么挑最相关的**。
    # 传 need（这次的一句话梗 + 情节设计 + 你的额外交代）进去，本地打分排序。
    # 挑出来的那几条还会带上 _pick_why，界面能摊开"为什么用了它"。
    rewrite_need = odb.rewrite_need(data)
    rewrites = (odb.rewrite_examples(owner, need=rewrite_need)
                if data.get("use_learning", True) else [])

    user_prompt = (data.get("user_prompt") or "").strip()

    # ---- 分类素材参考的开关和上限 ----
    # 默认关（她拍板的）：不开就一个字的素材都不发、不检索。
    ref_enabled = bool(data.get("reference_enabled"))
    try:
        ref_limit = int(data.get("reference_limit") or odb.REFERENCE_DEFAULT)
    except (TypeError, ValueError):
        ref_limit = odb.REFERENCE_DEFAULT
    ref_limit = max(0, min(ref_limit, odb.REFERENCE_MAX))

    # 预览时的粗筛：此刻还没有节点的 reference_needs（那要等规划跑完），
    # 拿一句话梗 + 情节设计当需求来估"大概会带哪些素材"。
    # 真正生成时按每个节点的 needs 精确检索 —— 集合可能不一样，
    # 但数量级和字数是对的，预览页会写明这是预计。
    ref_items = []
    if ref_enabled and ref_limit > 0:
        probe_needs = [p for p in (user_prompt, (data.get("one_sentence_hook") or ""),
                                   (data.get("plot_design") or "")) if p.strip()]
        try:
            found = odb.search_reference_cards(owner, probe_needs, limit=ref_limit)
            ref_items = odb.render_reference_items(found)
        except Exception:                                    # pragma: no cover
            ref_items = []

    return {
        "owner": owner,     # 检索分类素材要用（A 规划后按需求检索）
        "outline_type": odb.OUTLINE_TYPE_TW,
        "worldview": world,
        "world_name": (data.get("world_name") or "").strip(),
        "worldview_id": data.get("worldview_id") or None,
        "character_ids": char_ids,
        "characters": chars,
        "hook": (data.get("one_sentence_hook") or "").strip(),
        "design": (data.get("plot_design") or "").strip(),
        "target_words": target,
        "tier": odb.word_tier(target),
        "plots": plan["items"],
        "pool_ids": pool_ids,
        "blocked": plan["blocked"],
        "pool_source": plan["source"],
        "dropped": plan.get("dropped") or [],
        # 库里哪些零件"在、但状态够不上可用"（最典型：AI 内化出来的「待确认」）。
        # 预览页拿它报数 —— 不报的话，她只会看到"没有可用的剧情零件"，
        # 然后以为 AI 不肯用她辛苦做出来的内化库。
        "stuck": plan.get("stuck") or [],
        "plot_status_counts": plan.get("status_counts") or {},
        "learning": learning,
        "rewrites": rewrites,
        "user_prompt": user_prompt,
        "reference_enabled": ref_enabled,
        "reference_limit": ref_limit,
        "reference_cards": ref_items,
    }


def preview_input(owner, data):
    """"这次到底会发出去什么"的完整预览。一个字都不写库，也不调模型。

    计划第十一节第 6 条明确要求：`用户点击生成前显示将发送的字数、模型
    和隐私提示`。所以这里把三样都算出来。
    """
    ctx = build_ctx(owner, data)

    # ---- 校验 ----
    # 世界观和角色卡 2026-10-07 起是选填（见 create_run 里的说明），
    # 所以这里不再为它们报 problem。剩下的都是"真会出错"的项。
    problems = []
    if ctx["target_words"] < odb.TARGET_WORDS_MIN:
        problems.append({"field": "target_words",
                         "message": "预期字数至少 %d 字。" % odb.TARGET_WORDS_MIN})
    if ctx["target_words"] > odb.TARGET_WORDS_WARN:
        problems.append({"field": "target_words", "level": "warn",
                         "message": "%d 字已经接近中短篇了，结构规模会跟短篇很不一样。"
                                    "确认的话可以直接开始。"
                                    % ctx["target_words"]})
    if not ctx["plots"]:
        stuck = ctx["stuck"]
        if stuck:
            # 有零件、但一条都够不上可用 —— 这是最容易被误读成
            # "AI 不肯参考我的内化库"的情形。所以报成 bad（红色）、
            # 说清是几条、卡在哪个状态、去哪儿改。
            by = {}
            for x in stuck:
                by[x.get("status") or "?"] = by.get(x.get("status") or "?", 0) + 1
            how = "、".join("%s %d 条" % (k, v) for k, v in by.items())
            problems.append({
                "field": "plots",
                "message": "你库里有 %d 条剧情零件，可这次一条都用不上 —— 他们的状态是"
                           "：%s。候选池只收「已确认」和「已编辑」，"
                           "因为这些零件是 AI 提的、还没经过你的眼。"
                           "先去【剧情内化】把它们确认（弹层里或列表上都有确认按钮），"
                           "再回来生成 —— 不然这次 AI 只拿得到世界观和角色卡，"
                           "你内化的素材一条都参考不到。"
                           % (len(stuck), how)})
        else:
            problems.append({"field": "plots", "level": "warn",
                             "message": "一条可用的剧情零件都没有。"
                                        "大纲会完全靠世界观和角色卡推 —— "
                                        "先去【剧情内化】把零件确认几条会更好。"})

    # ---- 模型 ----
    keys = data.get("model_keys") or []
    if isinstance(keys, str):
        keys = [keys]
    public = {m["key"]: m for m in cls.llm.public_models()} \
        if hasattr(cls, "llm") else {}
    models = []
    for k in keys[:MAX_MODELS]:
        try:
            cfg = cls.pick_model(k if isinstance(k, str) else k.get("key"))
        except ValueError as e:
            models.append({"key": str(k), "label": str(k), "usable": False,
                           "reason": str(e)})
            continue
        models.append({"key": cfg["key"], "label": cfg.get("label") or cfg["key"],
                       "model": cfg.get("model") or "", "usable": True})
    if len(keys) > MAX_MODELS:
        problems.append({"field": "model_keys",
                         "message": "一次最多挑 %d 个模型，后面那几个没算进来。"
                                    % MAX_MODELS})
    if not models:
        problems.append({"field": "model_keys",
                         "message": "一个模型都没选。去「模型设置」里至少配一个。"})
    elif not any(m["usable"] for m in models):
        problems.append({"field": "model_keys",
                         "message": "选中的模型都用不了（多半是还没填 API Key）。"})

    # ---- 发送字数 ----
    tpl, src, warn = prompt_template()
    probe = dict(ctx)
    probe["template"] = tpl
    msgs = build_messages(probe)
    chars = sum(len(m["content"]) for m in msgs)
    if chars > BIG_INPUT_WARN:
        problems.append({"field": "size", "level": "warn",
                         "message": "这次要发出去约 %d 字，比较长。"
                                    "可以把零件池调小一点，或者少挑几条零件。"
                                    % chars})

    blocked = ctx["blocked"]
    return {
        "ok": not any(p.get("level") != "warn" for p in problems),
        "problems": problems,
        "worldview_chars": len(ctx["worldview"]),
        "world_name": ctx["world_name"],
        "character_ids": ctx["character_ids"],
        "characters": [{"id": c["id"], "name": c["name"],
                        "filled": c["filled"], "field_total": c["field_total"]}
                       for c in ctx["characters"]],
        "hook": ctx["hook"],
        "design": ctx["design"],
        "target_words": ctx["target_words"],
        "tier": {"key": ctx["tier"]["key"], "label": ctx["tier"]["label"],
                 "nodes": list(ctx["tier"]["nodes"]),
                 "hint": ctx["tier"]["hint"]},
        "plots": [{"id": p["id"], "title": p["title"], "plot_type": p["plot_type"],
                   "category_name": p.get("category_name") or "",
                   "ref_count": p.get("ref_count") or 0}
                  for p in ctx["plots"]],
        "plot_source": ctx["pool_source"],
        "blocked_plots": [{"id": p["id"], "title": p["title"]}
                          for p in blocked],
        # "在库里、但状态够不上可用"的那批（最典型就是 AI 内化出来的「待确认」）。
        # 光报一个数她还不知道自己该去改哪几条，所以连名字一起给。
        "stuck_plots": [{"id": p["id"], "title": p["title"],
                         "status": p["status"]} for p in ctx["stuck"]],
        "plot_status_counts": ctx["plot_status_counts"],
        # 库里可用零件总数（各状态相加，不查库）。她要"尽可能多参考"，
        # 就得先知道自己手上到底有多少条能用。
        "usable_total": sum(n for s, n in ctx["plot_status_counts"].items()
                            if s in odb.PLOT_USABLE_STATUS),
        "pool_want": int(data.get("pool_size") or DEFAULT_POOL),
        "pool_max": MAX_POOL,
        "learning_cases": len(ctx["learning"]),
        # ---- 分类素材参考（预览要让她看见会带哪些素材、多多少字）----
        # 这里的素材集是按"一句话梗+情节设计"粗筛的（真正的精确检索
        # 要等规划跑完、拿到每个节点的 reference_needs 才做），
        # 所以写明是预计。开着的时候 send_chars 已经把素材块算进去了。
        "reference_enabled": ctx["reference_enabled"],
        "reference_limit": ctx["reference_limit"],
        "reference_max": odb.REFERENCE_MAX,
        "reference_cards": [
            {"group_id": x.get("group_id"),
             "ref_card_id": x.get("ref_card_id"),
             "category_name": x.get("category_name") or "",
             "tags": x.get("tags") or [],
             "summary": x.get("summary") or "",
             "node_needs": x.get("node_needs") or []}
            for x in (ctx.get("reference_cards") or [])],
        "models": models,
        "send_chars": chars,
        "prompt_version": PROMPT_VERSION if src == "file" else PROMPT_VERSION_GENERIC,
        "prompt_src": src,
        "prompt_warning": warn,
        "privacy": _privacy_note(ctx, chars),
        "messages_preview": msgs[0]["content"],
    }


def _privacy_note(ctx, chars):
    """计划第十一节第 6 条要的隐私提示。用大白话写清楚发的是什么。"""
    n = len(ctx["plots"])
    parts = [
        "这次会把下面这些内容发给你选的模型服务商：",
        "· 世界观 %d 字" % len(ctx["worldview"]),
        "· 角色卡 %d 张" % len(ctx["characters"]),
        "· 剧情零件 %d 条" % n,
    ]
    if ctx["hook"]:
        parts.append("· 一句话梗")
    if ctx["design"]:
        parts.append("· 情节设计")
    if ctx["user_prompt"]:
        parts.append("· 你写的补充提示词")
    refs = ctx.get("reference_cards") or []
    if refs:
        parts.append("· 分类素材摘要 %d 条（只发摘要，不发原文）" % len(refs))
    parts.append("合计约 %d 字。" % chars)
    if ctx["blocked"]:
        parts.append(
            "另外有 %d 条零件来自标了「仅本地」的素材，已经自动排除、不会发出。"
            % len(ctx["blocked"]))
    parts.append("这些都是你自己的数据，除了你选的模型之外不会给任何人。")
    return "\n".join(parts)


# ----------------------------------------------------------------------
# 发起任务
# ----------------------------------------------------------------------


def _input_hash(snapshot, model_keys, prompt_version):
    """算这次生成的输入指纹（任务书补充二.5）。

    世界观 + 角色卡快照 + 一句话梗 + 情节设计 + 字数 + 零件池 +
    素材参考开关/上限 + 模型 + 提示词版本，全部喂进一个 hash。
    两个输入完全相同 = 同一个 hash = 能命中缓存、不重复收费。

    【为什么不直接用 input_json 字符串】input_json 里有些字段带时间戳
    或排序不稳定的数组，直接 hash 会误判成"不同输入"。这里只挑
    "内容真正变了才会变"的字段，排序后再序列化，保证幂等。
    """
    def norm(v):
        if isinstance(v, dict):
            return {k: norm(x) for k, x in sorted(v.items())}
        if isinstance(v, list):
            return [norm(x) for x in v]
        return v

    payload = {
        "worldview": snapshot.get("worldview") or "",
        "world_name": snapshot.get("world_name") or "",
        "worldview_id": snapshot.get("worldview_id") or None,
        "character_ids": snapshot.get("character_ids") or [],
        "character_snapshot": snapshot.get("character_snapshot") or [],
        "one_sentence_hook": snapshot.get("one_sentence_hook") or "",
        "plot_design": snapshot.get("plot_design") or "",
        "target_words": snapshot.get("target_words") or 0,
        "pool_source": snapshot.get("pool_source") or "",
        "blocked_plot_ids": snapshot.get("blocked_plot_ids") or [],
        "reference_enabled": bool(snapshot.get("reference_enabled")),
        "reference_limit": snapshot.get("reference_limit") or 0,
        "model_keys": list(model_keys or []),
        "prompt_version": prompt_version or "",
    }
    raw = json.dumps(norm(payload), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _find_cached_run(conn, owner, input_hash):
    """找"输入完全一样、而且已经成功跑完"的历史任务（缓存命中）。

    补充二.5 的语义：相同输入直接读草稿、不重复收费。
    【为什么要求 status 是完成】排队中/跑着的任务结果还没出来，
    读它等于拿半成品骗她；失败的更不能复用。只认跑完的。
    【为什么排除 retry】重试的任务本身就是"同一份输入再来一次"，
    拿它当缓存源会把自己绕进去。
    """
    if not input_hash:
        return None
    row = conn.execute(
        "SELECT id FROM outline_runs WHERE owner_id=? AND input_hash=?"
        " AND status=? AND (retry_of_run_id IS NULL OR retry_of_run_id=0)"
        " ORDER BY id DESC LIMIT 1",
        (owner, input_hash, RUN_COMPLETED)).fetchone()
    return row["id"] if row else None


def create_run(owner, data, background=True, retry_of_run_id=None,
               model_keys=None, ctx=None):
    """发起一次生成。返回 (结果字典, run_id)。

    立刻返回 run_id，活儿在后台线程里干（计划第十四.11：
    `不要把大纲生成任务放在长时间同步 HTTP 请求中`）。
    """
    data = data or {}

    # ---- 硬校验：只剩字数 ----
    # 世界观和角色卡 2026-10-07 由折腰拍板改成**选填**：
    #   原话「世界观跟角色卡改成非必填」。
    # 两者为空时照样能跑 —— 提示词里对应的只是两块空内容，不会报错；
    # 代价是那份大纲少了两样最重的输入，质量靠她自己把握。
    # 仍然放在**建任务之前**（而不是线程里）：她点了就该立刻看到
    # "哪儿没填"，而不是等半分钟从任务状态里读出"失败"。
    ctx = ctx or build_ctx(owner, data)
    if ctx["target_words"] < odb.TARGET_WORDS_MIN:
        return {"ok": False, "reason": "bad_words",
                "message": "预期字数至少 %d 字。" % odb.TARGET_WORDS_MIN}, None

    keys = model_keys if model_keys is not None else (data.get("model_keys") or [])
    if isinstance(keys, str):
        keys = [keys]
    keys = [str(k).strip() for k in keys if str(k or "").strip()]
    if not keys:
        try:
            keys = [cls.pick_model(None)["key"]]
        except ValueError as e:
            return {"ok": False, "reason": "no_model",
                    "message": str(e)}, None
    keys = keys[:MAX_MODELS]

    ok_keys, bad = [], []
    for k in keys:
        try:
            cls.pick_model(k)
            ok_keys.append(k)
        except ValueError as e:
            bad.append({"key": k, "message": str(e)})
    if not ok_keys:
        return {"ok": False, "reason": "no_model",
                "message": bad[0]["message"] if bad else "选中的模型都用不了。"}, None

    # 提示词快照 + 提示词库引用（跟内化一个规矩：任务级快照）
    prompt_id = 0
    try:
        prompt_id = int(data.get("prompt_id") or 0)
    except (TypeError, ValueError):
        prompt_id = 0
    if prompt_id:
        try:
            item = cls.resolve_prompt_for_use(owner, prompt_id,
                                             kind=USER_PROMPT_KIND_OUTLINE)
        except ValueError as e:
            return {"ok": False, "reason": "no_prompt", "message": str(e)}, None
        if not item:
            return {"ok": False, "reason": "no_prompt",
                    "message": "找不到你挑的那条提示词，可能已经被删了。"}, None
        ctx["user_prompt"] = item["content"]
        ctx["prompt_ref_id"] = item["id"]
        ctx["prompt_name"] = item.get("name") or ""
        ctx["prompt_owner"] = item.get("owner_id") or ""
    else:
        ctx["prompt_ref_id"] = 0
        ctx["prompt_name"] = ""
        ctx["prompt_owner"] = ""
        ctx["user_prompt"] = odb._txt(ctx.get("user_prompt"), USER_PROMPT_MAX,
                                      "补充提示词")

    tpl, src, warn = prompt_template()
    ctx["template"] = tpl
    ctx["prompt_src"] = src
    ctx["prompt_version"] = PROMPT_VERSION if src == "file" else PROMPT_VERSION_GENERIC

    # 发送字数（预览里已经算过，这里再算一次存进任务里）
    msgs = build_messages(ctx)
    send_chars = sum(len(m["content"]) for m in msgs)

    input_snapshot = {
        "worldview": ctx["worldview"],
        "world_name": ctx["world_name"],
        "worldview_id": ctx["worldview_id"],
        "character_ids": ctx["character_ids"],
        "character_snapshot": ctx["characters"],
        "one_sentence_hook": ctx["hook"],
        "plot_design": ctx["design"],
        "target_words": ctx["target_words"],
        "pool_source": ctx["pool_source"],
        "blocked_plot_ids": [p["id"] for p in ctx["blocked"]],
        # 分类素材参考的开关和上限（跑的过程中不再读界面）
        "reference_enabled": ctx.get("reference_enabled", False),
        "reference_limit": ctx.get("reference_limit", odb.REFERENCE_DEFAULT),
    }
    input_hash = _input_hash(input_snapshot, ok_keys, ctx["prompt_version"])

    ts = now_str()
    with _worker_lock:
        with db.connect() as conn:
            # 【缓存命中（补充二.5）】输入一模一样、且已经成功跑完过一次。
            # 这里**不偷偷吞掉她的重新生成** —— 只把上次那个 run 报回去，
            # 让前端问一句"要不要直接看上次的结果"，她点头才省这笔钱。
            # 她要是想重摇一遍（模型输出本来就不确定），点"重新生成"就行。
            cached_run_id = None
            if not retry_of_run_id and not data.get("cache_bypass"):
                cached_run_id = _find_cached_run(conn, owner, input_hash)
            if cached_run_id:
                return {"ok": True, "reason": "cache_hit",
                        "cached_run_id": cached_run_id,
                        "run_id": cached_run_id,
                        "status": RUN_COMPLETED,
                        "message": "这份输入你之前跑过一模一样的（任务 #%d）。"
                                   "可以直接看上次的结果，不用再花一次钱。"
                                   % cached_run_id}, cached_run_id

            # 【一个账号同时只能有一个大纲任务在跑】
            # 跟内化那边同一个判断：她连点两下、或者两个页面同时提交，
            # 两份任务各自并发打模型，钱是双份的，而且她自己都不知道。
            row = conn.execute(
                "SELECT id FROM outline_runs WHERE owner_id=? AND status IN (%s)"
                % ",".join("?" * len(RUN_ACTIVE)),
                [owner] + list(RUN_ACTIVE)).fetchone()
            if row and not retry_of_run_id:
                return {"ok": False, "reason": "busy",
                        "message": "已经有一个大纲任务在跑了（#%d）。"
                                   "等它跑完，或者先去把它取消。" % row["id"]}, None
            cur = conn.execute(
                """INSERT INTO outline_runs
                   (owner_id, outline_type, input_json, target_words,
                    model_keys_json, prompt_version, prompt_ref_id, prompt_name,
                    user_prompt, user_prompt_len, cand_plot_ids_json, status,
                    total_models, total_input_chars, retry_of_run_id,
                    input_hash, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (owner, ctx["outline_type"], odb._dumps(input_snapshot),
                 ctx["target_words"], odb._dumps(ok_keys),
                 ctx["prompt_version"], ctx["prompt_ref_id"], ctx["prompt_name"],
                 ctx["user_prompt"], len(ctx["user_prompt"]),
                 odb._dumps(ctx["pool_ids"]), RUN_QUEUED, len(ok_keys),
                 send_chars, retry_of_run_id, input_hash, ts))
            run_id = cur.lastrowid

    # ---- 记下"这一次实际采用了哪几条学习条目"（需求第 8 条）----
    # 【为什么必须落库】从这里开始，同一份输入哈希的 run 可能被缓存命中，
    # 也可能被重跑；她以后问"我明明接受了 5 条，为什么这次生成没变化"——
    # 只有这条记录能回答"这次只注入了 2 条"或"那 3 条是仅本篇"。
    #
    # ★ 阶段三：记录的活儿搬到 _execute 里去做了 ——
    #   那里才有**真实生成用的那份 ctx**。原来在这个位置记的是
    #   `build_ctx`（预览）算出来的那一批，跟真正发给模型的对不上：
    #   预览按 need 挑完是一批、真跑时如果开关被改过又是另一批，
    #   事后回看就成了一句空话。审计信息必须记"真发出去的那一份"。

    if background:
        t = threading.Thread(target=_run_worker, args=(run_id, owner),
                             name="outline-run-%d" % run_id, daemon=True)
        t.start()
        return {"ok": True, "run_id": run_id, "status": RUN_QUEUED,
                "stream_id": stream_id_for(run_id),
                "model_count": len(ok_keys), "models": ok_keys,
                "send_chars": send_chars,
                "skipped_models": bad,
                "message": "已开始，%d 个模型正在分别生成。" % len(ok_keys)}, run_id

    _execute(run_id, owner)
    return {"ok": True, "run_id": run_id, "status": RUN_COMPLETED}, run_id


def stream_id_for(run_id):
    """这次大纲任务在实时通道里的名字。

    【为什么单独一个函数】前端订阅、后端推送都得用同一个名字。
    两边各拼一个字符串的话，改前缀时就会变成"一切正常，就是没有字"——
    不报错的那种坏法最难查。所以只留这一个出口。
    """
    return "ol-%s" % (run_id,)


def _run_worker(run_id, owner):
    """后台线程的入口。什么都兜住 —— 线程里冒出来的异常没人接。

    【实时通道也在这里开和关】
    开一条流让页面能看"四个模型各自正在吐什么"。关必须放 finally：
    漏关的话那条连接会一直挂着等一个永远不来的结尾，
    页面上就是"字停住了但还在转圈" —— 比看不到流更让人不安。
    """
    sid = stream_id_for(run_id)
    livestream.open_stream(sid, {"kind": "outline", "run_id": run_id})
    try:
        _execute(run_id, owner)
    except Exception as e:                                   # pragma: no cover
        import traceback
        traceback.print_exc()
        try:
            with db.connect() as conn:
                _set_run(conn, run_id,
                         status=RUN_FAILED,
                         error="任务跑了但中途出错：%s" % e,
                         finished_at=now_str())
        except Exception:
            pass
        livestream.note(sid, "任务中途出错：%s" % e, level="bad")
    finally:
        livestream.close(sid)


def _set_run(conn, run_id, **fields):
    if not fields:
        return
    sets = ", ".join("%s=?" % k for k in fields)
    conn.execute("UPDATE outline_runs SET %s WHERE id=?" % sets,
                 list(fields.values()) + [run_id])


def _cancelled(run_id):
    with db.connect() as conn:
        r = conn.execute("SELECT status FROM outline_runs WHERE id=?",
                         (run_id,)).fetchone()
    return bool(r) and r["status"] == RUN_CANCELLED


# ----------------------------------------------------------------------
# 真正干活
# ----------------------------------------------------------------------

def _execute(run_id, owner):
    with db.connect() as conn:
        run = conn.execute("SELECT * FROM outline_runs WHERE id=?",
                           (run_id,)).fetchone()
        if not run:
            return
        if run["status"] == RUN_CANCELLED:
            return
        model_keys = odb._loads(run["model_keys_json"], [])
        input_snapshot = odb._loads(run["input_json"], {})
        pool_ids = odb._loads(run["cand_plot_ids_json"], [])
        prompt_version = run["prompt_version"] or prompt_version()
        user_prompt = run["user_prompt"] or ""

    # ---- 模板在任务开头解析一次就固定下来 ----
    # 为什么不在每个模型里现读文件：她跑的过程中改了 prompts/outline.txt，
    # 同一个任务的几个模型就会用两版提示词生成 —— 结果并排放着，
    # 而她以为差别只是模型不同。
    tpl, src, warn = prompt_template()

    # ---- ★ 阶段三：真实生成也要把「她的改写取向」发进去 ----
    # 【这里曾经是个静默的洞】`_system_block` 一直在读 ctx["rewrites"]，
    # 但 _execute 造的这个 ctx **从来没有塞过 rewrites** —— 于是
    # 预览页算得漂漂亮亮、"哪几条会进生成"也说得头头是道，
    # 真正跑起来却一条改写建议都没发给模型。她只会觉得"学了半天没变化"，
    # 而屏幕上不会有任何一处报错。
    # 【为什么用快照里的开关，而不是现读界面】跟世界观快照同一个道理：
    # 任务发起那一刻她勾没勾「参考以前学过的」，就该按那一刻算 ——
    # 跑的过程中她改界面不该影响已经跑着的任务。
    use_learning = input_snapshot.get("use_learning")
    if use_learning is None:
        use_learning = True
    # 相关性挑选要用"这次要写什么"，输入快照里正好有这几样。
    rewrite_need = odb.rewrite_need(input_snapshot)
    rewrites = (odb.rewrite_examples(owner, need=rewrite_need)
                if use_learning else [])

    ctx = {
        "owner": owner,     # A 规划后按节点需求检索分类素材要用
        "template": tpl,
        "prompt_src": src,
        "worldview": input_snapshot.get("worldview") or "",
        "characters": input_snapshot.get("character_snapshot") or [],
        "hook": input_snapshot.get("one_sentence_hook") or "",
        "design": input_snapshot.get("plot_design") or "",
        "target_words": int(input_snapshot.get("target_words") or 0),
        "tier": odb.word_tier(input_snapshot.get("target_words") or 0),
        "plots": odb.plot_blocks(owner, pool_ids),
        "user_prompt": user_prompt,
        # ★ 阶段三：不传这块，_system_block 里【她的改写取向】就永远是空的。
        "learning": (odb.learning_examples(owner, limit=3)
                     if use_learning else []),
        "rewrites": rewrites,
        "model_keys": model_keys,
        "prompt_version": prompt_version,
        # input_hash 跟着任务走（补充二.5）：写进候选行，方便对账
        # "这份候选是用哪一版输入跑出来的、有没有缓存命中"。
        "input_hash": run["input_hash"] or "",
        # 分类素材参考：开关和上限在发起任务那一刻就定死（存进快照），
        # 跑的过程中她改界面不影响已经跑着的任务 —— 跟世界观快照同一个道理。
        "reference_enabled": bool(input_snapshot.get("reference_enabled")),
        "reference_limit": int(input_snapshot.get("reference_limit")
                               or odb.REFERENCE_DEFAULT),
        "reference_cards": [],
    }
    # 零件块要把参考次数带上（模型要按"用过几次"权衡新鲜度）
    counts = odb.plot_ref_counts(owner, pool_ids) if pool_ids else {}
    for p in ctx["plots"]:
        p["ref_count"] = counts.get(p["id"], 0)

    with db.connect() as conn:
        _set_run(conn, run_id, status=RUN_RUNNING, started_at=now_str(),
                 heartbeat_at=now_str(), error=warn or "")
        for mk in model_keys:
            conn.execute(
                """INSERT OR IGNORE INTO outline_candidates
                   (run_id, owner_id, model_key, model_name, prompt_version,
                    status, created_at) VALUES (?,?,?,?,?,?,?)""",
                (run_id, owner, mk, _model_label(mk), prompt_version,
                 CAND_QUEUED, now_str()))

    # ---- ★ 阶段三：把"这一次真发给模型了哪几条学习条目"落库 ----
    # 【为什么放在这里，不放 create_run】这里才有**真实生成用的那份 ctx**。
    # create_run 里能拿到的是 build_ctx（预览）的结果 —— 两者按 need 挑出来
    # 的那一批不一定相同。审计记录必须记"真发出去的那一份"，否则她事后
    # 回看会看到一份跟这次输出对不上的清单，比没有记录更坏。
    # 两块分开记（stage=rewrite / case），界面才能分别说清各发了几条。
    # 写失败绝不能拖垮生成（它只是审计信息），所以整段兜住。
    try:
        used = []
        for r in (ctx.get("rewrites") or []):
            for p in (r.get("points") or []):
                used.append(p)
        if used:
            odb.record_learning_use(owner, run_id, used, batch="plan",
                                    stage="rewrite")
        cases = ctx.get("learning") or []
        if cases:
            odb.record_learning_use(
                owner, run_id,
                [{"id": None, "rewrite_id": None,
                  "point": (c.get("problem") or "")[:200],
                  "method": (c.get("note") or "")[:400],
                  "scope": "", "confidence": "",
                  "source_version": ""} for c in cases],
                batch="plan", stage="case")
    except Exception:                                       # pragma: no cover
        pass

    # ---- 多模型并发 ----
    # 为什么并发：她挑四个模型，串行跑要等四倍时间（一次一两分钟），
    # 而四个模型的输入完全一样、互不依赖。
    # 为什么有上限（MAX_MODELS=4）：并发打同一家的限流，八个请求
    # 多半集体 429。而且八份候选她也比不过来。
    todo = [k for k in model_keys]
    if todo:
        with ThreadPoolExecutor(max_workers=min(MAX_MODELS, len(todo))) as ex:
            for mk in todo:
                if _cancelled(run_id):
                    break
                ex.submit(_run_one_model, run_id, owner, mk, ctx)

    # ---- 汇总 ----
    with db.connect() as conn:
        r = conn.execute("SELECT status FROM outline_runs WHERE id=?",
                         (run_id,)).fetchone()
        cur_status = r["status"] if r else RUN_RUNNING
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM outline_candidates"
            " WHERE run_id=? GROUP BY status", (run_id,)).fetchall()
        by = {x["status"]: x["n"] for x in rows}
        done = by.get(CAND_DONE, 0)
        failed = by.get(CAND_FAILED, 0)
        if cur_status == RUN_CANCELLED:
            # 取消之后**不改状态**（她自己点的取消，界面已经显示了），
            # 但已经把结果补写进去了 —— 跑完的不浪费。
            _set_run(conn, run_id, done_models=done, failed_models=failed,
                     heartbeat_at=now_str(), finished_at=now_str())
        else:
            if done and not failed:
                st = RUN_COMPLETED
            elif done and failed:
                st = RUN_PARTIAL
            else:
                st = RUN_FAILED
            errs = [x["error"] for x in conn.execute(
                "SELECT error FROM outline_candidates WHERE run_id=?"
                " AND status=? AND error<>''", (run_id, CAND_FAILED)).fetchall()]
            _set_run(conn, run_id, status=st, done_models=done,
                     failed_models=failed,
                     error="；".join(errs[:2])[:2000],
                     heartbeat_at=now_str(), finished_at=now_str())


def _model_label(key):
    try:
        m = cls.llm.get_model(key)
        return (m.get("label") or key) if m else key
    except Exception:                                        # pragma: no cover
        return key


# ----------------------------------------------------------------------
# 分阶段生成：A 规划 → B 节点分批 → C 检查 → D 只补缺失
# ----------------------------------------------------------------------
#
# 【为什么要把"一次长请求"拆开】
# 她最大的两个抱怨：大纲在结尾被掐断、节点之间没因果。两者根子是同一个：
# 让模型一次吐出整篇几千字，字数上限一到，最后几段就断了；而一次生成的
# 注意力也顾不齐"每个节点怎么接下一个"。
# 拆成"先规划骨架 → 再按 2~4 个节点分批写细 → 收尾补高潮结局"之后：
#   · 每一批都很短，不会撞字数上限；就算撞了，只续写这一批，不动前面的
#   · 每批都把上一批最后一段喂进去，因果链是显式接上的，不是模型"自觉"
#   · 检查发现缺高潮/结局/断号时，只补那一点，不重写整篇（不重花钱）

NODE_BATCH = 3          # 一批写几个节点（任务书 2~4，取中间值）


def _chat_json(cfg, msgs, opts):
    """发一次 JSON 请求。返回 (解析出的对象或 None, usage, finish_reason, 错误串)。

    finish_reason 一路带上 —— 它是"这一批是不是被字数上限掐断"的唯一硬证据，
    跟单次长请求时代一样关键（见 _run_one_model 里那段注释）。
    """
    try:
        out = cls.llm.chat(cfg, msgs, json_mode=True, **opts)
    except Exception as e:
        # json_mode 不被支持时退一次（跟分类、内化同一套兜底）
        if getattr(e, "status", None) == 400:
            try:
                out = cls.llm.chat(cfg, msgs, json_mode=False, **opts)
            except Exception as e2:
                return None, {}, "", str(e2)
        else:
            return None, {}, "", str(e)
    raw = (out.get("content") or "").strip()
    finish = (out.get("finish_reason") or "").strip() or cls.llm.FINISH_UNKNOWN
    usage = out.get("usage") or {}
    if not raw:
        return None, usage, finish, "模型返回了空内容（可能是被截断或触发了内容策略）。"
    try:
        obj = _extract_json_object(raw)
    except ValueError as e:
        return None, usage, finish, str(e)
    return obj, usage, finish, ""


def _merge_usage(total, one):
    """把一次请求的 usage 累进 total（prompt/completion/total 三项）。"""
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        try:
            total[k] = total.get(k, 0) + int(one.get(k) or 0)
        except (TypeError, ValueError):
            pass
    return total


def _generate_staged(ctx, cfg, opts):
    """分阶段生成一份大纲。返回 (payload_dict, meta)。

    payload_dict 与 clean_outline_payload 的输出完全兼容，下游
    （候选卡渲染、diff、学习、落库）一个字都不用改。

    meta 记：每个阶段几批、token 累加、每批的 finish_reason、给她的警告、
    以及 A2 检索到的素材（reference_items —— 调用方要用它清洗节点上的
    reference 编号，函数内部改的 ctx 出不去，必须从 meta 带回来）。
    """
    known = {p["id"] for p in ctx["plots"]}
    target = int(ctx.get("target_words") or 0)
    tier = ctx.get("tier") or odb.word_tier(target)
    usage_total = {}
    stage_notes = []       # 给界面的分阶段进度（"规划 1 次 / 节点 3 批 / 收尾 1 次"）
    warns = []
    input_chars = 0

    # ============ A 规划 ============
    msgs = _staged_messages(ctx, "plan")
    input_chars += sum(len(m["content"]) for m in msgs)
    plan, u, fin, err = _chat_json(cfg, msgs, opts)
    _merge_usage(usage_total, u)
    if plan is None:
        raise ValueError("规划阶段失败：%s" % err)
    if not isinstance(plan, dict):
        plan = {}
    node_plan = plan.get("node_plan") or []
    if not isinstance(node_plan, list) or not node_plan:
        raise ValueError("规划阶段没给出节点骨架（node_plan 是空的）。")
    # 骨架编号由我们统一编 n1..nN —— 模型的编号可能跳、可能重，
    # 不在这里归一的话，后面"断号检查"和"排序"全错位。
    node_plan = [dict(x) if isinstance(x, dict) else {} for x in node_plan]
    for i, x in enumerate(node_plan, 1):
        x["node_id"] = "n%d" % i
    expected = len(node_plan)
    stage_notes.append("规划 1 次，%d 个节点" % expected)

    # ============ A2 按节点需求检索分类素材 ============
    # 规划阶段不带任何分类素材（任务书补充二.2：素材不参与第一轮全局规划），
    # 规划吐出来的 reference_needs 才是真正的需求清单 ——
    # 拿它去本地检索，检索结果喂给 B（节点）阶段。
    ref_items = []
    ref_meta = {"needs": 0, "candidates": 0, "injected": 0}
    if ctx.get("reference_enabled") and (ctx.get("reference_limit") or 0) > 0:
        needs = []
        for x in node_plan:
            rn = x.get("reference_needs")
            if isinstance(rn, list):
                needs.extend(str(v) for v in rn)
            elif isinstance(rn, str) and rn.strip():
                needs.append(rn.strip())
        ref_meta["needs"] = len(needs)
        try:
            found = odb.search_reference_cards(
                owner=ctx.get("owner") or "", needs=needs,
                limit=ctx.get("reference_limit"))
            ref_items = odb.render_reference_items(found)
        except Exception as e:                               # pragma: no cover
            warns.append("分类素材检索出错（不拦住生成）：%s" % str(e)[:120])
        ref_meta["injected"] = len(ref_items)
        if ref_items:
            # 换上检索结果 —— B 阶段的 _system_block 会把这块摘要发给模型。
            ctx = dict(ctx)
            ctx["reference_cards"] = ref_items
            stage_notes.append("参考素材 %d 条" % len(ref_items))
        else:
            # 有需求但一条没命中：把素材块换成"没有"，别让上一轮残留的
            # 候选摘要混进 B 阶段的提示词。
            ctx = dict(ctx)
            ctx["reference_cards"] = []
    ref_meta["candidates"] = ref_meta["injected"]

    # ============ B 节点分批写细 ============
    nodes = []              # 已写好的完整节点（按顺序）
    by_id = {}              # node_id → 节点，用于衔接和断号检查
    batch_plans = [node_plan[i:i + NODE_BATCH]
                   for i in range(0, len(node_plan), NODE_BATCH)]
    prev_tail = ""          # 上一批最后一段的因果尾巴，喂给下一批作衔接

    for bi, batch in enumerate(batch_plans, 1):
        # 这一批要写哪些节点 + 它们各自的骨架要求
        spec = "\n".join(
            "- %s：%s（约 %s 字；必须发生：%s）"
            % (str(x.get("node_id") or ""), str(x.get("purpose") or "")[:80],
               x.get("estimated_words") or 0,
               str(x.get("required_event") or "")[:120])
            for x in batch)
        ctx_note = ("这一批要写这些节点（编号必须一致）：\n%s\n" % spec)
        if prev_tail:
            ctx_note += ("上一批最后写到的因果是：%s\n"
                         "这一批第一段要从这里接着往下走。\n" % prev_tail[:300])
        ctx_note += ("上一批已经写好的节点（只作上下文，不要重写）：%s\n"
                     % ("、".join(by_id.keys()) if by_id else "（这是第一批）"))
        msgs = _staged_messages(ctx, "nodes", ctx_note)
        input_chars += sum(len(m["content"]) for m in msgs)
        obj, u, fin, err = _chat_json(cfg, msgs, opts)
        _merge_usage(usage_total, u)
        if obj is None:
            # 这一批整批失败：记警告，跳到下一批（别让一个批坏掉整篇）。
            # 后面 C 检查会抓出缺的节点，交给 D 补。
            warns.append("第 %d 批节点写失败（%s），后面检查会补。"
                         % (bi, err[:120]))
            stage_notes.append("第 %d 批失败" % bi)
            continue
        batch_nodes = obj.get("nodes") if isinstance(obj, dict) else None
        if not isinstance(batch_nodes, list):
            batch_nodes = []
        for nv in batch_nodes:
            if not isinstance(nv, dict):
                continue
            nid = str(nv.get("node_id") or "").strip()
            if not nid:
                continue
            if nid in by_id:   # 模型重写/重复给同一编号，跳过
                continue
            by_id[nid] = nv
            nodes.append(nv)
        # 记这一批的尾巴，给下一批衔接
        if batch_nodes:
            last = batch_nodes[-1]
            tail = " ".join(str(last.get(k) or "") for k in
                            ("event", "result", "connection_to_next"))
            prev_tail = tail[:300]
        # 被字数上限掐断：这一批可能没写全，C 会抓出来，D 补
        if fin == cls.llm.FINISH_LENGTH:
            warns.append("第 %d 批被字数上限掐断，可能有节点没写完，"
                         "检查后会补。" % bi)
        stage_notes.append("第 %d 批 %s" % (bi, "完成" if obj is not None else "失败"))

    # ============ C 收尾（补高潮/结局/总览） ============
    msgs = _staged_messages(ctx, "finalize")
    input_chars += sum(len(m["content"]) for m in msgs)
    finz, u, fin, err = _chat_json(cfg, msgs, opts)
    _merge_usage(usage_total, u)
    tail_fields = {}
    if isinstance(finz, dict):
        tail_fields = finz
    elif finz is not None:
        warns.append("收尾阶段没返回对象（%s），高潮结局可能缺失。" % err[:120])
    stage_notes.append("收尾 1 次")

    # ============ 拼最终 payload（与 clean_outline_payload 兼容）============
    payload = {
        "title_candidates": tail_fields.get("title_candidates") or [],
        "story_core": str(plan.get("story_core") or tail_fields.get("story_core") or ""),
        "theme_tone": str(tail_fields.get("theme_tone") or ""),
        "character_functions": plan.get("character_functions") or [],
        "overview": str(tail_fields.get("overview") or ""),
        "nodes": nodes,
        "climax": str(tail_fields.get("climax") or ""),
        "ending": str(tail_fields.get("ending") or ""),
        "logic_risks": tail_fields.get("logic_risks") or plan.get("logic_risks") or [],
    }

    # ============ C 检查（本地硬检查）============
    # 断号 / 缺节点：plan 里声明了 N 个，实际写出来 M 个
    planned_ids = [str(x.get("node_id") or "") for x in node_plan]
    got_ids = set(by_id.keys())
    missing = [x for x in planned_ids if x and x not in got_ids]
    if missing:
        warns.append("缺 %d 个节点没写出来：%s" % (len(missing),
                    "、".join(missing[:5])))

    # ============ D 只补缺失 ============
    if missing:
        msgs = _staged_messages(
            ctx, "repair",
            "缺这些节点，只补它们（编号必须一致）：%s\n"
            "每个节点写到完整程度，字段跟前面对齐。" % "、".join(missing))
        input_chars += sum(len(m["content"]) for m in msgs)
        rep, u, fin, err = _chat_json(cfg, msgs, opts)
        _merge_usage(usage_total, u)
        stage_notes.append("补缺 1 次")
        if isinstance(rep, dict):
            rep_nodes = rep.get("nodes") or []
            for nv in (rep_nodes if isinstance(rep_nodes, list) else []):
                if isinstance(nv, dict):
                    nid = str(nv.get("node_id") or "").strip()
                    if nid and nid in planned_ids and nid not in by_id:
                        by_id[nid] = nv
                        nodes.append(nv)
            # 补缺也可能顺便补了高潮结局
            if not payload["climax"] and rep.get("climax"):
                payload["climax"] = str(rep["climax"])
            if not payload["ending"] and rep.get("ending"):
                payload["ending"] = str(rep["ending"])
            if not payload["overview"] and rep.get("overview"):
                payload["overview"] = str(rep["overview"])
        else:
            warns.append("补缺失节点也失败了（%s）。" % err[:120])

    # nodes 按 plan 顺序重排（分批写出来可能乱序）
    order = {nid: i for i, nid in enumerate(planned_ids)}
    nodes.sort(key=lambda n: order.get(str(n.get("node_id") or ""), 9999))
    payload["nodes"] = nodes

    meta = {
        "usage_total": usage_total,
        "input_chars": input_chars,
        "output_chars": sum(len(str(n)) for n in nodes),
        "stage_notes": stage_notes,
        "warns": warns,
        "known": known,
        # A2 检索到的素材（ref_card_id 列表给清洗用，完整条目给记录用）
        "reference_items": ref_items,
        "reference_meta": ref_meta,
    }
    return payload, meta


def _run_one_model(run_id, owner, model_key, ctx):
    """一个模型的一次生成。**每个模型只碰自己那一行候选**，
    绝不读别的模型的结果（计划第五.5：不同模型不能互相读取结果）。"""
    t0 = time.time()
    label = _model_label(model_key)
    with db.connect() as conn:
        conn.execute("UPDATE outline_candidates SET status=?, model_name=?"
                     " WHERE run_id=? AND model_key=?",
                     (CAND_RUNNING, label, run_id, model_key))
        _set_run(conn, run_id, heartbeat_at=now_str())

    def _fail(msg):
        with db.connect() as conn:
            conn.execute(
                "UPDATE outline_candidates SET status=?, error=?, elapsed_ms=?"
                " WHERE run_id=? AND model_key=?",
                (CAND_FAILED, (msg or "")[:4000], int((time.time() - t0) * 1000),
                 run_id, model_key))

    try:
        cfg = cls.pick_model(model_key)
    except ValueError as e:
        _fail(str(e))
        return

    msgs = build_messages(ctx)
    input_chars = sum(len(m["content"]) for m in msgs)
    # 超时和重试次数显式给。吃 llm 的默认（180 秒 × 3 次）等于必然白等 9 分钟，
    # 理由见 OUTLINE_TIMEOUT / OUTLINE_MAX_RETRY 那两段注释。
    #
    # purpose / on_chunk 是"顺带记录"用的，不影响生成：
    #   purpose  让耗时表能分出"这是大纲的调用"，跟分类的别混在一起算
    #   on_chunk 每收到一块字就推到实时通道上，并**标上是哪个模型吐的** ——
    #            大纲是四五个模型同时跑，不标的话她看到的就是一堆
    #            交错在一起的乱码，反而更糊涂
    _sid = stream_id_for(run_id)
    _opts = dict(temperature=0.7, timeout=OUTLINE_TIMEOUT,
                 max_retry=OUTLINE_MAX_RETRY, purpose="outline",
                 on_chunk=lambda t: livestream.chunk(_sid, t, model=label))

    # ---- 分阶段生成：A 规划 → B 节点分批 → C 检查 → D 只补缺失 ----
    # 替代过去"一次 chat 出整篇"。理由见 _generate_staged 顶部注释。
    try:
        payload, meta = _generate_staged(ctx, cfg, _opts)
    except ValueError as e:
        _fail(str(e))
        return
    except Exception as e:                                   # pragma: no cover
        _fail("分阶段生成时出错：%s" % e)
        return

    # ---- 解析 + 校验（跟老路径同一套清洗，下游全不动）----
    known = meta["known"]
    # 本次实际发出去的素材编号：以 A2 检索结果为准（meta 里的 reference_items），
    # 函数内部改的 ctx 传不出来，所以不能读 ctx。没开开关就是空集 ——
    # 空集会把节点上所有 reference 编号抹掉，这是对的（没发的素材谈不上参考）。
    ref_items = meta.get("reference_items") or []
    known_refs = {x.get("ref_card_id") for x in ref_items
                  if isinstance(x, dict) and x.get("ref_card_id")}
    input_chars = meta["input_chars"] or input_chars
    usage_total = meta["usage_total"]
    stage_notes = meta.get("stage_notes") or []
    try:
        obj, warns = odb.clean_outline_payload(payload, known, known_refs)
    except ValueError as e:
        _fail("模型的返回没法当成大纲用：%s" % e)
        return
    except Exception as e:                                   # pragma: no cover
        _fail("读模型返回时出错：%s" % e)
        return

    warns = list(warns) + list(meta.get("warns") or []) \
        + odb.validate_outline(obj, ctx["target_words"], known)

    # ---- 结束原因 ----
    # 分阶段之后，没有"一次长请求的 finish_reason"了。但"有没有哪一批被
    # 字数上限掐断"仍然要让她看见 —— 把这层信息压进第一条警告。
    # 判断依据：任何一批报过 length，或者补缺后节点仍有缺失（结构缺口）。
    if stage_notes:
        warns.insert(0, "分阶段生成：%s。" % " / ".join(stage_notes))

    # 结构缺口单独算一份存下来：列表接口 /api/outline-runs 不带 content_json，
    # 而候选卡上那行"体检结论"要在列表里就显示得出来。
    gaps = odb.structure_gaps(obj)

    text = odb.render_outline_text(obj)
    usage = usage_total
    names = {p["id"]: (p.get("title") or "") for p in ctx["plots"]}
    # 分阶段没有"一次长请求的 finish_reason"。记 stop 表示"编排正常跑完"；
    # "有没有写完"由 gaps_json 和 warnings 兜底（缺高潮/结局会进 gaps）。
    finish = cls.llm.FINISH_STOP
    output_chars = meta.get("output_chars") or len(text)

    # 节省效果记录（任务书补充二.10）：需求几条、候选几条、实际注入几条，
    # 连 input_hash 一起写进候选行，方便以后对账和调阈值。
    ref_meta = meta.get("reference_meta") or {}

    with db.connect() as conn:
        conn.execute(
            """UPDATE outline_candidates SET status=?, content_json=?,
               content_text=?, used_plot_ids_json=?, used_plot_names_json=?,
               warnings_json=?, raw_response=?, input_chars=?, output_chars=?,
               input_tokens=?, output_tokens=?, finish_reason=?, gaps_json=?,
               elapsed_ms=?, model_name=?, prompt_version=?,
               reference_meta_json=?, input_hash=?
               WHERE run_id=? AND model_key=?""",
            (CAND_DONE, odb._dumps(obj), text,
             odb._dumps(obj.get("used_plot_ids") or []),
             odb._dumps([names.get(i, "") for i in (obj.get("used_plot_ids") or [])]),
             odb._dumps(warns), odb._dumps(payload)[:200000], input_chars,
             output_chars,
             int(usage.get("prompt_tokens") or 0),
             int(usage.get("completion_tokens") or 0), finish, odb._dumps(gaps),
             int((time.time() - t0) * 1000), label,
             ctx.get("prompt_version") or "", odb._dumps(ref_meta),
             ctx.get("input_hash") or "", run_id, model_key))
        _set_run(conn, run_id, heartbeat_at=now_str())


# ----------------------------------------------------------------------
# 读模型返回
# ----------------------------------------------------------------------

class _NotOutlineJson(Exception):
    """抠出来的东西顶层不是对象。

    【为什么要有这么个小异常】不能直接用 ValueError 当标记 ——
    json.loads 解不开时抛的 JSONDecodeError **就是** ValueError 的子类，
    用 ValueError 区分"JSON 语法错"和"顶层类型不对"会把两者混成一种：
    于是"前面有废话、后面有废话"这种最常见的返回会走不到"截大括号重试"
    那一步，直接判成失败。血泪教训，别合回去。
    """
    pass


def _extract_json_object(text):
    """从模型返回的文本里抠出 JSON 对象。

    模型很爱在 JSON 外面裹东西：```json 围栏、「好的，这就给你」、
    末尾再补一句「以上」。这些都是常态，不是极端情况。
    三步走：剥围栏 → 直接 parse → 找第一个 { 到最后一个 } 截出来 parse。
    """
    s = (text or "").strip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl >= 0:
            s = s[nl + 1:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3].rstrip()

    def _as_obj(d):
        if isinstance(d, dict):
            return d
        if isinstance(d, list):
            return {"nodes": d}          # 只给了节点数组，包一层
        raise _NotOutlineJson("返回的顶层是 %s，不是对象。" % type(d).__name__)

    try:
        return _as_obj(json.loads(s))
    except _NotOutlineJson as e:
        raise ValueError(str(e))
    except Exception:
        pass

    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        try:
            return _as_obj(json.loads(s[i:j + 1]))
        except _NotOutlineJson as e:
            raise ValueError(str(e))
        except Exception:
            pass

    raise ValueError("模型的返回里找不到 JSON 对象。它说的前 200 字：%s" % s[:200])


# ----------------------------------------------------------------------
# 任务：读 / 取消 / 重试
# ----------------------------------------------------------------------

def _run_dict(row, with_input=False):
    if row is None:
        return None
    inp = odb._loads(row["input_json"], {})
    d = {
        "id": row["id"],
        "outline_type": row["outline_type"],
        "status": row["status"],
        # 界面靠这个名字订阅"模型正在吐什么"。后端推、前端订共用这一个出口，
        # 免得两边各拼一个字符串、改前缀时静默对不上（那种坏法不报错）。
        "stream_id": stream_id_for(row["id"]),
        "target_words": row["target_words"],
        "model_keys": odb._loads(row["model_keys_json"], []),
        "prompt_version": row["prompt_version"],
        "prompt_name": row["prompt_name"],
        "user_prompt_len": row["user_prompt_len"],
        "cand_plot_count": len(odb._loads(row["cand_plot_ids_json"], [])),
        "blocked_plot_count": len(inp.get("blocked_plot_ids") or []),
        "world_chars": len(inp.get("worldview") or ""),
        "character_count": len(inp.get("character_snapshot") or []),
        "has_hook": bool(inp.get("one_sentence_hook")),
        "has_design": bool(inp.get("plot_design")),
        "total_models": row["total_models"],
        "done_models": row["done_models"],
        "failed_models": row["failed_models"],
        "total_input_chars": row["total_input_chars"],
        "saved_outline_id": row["saved_outline_id"],
        "retry_of_run_id": row["retry_of_run_id"],
        "note": row["note"],
        "error": row["error"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }
    # 【为什么只有单查才带 input】
    # 里面是世界观原文（最多三万字）和角色卡快照。列表一次给 8 条，
    # 全带上就是几十万字的白流量，而她列表上根本不用看这些。
    #
    # 【为什么前端需要它】她把一版候选推入大纲库时，要写进大纲的
    # 「世界观快照」必须是**生成那一刻**的那一份。从界面上现取的话，
    # 她要是生成完又改了世界观，存进大纲的就成了她新写的那段 ——
    # 跟 AI 实际看到的不一样，以后复盘时怎么都对不上。
    if with_input:
        d["input"] = inp
    return d


def get_run(run_id, owner):
    try:
        rid = int(run_id)
    except (TypeError, ValueError):
        return None
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM outline_runs WHERE id=? AND owner_id=?",
                           (rid, owner)).fetchone()
        if not row:
            return None
        d = _run_dict(row, with_input=True)
        cands = conn.execute(
            "SELECT id, model_key, model_name, status, error, used_plot_ids_json,"
            " used_plot_names_json, warnings_json, output_chars, elapsed_ms,"
            " finish_reason, gaps_json, adopted, outline_id, reference_meta_json,"
            " input_hash FROM outline_candidates"
            " WHERE run_id=? ORDER BY id", (rid,)).fetchall()
    d["candidates"] = []
    for c in cands:
        d["candidates"].append({
            # run_id 必须带上：前端点「把这一版做成大纲」时要拿它去取
            # **生成那一刻**的输入快照（世界观 / 角色卡 / 一句话梗），
            # 不能取界面上此刻的值 —— 她生成完可能又改过世界观。
            # 少了它，前端 data-run 就是 undefined，"undefined" 转数字成 NaN，
            # 请求变成 /api/outline-runs/NaN（422），然后**静默**退回表单现值：
            # 存下来的大纲跟 AI 当时看到的那份根本不是一回事，而且不报错。
            "run_id": rid,
            "id": c["id"], "model_key": c["model_key"],
            "model_name": c["model_name"], "status": c["status"],
            "error": c["error"][:600] if c["error"] else "",
            "used_plot_ids": odb._loads(c["used_plot_ids_json"], []),
            "used_plot_names": odb._loads(c["used_plot_names_json"], []),
            "warnings": odb._loads(c["warnings_json"], []),
            "output_chars": c["output_chars"],
            "elapsed_ms": c["elapsed_ms"],
            # 候选卡上那条"体检结论"靠这两个 —— 结束原因翻成中文，
            # 结构缺口直接给一串短词。都不让她自己拼英文单词。
            "finish_reason": c["finish_reason"] or "",
            "finish_label": cls.llm.finish_label(c["finish_reason"]),
            "gaps": odb._loads(c["gaps_json"], []),
            "adopted": bool(c["adopted"]),
            "outline_id": c["outline_id"],
            # 分类素材参考的节省效果（补充二.10）：需求/候选/注入三个数。
            "reference_meta": odb._loads(c["reference_meta_json"], {}),
        })
    return d


def list_runs(owner, limit=20):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM outline_runs WHERE owner_id=?"
            " ORDER BY id DESC LIMIT ?", (owner, int(limit))).fetchall()
    return [_run_dict(r) for r in rows]


def get_candidate(candidate_id, owner):
    try:
        cid = int(candidate_id)
    except (TypeError, ValueError):
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM outline_candidates WHERE id=? AND owner_id=?",
            (cid, owner)).fetchone()
        if not row:
            return None
        d = dict(row)
        # raw_response **不下发**（又长又乱，她看的是结构化那几个字段）
        d.pop("raw_response", None)
        d["content_json"] = odb._loads(row["content_json"], {})
        d["used_plot_ids"] = odb._loads(row["used_plot_ids_json"], [])
        d["used_plot_names"] = odb._loads(row["used_plot_names_json"], [])
        d["warnings"] = odb._loads(row["warnings_json"], [])
        d["gaps"] = odb._loads(row["gaps_json"], [])
        d["finish_label"] = cls.llm.finish_label(row["finish_reason"])
        d["reference_meta"] = odb._loads(row["reference_meta_json"], {})
        return d


def cancel_run(run_id, owner):
    """取消。**已经在路上的模型不会被打断** —— 见文件头第三节。

    所以这里如实告诉她会花掉多少钱：跑完的那几个结果会留着。
    """
    try:
        rid = int(run_id)
    except (TypeError, ValueError):
        raise ValueError("没说是哪个任务。")
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM outline_runs WHERE id=?"
                           " AND owner_id=?", (rid, owner)).fetchone()
        if not row:
            raise ValueError("没有这个大纲任务")
        if row["status"] not in RUN_ACTIVE:
            raise ValueError("这个任务已经结束了（%s），不用再取消。" % row["status"])
        _set_run(conn, rid, status=RUN_CANCELLED, finished_at=now_str())
        conn.execute("UPDATE outline_candidates SET status=?, error=?"
                     " WHERE run_id=? AND status=?",
                     (CAND_FAILED, "任务被取消了，这个模型没有发出去。",
                      rid, CAND_QUEUED))
    return True


def retry_run(run_id, owner, background=True):
    """重试。**只补真正没跑成的模型**，跑好的绝不重花钱。

    跟内化的 retry 是同一个思路：判断依据是"这个模型花过钱没有"，
    不是"结果好不好看"。一个候选只要 status=已完成，哪怕她嫌弃它，
    重试也不该再问一遍。
    """
    try:
        rid = int(run_id)
    except (TypeError, ValueError):
        raise ValueError("没说是哪个任务。")
    with db.connect() as conn:
        run = conn.execute("SELECT * FROM outline_runs WHERE id=? AND owner_id=?",
                           (rid, owner)).fetchone()
        if not run:
            raise ValueError("没有这个大纲任务")
        if run["status"] in RUN_ACTIVE:
            raise ValueError("这个任务还在跑，等它结束再说。")
        rows = conn.execute(
            "SELECT model_key, status FROM outline_candidates WHERE run_id=?",
            (rid,)).fetchall()
        failed = [r["model_key"] for r in rows if r["status"] != CAND_DONE]
        if not failed:
            raise ValueError("这次几个模型都跑成了，没有要补的。")

        # 沿用原任务那份输入和那句补充提示词 —— 不取"她现在写着什么"。
        snap = odb._loads(run["input_json"], {})
        data = {
            "worldview": snap.get("worldview") or "",
            "world_name": snap.get("world_name") or "",
            "worldview_id": snap.get("worldview_id"),
            "character_ids": snap.get("character_ids") or [],
            "one_sentence_hook": snap.get("one_sentence_hook") or "",
            "plot_design": snap.get("plot_design") or "",
            "target_words": run["target_words"],
            "plot_ids": odb._loads(run["cand_plot_ids_json"], []),
            "user_prompt": run["user_prompt"] or "",
            # 分类素材参考的开关/上限也要跟着原任务走 —— 漏了它，
            # 重试就悄悄退回"不开素材参考"，而她以为补跑的是同一份活。
            "reference_enabled": snap.get("reference_enabled", False),
            "reference_limit": snap.get("reference_limit", odb.REFERENCE_DEFAULT),
        }

    res, new_id = create_run(owner, data, background=background,
                             retry_of_run_id=rid, model_keys=failed)
    if not res.get("ok"):
        raise ValueError(res.get("message") or "重试失败")
    res["retried_models"] = failed
    res["source_run_id"] = rid
    return res, new_id


def delete_run(run_id, owner):
    """删掉一条任务记录（连它自己的候选行一起）。

    【边界一：正在跑的不给删】她一点删除，眼前就什么都没了 ——
    不知道跑没跑完、也不知道那笔钱花没花。要删，先「取消」。
    【边界二：已经推入大纲库的，大纲一个字都不动】大纲是独立的一份
    （outlines 表），零件参考次数挂在大纲上（outline_plot_refs）。
    这里只清任务和它的候选，不碰她的大纲。
    """
    try:
        rid = int(run_id)
    except (TypeError, ValueError):
        raise ValueError("没说是哪个任务。")
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM outline_runs WHERE id=?"
                           " AND owner_id=?", (rid, owner)).fetchone()
        if not row:
            raise ValueError("没有这个大纲任务")
        if row["status"] in RUN_ACTIVE:
            raise ValueError("这个任务还在跑，先点「取消」，等它停下来再删。")
        conn.execute("DELETE FROM outline_candidates WHERE run_id=?", (rid,))
        conn.execute("DELETE FROM outline_runs WHERE id=? AND owner_id=?",
                     (rid, owner))
    return True


def delete_finished_runs(owner):
    """一键清掉所有**已经结束**的任务（完成 / 部分失败 / 失败 / 已取消）。

    正在跑的留着 —— 她嫌"堆在一起"的是历史记录，不是手头这次活。
    返回清掉几个。
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM outline_runs WHERE owner_id=? AND status NOT IN (%s)"
            % ",".join("?" * len(RUN_ACTIVE)),
            [owner] + list(RUN_ACTIVE)).fetchall()
        ids = [int(r["id"]) for r in rows]
        if ids:
            q = ",".join("?" * len(ids))
            conn.execute("DELETE FROM outline_candidates WHERE run_id IN (%s)" % q,
                         ids)
            conn.execute("DELETE FROM outline_runs WHERE id IN (%s)" % q, ids)
    return len(ids)


# ----------------------------------------------------------------------
# 改写对比：她交一份成品，让模型看"她把 AI 的那版改成了什么样"
# ----------------------------------------------------------------------
#
# 【这一块跟上面那套"生成"最大的不同】
# 生成是"跑一大堆、挑一份"，改写对比是"一次调用、出一份结论"。
# 所以这里不需要任务表、不需要候选、不需要并发 ——
# 一行 outline_rewrites 就是一个活儿，跑完把结论写回那一行。
#
# 【为什么同步跑也敢】
# 这是一次几百字的短回答（不是几千字的大纲），一两分钟就完了。
# 但界面上仍然走"点一下 → 转圈 → 出结果"这条最简单的路，
# 不为它单开一套任务状态机 —— 那种复杂度换不来什么。

REWRITE_TIMEOUT = 300       # 一次对比等多久算超时
REWRITE_MAX_RETRY = 1       # **超时不重试**：服务端可能已经算完，重试就多扣一次钱


def pick_rewrite_model(owner, model_key=""):
    """挑一个用来做对比的模型。

    她指定了就用她指定的；没指定就用默认的那个。
    挑不到就报错 —— 这里**不能**静默退回一个"能跑就行"的模型：
    她会以为是自己选的那个在分析，结果换了模型也不知道。
    """
    key = (model_key or "").strip()
    if key:
        cfg = cls.pick_model(key)
        return cfg
    return cls.pick_model(None)


def _outline_text_of(obj):
    """把一份大纲 JSON 摊成可读全文。用数据层现成的渲染 ——
    前端"复制全文"、这里的对比输入，必须是**同一份文本**，
    两处各写一份迟早不一样。"""
    if not isinstance(obj, dict):
        return ""
    try:
        return odb.render_outline_text(obj)
    except Exception:                                        # pragma: no cover
        return ""


def _diff_block(diff):
    """把结构差异摊成一段给模型看的文字。

    【为什么还要人话版】模型能读 JSON，但读 JSON 的时候容易
    把字段名当成结论（把 removed_nodes 里的每个 id 都写成"删了"）。
    这里顺手翻成中文句子，并**明说这是程序自动认的、可能认错**。
    """
    d = diff if isinstance(diff, dict) else {}
    if not d or not d.get("by_text"):
        return "（这次的差异没算出来 —— 没有可对比的 AI 原稿。）"
    L = []
    if d.get("nodes_before") is not None:
        L.append("段落数：%s → %s" % (d.get("nodes_before"), d.get("nodes_after")))
    rm = d.get("removed_nodes") or []
    if rm:
        L.append("疑似被她删掉的段落 %d 段：%s"
                 % (len(rm), "、".join(x.get("node_title") or "（无标题）" for x in rm)))
    ad = d.get("added_nodes") or []
    if ad:
        L.append("疑似她新加的段落 %d 段：%s"
                 % (len(ad), "、".join(x.get("node_title") or "（无标题）" for x in ad)))
    ch = d.get("changed_nodes") or []
    if ch:
        L.append("疑似被她改过的段落 %d 段：" % len(ch) + "；".join(
            "%s（像 %.0f%%）" % (x.get("node_title") or "（无标题）",
                              (x.get("similarity") or 0) * 100) for x in ch))
    if d.get("reordered"):
        L.append("段落顺序跟 AI 那版不一样。")
    L.append("字数：%s → %s" % (d.get("words_before") or 0, d.get("words_after") or 0))
    L.append("")
    L.append("（再强调一次：上面这些是程序按段落位置自动认的，"
             "她改动大时很可能认错。以你读到的两份全文为准。）")
    return "\n".join(L)


def _align_block_of(rec, ai_text, user_text):
    """给她改动对比用的逐段对齐块（辅助线索，带不确定标记）。

    【为什么要重算，而不是拿 rec["diff"] 凑】
    rec["diff"] 是按 **node_id** 比出来的 —— 她"把两段合成一段"这种改动
    在 id 上表现为"新节点的 id 没见过"，于是被报成"删了 2 段 + 加了 1 段"。
    折腰第 4 条点名不许这么干（"不要为了让表格完整而强行配对"、
    "支持新增、删除、一对多、多对一以及无法确定对应关系"）。
    align_events 是按**正文**重算的，认得合并/拆分/移位，认不准的标 uncertain。

    算不出来（没有 AI 原稿、或者正文是那段占位说明）就如实说没有，
    不能让模型以为"对齐结果是空的 = 她没改"。
    """
    if not (ai_text or "").strip() or not (user_text or "").strip():
        return ("（这条没有 AI 原稿可对齐 —— 她这份是自己写的，"
                "所以下面没有『哪段改自哪段』的信息，"
                "请只从她自己那份里归纳她的写法。）")
    # 占位说明不是真原稿，别拿去对齐
    if ai_text.startswith("（这一条没有关联到任何一版 AI 原稿"):
        return ("（这条没有 AI 原稿可对齐 —— 她这份是自己写的，"
                "所以下面没有『哪段改自哪段』的信息，"
                "请只从她自己那份里归纳她的写法。）")
    try:
        a = odb.text_to_outline_json(ai_text)
        u = odb.text_to_outline_json(user_text)
        al = odb.align_events(a, u)
        return odb.align_block(al)
    except Exception as e:                                  # pragma: no cover
        return ("（自动对齐这次算不出来：%s。"
                "请直接读上面两份全文自己比对。）" % e)


def _worldview_block_of(text):
    t = (text or "").strip()
    return t if t else "（这次没有世界观。）"


def _characters_block_of(chars):
    if not chars:
        return "（这次没有角色卡。）"
    L = []
    for c in chars:
        bits = [c.get("name") or ""]
        for k, label in (("identity", "身份"), ("personality", "性格"),
                         ("goal", "目标"), ("relation", "关系"),
                         ("taboo", "禁忌")):
            v = (c.get(k) or "").strip()
            if v:
                bits.append("%s：%s" % (label, v))
        L.append("· " + "　".join(bits))
    return "\n".join(L)


def analyze_rewrite(owner, rewrite_id, background=False):
    """跑一次"AI 看她改了什么"。返回结果字典。

    两种跑法：
      background=False  当场跑完再返回（**测试必须用这条**，
                        否则断言拿到的是"还没跑完"）
      background=True   起个线程，立刻返回；结果写回那一行
    """
    rec = odb.get_rewrite(owner, rewrite_id)
    if not rec:
        return {"ok": False, "reason": "not_found",
                "message": "没有这一条改写记录。"}

    if background:
        t = threading.Thread(target=_do_analyze, args=(owner, int(rewrite_id)),
                             daemon=True)
        t.start()
        return {"ok": True, "started": True,
                "message": "开始对比了。它跑完会把结论写在这一条上。"}

    return _do_analyze(owner, int(rewrite_id))


def _do_analyze(owner, rewrite_id):
    rec = odb.get_rewrite(owner, rewrite_id)
    if not rec:
        return {"ok": False, "reason": "not_found",
                "message": "没有这一条改写记录。"}

    # 先标记"分析中"。失败了一定要改回去 —— 不然界面上会永远转圈，
    # 她只能靠删掉重建（这就是"卡住没出路"那一类伤）。
    odb.set_rewrite_summary(owner, rewrite_id, [], "", "",
                            status=odb.REWRITE_RUNNING)

    def _fail(msg):
        odb.set_rewrite_summary(owner, rewrite_id, [], "", "",
                                status=odb.REWRITE_FAILED, error=msg)
        return {"ok": False, "reason": "failed", "message": msg}

    # ---- 1) AI 原稿：只有挂了候选才有 ----
    ai_json = {}
    ai_text = ""
    world = ""
    chars = []
    hook = ""
    design = ""
    target_words = 0

    # 设定优先用**这一条 rewrite 上存的快照**（阶段一加的），
    # 老记录没有快照才退回那次任务的输入快照。
    #
    # 【为什么不直接读 run】需求第 4 条要"这条建议是在什么设定下得出的"
    # 能单独拿出来看。run 会被清理、它的输入也可能被改；
    # 存在 rewrite 行上的那份才是这条学习记录**自己的**来源。
    snap = rec.get("snapshot") or {}
    world = snap.get("worldview") or ""
    chars = snap.get("characters") or []
    hook = snap.get("one_sentence_hook") or ""
    design = snap.get("plot_design") or ""
    try:
        target_words = int(snap.get("target_words") or 0)
    except (TypeError, ValueError):
        target_words = 0

    if rec.get("candidate_id"):
        try:
            cand = get_candidate(int(rec["candidate_id"]), owner)
        except Exception:
            cand = None
        if cand:
            ai_json = cand.get("content_json") or {}
            ai_text = cand.get("content_text") or _outline_text_of(ai_json)
        # 老记录（阶段一之前建的）没有快照，退回 run 的输入。
        if not world and not chars:
            try:
                run = get_run(int(rec["run_id"]), owner) if rec.get("run_id") else None
            except Exception:
                run = None
            inp = (run or {}).get("input") or {}
            world = inp.get("worldview") or ""
            chars = inp.get("character_snapshot") or []
            hook = hook or inp.get("one_sentence_hook") or ""
            design = design or inp.get("plot_design") or ""
            if not target_words:
                try:
                    target_words = int(inp.get("target_words") or 0)
                except (TypeError, ValueError):
                    target_words = 0

    if not ai_text.strip():
        # 需求第 3 条：独立创作**不许假装是对 AI 原稿的修改**。
        # 这里明说没原稿，提示词那边也交代了模型别去编"她删掉了什么"。
        ai_text = ("（这一条没有关联到任何一版 AI 原稿 —— "
                   "她这份是完全自己写的，没有可对照的原稿。"
                   "**不要**去说「她删掉了什么」「她新加了什么」，"
                   "只从她自己这份里归纳她的取向。）")

    # ---- 2) 拼提示词 ----
    tpl, src, warn = rewrite_template()
    diff_block = _diff_block(rec.get("diff") or {})
    # 逐段对齐（阶段一加的）：程序按正文重新算一遍。
    # 老记录只存了 diff（按 node_id 比的），它认不出"两段被合成一段"，
    # 所以这里**重新算**，而不是把 diff 换个说法。算不出来就如实说。
    align_block = _align_block_of(rec, ai_text, rec.get("user_text") or "")
    slots = {
        "ai_outline": ai_text,
        "user_outline": rec.get("user_text") or "",
        "align": align_block,
        "diff": diff_block,
        "worldview": _worldview_block_of(world),
        "characters": _characters_block_of(chars),
    }

    def _sub(m):
        return slots.get(m.group(1), m.group(0))
    system = _REWRITE_SLOT_RE.sub(_sub, tpl)

    # ---- 当时的创作要求（一句话梗 / 情节设计 / 预期字数）----
    # 需求第 1 条点名的"原始创作要求"这三样，原来分析时**根本没给模型**，
    # 于是它没法判断"她这么改是顺着要求来的、还是跑偏了"。
    req_lines = []
    if hook.strip():
        req_lines.append("· 一句话梗：%s" % hook.strip())
    if design.strip():
        req_lines.append("· 情节要求：%s" % design.strip())
    if target_words:
        req_lines.append("· 预期字数：约 %d 字" % target_words)
    user = ("请按上面的要求，对照这两份大纲，只输出那个 JSON 对象。")
    if req_lines:
        user += "\n\n【当时给 AI 的创作要求（判断她改动意图时参考）】\n" \
                + "\n".join(req_lines)
    if rec.get("note"):
        user += "\n\n【她自己写的一句备注】%s\n" % rec["note"]

    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": user}]

    # ---- 3) 选模型、发请求 ----
    try:
        cfg = pick_rewrite_model(owner, "")
    except ValueError as e:
        return _fail(str(e))

    opts = dict(temperature=0.2, timeout=REWRITE_TIMEOUT,
                max_retry=REWRITE_MAX_RETRY, purpose="rewrite", stream=False)
    try:
        out = cls.llm.chat(cfg, msgs, json_mode=True, **opts)
    except Exception as e:
        if getattr(e, "status", None) == 400:
            try:
                out = cls.llm.chat(cfg, msgs, json_mode=False, **opts)
            except Exception as e2:
                return _fail("对比时出错：%s" % e2)
        else:
            return _fail("对比时出错：%s" % e)

    raw = (out.get("content") or "").strip()
    if not raw:
        return _fail("模型返回了空内容（可能被截断或触发了内容策略）。")

    # ---- 4) 解析 ----
    try:
        obj = _extract_json_object(raw)
    except ValueError as e:
        return _fail("模型返回的不是合法 JSON：%s" % e)

    points = obj.get("points")
    if not isinstance(points, list):
        points = []
    summary = (obj.get("summary") or "").strip()
    model_name = out.get("model") or cfg.get("model") or cfg.get("key") or ""

    # 落库要带上"这次用的是哪版提示词"+"人工稿是第几版"（需求第 4 条：
    # 分析结果 + 分析模型 + 分析版本都得可追溯）。
    odb.set_rewrite_summary(owner, rewrite_id, points, summary, model_name,
                            status=odb.REWRITE_DONE,
                            prompt_version=rewrite_prompt_version(),
                            user_version=rec.get("user_version") or 1)

    note = ""
    if warn:
        note = warn
    elif src == "builtin":
        note = ("没有找到 prompts/%s，这次用的是内置通用模板。"
                % PROMPT_FILE_REWRITE)
    return {"ok": True, "points": len(points), "summary": summary,
            "model_name": model_name, "prompt_source": src,
            "prompt_version": rewrite_prompt_version(), "note": note}


def reap_orphan_rewrites(reason="服务重启了"):
    """服务重启时把"分析中"的改写记录放下来。

    跟 reap_orphan_runs 同一个必要性：状态在库里、线程在内存里。
    不改回去的话那一条会永远显示"分析中"，她只能删掉重建。
    """
    with db.connect() as conn:
        n = conn.execute(
            "UPDATE outline_rewrites SET status=?, error=?, updated_at=?"
            " WHERE status=?",
            (odb.REWRITE_FAILED, reason + "，这次对比没有跑完。可以再点一次。",
             now_str(), odb.REWRITE_RUNNING)).rowcount
    return n


def reap_orphan_runs(reason="服务重启了"):
    """服务重启时收尾僵尸任务。

    跟内化那边同一个必要性：任务状态存在库里，而跑任务的线程在内存里。
    服务一重启，线程没了，库里那行还写着"进行中" ——
    界面会一直转圈，而且新任务会被"已经有一个在跑"挡住，
    她就卡死了，唯一的出路是手工改库。
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM outline_runs WHERE status IN (%s)"
            % ",".join("?" * len(RUN_ACTIVE)), list(RUN_ACTIVE)).fetchall()
        for r in rows:
            _set_run(conn, r["id"], status=RUN_FAILED,
                     error=reason + "，这次任务没有跑完。可以点重试继续。",
                     finished_at=now_str())
        conn.execute(
            "UPDATE outline_candidates SET status=?, error=?"
            " WHERE status IN (?,?)",
            (CAND_FAILED, reason + "，没有跑完。", CAND_QUEUED, CAND_RUNNING))
        return len(rows)


# ----------------------------------------------------------------------
# 自测：只测纯函数，不联网、不写库
# ----------------------------------------------------------------------

def _self_check():                                          # pragma: no cover
    ok = True

    def check(name, got, want):
        nonlocal ok
        if got != want:
            ok = False
            print("  [x] %s：得到 %r，期望 %r" % (name, got, want))
        else:
            print("  [v] %s" % name)

    tpl = GENERIC_OUTLINE_PROMPT
    check("通用模板七个槽位一个不少",
          [s for s in REQUIRED_SLOTS if ("{%s}" % s) not in tpl], [])
    check("通用模板一个密钥都没有", "sk-" in tpl, False)

    # ---- 改写对比模板（阶段一C）----
    rtpl = GENERIC_REWRITE_PROMPT
    check("改写通用模板六个槽位一个不少",
          [s for s in REWRITE_SLOTS if ("{%s}" % s) not in rtpl], [])
    check("改写槽位是六个（加了 align）", len(REWRITE_SLOTS), 6)
    check("改写通用模板带 align 槽位", "{align}" in rtpl, True)
    check("改写通用模板一个密钥都没有", "sk-" in rtpl, False)
    # 需求第 5 条：不许再说"不评价好坏"，得放开"有证据的具体问题"
    check("改写模板不再写『不要评价好坏』", "不要评价好坏" in rtpl, False)
    check("改写模板不再写『不评价好坏』", "不评价好坏" in rtpl, False)
    # 需求第 5 条底线仍在
    check("改写模板仍禁止脑补她的意图", "脑补理由" in rtpl, True)
    check("改写模板声明她改了≠更好", "的代名词" in rtpl, True)
    # 需求第 4 条：程序对齐只是线索、可能不准
    check("改写模板写明程序对齐可能认错", "可能认错" in rtpl, True)
    check("改写模板写明以两份全文为准", "以你读到的两份全文" in rtpl, True)
    # 需求第 6 条：每条要带适用/不适用
    check("改写模板要求 applies_when", "applies_when" in rtpl, True)
    check("改写模板要求 not_when", "not_when" in rtpl, True)
    # 需求第 1 条：scope 与 confidence 必须分开说
    check("改写模板 scope 只有三值",
          all(k in rtpl for k in ("长期偏好", "情境适用", "仅本篇")), True)
    check("改写模板写明 confidence 跟 scope 是两件事",
          "confidence 跟 scope 是两件事" in rtpl, True)
    # 兜底方向：拿不准填仅本篇（不能填长期）
    check("改写模板写明拿不准填仅本篇", "拿不准就填「仅本篇」" in rtpl, True)
    # 九类变化
    check("改写模板九类变化齐全",
          all(k in rtpl for k in ("新增事件", "删除事件", "顺序调整",
                                  "动机改变", "冲突处理改变",
                                  "信息揭露时机改变", "铺垫回收变化",
                                  "结局关系变化", "仅措辞格式标题")), True)
    # 旧的 kind 取值（节奏/篇幅/删减…）不该再出现
    check("改写模板不再用旧的 kind 分类",
          "节奏、篇幅、删减" in rtpl, False)
    # 8 项证据字段都要在输出格式里点名
    check("改写模板输出格式含 8 项证据字段",
          [k for k, _ in odb.REWRITE_PT_FIELDS
           if ('"%s"' % k) not in rtpl], [])
    # 空值写法统一（不许模型写"无""暂无"）
    check("改写模板要求不确定字段给空串",
          '不要写"无""暂无""N/A"' in rtpl, True)

    # 真实模板（存在时）也要过同一套槽位检查
    _rt, _rsrc, _rwarn = rewrite_template()
    check("真实改写模板槽位齐全（缺了就退回通用版）",
          _rwarn == "" if _rsrc == "file" else True, True)

    # 单遍替换：填进去的内容里带 {user_prompt} 也不能被二次替换
    out = _fill_slots("A={worldview} B={user_prompt}",
                      worldview="正文里写着 {user_prompt} 这几个字",
                      user_prompt="她的要求")
    check("填进去的 {user_prompt} 不会被二次替换",
          "正文里写着 {user_prompt} 这几个字" in out, True)
    check("真正的槽位被替换了", "B=她的要求" in out, True)

    # 抠 JSON：三种裹法都要能抠出来
    for raw, why in (('```json\n{"nodes":[]}\n```', "带围栏"),
                     ('好的：{"nodes":[]} 以上', "前后有废话"),
                     ('{"nodes":[]}', "干净 JSON")):
        try:
            d = _extract_json_object(raw)
            check("%s 能抠出来" % why, isinstance(d, dict), True)
        except ValueError:
            check("%s 能抠出来" % why, False, True)
    try:
        _extract_json_object("完全不是 JSON")
        check("抠不出 JSON 要报错", False, True)
    except ValueError:
        check("抠不出 JSON 要报错", True, True)

    check("只给数组时会包成 nodes",
          _extract_json_object('[{"node_title":"甲"}]'), {"nodes": [{"node_title": "甲"}]})

    # 提示词版本必须动态取
    check("提示词版本有动态来源",
          prompt_version() in (PROMPT_VERSION, PROMPT_VERSION_GENERIC), True)

    # 状态表自洽
    check("任务状态表里没有重复", len(ALL_RUN_STATUS), len(set(ALL_RUN_STATUS)))
    check("进行中只有排队中和进行中", tuple(RUN_ACTIVE), (RUN_QUEUED, RUN_RUNNING))
    check("补充提示词这一档是 outline", USER_PROMPT_KIND_OUTLINE, "outline")

    # 空输入不该拼出崩掉的消息
    msgs = build_messages({"template": tpl, "worldview": "", "characters": [],
                           "plots": [], "hook": "", "design": "",
                           "target_words": 8000, "tier": odb.word_tier(8000),
                           "learning": [], "user_prompt": ""})
    check("空输入也能拼出两条消息", len(msgs), 2)
    check("空输入时世界观有兜底说法",
          "没写世界观" in msgs[0]["content"], True)
    check("空输入时零件有兜底说法",
          "没有可用的剧情零件" in msgs[0]["content"], True)
    check("目标字数的档位写进去了",
          "10～17 个" in msgs[0]["content"], True)
    check("同一段里说清了每段该写多长（不然模型会把每段写肥）",
          "每段 450～800 字" in msgs[0]["content"], True)

    _privacy = _privacy_note({"worldview": "x" * 100, "characters": [{}],
                              "plots": [{}], "hook": "h", "design": "",
                              "user_prompt": "", "blocked": [{"id": 1}]}, 500)
    check("隐私提示会说仅本地的被排除", "仅本地" in _privacy, True)

    # ---- 超时与重试 ---------------------------------------------------
    # 这两个数错了就退回老毛病：8000 字的大纲必然超时，
    # 超时还重试 3 次 → 白等 9 分钟拿一个必然失败，还可能多扣几笔钱。
    check("大纲超时远大于 llm 默认值（不然 8000 字必超时）",
          OUTLINE_TIMEOUT > cls.llm.TIMEOUT * 2, True)
    check("大纲只发一次、不重试", OUTLINE_MAX_RETRY, 1)
    check("大纲最坏等待不超过 15 分钟（超了会被当成卡死）",
          OUTLINE_TIMEOUT * OUTLINE_MAX_RETRY <= 900, True)
    check("分类/内化那套默认值仍然会重试（短问答多试一次划算）",
          cls.llm.MAX_RETRY >= 2, True)

    print()
    print("编排层自测：%s" % ("全部通过" if ok else "有失败"))
    return ok


if __name__ == "__main__":                                  # pragma: no cover
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(0 if _self_check() else 1)
