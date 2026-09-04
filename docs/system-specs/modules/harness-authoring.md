# Authoring an operator harness descriptor

An **operator descriptor** teaches Kiro Crew to drive a harness Kiro Crew does
not bundle — any agent process that speaks public ACP over stdio. It is pure
configuration: an id, an executable, an argv template, a capability set, a model
source, and an MCP-delivery mode. No code ships with it — an operator descriptor
gets the generic adapter (`GenericAdapter`), whose every step is the standard
rule (PATH/absolute resolution, pure template rendering through the same
attestation seam the bundled harnesses use). A harness whose invocation cannot be
expressed as data belongs in `acp/harness_adapters.py` as a reviewed bundled
adapter, not here.

This page is the reference for what a descriptor field means and how it is
validated; the invariants that keep an added harness from disturbing the Kiro
path are in [harness-parity.md](harness-parity.md), and the provider/registry
model it plugs into is in [providers.md](providers.md).

## Where the descriptors live

Operator descriptors live in **`harnesses.json`**, a dedicated file beside
`config.json` (`~/.kiro/crew/harnesses.json` by default), keyed by harness id:

```jsonc
{
  "agy": {
    "display_name": "AGY",
    "executable": "agy-acp",
    "argv": ["{executable}", "acp", "--workdir", "{workdir}"],
    "model_args": ["--model", "{model}"],
    "capabilities": {},
    "model_source": "acp_advertised",
    "mcp_delivery": "wire_fed"
  }
}
```

A dedicated file rather than a config key, deliberately: a descriptor names a
binary Kiro Crew spawns as itself, and no loader clamp can bound that value —
so the file is **write-protected against the agent's own tools on both paths**
(the file-edit gate and shell redirects alike; reads through the file-read
tool stay open, the file holds no secret). `config.json` could not carry the
key safely because it must remain freely shell-readable, which leaves shell
redirects open there. This is the same relocation the repo applies to every
value that is an input to a security decision (the browse launch config, the
computer-use policy, the on-call rotation record). Only an operator editing
the file directly — outside an agent session — can author a descriptor.

Edit the file, then the descriptor is picked up on the next registry load. The
consumer surfaces land in the following parts of this stack: `GET
/api/harnesses` and **Settings → AI Backend** (part 3) list it alongside the
bundled rows once they ship. A descriptor that fails validation is not
silently dropped: the registry retains it under its `invalid` map with a
field-named reason, which those surfaces render inline (R6.1).

To make the harness the default for unselected sessions, set the
`agent.default_harness` config key to its id (a config key is safe here: it
only selects among already-registered ids, and an unknown or unavailable value
degrades to kiro-cli; honored once session selection lands in part 2); to bind
a single chat, pick it in the chat composer's harness picker
(part 3 — see [providers.md](providers.md) § Chat composer).

## A worked example: AGY, a third-party ACP adapter server

AGY is a fictional third-party agent shipped as its own ACP adapter binary
(`agy-acp`) on `PATH`. It speaks public ACP over stdio, advertises its models
over `session/new`, takes MCP servers on the wire, and has no bespoke Kiro Crew
adapter — the shape an operator descriptor is for. The descriptor above is
complete. Reading it field by field:

- **`agy`** (the key) is the harness id. Lowercase letters, digits, and hyphens
  only, ≤ 32 characters, unique across bundled and operator harnesses. It is the
  stable handle everything references: `agent.default_harness`, the composer
  pick, `GET /api/models?harness=agy`, the session-map binding.
- **`display_name: "AGY"`** is the human-facing name in the picker and Settings.
  Optional — it falls back to the id.
- **`executable: "agy-acp"`** is resolved at spawn. A bare name is found on
  `PATH` (plus Kiro Crew's augmented-PATH helper — mise shims, per-version
  manager bins); an **absolute path** is honored as-is. A **relative path is
  refused** — only an absolute path or a bare PATH name is legal, because a
  relative path resolves against a working directory that is not the operator's
  and would exec bytes nobody attested.
- **`argv: ["{executable}", "acp", "--workdir", "{workdir}"]`** is the ordered
  token list rendered to argv (never through a shell). The **first token must be
  `{executable}`** so the trust-attested executable is the one that execs — a
  literal first token would exec bytes the attestation never checked. Placeholders
  are a closed vocabulary: `{executable}`, `{agent}`, `{model}`, `{workdir}`. An
  unknown placeholder or an unbalanced brace (`--dir={workdir`) is a
  registration-time reason, not a half-working spawn.
- **no `agent_args`** — AGY has no `--agent` convention, so the block is omitted.
  An omitted convention block is dropped entirely rather than defaulted, so AGY
  does not inherit kiro-cli's `--agent` flag.
- **`model_args: ["--model", "{model}"]`** is the optional model convention,
  emitted **only when a model is pinned**. With no model selected the whole block
  is dropped — AGY runs on its own default — rather than execing an empty
  `--model` argument.
- **`capabilities: {}`** — empty, which means **every capability is off** (see
  below).
- **`model_source: "acp_advertised"`** — AGY's models come from what a live
  session advertised over `session/new`.
- **`mcp_delivery: "wire"`** — AGY reads no config file of ours, so MCP servers
  are delivered over `session/new`.

### The placeholder-block rule

`{model}` is only meaningful in `model_args` and `{agent}` only in `agent_args`,
because those are the blocks `render_argv` gates on a value being present. Using
either in the ungated `argv` block — or in each other's block — is **refused at
validation**: outside its gated block the placeholder renders to the empty string
whenever the value is absent, execing a silent empty argument (`--model=` or a
bare `""`). `{executable}` and `{workdir}` carry no gating and are legal in every
block.

So `argv: ["{executable}", "--model", "{model}"]` is rejected — put the model
flag in `model_args`, where it is emitted only when a model is actually pinned.

