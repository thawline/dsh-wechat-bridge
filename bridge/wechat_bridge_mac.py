#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DSH · 微信桥接 Mac 版 v2（OCR 采集 + AppleScript 发送；不注入、不修改微信、不碰数据库）

由 bridge/wechat_bridge.py 分发器在 macOS 上拉起（host.js 零改动）；采集工具二进制
默认在本目录旁的 mac-bin/（wx_winid / wx_ocr / wx_pixel，swift 源码在 ../swift/）。

与 Windows 版同一 HTTP 契约，桥脚本可互换：
  - 收：每轮截窗 → accurate OCR 读取「当前已打开会话」的气泡 → 新消息 POST /dsh-wechat-bridge/receive；
        侧栏快照差分只用于提示「其他会话有新消息」（不推送）
  - 发：GET /outbox → 校验目标会话已打开 → 剪贴板粘贴 → OCR 验证落进输入框 → 回车 →
        OCR 复核气泡 → POST /outbox/ack
  - 中间：桥作为 HTTP 客户端与 DSH 通信；host.js 只负责用「解释器」拉起本脚本

⚠ 架构决策（2026-11-27 22:44 干净登录态第 12 次实测）：
  微信 4.2 的 Cmd+F 搜索→回车开会话链路**不可用且有剧毒**——回车必然落进搜一搜网页，
  且搜索面板一旦打开就无法用 Esc/Cmd+W 关闭（合成鼠标被自绘 UI 全链路过滤），只能重启微信，
  而重启即登出。因此：**桥永不调用搜索**。使用前提 = 用户在微信里手动打开目标会话并保持打开。

防回环（三层）：
  1. 气泡方向：自己发的消息靠右（x_end 大），永不回推
  2. 文本回显：自己刚发出去的文本 180 秒内不再当作新消息
  3. 发送静默：发完往某会话后 15 秒内，该会话的新气泡不再推送

已验证的性能假设（2026-11-27 实测）：
  - accurate + 裁剪侧栏(310pt) ≈ 0.6s；accurate + 裁剪消息区 ≈ 1-2s；全窗 accurate ≈ 16s（禁止使用）
  - fast 模式中文输出为乱码（禁用）
  - 显示器睡眠期间截图返回黑图/失败、一切点击空操作 → 常驻 caffeinate -d + 截图体积合理性检查（capture_window）
  - 搜索面板卡在打开态（窗口宽 1174pt，Esc/Cmd+W 均无效）→ receive_once 拒绝解析并提示人工关闭
  - 发送后 osascript rc=0 不代表发出（焦点可能被搜索框劫持）→ send_via_ui OCR 复核气泡/输入框

手跑：
  python3 wechat_bridge.py --dsh http://127.0.0.1:62890 [--token xxx]
  python3 wechat_bridge.py --scan-debug        # 扫一次：侧栏快照 + 打开会话气泡解析
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from collections import deque

STATE_DIR = os.path.expanduser("~/.dsh/wx-mac-bridge")
STATE_FILE = os.path.join(STATE_DIR, "ocr_state.json")

DSH = ""
TOKEN = ""
POLL_SEC = 3.0
OUTBOX_POLL_SEC = 2.0   # 出站轮询独立于采集轮询：回复出箱延上限直接决定回复体感延迟
CFG = {"主人": [], "屏蔽": [], "活跃群": []}
MY_NICK = ""

TOOL_DIR = ""       # bin 目录（wx_winid / wx_ocr）

# ---- 运行时状态 ----
sidebar_snap = {}    # 会话名 -> 预览文本（上次扫描）
seen_bubbles = {}    # 会话名 -> deque([...]) 最近消息文本（防重复推送）
sent_echo = {}       # (会话名, 规整文本) -> 发送时刻
sent_texts = {}      # 规整文本 -> 发送时刻（全局回显键，免疫标题 OCR 变体）
muted_until = {}     # 会话名 -> 时刻（发送后静默窗口）
last_push = {}       # 会话名 -> 上次推送时刻（每会话限频）
_miss_streak = 0     # 窗口连续缺失轮数（自愈计数）
_last_reopen = 0.0   # 上次 reopen 尝试时刻
last_open_title = "" # 当前读取的已打开会话标题（切换会话时重置指纹，不推历史）
log_fd = None

