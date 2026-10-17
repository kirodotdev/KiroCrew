// AppleTranscribe.swift — on-device speech-to-text helper for Kiro Crew's `apple` STT provider.
//
// WHY A SEPARATE BINARY: Apple's SpeechAnalyzer / SpeechTranscriber (macOS 26+) is a
// Swift-only framework. The Kiro Crew gateway is Python, so the only way to reach it is
// a small out-of-process helper. This file is compiled on demand by
// ``kiro_crew.apple_speech`` into the data home's cache dir; a shipped build would
// precompile and sign it instead.
//
// CONTRACT: reads one audio file, writes ONE line of JSON to stdout, exits 0.
// On failure writes {"error": "..."} and exits 1. Never writes anything else to
// stdout — the Python caller parses stdout strictly (diagnostics go to stderr).
//
// Usage: AppleTranscribe --locale <bcp47> [--install] [--inventory] <audio-file>

import AVFoundation
import Foundation
import Speech

// MARK: - JSON output

/// Emit one JSON line and exit. The single exit point for the process so stdout
/// can never carry two objects (the Python side does a strict single-object parse).
func emit(_ payload: [String: Any], exitCode: Int32) -> Never {
    let data = (try? JSONSerialization.data(withJSONObject: payload)) ?? Data("{}".utf8)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
    exit(exitCode)
}

func fail(_ message: String, code: String = "error") -> Never {
    emit(["error": message, "code": code], exitCode: 1)
}

func note(_ message: String) {
    FileHandle.standardError.write(Data("\(message)\n".utf8))
}

// MARK: - Locale resolution

/// Pick the installed locale that best serves *requested*.
///
/// An exact BCP-47 match wins. Otherwise fall back to any installed locale with the
/// same language code — asking for `en-US` on a machine that only installed `en-GB`
/// should transcribe English, not refuse. Returns nil when the language is absent
/// entirely, which is the caller's cue to try an asset install.
func bestInstalledLocale(for requested: Locale, from installed: [Locale]) -> Locale? {
    let want = requested.identifier(.bcp47).lowercased()
    if let exact = installed.first(where: { $0.identifier(.bcp47).lowercased() == want }) {
        return exact
    }
    guard let lang = requested.language.languageCode?.identifier.lowercased() else { return nil }
    return installed.first { $0.language.languageCode?.identifier.lowercased() == lang }
}

// MARK: - Main

@main
struct AppleTranscribe {
    static func main() async {
        var localeID = "en-US"
        var audioPath: String?
        var wantInstall = false
        var inventoryOnly = false

        var args = Array(CommandLine.arguments.dropFirst())
        while let arg = args.first {
            args.removeFirst()
            switch arg {
            case "--locale":
                guard let value = args.first else { fail("--locale needs a value") }
                localeID = value
                args.removeFirst()
            case "--install":
                wantInstall = true
            case "--inventory":
                inventoryOnly = true
            default:
                audioPath = arg
            }
        }

        let requested = Locale(identifier: localeID)
        let supported = await SpeechTranscriber.supportedLocales
        let installed = await SpeechTranscriber.installedLocales

        if inventoryOnly {
            emit([
                "supported": supported.map { $0.identifier(.bcp47) },
                "installed": installed.map { $0.identifier(.bcp47) },
            ], exitCode: 0)
        }

        guard let audioPath, FileManager.default.fileExists(atPath: audioPath) else {
            fail("audio file not found: \(audioPath ?? "<none>")", code: "no_audio")
        }

        let supportedMatch = bestInstalledLocale(for: requested, from: supported)
        guard supportedMatch != nil else {
            fail("locale \(localeID) is not supported by SpeechTranscriber", code: "locale_unsupported")
        }

        // Resolve to something actually on disk; only download when asked to, so the
        // transcribe path never blocks on a surprise multi-hundred-MB fetch.
        var effective = bestInstalledLocale(for: requested, from: installed)
        var installSecs = 0.0

        // `transcription` is the batch (whole-file) preset. Punctuation and
        // capitalization are applied by the model itself — SpeechTranscriber's
        // TranscriptionOption set has no `.punctuation` member (that one belongs to
        // DictationTranscriber), so there is nothing to opt into here.
        let makeTranscriber = { (locale: Locale) in
            SpeechTranscriber(locale: locale, preset: .transcription)
        }

        if effective == nil {
            guard wantInstall else {
                fail(
                    "locale \(localeID) is supported but its model is not installed; "
                        + "run with --install once to fetch it",
                    code: "locale_not_installed"
                )
            }
            let probe = makeTranscriber(requested)
            let started = Date()
            do {
                if let request = try await AssetInventory.assetInstallationRequest(
                    supporting: [probe])
                {
                    note("installing model assets for \(localeID)...")
                    try await request.downloadAndInstall()
                }
                _ = try? await AssetInventory.reserve(locale: requested)
            } catch {
                fail("model asset install failed: \(error)", code: "install_failed")
            }
            installSecs = Date().timeIntervalSince(started)
            effective = bestInstalledLocale(
                for: requested, from: await SpeechTranscriber.installedLocales)
            guard effective != nil else {
                fail("model assets installed but locale still unavailable", code: "install_failed")
            }
        }

        let transcriber = makeTranscriber(effective!)
        let analyzer = SpeechAnalyzer(modules: [transcriber])

        do {
            let file = try AVAudioFile(forReading: URL(fileURLWithPath: audioPath))
            let durationSecs = Double(file.length) / file.fileFormat.sampleRate

            // Drain results concurrently with analysis: the sequence finishes when the
            // analyzer finalizes, so collecting afterwards would deadlock.
            let collector = Task { () -> String in
                var text = ""
                for try await result in transcriber.results {
                    text += String(result.text.characters)
                }
                return text
            }

            let started = Date()
            if let last = try await analyzer.analyzeSequence(from: file) {
                try await analyzer.finalizeAndFinish(through: last)
            } else {
                try await analyzer.finalizeAndFinishThroughEndOfInput()
            }
            let text = try await collector.value
            let elapsed = Date().timeIntervalSince(started)

            emit([
                "text": text.trimmingCharacters(in: .whitespacesAndNewlines),
                "locale": effective!.identifier(.bcp47),
                "requested_locale": localeID,
                "audio_secs": durationSecs,
                "transcribe_secs": elapsed,
                "install_secs": installSecs,
            ], exitCode: 0)
        } catch {
            fail("transcription failed: \(error)", code: "transcribe_failed")
        }
    }
}