## Every descriptor field and its validation rule

| Field | Required | Rule |
|---|---|---|
| `id` (map key) | yes | Non-empty; lowercase letters/digits/hyphens; ≤ 32 chars; unique across all harnesses. |
| `display_name` | no | Any string; falls back to the id when empty. |
| `executable` | yes | Non-empty. Absolute path, or a bare PATH-resolvable name. Relative paths are refused; resolution + trust attestation happen at spawn. |
| `argv` | yes | A sequence of string tokens (not a bare string), non-empty, first token exactly `{executable}`. Each token: only closed-vocabulary placeholders, no unbalanced braces, `{model}`/`{agent}` not used here. |
| `agent_args` | no | Sequence of string tokens; `{agent}` legal here (and only here); emitted only when an agent is selected. |
| `model_args` | no | Sequence of string tokens; `{model}` legal here (and only here); emitted only when a model is pinned. |
| `capabilities` | no | An object whose keys are the known capability names, each a strict `true`/`false`. Unknown key or non-bool value is refused. Absent ⇒ all off. |
| `model_source` | no | One of `acp_advertised` (default) or `static`. `static` **requires** a non-empty `models` list. |
| `models` | when `static` | Sequence of non-empty strings; consulted only when `model_source` is `static`. |
| `mcp_delivery` | no | One of the delivery modes (`wire` for a harness that reads no config of ours; default is wire-fed). |
| `adapter` | — | **Operator descriptors cannot set this.** Naming a bundled adapter is reviewed-code-only, so configuration can never select code. |

A validation failure names the field and the fix (e.g. `harness 'agy': argv
template must start with {executable} …`), surfaced both at config load and on the
Settings row — not only in logs (R6.1).

## What the capability flags do, and why undeclared means off

A descriptor's `capabilities` object is opt-in membership. Each flag Kiro Crew
reads off the session's **bound descriptor** at the call site that gates a
behavior; an undeclared flag reads `false`, **fail-closed**. The flags:

| Capability | Config-grantable? | When set | When unset (the fail-closed default) |
|---|---|---|---|
| `internal_sandbox` | **no — code-only** | Kiro Crew skips its own sandbox in favour of the harness's own | **Kiro Crew wraps the spawn** in its OS sandbox (Linux `unshare`, macOS Seatbelt) |
| `session_sharing` | **no — code-only** | the harness may host multiplexed subagent sessions on one process | one process per session |
| `steer` | yes | mid-turn steer messages are delivered | steer is withheld |
| `acp_runtime_pool` | **no — code-only** | the harness takes effort from the workspace `cli.json` overlay and pools runtimes | one process per session, no overlay |
| `kiro_identity_store` | **no — code-only** | the harness is retired by an external `kiro-cli logout` (kiro identity sweep) | no identity sweep watches it |
| `mcp_tool_search` | yes | the Tool Search `cli.json` overlay is written | no overlay |
| `reasoning_effort` | yes | the effort slider/`/effort` control is offered | no effort control |

**Code-only means an operator descriptor cannot claim it at all**: the four
flags marked code-only are honoured by machinery written for specific bundled
harnesses (kiro-cli's internal sandbox, the shared `AcpRuntime`'s kiro dialect,
kiro's session demux, kiro-cli's login store), so a config grant would not
light the feature up — it would point trusted machinery at a process that
never earned it. `internal_sandbox` is the sharpest case: granted to a binary
with no internal sandbox, the agent process runs with **no sandbox at all**.
A descriptor whose `capabilities` object names any of them — even as `false` —
is refused with a per-key reason and excluded from selection, like every other
validation failure. Granting one to a new harness is a code change in
`acp/harness_registry.py`, where the honouring code can be reviewed alongside
the claim.

An empty `capabilities` (AGY's case) therefore gets: Kiro Crew's own sandbox
wraps every spawn, no session sharing, no steer, no cli.json overlays, and no
identity sweep. That is the correct default for a harness whose isolation and
feature support Kiro Crew has not demonstrated: a capability granted by the
**absence** of a check is exactly the silent-capture failure harness-parity
exists to prevent (see [harness-parity.md](harness-parity.md) Group B).

## How availability and serviceability render

Two independent gates decide how a row appears in the picker and in Settings:

- **`available`** describes the machine: it is `true` when the executable
  resolves at listing time. Availability is lazy and non-blocking — installing
  the binary heals the row on the next listing with no gateway restart, and one
  unavailable harness never hides the others. An unavailable row stays visible,
  marked, and unselectable with its reason.
- **`serviceable`** describes this build. Every operator descriptor that speaks
  public ACP is serviceable — the row serves as soon as its binary resolves.
  (The only unserviceable bundled row is Claude Code, held back by the
  public-build posture; an operator descriptor naming `claude-agent-acp` is
  itself legal and serviceable.)

Both gates are shown in the picker and in the Settings inventory; a row is
selectable only when both pass. Invalid descriptors never appear as selectable
rows — they are listed under `invalid` with their field-named reason.

## Where models come from

`model_source` decides how `GET /api/models?harness=agy` answers:

- **`acp_advertised`** (AGY's case) reads what a **live session on that harness**
  advertised in its `session/new` response. Before any session has started the
  catalog is empty (`[]`) — the models appear once the harness has run once. This
  is the right default for a harness that enumerates its own models over ACP.
- **`static`** reads the descriptor's own `models` list and requires it to be
  non-empty. Use it for a harness that cannot enumerate models over ACP; an empty
  `static` list is a validation error, not an empty dropdown.

A model id belongs to one harness's namespace and is never compared across
harnesses; picking a harness drops a model carried over from a different one, so
a session never binds a model the chosen harness never advertised.