TIME_RE = re.compile(r"^((?:[01]?\d|2[0-3]):[0-5]\d|星期[一二三四五六日天]|昨天|前天|\d{1,2}/\d{1,2})$")
NEWMSG_RE = re.compile(r"新消息$")


def log(*a):
    line = "[%s] %s" % (time.strftime("%m-%d %H:%M:%S"), " ".join(str(x) for x in a))
    print(line, flush=True)
    if log_fd:
        log_fd.write(line + "\n")
        log_fd.flush()


def norm(t):
    return re.sub(r"\s+", "", str(t or ""))


def run(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return -1, "", repr(e)


# ---------------- HTTP ----------------

def http_json(path, payload=None, method=None):
    url = DSH + path
    data = None
    headers = {"content-type": "application/json"}
    if TOKEN:
        headers["x-bridge-token"] = TOKEN
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = method or "POST"
    req = urllib.request.Request(url, data=data, headers=headers, method=method or "GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return {"ok": False, "说": "HTTP %s" % e.code}
    except Exception as e:
        return {"ok": False, "说": str(e)}


def refresh_config():
    global CFG
    r = http_json("/dsh-wechat-bridge/state")
    c = (r or {}).get("配置") if isinstance(r, dict) else None
    if not isinstance(c, dict):
        return False
    CFG = {
        "主人": [str(x).strip() for x in (c.get("主人") or []) if str(x).strip()],
        "屏蔽": [str(x).strip() for x in (c.get("屏蔽") or []) if str(x).strip()],
        "活跃群": [str(x).strip() for x in (c.get("活跃群") or []) if str(x).strip()],
    }
    return True


# ---------------- 状态持久化 ----------------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state():
    global sidebar_snap, seen_bubbles
    os.makedirs(STATE_DIR, exist_ok=True)
    now = time.time()
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "sidebar": sidebar_snap,
                "seen": {k: list(v) for k, v in seen_bubbles.items()},
                "sent_texts": {k: v for k, v in sent_texts.items()
                               if now - v < 180},
            }, f, ensure_ascii=False)
    except Exception as e:
        log("⚠ 状态保存失败：", repr(e))


# ---------------- 采集三件套 ----------------

def window_info():
    rc, out, err = run([os.path.join(TOOL_DIR, "wx_winid")], timeout=15)
    if rc != 0:
        return None
    try:
        info = json.loads(out.strip().splitlines()[-1])
        return info if info.get("found") else None
    except Exception:
        return None


def reopen_wechat():
    """微信主窗口被关掉时（进程在、窗口无），把主窗口拉回来。
    坑（2026-11-28 实测）：`open -a WeChat` 对已运行但无窗的微信只激活、**不重建窗口**
    （桥连试 3 次 rc=0 但窗口始终没回来）；重建窗口必须走 osascript 的 reopen Apple Event。
    osascript activate/reopen 失败要打出原因（TCC 拒绝时 rc!=0，别静默）。"""
    rc, _, err = run(["osascript", "-e", 'tell application "WeChat" to reopen',
                      "-e", 'tell application "WeChat" to activate'], timeout=15)
    if rc != 0:
        log("⚠ osascript reopen/activate 失败 rc=%s %s" % (rc, (err or "").strip()[:120]))
        rc2, _, err2 = run(["open", "-a", "WeChat"], timeout=15)
        if rc2 != 0:
            log("⚠ open -a WeChat 兜底也失败 rc=%s %s" % (rc2, (err2 or "").strip()[:120]))


def capture_window(winid, out_png):
    """截窗。坑（2026-11-27 实测）：显示器睡眠期间 screencapture 返回黑图/失败，
    且黑图 os.path.exists 也能过 —— 必须按体积做合理性检查，小图先唤醒再重试。"""
    def _shot():
        rc, _, err = run(["/usr/sbin/screencapture", "-x", "-o",
                          "-l%d" % winid, out_png], timeout=20)
        ok = rc == 0 and os.path.exists(out_png)
        size = os.path.getsize(out_png) if ok else 0
        return ok and size > 150 * 1024, size   # 880pt 窗口真内容 ≥ 400KB，黑图 ≈ 100KB
    ok, size = _shot()
    if ok:
        return True
    # 2026-11-28 实测：显示器睡着时 screencapture 也可能直接报错（rc!=0、无文件）
    # —— 与「截到了但体积可疑地小」一并走唤醒重试，不区分失败形态
    run(["caffeinate", "-u", "-t", "2"], timeout=10)
    time.sleep(1.0)
    ok, _ = _shot()
    return ok


