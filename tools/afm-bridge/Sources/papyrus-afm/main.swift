// papyrus-afm: a tiny JSON-in / JSON-out bridge to Apple's on-device Foundation Models.
//
// One-shot:  echo '{"op":"availability"}' | papyrus-afm
// Serve:     papyrus-afm --serve   (one JSON request per stdin line, one JSON response per stdout line)
//
// Ops:
//   {"op":"availability"}
//     -> {"available":bool, "reason":str?, "context_tokens":int, "model":str, ...}
//   {"op":"count_tokens", "text":str}
//     -> {"ok":true, "tokens":int}
//   {"op":"extract", "instructions":str, "prompt":str, "schema":{JSON Schema subset},
//    "temperature":float?, "max_tokens":int?, "fit":bool? (default true),
//    "guardrails":"permissive"|"default"? (default permissive),
//    "use_case":"general"|"contentTagging"? (default general),
//    "guardrail_fallback":bool? (default true)}
//     -> {"ok":true, "result":{...}, "usage":{...}, "elapsed_ms":int, ...}
//     -> {"ok":false, "error":str, "kind":"context_overflow|guardrail|unavailable|other", ...}
//   {"op":"ocr", "path":str, "pages":[int], ...}   (Vision text recognition; see OCR.swift)
//     -> {"ok":true, "pages":[{"page":int, "text":str, "confidence":float}], ...}
//
// Every response echoes the request's "id" field if present. The process exits 0 even on
// model errors; only malformed invocations produce a non-zero exit.

import Foundation
import FoundationModels

typealias JSON = [String: Any]

// MARK: - I/O helpers

