// StreamTranscribe.swift — LIVE streaming speech-to-text for Kiro Crew's `apple`
// STT provider. Sibling of AppleTranscribe.swift (which does whole-file batch work).
//
// PROTOCOL (deliberately dumb, so the Python side stays a thin pipe)
//   stdin  : raw PCM, 16-bit signed little-endian, mono, --sample-rate Hz.
//            A continuous byte stream; chunk boundaries are irrelevant.
//            EOF means "the user stopped talking" -> finalize and exit.
//   stdout : one JSON object per line, flushed immediately:
//              {"type":"ready"}                     once, before any audio is read
//              {"type":"partial","text":"..."}      volatile hypothesis, replaces prior
//              {"type":"final","text":"..."}        stabilized, will not change
//              {"type":"done","text":"<full>"}      after finalization
//              {"type":"error","message":"..."}     fatal; exit 1
//   stderr : diagnostics only, never parsed.
//
// WHY A LONG-LIVED PROCESS: the batch helper pays ~70 ms of process start per call,
// which is invisible for a whole file but would dominate a 100 ms streaming cadence.
// One process per dictation session amortizes it to zero.
//
// WHY `.volatileResults`: that reporting option is what makes SpeechTranscriber emit
// hypotheses *before* the utterance ends — the whole point of live dictation. Results
// carry `isFinal`, so partial vs final is the framework's judgment, not a guess based
// on timing.

import AVFoundation
import Foundation
import Speech

// MARK: - Line-oriented JSON output

/// stdout writes must be atomic per line and unbuffered: the Python reader does
/// `readline()`, and a partial line would be parsed as a truncated object.
let stdoutLock = NSLock()

func emitLine(_ payload: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: payload) else { return }
    stdoutLock.lock()
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
    stdoutLock.unlock()
}

func die(_ message: String) -> Never {
    emitLine(["type": "error", "message": message])
    exit(1)
}

func note(_ message: String) {
    FileHandle.standardError.write(Data("\(message)\n".utf8))
}

// MARK: - PCM framing

/// Wrap raw interleaved Int16 bytes in an `AVAudioPCMBuffer`.
///
/// Returns nil for an empty or odd-length slice (a torn frame at a chunk boundary);
/// the caller carries the odd trailing byte into the next read rather than dropping
/// it, because a dropped byte would invert the endianness of every later sample.
func makeBuffer(_ bytes: [UInt8], format: AVAudioFormat) -> AVAudioPCMBuffer? {
    let frameCount = bytes.count / 2
    guard frameCount > 0,
        let buffer = AVAudioPCMBuffer(
            pcmFormat: format, frameCapacity: AVAudioFrameCount(frameCount))
    else { return nil }
    buffer.frameLength = AVAudioFrameCount(frameCount)
    guard let channel = buffer.int16ChannelData?[0] else { return nil }
    bytes.withUnsafeBufferPointer { raw in
        raw.baseAddress!.withMemoryRebound(to: Int16.self, capacity: frameCount) { samples in
            channel.update(from: samples, count: frameCount)
        }
    }
    return buffer
}

// MARK: - Format conversion

/// Renders the Int16 PCM the client streams into the format `SpeechAnalyzer` asked for.
///
/// `SpeechAnalyzer` accepts audio only in the format reported by
/// `bestAvailableAudioFormat(compatibleWith:)` — a Float32 layout at whatever rate the
/// on-device model wants, never the 16 kHz interleaved Int16 the browser sends. The
/// framework ships no converter of its own for this, so `AVAudioConverter` does the
/// work and each result is wrapped in `AnalyzerInput(buffer:)`.
///
/// `primeMethod = .none` trades a little resampling quality for latency: with priming
/// on, the converter withholds the leading frames of the stream to fill its filter
/// history, which delays the first partial against a ~100 ms chunk cadence.
final class AnalyzerFormatConverter {
    enum Failure: Error {
        case unsupported(from: AVAudioFormat, to: AVAudioFormat)
        case noOutputBuffer
        case failed(NSError?)
    }

    private let target: AVAudioFormat
    private var converter: AVAudioConverter?

    init(target: AVAudioFormat) {
        self.target = target
    }

    /// *buffer* in the analyzer's format — or *buffer* itself when the formats already
    /// match, since the copy would be pure overhead.
    func convert(_ buffer: AVAudioPCMBuffer) throws -> AVAudioPCMBuffer {
        let source = buffer.format
        guard source != target else { return buffer }
        if converter == nil || converter?.inputFormat != source {
            guard let made = AVAudioConverter(from: source, to: target) else {
                throw Failure.unsupported(from: source, to: target)
            }
            made.primeMethod = .none
            converter = made
        }
        guard let converter else { throw Failure.unsupported(from: source, to: target) }

        // Output frames scale with the sample-rate ratio. Round UP: truncating the
        // fractional frame would silently drop audio on every single chunk.
        let ratio = target.sampleRate / source.sampleRate
        let capacity = AVAudioFrameCount((Double(buffer.frameLength) * ratio).rounded(.up))
        guard capacity > 0,
            let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity)
        else { throw Failure.noOutputBuffer }