def ocr_png(png, crop=None):
    """裁剪 accurate OCR。crop=(x,y,w,h) 像素。返回 [{text,x,y,w,h}]。"""
    cmd = [os.path.join(TOOL_DIR, "wx_ocr"), png]
    if crop:
        cmd += [str(int(v)) for v in crop]
    rc, out, err = run(cmd, timeout=60)
    if rc != 0:
        log("⚠ OCR 失败 rc=%s %s" % (rc, (err or "").strip()[:120]))
        return []
    rows = []
    for line in out.splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def crop_from(info, x_pt, y_pt, w_pt, h_pt):
    """点坐标 -> 像素裁剪框（随窗口实际位置/缩放换算）。"""
    s = float(info.get("scale") or 2.0)
    x0 = max(0, int(x_pt * s))
    y0 = max(0, int(y_pt * s))
    w = max(1, int(w_pt * s))
    h = max(1, int(h_pt * s))
    return (x0, y0, w, min(h, int(info["h"] * s) - y0))


# ---------------- 颜色探针（确定性信号，不靠 OCR 猜字） ----------------
# 标定（2026-11-27 深色模式实测，bin/wx_pixel）：
#   侧栏选中行绿底 76,165,111（x=130..590 四点一致）／未选中行 47-68 灰
#   发出气泡绿 106,206,146 ／ 消息区空白 30,30,31
#   判据：绿通道显著高于红蓝 → 选中行 / 发出气泡

def probe_color(png, x, y, w=6, h=6):
    """区域平均色 (r,g,b)，失败返回 None。"""
    cmd = [os.path.join(TOOL_DIR, "wx_pixel"), png,
           str(int(x)), str(int(y)), str(int(w)), str(int(h))]
    rc, out, err = run(cmd, timeout=15)
    if rc != 0:
        return None
    try:
        r, g, b = (int(v) for v in out.strip().split(","))
        return (r, g, b)
    except Exception:
        return None


def is_wechat_green(rgb):
    return bool(rgb) and rgb[1] >= rgb[0] + 40 and rgb[1] >= rgb[2] + 30 and rgb[1] >= 120


def selected_chat(png, sidebar_y):
    """侧栏选中行 = 绿底。y 为全窗 OCR 行的名称行顶（px），探针取名称行中部（+30px）。
    坑：曾带旧裁剪偏移 +56px 探到行底边缘外，绿底漏判导致会话名回退成噪音标题
    （「李健主号く」），又误判为换会话重新铺底、漏推消息 —— 偏移必须与切片坐标系一致。"""
    for name, y in (sidebar_y or {}).items():
        if is_wechat_green(probe_color(png, 300, y + 30)):
            return name
    return None


_sidebar_y = {}      # parse_sidebar 顺带记录：会话名 -> 侧栏裁剪内 y（选中行颜色探针用）


# ---------------- 侧栏解析 ----------------

def is_noise_row(r, sidebar_right_px):
    t = (r.get("text") or "").strip()
    if not t:
        return True
    if TIME_RE.match(t):
        return True
    if re.fullmatch(r"[\dA-Za-z]{1,3}", t):   # 未读红点数字 / 静音图标误读
        return True
    if r.get("x", 0) >= sidebar_right_px:      # 时间/图标列
        return True
    return False


def parse_sidebar(rows, img_w):
    """侧栏 OCR 行 -> {会话名: 预览}。
    聚类规则（实测：同一会话行内 y 间隔约 39px，会话行间 ≥ 88px）：y 间隔 > 60px 切新会话；
    每簇最上面一行是会话名，其余行按 y 拼成预览。"""
    right_px = int(img_w * 0.79)   # 裁剪宽 310pt 内，时间列大约在 0.79 之后
    rows = [r for r in rows if r.get("y", 0) > 55]           # 滤掉顶部搜索框
    rows = [r for r in rows if not is_noise_row(r, right_px)]
    rows.sort(key=lambda r: (r["y"], r["x"]))
    clusters = []
    for r in rows:
        if clusters and r["y"] - clusters[-1][-1]["y"] < 60:
            clusters[-1].append(r)
        else:
            clusters.append([r])
    out = {}
    _sidebar_y.clear()
    for cl in clusters:
        name = (cl[0].get("text") or "").strip()
        if not name or name in ("搜索",):
            continue
        preview = "".join((c.get("text") or "").strip() for c in cl[1:])
        out[name] = preview
        _sidebar_y[name] = cl[0]["y"]
    return out


