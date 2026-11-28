import Foundation
import CoreGraphics
import AppKit

// wx_winid — 找微信主窗口（layer 0、面积最大），输出一行 JSON：
//   {"found":true,"windowid":987,"x":295,"y":158,"w":880,"h":640,"onscreen":true}
//   {"found":false,"reason":"..."}
// 坐标是屏幕点；截图由调用方用 `screencapture -x -o -l<windowid> <png>` 完成
// （不要在本工具里拉 screencapture：中间隔一层无签名二进制会断 TCC 屏幕录制权限归因）。
// 坑（2026-11-28 实测）：微信失焦时主窗口被隐藏，onScreenOnly 列表里没有它，
// 但窗口对象仍在——加 optionAll 兜底；隐藏窗口 screencapture -l 照样能截到真内容，
// 收链路不需要抢焦点。

func isWeChatPID(_ pid: Int) -> Bool {
    NSRunningApplication(processIdentifier: pid_t(pid))?.bundleIdentifier == "com.tencent.xinWeChat"
}

func emit(_ obj: [String: Any]) {
    if let d = try? JSONSerialization.data(withJSONObject: obj),
       let s = String(data: d, encoding: .utf8) {
        print(s)
    }
}

func pick(_ onScreenOnly: Bool) -> (CGWindowID, CGRect, CGFloat)? {
    var opts: CGWindowListOption = [.excludeDesktopElements]
    if onScreenOnly { opts.insert(.optionOnScreenOnly) }
    guard let list = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) as? [[String: Any]] else { return nil }
    var best: (CGWindowID, CGRect, CGFloat)? = nil
    for info in list {
        let layer = info[kCGWindowLayer as String] as? Int ?? -1
        guard layer == 0 else { continue }
        let pid = info[kCGWindowOwnerPID as String] as? Int ?? -1
        guard isWeChatPID(pid) else { continue }
        guard let b = info[kCGWindowBounds as String] as? [String: NSNumber] else { continue }
        let w = b["Width"]?.doubleValue ?? 0
        let h = b["Height"]?.doubleValue ?? 0
        guard w > 300, h > 300 else { continue } // 跳掉小悬浮窗
        let area = w * h
        if best == nil || area > best!.2 {
            let id = info[kCGWindowNumber as String] as? Int ?? 0
            let rect = CGRect(x: b["X"]?.doubleValue ?? 0, y: b["Y"]?.doubleValue ?? 0,
                              width: w, height: h)
            best = (CGWindowID(id), rect, area)
        }
    }
    return best
}

if let win = pick(true) {
    emit(["found": true, "onscreen": true,
          "windowid": Int(win.0),
          "x": win.1.origin.x, "y": win.1.origin.y,
          "w": win.1.width, "h": win.1.height])
    exit(0)
}
if let win = pick(false) {
    // 窗口被隐藏（微信失焦）但对象还在：截窗仍可用
    emit(["found": true, "onscreen": false,
          "windowid": Int(win.0),
          "x": win.1.origin.x, "y": win.1.origin.y,
          "w": win.1.width, "h": win.1.height])
    exit(0)
}
emit(["found": false, "reason": "微信主窗口不存在（未运行/未登录）"])
exit(2)
