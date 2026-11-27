#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_already_sent / _same_sent_text 的回归测试（不启动微信、不连库）。

背景
----
_already_sent() 用来判断「这条回复其实已经发出去了，别再点一次」，避免重复发。
它同时被 4 处复用：重试前自检、接口报失败后回查、补回车后回查、键盘通道与兜底通道的确认。

旧实现有两个缺陷叠加：
  1. 查最近 12 条消息时不看方向，主人自己发的话也在里面；
  2. 判定用包含关系 `c == text or text in c or c in text`，
     其中 `c in text` 会把对方的短消息当成本条回复已发。
于是「回复复述了对方原话」就会被整条跳过，例如：
  对方 21:31:43 发 `[玫瑰]` → 回复 `[玫瑰] 收下了…` → `"[玫瑰]" in 回复` 成立 → 不发送。

本测试锁定三件事：
  A. 回归：对方消息与回复存在包含关系时，不得判定为「已发」；
  B. 保留原设计：真正发出去的那条（精确相等 / 带引用前缀 / @ 清洗后相等）仍要判为已发；
  C. 不引入反向问题：方向字段缺失（老库）时仍能识别精确相等，避免重复发送。

用法
----
    python bridge/test_dedup.py                  # 测同目录下的 wechat_bridge.py
    python bridge/test_dedup.py  /path/to/x.py   # 测指定的副本（部署前后的对照）

全通过时退出码 0，失败打印明细并退出 1。

为了让测试不依赖 wechat_bridge.py 的运行时（它 import 时就要连库、起 UI 自动化），
这里用 ast 从源码里摘出被测函数与 clean_text 及其依赖的正则常量，再在隔离命名空间里执行。
"""

import ast
import pathlib
import sys
import time
import re

HERE = pathlib.Path(__file__).resolve().parent
SRC_PATH = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE / "wechat_bridge.py"

# 需要从源码摘出来的名字：正则常量 + 纯函数
WANT_CONSTS = {"_MENTION_ID", "_AT_TAG"}
WANT_FUNCS = {"clean_text", "_same_sent_text", "_already_sent"}

ME = "wxid_me_0001"
OTHER = "wxid_5u5u0u79mtlf22"      # 真实场景里的主人账号（示例值，测试内无副作用）


def load_module_pieces():
    src = SRC_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    parts = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if names & WANT_CONSTS:
                parts.append(ast.get_source_segment(src, node))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in WANT_FUNCS:
            parts.append(ast.get_source_segment(src, node))
    missing = WANT_FUNCS - {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    if missing:
        raise SystemExit("源码里找不到被测函数：" + "、".join(sorted(missing)))
    ns = {"re": re, "time": time, "db": None}
    exec("\n\n".join(parts), ns)          # noqa: S102 - 只执行本仓库自己的源码片段
    return ns


class FakeDB:
    """只提供 _already_sent 用到的那点接口。"""

    def __init__(self, rows, wxid=ME):
        self._rows = rows
        self.wxid = wxid

    def get_messages(self, username, limit=12):
        return self._rows[:limit]


def make_checker(ns, rows, wxid=ME):
    ns["db"] = FakeDB(rows, wxid=wxid)
    ns["_chat_username"] = lambda chat: chat

    def already_sent(text, within=60):
        return ns["_already_sent"](text, "周践知", within=within)

    return already_sent


def row(content, sender=ME, age=1):
    return {
        "content": content,
        "sender_username": sender,
        "create_time": time.time() - age,
    }


CASES = []


def case(name, expect, rows, sent, within=60, wxid=ME):
    CASES.append((name, expect, rows, sent, within, wxid))


# --- A. 回归：当前事故 -----------------------------------------------------------------
case(
    "对方发 [玫瑰]，回复以 [玫瑰] 开头 → 不算已发（本次事故）",
    False,
    [row("[玫瑰]", sender=OTHER)],
    "[玫瑰] 收下了\n\n那事就先记着,你想动了直接喊我代号就行——不用挑,随便说一句我就接。",
)
case(
    "对方发「你好」，回复以「你好」开头 → 不算已发（同一 bug 的另一例）",
    False,
    [row("你好", sender=OTHER)],
    "你好 [微笑]\n\n\n\n昨晚那套 MemOS 记忆插件装完就一直躺在 profile 里了",
)
case(
    "对方长文里包含我们的短回复 → 不算已发",
    False,
    [row("我觉得你说得对，好的，那就这么办", sender=OTHER)],
    "好的",
)
case(
    "我方短消息落在我方长回复里 → 不算已发（旧实现会误判）",
    False,
    [row("[微笑]", sender=ME)],
    "[微笑] 收到啦，这就去办",
)

# --- B. 保留原设计：真的发过要能认出来 -------------------------------------------------
case(
    "自己发的那条，完全相等 → 判定已发",
    True,
    [row("已经发出去了的这句话")],
    "已经发出去了的这句话",
)
case(
    "自己发的那条带了引用前缀 → 我方文本被包含，判定已发",
    True,
    [row("（引用：某条旧消息）已经发出去了的这句话")],
    "已经发出去了的这句话",
)
case(
    "@ 清洗后相等 → 判定已发",
    True,
    [row("@小周 已经发出去了的这句话")],
    "已经发出去了的这句话",
)
case(
    "自己发的短句，完全相等 → 判定已发（短句只走精确相等）",
    True,
    [row("[呲牙]", sender=ME)],
    "[呲牙]",
)

# --- C. 兼容与边界 ---------------------------------------------------------------------
case(
    "方向字段缺失（老库），精确相等 → 仍判定已发（不能变成重复发）",
    True,
    [{"content": "已经发出去了的这句话", "create_time": time.time() - 1}],
    "已经发出去了的这句话",
)
case(
    "超出时间窗 → 不算已发",
    False,
    [row("已经发出去了的这句话", age=300)],
    "已经发出去了的这句话",
    within=60,
)
case(
    "空消息 / 空文本 → 不算已发，且不抛异常",
    False,
    [row("", sender=ME)],
    "",
)
case(
    "我方 wxid 未知时（db.wxid 为空）不按方向过滤，但仍不误判短片段",
    False,
    [row("[玫瑰]", sender=OTHER)],
    "[玫瑰] 收下了",
    wxid="",
)


def main():
    ns = load_module_pieces()
    failed = 0
    print("被测源码：%s\n" % SRC_PATH)
    for name, expect, rows, sent, within, wxid in CASES:
        got = make_checker(ns, rows, wxid=wxid)(sent, within=within)
        ok = got == expect
        if not ok:
            failed += 1
        print("%s %s\n     期望=%s 实际=%s" % ("PASS" if ok else "FAIL", name, expect, got))
    total = len(CASES)
    print("\n%d/%d 通过" % (total - failed, total))
    if failed:
        print("有 %d 条不符合预期" % failed)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