# ---------------- 消息区解析 ----------------

def parse_bubbles(rows, img_w, png=None, ox=0, oy=0):
    """消息区 OCR 行 -> [(方向, 文本)]。方向 'in'=别人，'out'=自己。
    分组规则（实测：同一气泡内 y 间隔 < 55px，气泡间 ≥ 55px）；
    方向规则：优先颜色探针（气泡左缘内侧绿 = 自己发的，标定 106,206,146），
    探针不可用时退回几何：右缘 x_end > 0.935*img_w => 自己；左缘 < 0.45*img_w => 别人；
    其余按 in 处理，靠 sent_echo 文本兜底（在 receive_loop 里过滤）。"""
    if not rows:
        return []
    rows = sorted(rows, key=lambda r: (r["y"], r["x"]))
    bubbles = []
    for r in rows:
        t = (r.get("text") or "").strip()
        if not t or TIME_RE.match(t) or NEWMSG_RE.search(t):
            continue
        x_end = r["x"] + r["w"]
        if bubbles and r["y"] - bubbles[-1]["rows"][-1]["y"] < 55:
            bubbles[-1]["rows"].append(r)
            bubbles[-1]["x_start"] = min(bubbles[-1]["x_start"], r["x"])
            bubbles[-1]["x_end"] = max(bubbles[-1]["x_end"], x_end)
        else:
            bubbles.append({"rows": [r], "x_start": r["x"], "x_end": x_end})
    out = []
    for b in bubbles:
        text = "\n".join((r.get("text") or "").strip() for r in sorted(b["rows"], key=lambda r: r["y"]))
        if not text:
            continue
        side = None
        if png:
            mid_y = (b["rows"][0]["y"] + b["rows"][-1]["y"]) // 2 + 12
            rgb = probe_color(png, ox + b["x_start"] + 4, oy + mid_y)
            if rgb:
                side = "out" if is_wechat_green(rgb) else "in"
        if side is None:
            if b["x_end"] > img_w * 0.935:
                side = "out"
            elif b["x_start"] < img_w * 0.45:
                side = "in"
            else:
                side = "in"
        out.append((side, text))
    return out


# ---------------- 发送 ----------------

def frontmost_app():
    """当前前台应用名；查询失败返回 None。"""
    rc, out, err = run(["osascript", "-e",
                        'tell application "System Events" to get name of first process whose frontmost is true'],
                       timeout=15)
    return out.strip() if rc == 0 and out.strip() else None


