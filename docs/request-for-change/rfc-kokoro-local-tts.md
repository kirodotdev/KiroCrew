---
title: Add a local Kokoro text-to-speech provider -- Apache-2.0 weights, a hash-locked install and no GPL code answer the two reasons Piper was removed
status: rejected
author: T1M30UT, with kirocrew
created: 2026-11-25
last-audited: 2026-11-25
audited-at: d336cc62a7
doc-pr: 42843
implementation-prs: []
tracking-issues: [42838]
supersedes: []
superseded-by: []
---

# RFC: Add a local Kokoro text-to-speech provider

- Status: rejected. Nothing built. A maintainer (Bolin) declined it on 2026-11-25 on [#42843](https://github.com/kirodotdev/KiroCrew/pull/42843): a local Kokoro provider is not planned for now. The body is kept for the record. Measured at `d336cc62a7`; the measurements below come from a scratch prototype outside this repository.
- Tracking issue: [#42838](https://github.com/kirodotdev/KiroCrew/issues/42838)
- Related: [rfc-remove-piper-tts](rfc-remove-piper-tts.md) (the decision this document has to answer), [rfc-pluggable-speech-providers](rfc-pluggable-speech-providers.md) (the provider seam), [voice-streaming](../system-specs/modules/voice-streaming.md) (the shipped TTS contract)

## Summary

Add `kokoro`, a local neural text-to-speech provider built on the Kokoro-82M model (Apache-2.0). It is opt-in: the owner presses Install in Settings → Voice, the gateway installs hash-locked wheels into the data home and downloads two sha256-pinned model files, and synthesis runs in a sandboxed child process. The default stays `system`. No GPL code is installed or imported: the model runs on `onnxruntime` (MIT), English words become phonemes through `misaki` (Apache-2.0) without its espeak-ng extra, and words missing from misaki's dictionaries go through a copied `g2p_en` model (Apache-2.0) instead of espeak-ng.

## Motivation

### Current state

- `VALID_PROVIDERS = frozenset({PROVIDER_POLLY, PROVIDER_SYSTEM})` in `src/kiro_crew/speech_providers.py`, with `DEFAULT_PROVIDER = PROVIDER_SYSTEM`. `piper` is in `REMOVED_PROVIDERS`.
- `system` is the OS engine: `say` on macOS, SAPI on Windows, `espeak-ng` on Linux. It works everywhere and sounds dated.
- `polly` sounds much better, especially on its generative engine, but it needs an AWS account and sends every reply to a cloud service.
- There is no local voice that sounds natural. The only one there was, Piper, was removed by [rfc-remove-piper-tts](rfc-remove-piper-tts.md).

### Why Piper was removed, and how this proposal differs

That RFC gave two reasons, and named pinning plus a checksummed model list as a rejected alternative because it "makes the project the curator of third-party voice files, and it leaves the GPL engine in place".

| Piper's problem | This proposal |
|---|---|
| `pip install piper-tts` with no version or hash | Python dependencies install through the existing `sdk_extras` path (`src/kiro_crew/sdk_extras.py`): `pip --require-hashes --no-deps` against a lock shipped in the package, into `<data home>/extras/kokoro/<python tag>/`, a leaf that is READONLY in every agent sandbox (`_CREW_READONLY_LEAVES` in `src/kiro_crew/sandbox_leaves.py`) and fenced from the agent's file tools (`src/kiro_crew/security/paths_lists.py`) |
| Model `.onnx` fetched by hand, no checksum | Two files in a fixed catalog with sha256 pins, following the `stt.models` doctrine (`src/kiro_crew/stt/models.py`): verified while streaming and on every load, written under `<data home>/models`, which is also in `_CREW_READONLY_LEAVES` |
| GPL-3.0 engine | `onnxruntime` (MIT) runs the model. Nothing in the lock is GPL; see the licence table below |
| The project becomes the curator of voice files | Core already curates a sha256-pinned catalog of third-party weights for local speech-to-text (`CATALOG` in `src/kiro_crew/stt/models.py`, whisper.cpp models). This adds one model file and one voices file from one source, with no user-supplied model path |

## Goals

- A natural-sounding English voice that runs on the owner's own machine, with no account and no network after install.
- Nothing installed without the owner pressing Install, and every byte installed verified against a pin shipped in the package.
- No GPL code installed, imported or recommended.
- Works with every existing speech path: dashboard auto-speak, Read aloud, Slack and other channel voice replies, through `voice_reply`'s provider dispatch.
- Names, acronyms and identifiers common in agent replies (pull request numbers, AWS regions, ARNs, resource and request IDs, versions) are spoken intelligibly.

## Non-goals

- Voice cloning, emotion tags, or more than one speaker. Kokoro has fixed voice packs.
- Languages other than English in v1. misaki has a Chinese front end; it is a later, separate change.
- Making `kokoro` the default. `system` stays the default on every edition that does not set one.
- The spoken-summary ("podcast-style") feature. It is provider-independent and gets its own RFC.

## Design

### Provider

`kokoro` joins `VALID_PROVIDERS` with the label "Kokoro (on this computer)". `voice_reply.is_available()` is true only when the `kokoro` extra is installed for this interpreter (`sdk_extras.is_installed`) and both model files verify. Otherwise Settings → Voice shows the same Install card the channel SDKs use, with the download size. New config keys: `voice_reply.kokoro_voice` (default `af_heart`). The existing `rate` maps onto Kokoro's speed (0.5 to 2.0).

### Install

- Lock `src/kiro_crew/sdk_extras_locks/kokoro.txt`: `onnxruntime`, `misaki` **without** the `[en]` extra (that extra pulls `espeakng-loader` and `phonemizer-fork`, both GPL), `spacy` and its closure, `regex`, `addict`, and the `en_core_web_sm` model wheel as a direct URL with its hash. `num2words` is left out (open question 1). About 200 MB installed.
- Model catalog: `kokoro-v1.0.int8.onnx` (92,361,271 bytes, sha256 `6e742170…406cb`), `voices-v1.0.bin` (28,214,398 bytes, sha256 `bca610b8…1fbf7d`), and the `g2p_en` weights `checkpoint20.npz` (about 3.3 MB). Full digests go in the code; these were obtained by downloading and hashing each file.
- misaki calls `spacy.cli.download` when its spaCy model is missing (`misaki/en.py`, `G2P.__init__`), which would pip-install an unpinned package. The worker checks `spacy.util.is_package("en_core_web_sm")` before constructing misaki and refuses to start without it, so that call never runs.

### Worker

A resident child process started through `sandboxed_spawn_argv_async` (`src/kiro_crew/sandbox_spawn.py`), like the other local TTS engines. It reads requests as JSON lines on stdin, so text never reaches argv, and writes WAV (24 kHz mono). It does not use the `kokoro-onnx` wrapper, which imports `espeakng_loader` at import time; it calls `onnxruntime.InferenceSession` directly (about 60 lines), with two intra-op threads by default (see Measurements). It exits after an idle timeout and is restarted on the next request. The gateway never imports `onnxruntime`, spaCy or misaki.

### Text front end

Applied in this order, per request:

1. **ID rules.** Identifiers are rewritten into speakable words before misaki sees them. Ordinary words and names pass through untouched. The rules run most-specific first:

   | Pattern | Example | Spoken as |
   |---|---|---|
   | ARN | `arn:aws:lambda:us-east-1:123456789012:function:order-sync` | "A R N, lambda, U S East one, function order sync". The partition and the account ID are dropped. |
   | Region and Availability Zone | `us-west-2`, `eu-central-1a` | "U S West two", "E U Central one A". A known region prefix (`us`, `eu`, `ap`, `ca`, `sa`, `me`, `af`, `il`, `mx`) is spelled, never read as a word. |
   | Resource ID with a type prefix | `i-0a1b2c3d4e5f67890`, `vpc-1a2b3c4d`, `sg-…`, `subnet-…`, `ami-…`, `vol-…`, `snap-…` | "instance ending in 7 8 9 0", "V P C ending in 3 c 4 d". The prefix becomes the resource type; only the last four characters are read. |
   | UUID, request ID or long hex string | `3f2a9c1e-…-7b9d4e1f0a2c` | "ID ending in 0 a 2 c" |
   | Account ID | a bare 12-digit number | read digit by digit in groups of four |
   | Version | `v1.2.3`, `1.32.0` | "version one point two point three" |
   | Long number | five or more digits, such as `PR 40288` | digit by digit: "P R four zero two eight eight" |
   | Any other hyphenated, underscored or letter-and-digit word | `order-sync-v2`, `my_stack_dev` | Split into parts. A known word, or a compound of two known words, is kept; an unknown part of four letters or fewer is spelled out; a longer one goes to the fallback below. |

   The prototype implemented only the last two rows; the others are proposed and are covered by the Phase 3 fixture.
2. **misaki** with its US dictionaries (about 180,000 checked and 187,000 lower-confidence words), numbers and acronyms.
3. **Pronunciation list.** A small built-in list of Kiro Crew's own terms (Kiro, Crew, KAS), then the owner's own list from Settings (Phase 3). Neither is shipped with employer- or user-specific words.
4. **g2p_en fallback** for any word still without a pronunciation, through a copied runner over its Apache-2.0 weights. The `g2p_en` package itself is not installed: it depends on `distance` (GPL) and downloads NLTK data at runtime, and misaki already does the part-of-speech work NLTK was used for.
5. **Chunking.** Kokoro's author notes it is weak on utterances shorter than about 10 to 20 tokens. Sentences shorter than that are merged with the next one before synthesis, and a chunk is capped at the model's 510-token input.

### Licences of what is installed

| Component | Licence |
|---|---|
| Kokoro-82M weights and voices | Apache-2.0 |
| onnxruntime | MIT |
| misaki (base, no extras) | Apache-2.0 |
| spaCy, en_core_web_sm | MIT |
| g2p_en weights (copied, not installed) | Apache-2.0 |
| num2words | LGPL-2.1; proposed to be replaced in-tree and not installed (open question 1) |

### Measurements (scratch prototype, Apple M4 Pro with 14 cores, CPU only)

A 188-word briefing script, 67 s of audio, synthesized sentence by sentence with `onnxruntime` called directly, no PyTorch and no GPL packages installed:

| Setup | Total synthesis | First sentence ready | CPU time | Peak memory |
|---|---|---|---|---|
| int8 model, all cores (onnxruntime default) | 4.2 to 4.3 s | 0.2 s | 28 s | 624 to 629 MB |
| int8 model, 4 threads | 5.3 s | 0.24 s | 21 s | 622 MB |
| int8 model, 2 threads | 8.1 s | 0.36 s | 17 s | 628 MB |
| fp32 model, all cores | 8.6 s | 0.37 s | 59 s | 870 MB |

Fixed costs per worker start, not per reply: 0.7 to 1.4 s of Python imports and 0.5 s to load the model and misaki.

| Size | Value |
|---|---|
| Model files | 120 MB for int8 (92 MB model plus 28 MB for all 54 voices); the fp32 model alone is 325 MB |
| Installed Python closure | about 200 MB |

Proposed defaults from these numbers: the int8 model, and `intra_op_num_threads = 2`. Two threads stay about 8 times faster than real time while halving the burst of CPU use, so speaking a reply does not slow a build running beside it. Memory does not depend on the thread count; the worker's idle timeout is what releases it.

## Migration plan

1. **This document.** Exit: on main with `status: accepted`.
2. **Provider, install and worker.** The lock, the model catalog entries, the worker, `kokoro` in `VALID_PROVIDERS`, the Settings option, Install card and voice picker, and the `voice-streaming` spec section. Exit: a fresh data home shows Install; after Install, `voice_reply.is_available()` is true and Read aloud speaks; a tampered model file fails verification and reports unavailable; `pip list` of the extra contains no GPL distribution; the gateway process never imports `onnxruntime`.
3. **Text front end.** The ID rules, the built-in pronunciation list, the `g2p_en` fallback and chunk merging. Exit: a fixture with one or more cases for every ID-rule row (ARN, region, Availability Zone, typed resource ID, UUID, account ID, version, long number, hyphenated name) produces the spoken form in the table, and every word in it gets a pronunciation, with no unknown-symbol tokens.
4. **Owner's pronunciation list.** A Settings list of word → "sounds like" pairs, stored in config. Exit: an added word is spoken as entered, and the list is not writable from the agent sandbox.

Phases 2 to 4 are each shippable and abandonable on their own. Phase 3 improves quality but Phase 2 is usable without it.

## Backward compatibility

- No existing config changes. A config without `kokoro` behaves exactly as today.
- A config naming `kokoro` on a build without it falls back to `system` through the existing invalid-provider rule.
- `kokoro` becomes a built-in name, so `validate_speech_registry` refuses an edition that registers it.
- An app update that changes the lock pins finds an install whose marker hash differs and offers Install again (existing `sdk_extras` behaviour).

## Security considerations

- Every installed byte is checked against a pin shipped in the package; the install runs in the gateway, outside the sandbox, only after the owner presses Install.
- The install and model directories are already masked or read-only for the agent, so the verified bytes are the loaded bytes.
- The worker runs under the existing TTS sandbox spawn and needs no network or credentials.
- The owner's pronunciation list is config, edited from Settings, not from the agent.

## Alternatives considered

- **Bring Piper back with pins.** Rejected: the engine is still GPL-3.0.
- **The `kokoro-onnx` wrapper.** Rejected: it imports `espeakng_loader` and depends on `phonemizer-fork` (GPL).
- **espeak-ng as an out-of-process fallback**, as `system` uses it on Linux. Rejected for v1: it reintroduces a GPL program the project would have to tell users to install.
- **OpenPhonemizer** (BSD-3-Clause-Clear) as the fallback. Rejected: its author marks it unmaintained, and it outputs espeak's symbols, not Kokoro's.
- **Leokani G2P** (code MIT, weights CC BY 4.0). Kept as the backup fallback if `g2p_en` proves too inaccurate; its attribution requirement and short history argued against it for v1.
- **Ship it from an edition (companion package).** Not available to third parties: discovery refuses more than one companion entry point as ambiguous (`src/kiro_crew/platform/discovery.py`), and the speech seam's own RFC lists "not a general plugin loader" as a non-goal.
- **Ship it as an app.** Apps cannot register speech providers, and core accepts no new built-in apps.
- **Bundle the dependencies in the desktop app.** Rejected: it adds about 320 MB to every install for an opt-in feature, and a code-signed bundle cannot be patched.
- **Orpheus (3B).** More expressive, but it needs a GPU and about 18 times Kokoro's compute.

## Open questions

1. `num2words` is LGPL-2.1, and LGPL is not on the Dependency License Gate's allowlist (`.github/dependency-review-config.yml`). The repo has two shipped LGPL exceptions, `heic-to` and the bundled FFmpeg audio decoder, and the `heic-to` entry justifies itself by there being no permissive alternative. That is not true here: misaki only calls `num2words` for English cardinals, ordinals, years and decimals. Proposed: do not install it, and have the worker register a small in-tree, Apache-2.0 module under the name `num2words` before importing misaki. The alternative is a written per-package exception.
2. The rest of the closure is 53 distributions. All are MIT, BSD, Apache-2.0, ISC, MPL-2.0 or PSF-licensed, except `regex` (Apache-2.0 AND CNRI-Python), which needs a per-package entry or confirmation that CNRI-Python counts as the allowed `Python-2.0`. Keep spaCy, or replace it with a lighter tokenizer and lose some of misaki's part-of-speech handling?
3. Model source: the `kokoro-onnx` GitHub release files used in the prototype, or the `onnx-community` Hugging Face conversion? Both are conversions of the Apache-2.0 weights. The measurements favour int8 (92 MB) over fp32 (325 MB, about twice as slow, 40% more memory); the open part is whether fp32 sounds noticeably better.
4. Which voices to offer in the picker: all English voices, or a short tested list?
5. Wheel availability for every platform the desktop app ships, which Phase 2 has to list and check. A platform without an `onnxruntime` or spaCy wheel reports Install as unsupported (`sdk_extras.install_supported`).
