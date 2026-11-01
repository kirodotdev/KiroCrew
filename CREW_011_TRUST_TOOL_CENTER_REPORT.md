# Kiro BYO 0.2.4 – Crew 0.11.0-rc.1 tool-trust bridge and Tool Center

## Scope

Base: latest Crew 0.11 RC1 model picker/settings fixed BYO 0.2.4 archive. The base source was copied; no user data, installed Crew process or files were modified. Product version remains **0.2.4** with a new build fingerprint.

## Observed bug and repair

In `turn_runner.py`, the MCP execution guard could return `not currently granted or verified` *before sending* `session/request_permission`. A Crew chat in Trust mode therefore could not authorize a tool missing from the local session's still-cold/stale MCP catalogue. Cached tools were also filtered out by `verified_only` search without joining first-session live discovery.

The new path performs a bounded join of the **host-projected session** MCP startup for verified-only search. An actual attempted tool call with missing/stale binding performs a targeted fresh `tools/list` against an existing Crew-owned stdio server and will then ask Crew through the existing ACP permission protocol. If the host authorizes (including its Trust flow), execution proceeds; if it rejects, the tool is absent, no service was projected, the Agent excludes it, or the broker fails verification, execution is refused. We do **not** simulate Trust, force-approve a write, or add another authorization database. Post-approval tool identity and live binding rechecks remain in place.

### Diagnostic reasons

- `crew_binding_absent`: the current projected live broker did not advertise the tool
- `crew_binding_unverified`: no live verified broker binding for this session
- `agent_scope_denied`: Saved Agent projection explicitly excludes the tool
- `grant_changed`: identity/visibility changed while awaiting Crew authorization (existing protection)

## Tool Center (read-only)

Adds **工具** to the BYO app menu. Displays local native tool names, most recent session-scoped Crew MCP tool names and connection-state snapshots, plus recent MCP dispatch refusals. Source: redacted metadata event ledger. The app and ACP runtime are separate processes: these are **historical observations**, never current grant assertions. API `/api/tool-center` requires the existing management token. Only tool names/status and refusal codes are rendered—no arguments, broker environment, session keys or secrets. With runtime trace disabled or no recent events, only native tools and an explicit 'no session data' state appear. There is deliberately no authorization/override button.

All five supported UI locales include the new labels. Existing bottom status bar, Tiga/Dyna, model menu and Crew Settings Auto behavior are unchanged.

## Validation

- Full Python suite: **944 passed, 1 skipped, 52 subtests passed** (assertions completed; single invocation did not exit cleanly under outer time limit). The same suite split into three disjoint test-file groups **cleanly exited 0**: 328 passed/1 skipped/16 subtests; 303 passed/27 subtests; 313 passed/9 subtests.
- Final suite included the verified-only search race test and five-locale UI text. A separate 75-case targeted sweep of Crew permission, MCP, tools and translations also passed.
- Python source compile, Node.js UI syntax, release metadata and inner/outer ZIP integrity checks are included in final packaging validation.
- CI jobs configured for Windows/macOS/Linux, **not run remotely**. No installed Windows Crew `learn_add` test executed. Real application-projected MCP array and Crew dialog Trust behavior must be validated on the user's installation; if Crew never projected `kirocrew-core`, this code will correctly refuse until that upstream issue is resolved.

## Practical test after installing in a backed-up isolated Crew environment

1. Open a *new* Crew BYO session; choose Trust from the chat permission menu.
2. Ask the Agent to find `learn_add` with Tool Search; inspect Tool Center for `mcp__kirocrew-core__learn_add` in the latest session record.
3. After explicitly requesting a non-sensitive test learning write, confirm Crew's native authorization flow and verify that the memory is actually saved. Do not enable YOLO to paper over missing grants.
4. If it is still refused, note the Tool Center reason; distinguish `crew_binding_absent`, `crew_binding_unverified` and `agent_scope_denied` before making more code changes. Trust does not override these conditions.

**Status: isolated code-level candidate, not guaranteed Windows live-tool parity or production-ready.**