def send_via_ui(chat, text):
    """发送到「当前已打开的会话」（禁用搜索链路）+ OCR 双重验证。
    坑（2026-11-27 第 12 次实测）：4.2.0 的 Cmd+F 搜索→回车必然落进搜一搜网页且面板关不掉
    （只能重启=登出），所以发送绝不碰搜索；目标会话没打开就直接失败。
    安全红线：按键前必须确认前台是微信——activate 若静默失败，Cmd+V 会落进用户正在用的
    应用（浏览器输入框等），AI 回复内容就发到别处了。
    流程：验标题 → activate+验前台 → 粘贴 → OCR 验输入框 → 回车 → OCR 复核气泡。"""
    info = window_info()
    if not info:
        return False, "微信主窗口不存在"
    png = os.path.join(STATE_DIR, "wx_send0.png")
    title, _ = read_open_chat(info, png)
    if not title:
        return False, "没有已打开的会话——请先在微信里打开「%s」" % chat
    nc, nt = norm(chat), norm(title)
    if nc not in nt and nt not in nc:
        return False, "目标会话「%s」未打开（当前打开：「%s」）" % (chat, title)

    prev_app = frontmost_app()
    run(["open", "-a", "WeChat"], timeout=15)
    time.sleep(0.8)
    front = frontmost_app()
    if not front or ("微信" not in front and "WeChat" not in front):
        return False, "无法把微信带到前台（当前前台：%s），拒绝盲发" % (front or "未知")
    try:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True, timeout=10)
    except Exception as e:
        return False, "剪贴板写入失败：" + repr(e)
    run(["osascript", "-e",
         'tell application "System Events" to keystroke "v" using command down'],
        timeout=15)
    time.sleep(0.8)

    # 验证粘贴落进输入框（绝不盲回车；全窗 OCR + 切片，紧裁剪会降精度）
    info2 = window_info() or info
    png2 = os.path.join(STATE_DIR, "wx_send1.png")
    if not capture_window(info2.get("windowid", 0), png2):
        return False, "粘贴后截窗失败"
    s2 = float(info2.get("scale") or 2.0)
    rows2 = ocr_png(png2)
    inp = norm("".join((r.get("text") or "") for r in slice_rows(
        rows2, int(312 * s2), int((int(info2["h"]) - 145) * s2),
        int(info2["w"] * s2), int(info2["h"] * s2))))
    want = norm(text)[:10]
    if want and want not in inp:
        # 粘贴落错地方：清场（全选删除），绝不发送。
        # 坑（2026-11-28 实测）：Esc(key code 53) 会把微信主窗口整个关掉（窗口对象销毁，
        # open -a 都唤不回来，只能 osascript reopen）—— 清场绝不碰 Esc。
        run(["osascript", "-e",
             'tell application "System Events"\n'
             'keystroke "a" using command down\n'
             'delay 0.2\n'
             'key code 51\n'
             'end tell'], timeout=15)
        return False, "粘贴未落入输入框（焦点被劫持），已放弃发送"
    # 发送前先武装防回环（发送静默 20s + 文本回显 180s）——防接收循环竞态：
    # 接收循环可能在回车后的下一拍就把新气泡读到，若先读后武装就会自己推自己
    sent_echo[(chat, norm(text))] = time.time()
    muted_until[chat] = time.time() + 20
    run(["osascript", "-e", 'tell application "System Events" to key code 36'],
        timeout=15)
    time.sleep(1.0)
    #  politely 把前台还给用户之前的应用（截窗 -l 对隐藏窗口照样可用，不影响复核）
    if prev_app and "微信" not in prev_app and "WeChat" not in prev_app:
        run(["open", "-a", prev_app], timeout=15)

    # 发送复核：文本出现在右侧气泡 或 输入框已清空
    info3 = window_info() or info2
    png3 = os.path.join(STATE_DIR, "wx_send2.png")
    if capture_window(info3.get("windowid", 0), png3):
        s3 = float(info3.get("scale") or 2.0)
        w_px3 = int(info3["w"] * s3)
        side_px3 = int(312 * s3)
        top3 = int(88 * s3)
        bot3 = int((int(info3["h"]) - 140) * s3)
        rows3 = ocr_png(png3)
        bubbles = parse_bubbles(slice_rows(rows3, side_px3, top3, w_px3, bot3),
                                w_px3 - side_px3, png3, side_px3, top3)
        outs = norm("".join(t for side, t in bubbles if side == "out"))
        if want and want in outs:
            return True, "已发送（气泡复核命中）"
        inp3 = norm("".join((r.get("text") or "") for r in slice_rows(
            rows3, side_px3, bot3, w_px3, int(info3["h"] * s3))))
        if not inp3:
            return True, "已发送（输入框已清空）"
        return False, "发送未确认：气泡/输入框均未检测到"
    return True, "已发送（无法截窗复核）"


def outbox_loop(dry_send):
    log("出站轮询已启动（每 %.0f 秒）" % OUTBOX_POLL_SEC)
    while True:
        time.sleep(OUTBOX_POLL_SEC)
        try:
            r = http_json("/dsh-wechat-bridge/outbox")
            if not r.get("ok"):
                continue
            for item in r.get("消息") or []:
                chat = str(item.get("chat") or "").strip()
                text = str(item.get("text") or "").strip()
                if not chat or not text:
                    continue
                if dry_send:
                    res = {"ok": True, "说": "[dry] " + text[:40]}
                    log("[dry-send] → %s：%s" % (chat, text[:40]))
                else:
                    ok, 说 = send_via_ui(chat, text)
                    res = {"ok": ok, "说": 说}
                    log("%s → %s：%s" % ("✓" if ok else "✗", chat, text[:40]))
                if res["ok"]:
                    sent_echo[(chat, norm(text))] = time.time()
                    muted_until[norm(chat)] = time.time() + 15
                    # 全局回显键：标题 OCR 变体（「李健主号」/「李健 主号」）互换也能拦住
                    sent_texts[norm(text)] = time.time()
                http_json("/dsh-wechat-bridge/outbox/ack", {
                    "ok": bool(res["ok"]),
                    "chat": chat,
                    "说": res.get("说", ""),
                })
        except Exception as e:
            log("出站轮询出错：", repr(e))