        var error: NSError?
        var consumed = false
        let status = converter.convert(to: out, error: &error) { _, inputStatus in
            // Exactly one input buffer per call: `.haveData` once, then `.noDataNow` so
            // the converter emits what it holds instead of waiting for more input that
            // this call will never supply.
            defer { consumed = true }
            inputStatus.pointee = consumed ? .noDataNow : .haveData
            return consumed ? nil : buffer
        }
        guard status != .error else { throw Failure.failed(error) }
        return out
    }
}

// MARK: - Main

@main
struct StreamTranscribe {
    static func main() async {
        var localeID = "en-US"
        var sampleRate = 16000.0
        var chunkBytes = 3200  // 100 ms at 16 kHz mono Int16
        var debugTiming = false
        // DictationTranscriber is the DEFAULT because it is the only one of the two
        // that actually streams. Measured on 5.7s of speech, first partial as a
        // fraction of the utterance: SpeechTranscriber 69%, +fastResults 18%, and
        // DictationTranscriber 12% with a ~210ms cadence between partials.
        // SpeechTranscriber emitted its whole result history in ONE burst either way
        // (0ms between partials), which is batch behavior wearing a stream's clothes.
        var moduleKind = "dictation"
        var fastResults = false

        let t0 = Date()
        func trace(_ what: String) {
            if debugTiming {
                note(String(format: "[%.3f] %@", Date().timeIntervalSince(t0), what))
            }
        }

        var args = Array(CommandLine.arguments.dropFirst())
        while let arg = args.first {
            args.removeFirst()
            switch arg {
            case "--locale":
                guard let v = args.first else { die("--locale needs a value") }
                localeID = v
                args.removeFirst()
            case "--sample-rate":
                guard let v = args.first, let hz = Double(v) else { die("--sample-rate needs Hz") }
                sampleRate = hz
                args.removeFirst()
            case "--chunk-bytes":
                guard let v = args.first, let n = Int(v) else { die("--chunk-bytes needs a size") }
                chunkBytes = max(320, n)
                args.removeFirst()
            case "--debug-timing":
                debugTiming = true
            case "--module":
                guard let v = args.first else { die("--module needs speech|dictation") }
                moduleKind = v
                args.removeFirst()
            case "--fast":
                // SpeechTranscriber.ReportingOption.fastResults / the DictationTranscriber
                // equivalent frequentFinalization. `.volatileResults` alone makes the
                // transcriber CAPABLE of emitting hypotheses; measurements showed it
                // still batches them to the end of input, so this is the knob that
                // actually buys live output.
                fastResults = true
            default:
                die("unexpected argument: \(arg)")
            }
        }

        let requested = Locale(identifier: localeID)
        let installed = await SpeechTranscriber.installedLocales
        let supported = await SpeechTranscriber.supportedLocales

        func match(_ pool: [Locale]) -> Locale? {
            let want = requested.identifier(.bcp47).lowercased()
            if let exact = pool.first(where: { $0.identifier(.bcp47).lowercased() == want }) {
                return exact
            }
            guard let lang = requested.language.languageCode?.identifier.lowercased() else {
                return nil
            }
            return pool.first { $0.language.languageCode?.identifier.lowercased() == lang }
        }

        guard match(supported) != nil else { die("locale \(localeID) is not supported") }
        guard let effective = match(installed) else {
            die("locale \(localeID) has no installed model; run the batch helper with --install")
        }

        // `.volatileResults` makes the module CAPABLE of pre-utterance hypotheses.
        // Measured: with volatileResults alone every result still arrived in one burst
        // after the final audio chunk, so the fast/frequent option is what actually
        // produces live output. Both modules are selectable because they are tuned
        // differently — SpeechTranscriber for general transcription, DictationTranscriber
        // for keyboard-style dictation.
        let module: any SpeechModule
        let resultStream: AsyncStream<(String, Bool)>
        let resultFeed: AsyncStream<(String, Bool)>.Continuation
        (resultStream, resultFeed) = AsyncStream<(String, Bool)>.makeStream()

        if moduleKind == "dictation" {
            var reporting: Set<DictationTranscriber.ReportingOption> = [.volatileResults]
            if fastResults { reporting.insert(.frequentFinalization) }
            let d = DictationTranscriber(
                locale: effective,
                contentHints: [],
                // `.punctuation` lives on DictationTranscriber, NOT SpeechTranscriber,
                // and it is opt-IN: without it the live path returns an unpunctuated
                // run-on ("green then tell me"), which is unusable as chat input.
                // `.etiquetteReplacements` is deliberately NOT set — it masks profanity,
                // which would silently corrupt a user's own words.
                transcriptionOptions: [.punctuation],
                reportingOptions: reporting,
                attributeOptions: []
            )
            module = d
            Task {
                do {
                    for try await r in d.results {
                        resultFeed.yield((String(r.text.characters), r.isFinal))
                    }
                } catch {
                    emitLine(["type": "error", "message": "result stream failed: \(error)"])
                }
                resultFeed.finish()
            }
        } else {
            var reporting: Set<SpeechTranscriber.ReportingOption> = [.volatileResults]
            if fastResults { reporting.insert(.fastResults) }
            let t = SpeechTranscriber(
                locale: effective,
                transcriptionOptions: [],
                reportingOptions: reporting,
                attributeOptions: []
            )
            module = t
            Task {
                do {
                    for try await r in t.results {
                        resultFeed.yield((String(r.text.characters), r.isFinal))
                    }
                } catch {
                    emitLine(["type": "error", "message": "result stream failed: \(error)"])
                }
                resultFeed.finish()
            }
        }

        guard
            let analyzerFormat = await SpeechAnalyzer.bestAvailableAudioFormat(
                compatibleWith: [module])
        else {
            die("no compatible audio format for locale \(effective.identifier(.bcp47))")
        }

        guard
            let inputFormat = AVAudioFormat(
                commonFormat: .pcmFormatInt16,
                sampleRate: sampleRate,
                channels: 1,
                interleaved: true)
        else { die("could not build a \(Int(sampleRate)) Hz mono Int16 input format") }

        let (inputStream, inputContinuation) = AsyncStream<AnalyzerInput>.makeStream()
        let analyzer = SpeechAnalyzer(modules: [module])

        // Emit results as they arrive. Volatile results replace each other, so the
        // consumer keeps only the latest partial and appends each final.
        let resultsTask = Task {
            var finalText = ""
            for await (text, isFinal) in resultStream {
                trace("result isFinal=\(isFinal) chars=\(text.count)")
                if isFinal {
                    finalText += text
                    emitLine(["type": "final", "text": text])
                } else {
                    emitLine(["type": "partial", "text": text])
                }
            }
            return finalText
        }

        do {
            try await analyzer.start(inputSequence: inputStream)
        } catch {
            die("analyzer failed to start: \(error)")
        }

        emitLine(["type": "ready"])

        // Read stdin on a detached thread: FileHandle reads are blocking, and doing
        // them on the cooperative pool would starve the analyzer's own tasks.
        let readerDone = DispatchSemaphore(value: 0)
        Thread.detachNewThread {
            // Built HERE, not in the enclosing scope: this thread is the converter's
            // only user, so owning it locally keeps it off a cross-thread capture and
            // out of the way of Swift's Sendable checking.
            let converter = AnalyzerFormatConverter(target: analyzerFormat)
            let input = FileHandle.standardInput
            var carry: [UInt8] = []  // odd trailing byte from a torn Int16
            while true {
                let chunk = input.availableData
                if chunk.isEmpty { break }  // EOF
                var bytes = carry + [UInt8](chunk)
                carry = []
                if bytes.count % 2 == 1 {
                    carry = [bytes.removeLast()]
                }
                guard !bytes.isEmpty else { continue }
                // Slice into analyzer-sized pieces so a large WebSocket frame does
                // not arrive as one huge buffer and stall partial emission.
                var offset = 0
                while offset < bytes.count {
                    let end = min(offset + chunkBytes, bytes.count)
                    let slice = Array(bytes[offset..<end])
                    offset = end
                    guard let buffer = makeBuffer(slice, format: inputFormat) else { continue }
                    do {
                        let converted = try converter.convert(buffer)
                        // A converter that produced no frames (short chunk swallowed by
                        // resampling) has nothing to analyze; yielding it would only cost
                        // a needless trip through the analyzer.
                        guard converted.frameLength > 0 else { continue }
                        trace("fed \(slice.count)B -> \(converted.frameLength) frame(s)")
                        inputContinuation.yield(AnalyzerInput(buffer: converted))
                    } catch {
                        note("convert failed: \(error)")
                    }
                }
            }
            inputContinuation.finish()
            readerDone.signal()
        }

        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            DispatchQueue.global().async {
                readerDone.wait()
                cont.resume()
            }
        }

        do {
            try await analyzer.finalizeAndFinishThroughEndOfInput()
        } catch {
            note("finalize failed: \(error)")
        }

        let full = await resultsTask.value
        emitLine(["type": "done", "text": full.trimmingCharacters(in: .whitespacesAndNewlines)])
        exit(0)
    }
}