func emit(_ obj: JSON) {
    var obj = obj
    // JSONSerialization cannot encode NaN/inf; we never produce them, but be safe.
    if !JSONSerialization.isValidJSONObject(obj) {
        obj = ["ok": false, "error": "internal: response not JSON-serialisable", "kind": "other"]
    }
    let data = (try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys, .withoutEscapingSlashes]))
        ?? Data(#"{"ok":false,"error":"internal: encode failed","kind":"other"}"#.utf8)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

func ms(since start: ContinuousClock.Instant) -> Int {
    let d = ContinuousClock.now - start
    return Int(d.components.seconds * 1000 + d.components.attoseconds / 1_000_000_000_000_000)
}

struct BridgeError: Error, CustomStringConvertible {
    let kind: String
    let message: String
    var description: String { message }
}

// MARK: - JSON Schema -> DynamicGenerationSchema

/// Converts the conservative JSON Schema subset used by papyrus into a DynamicGenerationSchema.
/// Supported: type object/array/string/integer/number/boolean (or ["x","null"]), properties,
/// required, items, enum (strings), description, minimum/maximum, minItems/maxItems, pattern.
struct SchemaConverter {
    private var usedNames = Set<String>()

    private mutating func uniqueName(_ base: String) -> String {
        let cleaned = base.map { $0.isLetter || $0.isNumber ? $0 : "_" }
        var name = String(cleaned)
        if name.isEmpty { name = "Value" }
        var candidate = name
        var i = 2
        while usedNames.contains(candidate) {
            candidate = "\(name)_\(i)"
            i += 1
        }
        usedNames.insert(candidate)
        return candidate
    }

    private static func typeName(_ s: JSON) -> String? {
        if let t = s["type"] as? String { return t }
        if let ts = s["type"] as? [Any] {
            return ts.compactMap { $0 as? String }.first { $0 != "null" }
        }
        if s["properties"] != nil { return "object" }
        if s["items"] != nil { return "array" }
        if s["enum"] != nil { return "string" }
        return nil
    }

    private static func int(_ v: Any?) -> Int? {
        if let n = v as? NSNumber { return n.intValue }
        return nil
    }

    private static func double(_ v: Any?) -> Double? {
        if let n = v as? NSNumber { return n.doubleValue }
        return nil
    }

    mutating func convert(_ s: JSON, name: String) throws -> DynamicGenerationSchema {
        let desc = s["description"] as? String
        if let en = s["enum"] as? [Any] {
            let choices = en.compactMap { v -> String? in
                if v is NSNull { return nil }
                if let str = v as? String { return str }
                return "\(v)"
            }
            guard !choices.isEmpty else { throw BridgeError(kind: "other", message: "schema: empty enum at \(name)") }
            return DynamicGenerationSchema(name: uniqueName(name), description: desc, anyOf: choices)
        }
        switch Self.typeName(s) {
        case "object":
            let props = s["properties"] as? JSON ?? [:]
            let required = Set((s["required"] as? [Any] ?? []).compactMap { $0 as? String })
            // Keep declaration order if the caller used an ordered dict; JSONSerialization loses
            // order, so honour an optional "propertyOrdering"/"x-order" hint, else required-first.
            var keys = (s["propertyOrdering"] as? [String]) ?? (s["x-order"] as? [String]) ?? []
            let rest = props.keys.filter { !keys.contains($0) }.sorted { a, b in
                let ra = required.contains(a), rb = required.contains(b)
                return ra != rb ? ra : a < b
            }
            keys = keys.filter { props[$0] != nil } + rest
            var properties: [DynamicGenerationSchema.Property] = []
            for key in keys {
                guard let ps = props[key] as? JSON else { continue }
                let child = try convert(ps, name: "\(name)_\(key)")
                properties.append(.init(
                    name: key,
                    description: ps["description"] as? String,
                    schema: child,
                    isOptional: !required.contains(key)
                ))
            }
            return DynamicGenerationSchema(name: uniqueName(name), description: desc, properties: properties)
        case "array":
            let items = s["items"] as? JSON ?? ["type": "string"]
            let child = try convert(items, name: "\(name)_item")
            return DynamicGenerationSchema(
                arrayOf: child,
                minimumElements: Self.int(s["minItems"]),
                maximumElements: Self.int(s["maxItems"])
            )
        case "string", nil:
            var guides: [GenerationGuide<String>] = []
            if let p = s["pattern"] as? String, let re = try? Regex(p) {
                guides.append(.pattern(re))
            }
            return DynamicGenerationSchema(type: String.self, guides: guides)
        case "integer":
            var guides: [GenerationGuide<Int>] = []
            let lo = Self.int(s["minimum"]), hi = Self.int(s["maximum"])
            if let lo, let hi { guides.append(.range(lo...hi)) }
            else if let lo { guides.append(.minimum(lo)) }
            else if let hi { guides.append(.maximum(hi)) }
            return DynamicGenerationSchema(type: Int.self, guides: guides)
        case "number":
            var guides: [GenerationGuide<Double>] = []
            let lo = Self.double(s["minimum"]), hi = Self.double(s["maximum"])
            if let lo, let hi { guides.append(.range(lo...hi)) }
            else if let lo { guides.append(.minimum(lo)) }
            else if let hi { guides.append(.maximum(hi)) }
            return DynamicGenerationSchema(type: Double.self, guides: guides)
        case "boolean":
            return DynamicGenerationSchema(type: Bool.self)
        case let other?:
            throw BridgeError(kind: "other", message: "schema: unsupported type '\(other)' at \(name)")
        }
    }

    static func generationSchema(from json: JSON) throws -> GenerationSchema {
        var conv = SchemaConverter()
        let root = try conv.convert(json, name: (json["title"] as? String) ?? "Result")
        do {
            return try GenerationSchema(root: root, dependencies: [])
        } catch {
            throw BridgeError(kind: "other", message: "schema: \(error.localizedDescription)")
        }
    }
}

// MARK - Model handling

struct ModelKey: Hashable {
    let useCase: String
    let guardrails: String
}

@MainActor
final class Bridge {
    private var models: [ModelKey: SystemLanguageModel] = [:]

    func model(useCase: String = "general", guardrails: String = "permissive") -> SystemLanguageModel {
        let key = ModelKey(useCase: useCase, guardrails: guardrails)
        if let m = models[key] { return m }
        let uc: SystemLanguageModel.UseCase = useCase == "contentTagging" ? .contentTagging : .general
        let gr: SystemLanguageModel.Guardrails = guardrails == "default" ? .default : .permissiveContentTransformations
        let m = SystemLanguageModel(useCase: uc, guardrails: gr)
        models[key] = m
        return m
    }

    static func modelName(_ m: SystemLanguageModel) -> (name: String, variant: String?) {
        if #available(macOS 27.0, *) {
            let v = m.variant
            if v == .core3 { return ("core3", v.displayName) }
            if v == .coreAdvanced3 { return ("coreAdvanced3", v.displayName) }
            return (v.displayName.replacingOccurrences(of: " ", with: "-"), v.displayName)
        }
        return ("system-default", nil)
    }

    /// With probe=true, also run a tokenizer call to prove the model service will actually
    /// accept requests right now (catches the memory-pressure refusal).
    func availability(probe: Bool) async -> JSON {
        var out = availabilityStatic()
        if probe, out["available"] as? Bool == true {
            do {
                _ = try await countTokens("ping")
                out["ready"] = true
                let ctx = SystemLanguageModel.default.contextSize
                if ctx > 0 { out["context_tokens"] = ctx }
                out["model"] = Self.modelName(SystemLanguageModel.default).name
            } catch {
                out["ready"] = false
                out["available"] = false
                let c = Self.classify(error)
                out["reason"] = c["detail"] ?? c["error"]
            }
        }
        return out
    }

    func availabilityStatic() -> JSON {
        let m = SystemLanguageModel.default
        var out: JSON = [:]
        switch m.availability {
        case .available:
            out["available"] = true
        case .unavailable(let reason):
            out["available"] = false
            switch reason {
            case .deviceNotEligible: out["reason"] = "deviceNotEligible"
            case .appleIntelligenceNotEnabled: out["reason"] = "appleIntelligenceNotEnabled"
            case .modelNotReady: out["reason"] = "modelNotReady"
            @unknown default: out["reason"] = "\(reason)"
            }
        @unknown default:
            out["available"] = false
            out["reason"] = "unknown"
        }
        // contextSize reads 0 when modelmanagerd cannot serve metadata (e.g. memory pressure).
        let ctx = m.contextSize
        out["context_tokens"] = ctx > 0 ? ctx : NSNull()
        let (name, variant) = Self.modelName(m)
        out["model"] = name
        if let variant { out["variant"] = variant }
        out["os"] = ProcessInfo.processInfo.operatingSystemVersionString
        out["supports_locale"] = m.supportsLocale()
        if #available(macOS 27.0, *) {
            var caps: [String] = []
            let c = m.capabilities
            if c.contains(.guidedGeneration) { caps.append("guidedGeneration") }
            if c.contains(.toolCalling) { caps.append("toolCalling") }
            if c.contains(.vision) { caps.append("vision") }
            if c.contains(.reasoning) { caps.append("reasoning") }
            out["capabilities"] = caps
        }
        return out
    }

    func countTokens(_ text: String) async throws -> Int {
        if #available(macOS 26.4, *) {
            return try await SystemLanguageModel.default.tokenCount(for: text)
        }
        // Fallback heuristic (pre-26.4 has no tokenizer API): ~3 chars per token, conservative.
        return text.utf8.count / 3 + 1
    }

    // MARK: extract

    func extract(_ req: JSON) async -> JSON {
        let start = ContinuousClock.now
        guard let prompt = req["prompt"] as? String else {
            return ["ok": false, "error": "missing 'prompt'", "kind": "other"]
        }
        let instructions = req["instructions"] as? String ?? ""
        guard let schemaJSON = req["schema"] as? JSON else {
            return ["ok": false, "error": "missing 'schema'", "kind": "other"]
        }
        let temperature = (req["temperature"] as? NSNumber)?.doubleValue ?? 0.0
        let maxTokens = (req["max_tokens"] as? NSNumber)?.intValue ?? 1024
        let fit = req["fit"] as? Bool ?? true
        let guardrails = req["guardrails"] as? String ?? "permissive"
        let useCase = req["use_case"] as? String ?? "general"
        let fallback = req["guardrail_fallback"] as? Bool ?? true

        let model = self.model(useCase: useCase, guardrails: guardrails)
        if !model.isAvailable {
            return ["ok": false, "error": "model unavailable: \(model.availability)", "kind": "unavailable"]
        }

        let schema: GenerationSchema
        do {
            if req["schema_mode"] as? String == "codable" {
                // GenerationSchema is Codable and accepts (Apple's dialect of) JSON Schema directly;
                // this is what apple/python-apple-fm-sdk does. Default is our own converter.
                let data = try JSONSerialization.data(withJSONObject: schemaJSON)
                do {
                    schema = try JSONDecoder().decode(GenerationSchema.self, from: data)
                } catch {
                    throw BridgeError(kind: "other", message: "schema (codable): \(error)")
                }
            } else {
                schema = try SchemaConverter.generationSchema(from: schemaJSON)
            }
        } catch let e as BridgeError {
            return ["ok": false, "error": e.message, "kind": e.kind]
        } catch {
            return ["ok": false, "error": "\(error)", "kind": "other"]
        }

        var meta: JSON = ["model": Self.modelName(model).name, "context_tokens": model.contextSize]

        // Fit the prompt into the context window using the real tokenizer (macOS 26.4+).
        var promptText = prompt
        if fit, #available(macOS 26.4, *) {
            do {
                let fixed = try await model.tokenCount(for: Instructions(instructions))
                    + model.tokenCount(for: schema)
                let budget = model.contextSize - maxTokens - fixed - 64  // template/role overhead
                meta["overhead_tokens"] = fixed
                var ptoks = try await model.tokenCount(for: promptText)
                meta["prompt_tokens_in"] = ptoks
                if budget <= 0 {
                    return ["ok": false, "kind": "context_overflow",
                            "error": "instructions+schema+max_tokens (\(fixed)+\(maxTokens)) exceed context (\(model.contextSize))"]
                        .merging(meta) { a, _ in a }
                }
                var rounds = 0
                while ptoks > budget && rounds < 6 {
                    let ratio = Double(budget) / Double(ptoks) * 0.97
                    let keep = max(0, Int(Double(promptText.count) * ratio))
                    var cut = promptText.prefix(keep)
                    if let ws = cut.lastIndex(where: { $0 == " " || $0 == "\n" }),
                       cut.distance(from: cut.startIndex, to: ws) > keep * 9 / 10 {
                        cut = cut[..<ws]  // don't end mid-word
                    }
                    promptText = String(cut)
                    ptoks = try await model.tokenCount(for: promptText)
                    rounds += 1
                }
                if promptText.count < prompt.count {
                    meta["truncated"] = true
                    meta["kept_chars"] = promptText.count
                }
                meta["prompt_tokens"] = ptoks
            } catch {
                meta["fit_error"] = "\(error)"
            }
        }

        let options: GenerationOptions = temperature <= 0
            ? GenerationOptions(samplingMode: .greedy, maximumResponseTokens: maxTokens)
            : GenerationOptions(temperature: temperature, maximumResponseTokens: maxTokens)

        let busyRetries = (req["busy_retries"] as? NSNumber)?.intValue ?? 2
        var attempt = 0
        while true {
        do {
            let session = LanguageModelSession(model: model, instructions: instructions.isEmpty ? nil : instructions)
            let response = try await session.respond(to: promptText, schema: schema, includeSchemaInPrompt: true, options: options)
            if attempt > 0 { meta["busy_retries_used"] = attempt }
            var out: JSON = ["ok": true, "mode": "guided", "result": Self.toJSON(response.content)]
            if #available(macOS 27.0, *) {
                out["usage"] = [
                    "input_tokens": response.usage.input.totalTokenCount,
                    "cached_tokens": response.usage.input.cachedTokenCount,
                    "output_tokens": response.usage.output.totalTokenCount,
                ]
            }
            out["elapsed_ms"] = ms(since: start)
            return out.merging(meta) { a, _ in a }
        } catch {
            if Self.isSystemBusy(error) && attempt < busyRetries {
                attempt += 1
                try? await Task.sleep(for: .seconds(2 * attempt))
                continue
            }
            var err = Self.classify(error)
            if err["kind"] as? String == "guardrail" && fallback {
                // Guided generation still runs the default output guardrails; plain-text
                // generation under .permissiveContentTransformations does not, so retry by
                // asking for JSON as text and parsing it ourselves.
                if let fb = await unguidedFallback(instructions: instructions, prompt: promptText,
                                                   schemaJSON: schemaJSON, options: options, useCase: useCase) {
                    var out = fb
                    out["guided_error"] = err["error"]
                    out["elapsed_ms"] = ms(since: start)
                    return out.merging(meta) { a, _ in a }
                }
            }
            err["ok"] = false
            err["elapsed_ms"] = ms(since: start)
            return err.merging(meta) { a, _ in a }
        }
        }
    }

    private func unguidedFallback(instructions: String, prompt: String, schemaJSON: JSON,
                                  options: GenerationOptions, useCase: String) async -> JSON? {
        let model = self.model(useCase: useCase, guardrails: "permissive")
        let schemaText = (try? JSONSerialization.data(withJSONObject: schemaJSON, options: [.sortedKeys]))
            .flatMap { String(data: $0, encoding: .utf8) } ?? "{}"
        let instr = instructions + "\n\nRespond with ONLY a single JSON object (no prose, no code fences) that conforms to this JSON Schema:\n" + schemaText
        do {
            let session = LanguageModelSession(model: model, instructions: instr)
            let response = try await session.respond(to: prompt, options: options)
            let text = response.content
            guard let lo = text.firstIndex(of: "{"), let hi = text.lastIndex(of: "}"), lo < hi,
                  let obj = try? JSONSerialization.jsonObject(with: Data(text[lo...hi].utf8)) as? JSON
            else {
                return ["ok": false, "kind": "other", "error": "fallback produced non-JSON output", "raw": String(text.prefix(2000))]
            }
            return ["ok": true, "mode": "unguided_fallback", "result": obj]
        } catch {
            var e = Self.classify(error)
            e["ok"] = false
            e["mode"] = "unguided_fallback"
            return e
        }
    }

    static func toJSON(_ content: GeneratedContent) -> Any {
        let s = content.jsonString
        if let obj = try? JSONSerialization.jsonObject(with: Data(s.utf8), options: [.fragmentsAllowed]) {
            return obj
        }
        return s
    }

    /// modelmanagerd refuses requests from background (non-app) processes while the system is
    /// under memory pressure: ModelManagerError 1013 "Not executed due to current system state
    /// [CriticalMemoryPressure], try again later". It surfaces wrapped in other errors.
    static func isSystemBusy(_ error: Error) -> Bool {
        func walk(_ e: NSError, depth: Int) -> Bool {
            if e.domain.contains("ModelManagerError") && e.code == 1013 { return true }
            guard depth < 6 else { return false }
            let subs = (e.userInfo[NSMultipleUnderlyingErrorsKey] as? [NSError] ?? [])
                + [e.userInfo[NSUnderlyingErrorKey] as? NSError].compactMap { $0 }
            return subs.contains { walk($0, depth: depth + 1) }
        }
        if walk(error as NSError, depth: 0) { return true }
        return String(describing: error).contains("ModelManagerError error 1013")
    }

    static func classify(_ error: Error) -> JSON {
        if isSystemBusy(error) {
            return ["kind": "unavailable", "detail": "system_busy", "retryable": true,
                    "error": "model refused by modelmanagerd (likely CriticalMemoryPressure; background requests are denied). Try again later."]
        }
        if #available(macOS 27.0, *) {
            if let e = error as? LanguageModelError {
                switch e {
                case .contextSizeExceeded(let c):
                    return ["kind": "context_overflow", "error": c.debugDescription,
                            "context_size": c.contextSize, "token_count": c.tokenCount]
                case .guardrailViolation(let c):
                    return ["kind": "guardrail", "detail": "guardrailViolation", "error": c.debugDescription]
                case .refusal(let c):
                    return ["kind": "guardrail", "detail": "refusal", "error": c.debugDescription]
                case .rateLimited(let c):
                    return ["kind": "other", "detail": "rateLimited", "error": c.debugDescription]
                case .unsupportedLanguageOrLocale(let c):
                    return ["kind": "other", "detail": "unsupportedLanguageOrLocale", "error": c.debugDescription]
                case .unsupportedGenerationGuide(let c):
                    return ["kind": "other", "detail": "unsupportedGenerationGuide", "error": c.debugDescription]
                case .timeout(let c):
                    return ["kind": "other", "detail": "timeout", "error": c.debugDescription]
                default:
                    return ["kind": "other", "error": e.debugDescription]
                }
            }
            if let e = error as? SystemLanguageModel.Error {
                return ["kind": "unavailable", "error": e.debugDescription]
            }
            if let e = error as? GeneratedContent.ParsingError {
                return ["kind": "other", "detail": "parsing", "error": e.debugDescription]
            }
            if let e = error as? LanguageModelSession.Error {
                return ["kind": "other", "detail": "session", "error": e.debugDescription]
            }
        }
        if let e = error as? LanguageModelSession.GenerationError {
            switch e {
            case .exceededContextWindowSize(let c):
                return ["kind": "context_overflow", "error": c.debugDescription]
            case .guardrailViolation(let c):
                return ["kind": "guardrail", "detail": "guardrailViolation", "error": c.debugDescription]
            case .refusal(_, let c):
                return ["kind": "guardrail", "detail": "refusal", "error": c.debugDescription]
            case .assetsUnavailable(let c):
                return ["kind": "unavailable", "error": c.debugDescription]
            default:
                return ["kind": "other", "error": e.localizedDescription]
            }
        }
        return ["kind": "other", "error": String(describing: error)]
    }

    // MARK: dispatch

    func handle(_ line: String) async -> JSON {
        guard let data = line.data(using: .utf8),
              let req = (try? JSONSerialization.jsonObject(with: data)) as? JSON
        else {
            return ["ok": false, "error": "request is not a JSON object", "kind": "other"]
        }
        var out: JSON
        switch req["op"] as? String {
        case "availability":
            out = await availability(probe: req["probe"] as? Bool ?? false)
        case "count_tokens":
            do {
                out = ["ok": true, "tokens": try await countTokens(req["text"] as? String ?? "")]
            } catch {
                out = Self.classify(error).merging(["ok": false]) { a, _ in a }
            }
        case "extract":
            out = await extract(req)
        case "ocr":
            out = await OCR.run(req)  // OCR.swift
        case let op:
            out = ["ok": false, "error": "unknown op: \(op ?? "<missing>")", "kind": "other"]
        }
        if let id = req["id"] { out["id"] = id }
        return out
    }
}

// MARK: - main

let args = CommandLine.arguments.dropFirst()
if args.contains("--help") || args.contains("-h") {
    print("usage: papyrus-afm [--serve]  (reads JSON request(s) from stdin; see README.md)")
    exit(0)
}
let bridge = Bridge()
if args.contains("--serve") {
    while let line = readLine(strippingNewline: true) {
        if line.trimmingCharacters(in: .whitespaces).isEmpty { continue }
        emit(await bridge.handle(line))
    }
} else {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    let text = String(decoding: data, as: UTF8.self)
    emit(await bridge.handle(text))
}