# ---------------- 采集主循环 ----------------
# （scan_sidebar 已并入 receive_once 的全窗 OCR 单次切片，避免二次识别）
def slice_rows(rows, x0, y0, x1, y1):
    """全窗 OCR 行按 px 区域切片（左闭右开），坐标平移为区域相对。"""
    out = []
    for r in rows:
        x, y = r.get("x", 0), r.get("y", 0)
        if x0 <= x < x1 and y0 <= y < y1:
            out.append({**r, "x": x - x0, "y": y - y0})
    return out


def read_open_chat(info, png, rows=None, sidebar_y=None):
    """读取「当前已打开会话」——单次全窗 OCR + 坐标切片。
    坑（2026-11-27 实测）：wx_ocr 内部紧裁剪会显著降精度（标题错字「李健主号→子健エ子」、
    单字气泡丢失），全窗 OCR 全部读对且仅 ~0.25s —— 禁用一切内部裁剪。
    会话名优先用侧栏绿底颜色探针（确定性），OCR 标题兜底（滤时间戳行）。
    返回 (会话名或 None, [(方向, 文本)])。"""
    s = float(info.get("scale") or 2.0)
    w_px, h_px = int(info["w"] * s), int(info["h"] * s)
    side_px = int(312 * s)
    if rows is None:
        if not capture_window(info.get("windowid", 0), png):
            return None, []
        rows = ocr_png(png)
    if sidebar_y is None:
        parse_sidebar(slice_rows(rows, 0, 0, side_px, h_px), side_px)  # 填充 _sidebar_y
        sidebar_y = dict(_sidebar_y)
    name = selected_chat(png, sidebar_y)
    # 标题兜底：标题带 y < 50pt，滤掉时间戳行
    parts = [(r.get("text") or "").strip()
             for r in slice_rows(rows, side_px, 0, w_px, int(50 * s))]
    parts = [t for t in parts if t and not TIME_RE.match(t)]
    title = "".join(parts).strip()
    # 消息区：88pt ~ (h-140)pt；方向用颜色探针。
    # 坑（2026-11-28 实测）：输入框是固定高约 130pt 的部件，粘贴文字顶在 ~h-131pt——
    # 旧边界 h-95 会把输入框首行漏进消息区（自己刚贴的文字被当成别人消息推走），
    # 又把输入校验的切片起点压到文字下方（校验恒失败、误清场）。
    top_px = int(88 * s)
    bot_px = max(top_px + 1, int((int(info["h"]) - 140) * s))
    msg = slice_rows(rows, side_px, top_px, w_px, bot_px)
    bubbles = parse_bubbles(msg, w_px - side_px, png, side_px, top_px)
    return (name or title or None), bubbles


def push_message(chat, text):
    now = time.time()
    k = norm(chat)
    if now - last_push.get(k, 0) < 5:
        return False
    last_push[k] = now
    r = http_json("/dsh-wechat-bridge/receive", {
        "text": text,
        "chat": chat,
        "chatId": "",
        "chatType": "private",
        "sender": chat,
        "senderId": "",
        "atMe": False,
        "图片": [],
        "图片类型": "",
    })
    log("→ DSH：%s ｜ %s" % (chat, text[:40].replace("\n", " ")))
    return True


