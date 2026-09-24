#!/usr/bin/env swift
// Read-only Vision observations for the standalone crop trial.
import Foundation
import Vision

guard CommandLine.arguments.count >= 2 else {
    fputs("usage: crop-trial-vision image.jpg | crop-trial-vision score image.jpg...\n", stderr)
    exit(2)
}

if CommandLine.arguments[1] == "score" {
    var scores: [[String: Any]] = []
    for path in CommandLine.arguments.dropFirst(2) {
        let request = VNCalculateImageAestheticsScoresRequest()
        do {
            try VNImageRequestHandler(url: URL(fileURLWithPath: path), options: [:]).perform([request])
            scores.append(["path": path,
                           "score": request.results?.first.map { Double($0.overallScore) } as Any? ?? NSNull()])
        } catch {
            scores.append(["path": path, "score": NSNull(), "error": String(describing: error)])
        }
    }
    let data = try JSONSerialization.data(withJSONObject: scores, options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!)
    exit(0)
}

guard CommandLine.arguments.count == 2 else {
    fputs("usage: crop-trial-vision image.jpg\n", stderr)
    exit(2)
}

let url = URL(fileURLWithPath: CommandLine.arguments[1])
let face = VNDetectFaceRectanglesRequest()
let human = VNDetectHumanRectanglesRequest()
let attention = VNGenerateAttentionBasedSaliencyImageRequest()
let objectness = VNGenerateObjectnessBasedSaliencyImageRequest()
let horizon = VNDetectHorizonRequest()

func box(_ rect: CGRect, confidence: Float) -> [String: Double] {
    ["x": Double(rect.minX), "y": Double(1 - rect.maxY),
     "width": Double(rect.width), "height": Double(rect.height),
     "confidence": Double(confidence)]
}

do {
    try VNImageRequestHandler(url: url, options: [:]).perform(
        [face, human, attention, objectness, horizon]
    )
    let payload: [String: Any] = [
        "faces": (face.results ?? []).map { box($0.boundingBox, confidence: $0.confidence) },
        "humans": (human.results ?? []).map { box($0.boundingBox, confidence: $0.confidence) },
        "attention": (attention.results?.first?.salientObjects ?? []).map {
            box($0.boundingBox, confidence: $0.confidence)
        },
        "objects": (objectness.results?.first?.salientObjects ?? []).map {
            box($0.boundingBox, confidence: $0.confidence)
        },
        "horizon_degrees": horizon.results?.first.map { Double($0.angle) * 180 / .pi } as Any? ?? NSNull(),
    ]
    let data = try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!)
} catch {
    fputs("Vision failed: \(error)\n", stderr)
    exit(1)
}
