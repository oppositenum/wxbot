import Foundation
import Vision
let u=URL(fileURLWithPath:CommandLine.arguments[1])
let request=VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.usesLanguageCorrection = false
try VNImageRequestHandler(url:u, options:[:]).perform([request])
let out=(request.results ?? []).compactMap { x -> [String:Any]? in
 guard let t=x.topCandidates(1).first else { return nil }
 let r=x.boundingBox
 return ["text":t.string,"confidence":t.confidence,"box":[r.minX,r.minY,r.width,r.height]]
}
let data=try JSONSerialization.data(withJSONObject:out)
FileHandle.standardOutput.write(data)