def receive_once(debug=False):
    """跑一轮采集。返回本轮处理的会话数。"""
    global _miss_streak, _last_reopen
    receive_once._panel_warned = getattr(receive_once, "_panel_warned", False)
    info = window_info()
    if not info:
        _miss_streak += 1
        # 自愈：窗口连续 15 轮不在（约 45s，多半是主窗口被关了），尝试 reopen（限频 60s）
        if _miss_streak >= 15 and time.time() - _last_reopen > 60:
            log("窗口连续 %d 轮不在，尝试 reopen 微信主窗口" % _miss_streak)
            reopen_wechat()
            _last_reopen = time.time()
            _miss_streak = 0
        return 0
    _miss_streak = 0
    # 坑（2026-11-27 实测）：搜索面板卡在打开态时窗口宽 1174pt，Esc/Cmd+W 都关不掉，
    # 此时标题/消息区裁剪会读到面板内容 —— 直接跳过本轮并提示人工关闭
    if int(info.get("w") or 0) >= 1000:
        if not receive_once._panel_warned:
            log("⚠ 微信搜索面板处于打开状态（窗口 %spt），请按 Esc 或点 ✕ 关闭后自动恢复"
                % info.get("w"))
            receive_once._panel_warned = True
        return 0
    receive_once._panel_warned = False
    png = os.path.join(STATE_DIR, "wx_now.png")
    if not capture_window(info.get("windowid", 0), png):
        # 坑（2026-11-28 实测）：windowid 会过期（窗口重建/切 Space 后句柄失效），
        # 先重查窗口拿新 id 再试一次；同 id 失败多半是瞬时抖动，下一轮自然重试
        info2 = window_info()
        if info2 and info2.get("windowid") != info.get("windowid"):
            info = info2
            if not capture_window(info.get("windowid", 0), png):
                log("⚠ 截窗失败（换新 windowid=%s 仍失败）" % info.get("windowid"))
                return 0
        else:
            log("⚠ 截窗失败（windowid=%s，下一轮自动重试）" % info.get("windowid"))
            return 0
    s = float(info.get("scale") or 2.0)
    side_px, h_px = int(312 * s), int(info["h"] * s)
    rows = ocr_png(png)
    cur = parse_sidebar(slice_rows(rows, 0, 0, side_px, h_px), side_px)
    title, bubbles = read_open_chat(info, png, rows=rows, sidebar_y=dict(_sidebar_y))
    if debug:
        print(json.dumps({"sidebar": cur, "open_chat": title, "bubbles": bubbles},
                         ensure_ascii=False, indent=2))
        return 0

    global sidebar_snap, last_open_title
    now = time.time()
    processed = 0

    # 核心：只采集「当前打开会话」的新消息（永不搜索开会话）
    if title:
        # 坑（2026-11-28 实测）：同一会话标题会被 OCR 成「李健主号」/「李健 主号」两种变体，
        # 按原样当钥匙会把 seen/静默/回显全部劈成两份 → 切换判定抖动 + 自己的回声被当新消息
        # （自检时 AI 回复被标题变体判定为「切会话→停机漏收」实锤推回 DSH）。统一用归一键。
        ckey = norm(title)
        incoming = [t for side, t in bubbles if side == "in"]
        seen = seen_bubbles.setdefault(ckey, deque(maxlen=40))
        if ckey != last_open_title:
            # 刚切到这个会话：铺底防回环。
            # 坑（2026-11-28 实测）：停机期间到达的消息不在持久指纹里，整体铺底会把
            # 它们当历史吞掉（用户 3 条消息实锤被吞、桥无回复）。改为：持久指纹里
            # 已有的照旧吞（历史回放），没有的视为停机漏收 → 补推（≤3 条，过 5s 节流）；
            # 仅首次运行（无任何持久状态）才整体铺底，避免首跑推送全部历史。
            # 回声兜底：180 秒内自己发过的文本（全局键）永不当「漏收」补推。
            last_open_title = ckey
            backlogged = [t for t in incoming
                          if norm(t) not in seen
                          and now - sent_texts.get(norm(t), 0) >= 180
                          ] if seen_bubbles else []
            for t in incoming:
                if norm(t) not in seen:
                    seen.append(norm(t))
            if backlogged:
                log("采集会话：「%s」｜ 发现 %d 条停机漏收，补推" % (title, len(backlogged)))
                for t in backlogged[-3:]:
                    if push_message(title, t):
                        processed += 1
                    time.sleep(5.2)  # push_message 每会话 5s 节流
            else:
                log("采集会话：「%s」（已有 %d 条，不推历史）" % (title, len(incoming)))
        else:
            fresh = [t for t in incoming
                     if norm(t) not in seen
                     and now >= muted_until.get(ckey, 0)
                     and now - sent_texts.get(norm(t), 0) >= 180]
            for t in fresh[-3:]:
                if push_message(title, t):
                    processed += 1
            for t in incoming:
                if norm(t) not in seen:
                    seen.append(norm(t))

    # 侧栏差分：仅提示「未打开会话有新消息」，不推送（无法免搜索读取）
    if cur:
        first = not sidebar_snap
        for name, preview in cur.items():
            if (first or sidebar_snap.get(name) != preview) \
                    and name != title and name not in CFG["屏蔽"] \
                    and now >= muted_until.get(norm(name), 0) and preview:
                log("ℹ「%s」有新消息但未打开该会话（桥只读打开的会话）：%s"
                    % (name, preview[:24]))
        for name in list(sidebar_snap):
            if name not in cur:
                del sidebar_snap[name]
        if first:
            log("侧栏快照初始化（%d 个会话）" % len(cur))
        sidebar_snap = dict(cur)
        save_state()
    if processed:
        save_state()
    return processed


