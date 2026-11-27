import Foundation
import Vision
import AppKit

// wx_ocr <imagePath> [fast] [cropX cropY cropW cropH]
// macOS Vision OCR，输出 JSON 行：{"text":..,"x":..,"y":..,"w":..,"h":..}（像素坐标，y 从顶部起）
// fast：识别速度优先（轮询侦测用）；默认 accurate（消息精读用）
// crop：可选像素裁剪框，只识别该区域（大幅提速）

let args = CommandLine.arguments
guard args.count >= 2 else { print("{\"error\":\"usage: wx_ocr <image> [fast] [x y w h]\"}"); exit(1) }
let url = URL(fileURLWithPath: args[1])
guard let img = NSImage(contentsOf: url),
      let full = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    print("{\"error\":\"cannot load image\"}"); exit(1)
}

// 解析参数：<image> [fast] [x y w h]
var idx = 2
var fastMode = false
if args.count > idx, args[idx] == "fast" { fastMode = true; idx += 1 }
var cropRect: CGRect? = nil
if args.count >= idx + 4,
   let cx = Int(args[idx]), let cy = Int(args[idx + 1]),
   let cw = Int(args[idx + 2]), let ch = Int(args[idx + 3]) {
    cropRect = CGRect(x: cx, y: cy, width: cw, height: ch)
}

var cg = full
if let rect = cropRect {
    let clamped = rect.intersection(CGRect(x: 0, y: 0, width: full.width, height: full.height))
    if !clamped.isEmpty, let cropped = full.cropping(to: clamped) {
        cg = cropped
    }
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = fastMode ? .fast : .accurate
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.usesLanguageCorrection = false
let handler = VNImageRequestHandler(cgImage: cg, options: [:])
try? handler.perform([request])

// 裁剪时把局部坐标平移回整图坐标
let ox = Int(cropRect?.origin.x ?? 0)
let oy = Int(cropRect?.origin.y ?? 0)
let h = Double(cg.height)
for obs in (request.results ?? []) {
    guard let t = obs.topCandidates(1).first else { continue }
    let bb = obs.boundingBox
    let x = Double(ox) + bb.origin.x * Double(cg.width)
    let y = Double(oy) + (1 - bb.origin.y - bb.height) * h
    let payload: [String: Any] = [
        "text": t.string,
        "x": Int(x), "y": Int(y),
        "w": Int(bb.width * Double(cg.width)), "h": Int(bb.height * h),
    ]
    if let d = try? JSONSerialization.data(withJSONObject: payload),
       let s = String(data: d, encoding: .utf8) {
        print(s)
    }
}
