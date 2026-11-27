#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH · 微信桥接入口分发器（host.js 无需感知平台，始终 spawn 本文件）

按 sys.platform 分发到对应采集器：
  - darwin（macOS）→ wechat_bridge_mac.py：纯屏幕 OCR 采集 + 剪贴板/按键发送，
    不解密数据库、不注入、不改微信本体（Mac 微信 4.2+ 读库路线不可行）
  - 其他（Windows）→ wechat_bridge_windows.py：原版行为，一行未改

参数原样透传（--dsh / --token / ...），stdout/stderr 继承，host 的日志管道不受影响。
"""
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
target = os.path.join(
    here,
    "wechat_bridge_mac.py" if sys.platform == "darwin" else "wechat_bridge_windows.py",
)
os.execv(sys.executable, [sys.executable, target] + sys.argv[1:])