def receive_loop(once=False, debug=False):
    global sidebar_snap, seen_bubbles, sent_texts
    st = load_state()
    sidebar_snap = st.get("sidebar") or {}
    seen_bubbles = {k: deque(v, maxlen=40) for k, v in (st.get("seen") or {}).items()}
    now = time.time()
    sent_texts = {k: float(v) for k, v in (st.get("sent_texts") or {}).items()
                  if now - float(v) < 180}
    if sidebar_snap:
        log("恢复状态：快照 %d 会话" % len(sidebar_snap))
    empty_streak = 0
    while True:
        try:
            n = receive_once(debug=debug)
            if n == 0:
                empty_streak += 1
            else:
                empty_streak = 0
            if empty_streak > 0 and empty_streak % 40 == 0:
                log("…仍在轮询（暂无新消息，连续 %d 轮空转）" % empty_streak)
        except Exception as e:
            log("收消息轮询出错：", repr(e))
        if once:
            break
        time.sleep(POLL_SEC)


# ---------------- 入口 ----------------

def main():
    global DSH, TOKEN, POLL_SEC, MY_NICK, log_fd, TOOL_DIR
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser()
    p.add_argument("--dsh", default=os.environ.get("DSH_ORIGIN", "http://127.0.0.1:3080"))
    p.add_argument("--token", default="")
    p.add_argument("--poll", type=float, default=3.0)
    p.add_argument("--nick", default="", help="小号昵称（预留）")
    p.add_argument("--tools-dir", default=os.path.join(here, "mac-bin"))
    p.add_argument("--dry-send", action="store_true", help="不真发，只打日志（测试用）")
    p.add_argument("--once", action="store_true", help="收消息只跑一轮（测试用）")
    p.add_argument("--no-keep-awake", action="store_true",
                   help="不阻止显示器睡眠（睡眠期间截图全黑、点击全失效）")
    p.add_argument("--scan-debug", action="store_true",
                   help="扫一轮：侧栏快照 + 打开会话气泡解析")
    args = p.parse_args()

    DSH, TOKEN, MY_NICK = args.dsh, args.token, args.nick
    POLL_SEC = max(2.0, args.poll)
    TOOL_DIR = args.tools_dir

    os.makedirs(STATE_DIR, exist_ok=True)
    log_path = os.path.join(STATE_DIR, "bridge.log")
    try:  # 简单轮转：超 5MB 挪到 .1，防长期运行无限膨胀
        if os.path.exists(log_path) and os.path.getsize(log_path) > 5 * 1024 * 1024:
            os.replace(log_path, log_path + ".1")
    except Exception:
        pass
    log_fd = open(log_path, "a", encoding="utf-8")

    keep_awake = None
    if not args.no_keep_awake:
        try:
            keep_awake = subprocess.Popen(
                ["caffeinate", "-d"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log("显示器防睡眠已开启（caffeinate -d）")
        except Exception as e:
            log("⚠ 防睡眠启动失败：", repr(e))

    log("=== Mac 桥 v2 启动 → %s（工具=%s，发=%s，轮询=%.0fs）===" % (
        DSH, TOOL_DIR, "dry" if args.dry_send else "OCR验证", POLL_SEC))

    def _on_sigterm(signum, frame):
        raise KeyboardInterrupt  # 借既有退出路径：记退出日志并走 finally 终止 caffeinate（防孤儿堆积）

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except Exception:
        pass

    refresh_config()
    import threading
    threading.Thread(target=outbox_loop, args=(args.dry_send,), daemon=True).start()
    try:
        receive_loop(once=args.once or args.scan_debug, debug=args.scan_debug)
    except KeyboardInterrupt:
        log("收到中断，退出")
    finally:
        if keep_awake:
            keep_awake.terminate()


if __name__ == "__main__":
    main()
