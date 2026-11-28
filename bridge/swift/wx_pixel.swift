import AppKit
import Foundation

// wx_pixel <png> x y [w h] -> 输出区域平均色 "r,g,b"（像素坐标）
let args = CommandLine.arguments
guard args.count >= 4,
      let x = Int(args[2]), let y = Int(args[3]) else {
    print("usage: wx_pixel <png> x y [w h]"); exit(2)
}
let w = args.count >= 6 ? (Int(args[4]) ?? 8) : 8
let h = args.count >= 6 ? (Int(args[5]) ?? 8) : 8
guard let img = NSImage(contentsOfFile: args[1]),
      let tiff = img.tiffRepresentation,
      let rep = NSBitmapImageRep(data: tiff) else {
    print("ERR_LOAD"); exit(1)
}
var r = 0, g = 0, b = 0, n = 0
for py in y..<(y + h) {
    for px in x..<(x + w) {
        guard px >= 0, py >= 0, px < rep.pixelsWide, py < rep.pixelsHigh else { continue }
        guard let c = rep.colorAt(x: px, y: py) else { continue }
        r += Int(c.redComponent * 255)
        g += Int(c.greenComponent * 255)
        b += Int(c.blueComponent * 255)
        n += 1
    }
}
if n == 0 { print("ERR_RANGE"); exit(1) }
print("\(r / n),\(g / n),\(b / n)")
