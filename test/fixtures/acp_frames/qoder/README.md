# Qoder frame corpus

Read `../README.md` first for fixture format, redaction, and the limits of replay.

## Historical qodercli 1.1.17 evidence

The files without the `v167-` prefix are live 1.1.17 captures from probe 4. They remain
as historical evidence for the old precedence and replay-gap observations; they are not
the supported-release handshake. `handshake-live.jsonl` and `turn-live.jsonl` are the
original earlier reduced corpus. Their `initialize` answers advertise `qodercli-login`.
Two of the captures below (`unpinned-handshake-live.jsonl` and `v167-handshake-live.jsonl`)
were recorded with an `authenticate` request sent before `session/new`, and
`handshake-live.jsonl`'s note describes one too. Kiro Crew no longer sends `authenticate`;
a valid sign-in works without it. Temporary paths were normalized to `/qoder-probe/<case>`,
session and user-message ids to fixture-local synthetic values, and `toolCallId`s were
kept verbatim. No `~/.qoder` content is included.

| File | Capture argv | What it proves | What it does not prove |
|---|---|---|---|
| `project-allow-ask-live.jsonl` | `qodercli --acp --settings '{"permissions":{"ask":["*"],"defaultMode":"default"}}'` | With a project seeded for allow rules, `bypassPermissions`, and an allow-returning PreToolUse hook, Bash still emits `tool_call` pending, then `session/request_permission`; `cancel` (`reject_once`) ends it `failed`. Ask outranks those lower project sources. | The capture argv omitted `--permission-mode default` and `--strict-mcp-config`, so this alone is not the complete Crew argv. It proves precedence only for this inline-vs-project measurement. |
| `session-mcp-ask-live.jsonl` | `qodercli --acp --settings '{"permissions":{"ask":["*"],"defaultMode":"default"}}' --strict-mcp-config` | A stdio server mounted through `session/new` produces an MCP `tool_call`; `session/request_permission` follows, and reject_once ends it `failed`. | The capture argv omitted `--permission-mode default`. It does not measure an unstartable MCP element or a full agent-spec mirror. |
| `empty-project-ask-live.jsonl` | `qodercli --acp --setting-sources ''` | With no project, Bash still asks and reject_once ends it failed. This is an independent observation of Qoder's own ask, not a Kiro Crew pin. | It is explicitly not the pinned argv and does not measure a permissive project. |
| `unpinned-allow-bypass-control-live.jsonl` | `qodercli --acp` | Negative control with the same seeded project grant/hook shape: Bash goes pending and completes with no permission request. This is the silent-execution shape the unasked-call tripwire must abort. | It proves the unpinned bypass, not that every lower-precedence layer behaves the same way. |
| `unpinned-handshake-live.jsonl` | `qodercli --acp` | Real initialize reports `1.1.17`, and the capture's `authenticate` for `qodercli-login` was answered before `session/new`, which reports `currentModeId: default`. Useful for version/mode read-back tests. | The NOTE/source argv records that the pinned argv was **not** used; this handshake does not prove the pin or resolved permission rules. |

The earlier `handshake-live.jsonl` and `turn-live.jsonl` remain in place for the
original reduced replay snapshot. Their notes describe their own reduction.


Note: the captures in this directory were recorded before the pinned argv carried
`disableAllHooks: true`. Their argv column shows the settings without that key, so they
do not show hook suppression. The hook measurements are in
[harness-onboarding](../../../../docs/system-specs/modules/harness-onboarding.md).

## Supported qodercli 1.1.67 evidence

These are live captures under the complete pinned argv unless the row says otherwise.
Temporary paths are normalized to `/qoder-probe-1167`, session ids to fixture-local
values, and toolCallIds remain verbatim.

| File | What it proves | What it does not prove |
|---|---|---|
| `v167-handshake-live.jsonl` | initialize reports `1.1.67`, the capture's `authenticate` succeeded before `session/new`, which reports `default`, and `reasoning_effort=low` is accepted/reflected as `xhigh -> low`. | It does not prove an effect on model behaviour or a resolved permission read-back. |
| `v167-project-ask-live.jsonl` | With project allow/bypass and an allow-returning PreToolUse hook, Bash still asks and reject_once ends it failed. | It does not prove the documented hook-allow “skip” semantics; the divergence is why the version gate and unasked-call tripwire exist. |
| `v167-load-replay-live.jsonl` | After two turns, close + load replays two user/agent exchanges and the load response reports `default`; continuity was observed in the source probe. | It does not prove process-map eviction, session sharing, or `session/resume`. |
| `v167-load-replay-toolcall-live.jsonl` | After a rejected Bash turn, close + load replays user/thought/`tool_call` (terminal `failed`)/thought/message frames **before** the load response. No `session/request_permission` occurs in this new client session, so it is the real replay shape the live-prompt arming must ignore. | It captures a failed replayed call, not an approved/completed one. It does not prove that every future release emits replay before the response; the tripwire stays disarmed until the first live prompt for either ordering. |
| `v167-catalog-live.jsonl` | The 2026-10-11 pinned session/new catalog: 17 `modelId`/`name`/`description` rows (including accepted `auto` and credit multipliers), with `auto` as `currentModelId`. This is the picker/catalog-refresh ground truth. | The catalog is account- and release-dependent. It does not prove every model's behaviour, that the set is permanent, or that another account sees the same rows. |
