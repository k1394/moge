r"""
墨阁 · 第 1 天练习（零基础版）

规则：
  左边是「已经写好的代码」，你要做的是：
  1. 先原样照着敲一遍（打字，不要复制），运行，看结果
  2. 再按提示「改一个地方」，运行，看结果变了没有
  3. 想不清就改数字、改文字，随便试，不会坏

  第一天不要求你写出新代码。要求是：手熟、不怕、看得懂在干什么。

怎么用：
  1. 双击 G:\docker\moge\第1天练习.bat，先看一遍输出
  2. 用记事本打开本文件，找到【照抄这段】和【改一处】
  3. 照抄一遍、按提示改一处，再双击 bat 跑一次，看输出变了没

  第一天不要求你写出新代码。要求是：手熟、不怕、看得懂在干什么。
  改错了不会坏 —— 本文件在 git 里有记录，一条命令就能还原。
"""

import sys

# 固定输出编码，避免中文在命令行窗口里乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

print("=" * 48)
print("  墨阁 · 第 1 天练习")
print("=" * 48)
print("  照抄 → 改一处 → 再跑一次。改错了不会坏。")
print("  哪里没懂直接问，不用自己硬啃。")
print("=" * 48)
print()

print("=" * 40)
print("练习 1：变量是什么")
print("=" * 40)

# 【照抄这段】
site_name = "墨阁"
print(site_name)

# 【改一处】把 "墨阁" 改成 "墨阁写作助手"，再运行，看输出变了没


print()
print("=" * 40)
print("练习 2：f-string 把变量塞进句子里")
print("=" * 40)

# 【照抄这段】
site_name = "墨阁"
print(f"欢迎来到{site_name}")

# 【改一处】改成 print(f"{site_name}，我来啦")，运行看效果


print()
print("=" * 40)
print("练习 3：数字和运算")
print("=" * 40)

# 【照抄这段】
a = 27
b = 5
print(f"a + b = {a + b}")
print(f"a - b = {a - b}")
print(f"a * b = {a * b}")
print(f"a / b = {a / b}")

# 【改一处】把 b 改成 2，运行，看 a / b 的结果变成什么
# 想一想：为什么 a / b 有小数点，但 a * b 没有？


print()
print("=" * 40)
print("练习 4：字符串的常用动作")
print("=" * 40)

# 【照抄这段】
text = "  老九门 二月红  "
print(f"原样：[{text}]")
print(f"去掉空格后：[{text.strip()}]")
print(f"按空格切开后：{text.split()}")
print(f"一共几个字：{len(text)}")

# 【改一处】把 text 改成 "  陈皮阿四  ", 运行看三个结果分别变成什么


print()
print("=" * 40)
print("练习 5：列表（一排东西）")
print("=" * 40)

# 【照抄这段】
titles = ["九门旧事", "二月红传", "陈皮阿四"]
print(f"全部：{titles}")
print(f"第一个：{titles[0]}")
print(f"最后一个：{titles[-1]}")
print(f"前两个：{titles[0:2]}")

# 【改一处】往列表里再加一个书名，比如 "长沙夜雨"，运行看结果


print()
print("=" * 40)
print("练习 6：字典（带标签的一堆信息）")
print("=" * 40)

# 【照抄这段】
material = {
    "标题": "九门旧事",
    "类型": "同人短篇",
    "来源": "九门资料库",
    "标签": ["民国", "师徒"],
}
print(f"整个字典：{material}")
print(f"只看标题：{material['标题']}")
print(f"只看标签：{material['标签']}")

# 【改一处】把 "标题" 的值改成你自己写的某篇的名字，运行看结果
# 想一想：列表和字典的区别 —— 列表靠位置找，字典靠名字找


print()
print("=" * 40)
print("练习 7：今天日期")
print("=" * 40)

# 【照抄这段】
import datetime

today = datetime.date.today()
print(f"今天是 {today}")

# 【改一处】把 today 换成 datetime.date(2026, 10, 1)，运行看输出


print()
print("=" * 40)
print("今天的 7 个练习跑完了。")
print("如果每一段你都照抄过、至少改过一处、并且看得懂它在干什么，")
print("那第 1 天就达标了 —— 不需要能自己写出新代码。")
print("=" * 40)

print()
print("接下来做两件事：")
print("  1. 打开学习工作台，把 py1(变量) py2(列表) py3(字典) 点亮")
print("  2. 哪里没懂，直接跟阿墨说，不用自己硬啃")
print("=" * 40)
