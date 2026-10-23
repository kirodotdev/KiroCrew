---
title: Remove the Piper text-to-speech provider -- unpinned install and model downloads, and a GPL engine, are risks the project will not carry
status: accepted
author: iamwhatever, with kirocrew-worker
created: 2026-10-22
last-audited: 2026-10-22
audited-at: 69b2e503eb
doc-pr: null
implementation-prs: []
tracking-issues: [23154]
supersedes: []
superseded-by: []
---

# RFC: Remove the Piper text-to-speech provider

- Status: accepted on the repository owner's decision of 2026-10-22. Nothing is removed yet. This document lands first because the change removes a user-facing capability, and the First Principles lane reads that decision off the base branch.
- Tracking issue: [#23154](https://github.com/kirodotdev/KiroCrew/issues/23154)
- Related: [voice-streaming](../system-specs/modules/voice-streaming.md) (the shipped TTS contract), [chat/voice.md](../reference/kiro-cli/chat/voice.md) (the user setup guide), [rfc-pluggable-speech-providers](rfc-pluggable-speech-providers.md) (whose provider table lists `piper`)

## Summary

Voice replies have three text-to-speech providers: `system` (the OS engine, the default), `piper` (a local neural engine) and `polly` (Amazon Polly). `piper` is removed. `system` and `polly` stay. A saved config that selected Piper keeps working and speaks through `system`, with a one-time log line saying why. Nothing on the user's disk is deleted.

## Problem

Measured at `69b2e503eb`.

1. **Supply chain.** Nothing about a Piper install is pinned or verified. `docs/reference/kiro-cli/chat/voice.md` and the Slack "not configured" notice in `src/kiro_crew/slack/handler_runtime/voice.py` tell users to run `pip install piper-tts` with no version or hash. The Settings hint for the model path sends them to a third-party model host to download a `.onnx` file by hand, with no checksum. The gateway then runs the installed `piper` binary on that model, and `src/kiro_crew/piper_runtime.py` starts a resident worker (`src/kiro_crew/piper_worker.py`) that imports the `piper` package. The project ships code that executes whatever arrived through that path.
2. **License.** The maintained engine is `OHF-Voice/piper1-gpl` (the repository the `voice_reply.py` module docstring links), whose `COPYING` is GPL-3.0. Each voice model carries its own license on top. Kiro Crew is Apache-2.0. Recommending the engine, documenting its install, and importing it from a worker the project ships is a license question the project should not have to keep answering for every voice and every release.

Neither risk can be fixed from inside Kiro Crew. Pinning the wheel does not cover the models, and checksumming a curated model list makes the project the curator of third-party voice files.

## Decision

1. Remove `PROVIDER_PIPER` from `VALID_PROVIDERS` in `src/kiro_crew/voice_reply.py`, and remove the Piper synthesis, streaming and binary-resolution code, `src/kiro_crew/piper_runtime.py`, `src/kiro_crew/piper_worker.py`, the dashboard's resident Piper runtime in `src/kiro_crew/dashboard/chat_voice.py`, and the Piper fields and notice in `src/kiro_crew/slack/handler.py` and `src/kiro_crew/slack/handler_runtime/voice.py`. Comments in the Telegram and WhatsApp clients that name Piper as the WAV source are reworded. The specs and settings reference that describe this code (`voice-streaming`, `platform-compat`, `src/kiro_crew/docs/configuration.md`, the Windows install guide) change with it.
2. Remove the Piper option, its model, binary and speed fields from Settings → Voice, the strings that only those fields use, and the generated settings registries.
3. Remove the Piper path from the dev-fleet demo-recording skill (`references/narrate.py`, `references/deps.py`, `SKILL.md`, and the `dev-fleet` spec); its `auto` provider picks `polly` or stops.
4. Remove Piper from the user guide (`docs/reference/kiro-cli/chat/voice.md` and its row in `docs/reference/kiro-cli/README.md`). Historical CHANGELOG entries stay as they are.

## Migration

- `voice_reply.provider: "piper"` resolves to `system`. A new once-per-process guard logs one warning naming the removal and the tracking issue; the existing invalid-provider warning fires on every read, which would repeat on every Slack, Telegram and dashboard load.
- A config with no `provider` but a `piper_model` (the pre-`system`-default upgrade path in `resolve_configured_provider`) resolves to `system` the same way, instead of to Piper.
- The `piper_*` keys in `config.json` are left in place and no longer read. They are not rewritten or deleted, so a downgrade still finds them.
- A Settings `PUT` naming `piper` is ignored, as any unknown provider already is.
- Voice model files and Piper installs on disk are not touched. Removing them is the user's choice.

`system` is the fallback rather than `polly` because the existing rule for an invalid provider already falls back to the local default: reaching a paid cloud service is never a decision the operator made by having an old value in their config.

## Rollout

1. This document. Exit: it is on main with `status: accepted`.
2. Decisions 1, 2 and 4 and the migration above, in one PR, with tests. Exit: `git grep -il piper -- src/kiro_crew ':!src/kiro_crew/apps'` finds only the migration in `voice_reply.py` and the settings reference's note about it; a config naming `piper` resolves to `system` with exactly one warning; the Voice panel offers `system` and `polly` only; no locale carries a Piper-only key; and the user guide no longer tells anyone to install Piper.
3. Decision 3. Exit: the demo-recording skill and the `dev-fleet` spec no longer name Piper.

The server and the Settings UI change together so the panel never offers a provider the server has dropped: shipping them apart would leave a Piper choice in Settings that saves nothing and says nothing. The skill is a separate script with its own provider logic, so it moves on its own.

## Alternatives considered

- Pin `piper-tts` and checksum a fixed model list. Rejected: it makes the project the curator of third-party voice files, and it leaves the GPL engine in place.
- Move Piper into an opt-in app. Rejected: the risks are in the install and the engine, not in where the integration lives; an app would still tell users to install it.
- Keep it and document the risks. Rejected: the default is already `system`, so the cost of carrying Piper buys only an optional quality upgrade.

## Risks

- A user who relied on Piper's offline quality gets the OS voice instead. Mitigation: replies still speak, and the log says why. `polly` remains for higher quality where a cloud call is acceptable.
- A headless Linux host with no `espeak-ng` that used Piper now has no local engine. Mitigation: that is the existing `system` behaviour on such a host, which reports TTS as unavailable rather than failing silently.
