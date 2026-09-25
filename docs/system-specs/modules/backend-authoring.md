# Authoring a config-defined backend

A **backend descriptor** teaches Kiro Crew to drive an agent process it does not
bundle — any executable that speaks public ACP over stdio and drives an LLM with
its own authentication. It is pure configuration: an id, an executable, an argv
template, a model source, a routing declaration, and an
MCP-delivery mode. No code ships with it, and no capability claim either: a
descriptor cannot admit its harness to any session-path capability set (see
*Capabilities are not configurable* below). A descriptor is served by one generic
runtime class, `DescriptorHarness`, which answers every harness seam from the
descriptor data, so an ACP host an operator already runs is served with no
per-host code branch.

The line this feature draws is deliberate and load-bearing: **a descriptor names
no code.** The schema has no `adapter` key, and the bundled harnesses
(`KiroHarness`, `KasHarness`, `CodexHarness`, and the rest) are hand-written
classes that never pass through the descriptor parser — a config key that
selected a Python entry point would let configuration choose code. A harness
whose invocation genuinely cannot be expressed as an argv template belongs
upstream as a reviewed bundled class, not here.

This page is the operator reference: what `harnesses.json` is, where it lives,
why an agent cannot write it, what every field means and how it is validated,
what routing decides, and how an edit reaches a served backend. The invariants
that keep an added harness from disturbing the Kiro path are in
[harness-parity.md](harness-parity.md); the onboarding sequence a *bundled*
harness walks is [harness-onboarding.md](harness-onboarding.md); the
provider/registry model both plug into is in [providers.md](providers.md).

## Where the descriptors live, and why the agent cannot write them

Operator descriptors live in **`harnesses.json`**, a dedicated file directly
under the crew home beside `config.json` (resolved through
`descriptor.operator_harnesses_path`, which reads the same `config_dir` the
config file does, so it honours `KIROCREW_HOME` and the test data home
identically). The file is a JSON object keyed by harness id:

```jsonc
{
  "acme": {
    "display_name": "Acme Agent",
    "executable": "acme-acp",
    "argv": ["{executable}", "acp", "--workdir", "{workdir}"],
    "agent_args": ["--agent", "{agent}"],
    "model_source": "acp_advertised",
    "routing": "agent_spec"
  }
}
```

A dedicated file rather than a `config.json` key, deliberately. Each descriptor
names an `executable` and an `argv` template that the gateway **resolves and
spawns** to serve a backend, so the file IS an execution grant, not an input to a
decision about one. That puts it in the strongest class of the write-protection
set (`security._WRITE_PROTECTED_HOME_PATHS`, the same footing as `app-sources`):
an agent that could write the file could plant an attacker-chosen
`executable`/`argv` and have Kiro Crew's own trusted spawner run it — arbitrary
code laundered through the product's backend launcher, re-armed on every
listing. Nothing downstream neutralizes it, because unlike a `config.json` value
the loader clamps, a descriptor's command is simply spawned; the registry's
validation pass costs a malformed row its listing but does nothing to a
well-formed forgery pointing at an attacker binary.

The protection is two-sided and reads-open:

- **The file-edit tool gate** refuses the agent's own write to the path
  (`security._WRITE_PROTECTED_HOME_PATHS`). Kiro Crew's own writer opens the path
  directly through `atomic_write` and does not route through the gate, so an
  operator editing the file — outside an agent session — keeps working.
- **The OS sandbox** seals the leaf read-only for a sandboxed child
  (`sandbox._CREW_READONLY_LEAVES`), with a Linux absent-file pre-create
  (`sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES`) so the mount seal has a target
  even on an install that has never authored a descriptor — the absent-and-
  therefore-writable default this list exists to close. This is the shell-side
  half: a command-text matcher matches no paths here (as with `config.json`), so
  a kernel write denial is what holds regardless of how a command spells its way
  there, including runtime construction like `$(printf ...)` that the deny-list's
  text tiers cannot see.

The file is **write-protected but deliberately not sensitive**: it holds no
secret and is READ on every listing (the registry loads it to enumerate
backends, and Settings reads it to render the inventory), so classifying it
sensitive would break the feature. The file-read tool stays open.

## The full field reference

Every descriptor is parsed by `descriptor.descriptor_from_mapping`, which never
raises: a malformed entry returns a list of diagnosable reasons and costs that
one harness its row, never the gateway's boot. Unknown top-level keys are a
validation failure (`descriptor.DESCRIPTOR_KEYS` is closed) — a typo'd key would
otherwise be silently ignored, leaving the operator with a harness that quietly
does not do what they configured.

| Field | Required | Rule |
|---|---|---|
| `id` (map key) | yes | Non-empty; lowercase letters, digits, and hyphens only; at most `descriptor.HARNESS_ID_MAX_LEN` (32) characters; unique across all harnesses. A descriptor may also carry `id` as a field, and the two must agree. |
| `display_name` | no | Any string; falls back to the id when empty (`HarnessDescriptor.label`). |
| `executable` | yes | Non-empty. An absolute path, or a bare PATH-resolvable name. Resolution and trust attestation happen at spawn. |
| `argv` | yes | An array of string tokens (not a bare string), non-empty, whose **first token is exactly `{executable}`**. Each token may use only closed-vocabulary placeholders and carry no unbalanced brace; `{model}`/`{agent}` are refused here (see the placeholder rule). |
| `agent_args` | with `agent_spec` routing | Array of string tokens; `{agent}` is legal here and only here; emitted only when an agent is selected. Required to carry `{agent}` when `routing` is `agent_spec`; optional otherwise. |
| `model_args` | no | Array of string tokens; `{model}` is legal here and only here; emitted only when a model is pinned. |
| `model_source` | no | One of `acp_advertised` (default) or `static`. `static` **requires** a non-empty `models` list. |
| `models` | when `static` | Array of non-empty strings; consulted only when `model_source` is `static`. |
| `mcp_delivery` | no | One of `agent_file` (default) or `session_array`. |
| `routing` | no | One of `agent_spec` or `session_config`, or absent. Absent or unrecognized registers the harness known-but-**unselectable**. See routing below. |
| `permission_config` | when `session_config` | An object `{option, value}` (both non-empty strings), required when `routing` is `session_config` and forbidden otherwise. |
| `adapter` | — | **Not a key.** A descriptor never names code; the bundled harnesses are classes that never pass through this parser. |

A validation failure names the field and the fix (for example `entry #2:
argv[0] must be {executable} so the executable that is trust-attested is the
one that runs`) — by field and **file position**, never by quoting the operator's
value: the reasons are rendered on a listing every authenticated user can read,
and a value in any field may be a credential pasted in the wrong place. That
includes the entry's own key: an invalid entry is listed under a synthetic
position identifier (`#2`, which no real id can spell) rather than the key it was
filed under, because the key of a malformed entry is the field most likely to be
the mistake. A repeated key anywhere in the file is refused as "a key is
repeated", without saying which. The reasons stay retrievable for the
Settings surface through `operator_registry.invalid_operator_harnesses`, so a
malformed entry is shown inline with what is wrong rather than dropped silently.

### The argv template and the `{executable}` rule

`argv` is rendered to a concrete argv list by `descriptor.render_argv` through
token-wise substitution — never through a shell. The placeholder vocabulary is
closed (`descriptor.ARGV_PLACEHOLDERS`): `{executable}`, `{agent}`, `{model}`,
`{workdir}`. An unknown placeholder or an unbalanced brace (`--dir={workdir`) is
a registration-time reason, not a half-working spawn, because matching the whole
brace run is what makes an unknown token detectable instead of surviving to exec
as a literal. Every such reason names the offending token by **position**
(`argv[2] uses an unknown placeholder`), never by content: the reasons are shown
on the backends listing, which every authenticated user can read, and an argv
literal is operator text that may carry a credential — including one typed inside
braces by mistake.

The **first token must be `{executable}`.** `argv[0]` IS the program, and
`executable` is the field that is resolved and trust-attested at spawn; a
template whose first token is a literal would exec bytes nobody checked, because
a bare name is re-resolved by exec through PATH at spawn time and the file that
was attested and the file that runs need not be the same one. Requiring the
placeholder is what makes the attestation load-bearing rather than decorative.

Substitution is single-pass: a model id or agent name that happens to contain
`{workdir}` reaches exec as those literal bytes, not as the working directory.
And because rendering builds a `list[str]` for `subprocess` with no shell, every
value lands as exactly one argv element regardless of the spaces, quotes, or
metacharacters it contains.

### The placeholder-block rule

`{model}` is meaningful only in `model_args` and `{agent}` only in `agent_args`,
because those are the blocks `render_argv` gates on a value being present: the
`agent_args` block is emitted only when an agent is selected, the `model_args`
block only when a model is pinned. In the ungated `argv` block — or in each
other's block — those placeholders render to the empty string whenever the value
is absent, execing a silent empty argument (`--model=` or a bare `""`). Rejecting
them at validation turns that footgun into a registration-time reason.

So `argv: ["{executable}", "--model", "{model}"]` is refused — put the model flag
in `model_args`, where it is emitted only when a model is actually pinned.
`{executable}` and `{workdir}` carry no gating and stay legal in every block.

### Capabilities are not configurable

A descriptor has **no `capabilities` key**, and a file that carries one is
refused with an unknown-field reason. Every session-path capability in Kiro Crew
is a frozen, code-reviewed membership set in `agent_sdk.backends`
(`ACP_BACKENDS_SESSION_MCP_ARRAY`, `ACP_BACKENDS_HARNESS_OWNED_SESSIONS`,
`ACP_BACKENDS_LOAD_WITHOUT_MODES`, `ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION`,
`ACP_BACKENDS_ADVERTISED_MODEL_SELECTION`, and the kiro-only sets such as
`ACP_BACKENDS_INTERNAL_SANDBOX` and `ACP_BACKENDS_SESSION_SHARING`), and a
registered descriptor id is a member of none of them. Every gate that reads one
of those sets therefore takes its default branch for a config-authored host:

| Frozen set | What a descriptor-backed harness gets |
|---|---|
| `SESSION_MCP_ARRAY` | The `session/new` `mcpServers` array is not the channel the client assumes the harness reads its servers from. (`mcp_delivery: session_array` still shapes what `DescriptorHarness.session_mcp_servers` does with the array -- that is a harness-seam decision, not a session-path gate.) |
| `HARNESS_OWNED_SESSIONS` | A resume is pre-checked against the Crew-side transcript before `session/load`, the default posture. |
| `LOAD_WITHOUT_MODES` | A reopened session is judged loaded by the presence of a `modes` block, the default posture. |
| `MODEL_VIA_CONFIG_OPTION` | A model change does not travel over `session/set_config_option`; pin the model through `model_args` instead. |
| `ADVERTISED_MODEL_SELECTION` | The pinned model is not resolved from the list advertised at `session/new`. |

This is harness-parity H6/H7 applied to configuration: a capability is granted
by opt-in membership that a reviewer can read in the vocabulary home, never by a
claim in a data file. A harness that needs one of these branches is admitted by
adding its id to the named set -- a core change with a test, not a line in
`harnesses.json`.

A registered descriptor id is in none of the `ACP_BACKENDS_*` sets on the
runtime side either, so `DescriptorHarness` inherits the fail-safe answers from
its `MembershipHarness` base: `internal_sandbox`, `pod_home_remap` and
`reads_markdown_agent_specs` all answer False, and the reclaim policy passes the
operator's thresholds through — exactly the posture a host that has demonstrated
nothing should get.

### Where the models come from

`model_source` decides how the model catalog for the harness answers (read back
through `GET /api/models` under the harness's own namespace):

- **`acp_advertised`** (the default) reads what a live session on that harness
  advertised in its `session/new` response. Before any session has started the
  catalog is empty — the models appear once the harness has run once. This is the
  right default for a harness that enumerates its own models over ACP.
- **`static`** reads the descriptor's own `models` list and requires it to be
  non-empty. `static` is the declaration "I cannot enumerate my models over ACP,
  here they are instead", so an empty list is refused rather than accepted-and-
  listed-empty: it would leave the composer with no model to offer and no way to
  obtain one.

### MCP delivery

`mcp_delivery` chooses the SHAPE of `DescriptorHarness.session_mcp_servers`, and
it is only ever a transform — a descriptor can never make this seam mount a
server the caller did not request:

- **`agent_file`** (the default) is passthrough. The harness reads its MCP
  servers from its own agent-spec/config channel, so the `session/new` array is
  an override of same-named entries and returning the caller's list unchanged
  keeps the request byte-identical — the kiro-family posture, and the posture
  that touches nothing.
- **`session_array`** is narrowing. The harness learns its servers only from the
  `session/new` array, so an element whose transport it never advertised can fail
  the whole `session/new`; the array is narrowed against the `mcpCapabilities`
  this session's handshake reported, reusing the codex path's
  `drop_unadvertised_transports`. An unknown handshake (absent or non-dict
  `mcpCapabilities`) passes the array through untouched — empty means "nothing is
  known", never "nothing is supported", so narrowing to nothing there would strip
  every tool from every session.

## Routing: the selectability gate

Routing is the field that decides whether an operator can *choose* a valid
descriptor, and it is where this feature is honest about a limit. A descriptor is
registered as **known** whenever it validates — spellable in a config value,
nameable in a governance rule — but it is **selectable** (offered as a session
backend) only when it declares how a session's permission decision reaches the
harness. The two verified forms are the only two the generic OS-boundary mask
path can honour:

- **`agent_spec`** — the permission decision travels as an agent-spec selection.
  In practice: your binary reads agent specs (a `--agent`-style selection on the
  command line, carried by an `agent_args` block) and asks by construction. This
  routing **requires** an `agent_args` block that carries `{agent}`: the routing
  says the selection travels on the command line, and that block is the only
  thing that can deliver it, so an `agent_spec` descriptor with an empty
  `agent_args`, or one of fixed flags without the placeholder, is a reason at
  load (the mirror of `session_config`'s `permission_config` rule below). A
  registered `agent_spec` descriptor therefore always made a selection at spawn
  for the later activation check to confirm
  (`DescriptorHarness.verifies_agent_activation`).
- **`session_config`** — the permission decision travels over
  `session/set_config_option`. In practice: your binary advertises an ACP config
  option, Kiro Crew sets it and verifies it in force. This routing **requires**
  `permission_config: {option, value}` naming the option and the value to set;
  the coupling is enforced both ways (a `session_config` routing with no
  `permission_config` is a reason, and a `permission_config` on any other routing
  is a reason). For such a descriptor the credential mask is resolved at spawn on
  the same routing key the codex harness uses, and a tier that would drop it
  aborts the spawn — a `session_config` host's privileged tools ask over the
  config option rather than by construction, so ACP cannot make it ask about a
  passive read, and the OS-boundary mask is the compensating control.
- **neither** (absent or an unrecognized string) — your backend is listed but
  **not selectable**, because nothing establishes that its tool calls reach the
  host permission gate. It stays visible in Settings with that reason, retrievable
  through `operator_registry.unselectable_operator_harnesses`, and it is never
  silently servable. This is a valid state, not a malformed one: an empty routing
  is not a validation error.

The registration order (`operator_registry.load_and_register_operator_descriptors`)
makes this concrete for each valid descriptor: `register_known_backend` first (it
records the routing the selection step then reads), then
`register_operator_harness` (so the id resolves through `harness_for` as a
`DescriptorHarness`), then `register_selectable_backend` **only** when the
routing is recognized **and** the gateway holds a matching routing attestation
(next section). An unroutable descriptor is left known-but-unselectable with its
reason; a routed-but-unverified one is left known-but-unselectable with the
reason that it has not been verified yet.

## Routing verification: a declaration is not evidence

A `routing` value is what the descriptor *says*. A host can load the declared
agent or accept the declared config option and still execute tool calls without
ever sending `session/request_permission` — and every tool call it made would
bypass PreToolUse, the governance deny rules and the SEL audit. So a declared
routing never makes a descriptor selectable on its own. Selectability requires an
**attestation this gateway recorded after verifying the routing end to end**
(`acp/harness/routing_verification.py`).

Verification is an operator action: **Settings → AI backends → Verify routing**
on the row (`POST /api/backends/{id}/verify`, owner-gated and audited). The
gateway then:

1. spawns the harness once, through **the descriptor's own provider constructed
   directly** (`AcpProvider(acp_backend=<id>)` — never the per-chat factory, whose
   selection gate refuses an unselectable pick outright) with the
   same argv, agent selection or config-option write, sandbox mask and runtime
   start path a chat would use — in an empty scratch working directory;
2. sends one probe turn asking the agent for **two writes to one file** with a
   fixed name in that directory: first through its file-editing tool, then
   through its shell tool (`echo probe >> KIROCREW_ROUTING_PROBE.txt`) — one
   write per tool class, because a host may gate one class and not the other;
3. **denies every permission request** the harness raises, and records which
   class each one was for;
4. when the turn ends, checks whether the file exists.

| Verdict | Meaning | Effect |
|---|---|---|
| `verified` | A permission request arrived for **each** tool class — before the file edit and before the shell command — and nothing was written: the host asked before acting on both planes and honoured every refusal. | An attestation is recorded and the backend becomes selectable immediately, no restart. |
| `violation` | The probe file exists although every request was denied: a tool call executed without going through the permission gate. | Nothing recorded; the row shows the reason. Fix the harness, then verify again. |
| `inconclusive` | No permission request and nothing written (the agent refused the task, answered in prose, errored, or the turn timed out) — or only **one** class asked and the other write was never attempted: a gate on one tool class proves nothing about the other. | Nothing recorded; run it again, or check that the harness has a model that can act and exposes both a file-editing tool and a shell tool. |

The attestation is bound to three things. First, the descriptor's **spawn
fingerprint** — a hash over `executable`, `argv`, `agent_args`, `model_args`,
`routing`, `permission_config` and `mcp_delivery`; editing any of those revokes it
by construction. Renaming (`display_name`) or changing a static `models` list does
not. Second, the **content digest of the executable that was verified**: a path
is what the descriptor says, a digest is what actually ran, and a binary replaced
under an unchanged path would otherwise inherit the attestation. Third, the
**agent the probe ran under**. An `agent_spec` host's permission decision *is* its
agent selection, and any descriptor may hand the agent name to its binary, so a
verdict reached under one agent says nothing about another: a descriptor spawn
for an agent the record does not name is refused (that spawn only — the backend
stays verified for the agents it was probed with). The Settings button probes
under the configured default agent; `POST /api/backends/<id>/verify` with a body
of `{"agent": "<name>"}` probes under that agent and, while the descriptor and the
binary are unchanged, **adds** it to the record (up to sixteen agents; a further
distinct agent is refused at verification with the remedy in the reason), so a
backend meant to serve several agents is verified once per agent. Before any of that, the executable's
**provenance** has to pass, and the bar is the **strict** form of the rule every
provider CLI the gateway runs is judged by (`validate_provider_executable` with
`require_protected`): a **protected install** — canonical and symlink-free,
root-owned, writable by the gateway user through none of its parents (on
Windows, the same questions of the ACL). A harness under `/usr/local/bin` or
`/opt` passes; one under the gateway user's home (`~/.local/bin`, an `npm -g`
prefix under `~/.nvm`) is refused, with the reason naming the bar. The agent runs
**as** the gateway user, so an executable that user can write is one the agent
can replace — with a harness built to recognise the fixed probe, answer it
correctly on purpose, and misbehave once verified — which is why the relaxed
rule (merely outside the agent-writable trees) is not enough here. A `#!`
launcher's interpreter is held to the same bar, and must be named by absolute
path: a launcher whose `#!` line resolves its interpreter through PATH
(`#!/usr/bin/env node`) or by a relative name is refused, because what it runs is
whatever the child's PATH or working directory supplies, which no file's bytes
can bind. Provenance is judged at verification (a failure records nothing), at
every spawn, and at boot. The digest is re-checked at boot, and the spawn path
streams the operator's file (bounded chunks — a harness binary is never held
whole) to judge it against the attestation immediately before the exec, then
**execs that path in place** (`DescriptorHarness.resolve_spawn` →
`routing_verification.pin_verified_executable`); protected provenance is what
makes that sufficient — nothing running as the gateway user can rewrite the file
between the judgement and the exec, so the judged bytes are the bytes that run,
and a launcher keeps the siblings it locates relative to itself. A mismatch refuses that
spawn, withdraws selectability on the spot, and the row is back to *Routing not
verified* with the reason, until the operator verifies the replacement — and
because the withdrawal is live, the configured default is put through the
selection gate again for every session, so a default that was withdrawn since the
gateway started degrades to Kiro rather than being retried chat after chat. A
**per-chat pin** of the withdrawn backend is treated differently: the gate
**refuses** it (`members.select_provider_backend` raises
`BackendPinNotSelectable`) and the turn ends with an error card and nothing sent.
Degrading the pin to Kiro, or letting it fall to the member route or the
configured default, would route the prompt to a provider the chat did not pick
while the chat still shows its pin as honoured; withdrawal is an ordinary event
(a routine binary upgrade triggers it), so the card is the per-chat correction
path and names both ways out — pick another backend for the chat, or re-verify
the backend under Settings → AI backends. A sub-agent spawn that names the
withdrawn backend is refused at admission by the same selectable set.

The pin's durable home is **not** the chat transcript. The transcript's metadata
line still carries `acp_backend` so a transcript says what backend served it, but
that file is editable by the agent's own tools, and a pin read back from it would
let a prompt-injected agent hand the chat's next prompt to a provider the user
never picked — the same reason `jev_route` is never restored from a transcript.
Pins live in `chat-backend-pins.json` under the crew home
(`dashboard.backend_pins`: slot key → `{"backend", "owner"}`, `""` a Kiro pin, an
absent key "inherit"; `owner` is the holding slot's `created_at`), written only by
the gateway's slot routes and slot lifecycle (create, switch, fork, history
delete) and sealed in the same class as the attestation store. A history delete
drops the pin only for the slot it popped, and only while the store still names
that slot as the owner — compared and deleted in one locked read-modify-write —
so a replacement chat created under the same key while the delete was pending
keeps its pin. Both this store's and the attestation store's writers read
strictly: an ABSENT document is the one legitimate empty starting point, while an
UNREADABLE one (truncated, not an object, aliased) aborts the read-modify-write
with an error instead of rewriting the document from `{}` and dropping every other
entry. The READERS refuse an unreadable store too, for the mirror-image reason:
"no pins" read from an unreadable document would restore every explicitly pinned
chat as INHERIT and run its next prompt on the global backend, the retarget this
store exists to prevent. So a restore takes `load_backend_pins_snapshot` (which
answers the `PINS_UNREADABLE` marker instead of raising, because the chats still
exist and their transcripts are readable) and stamps each slot through
`apply_restored_pin`: with a readable store the slot gets its pin **only when the
record's `owner` is that slot's `created_at`** — a record another creation owns
(a stale pin left under a reused key by a chat whose delete-time cleanup failed)
is ignored and logged, and the chat inherits, since it never selected a backend;
with the marker its pin is **unresolved** — `acp_backend` stays null so nothing
claims a backend the store did not say, the slot carries
`backend_pin_unresolved: true` (serialized, so a client never shows "inherit" for
a chat whose pin is merely unknown), the speculative pre-spawn stands down, and
the chat's next send re-reads the store off the loop: readable → the pin resolves
and the turn runs on it; still not → the send is refused with a card
(`backend_pin_unresolved`, nothing sent, no session failure) naming the way out
(repair the store, or pick a backend for the chat, which records a fresh pin and
clears the state). A fork of such a chat inherits the unresolved state. An ABSENT
store is the ordinary "no pins" and leaves nothing unresolved. The store is sealed
in the same class as the attestation store — write-protected against the file-edit
tool, read-only and **strict no-follow** in the sandbox, pre-created so the seal
has a target, and refused by its readers when the name is a link or carries a
second hardlink (unresolved, as above; never the linked document's pins and never
"no pins"). All three slot-restore paths — the persistence rehydrate, the
persistence restore and the channel-slot surface — read that store and nothing
else for this field.

**Persist before publish, at both writers.** The create route and the backend
route write the store BEFORE they assign the slot's `acp_backend`, so no reader
— a concurrent slots GET, a send that slips into the window — ever sees a backend
the store does not hold. Creation: store write, then (if a send slipped in and
started a session on the still-unpinned newborn) that session is discarded, then
the field. A failed write retracts the newborn (or discards the slipped-in
session) and answers a coded 500 (`backend_persist_failed`); a 200 for an unpinned
chat is never sent. Switch: store write first (a failure changes nothing), then
the session reset while the field still says the prior backend (the reset's
refusals — a turn in flight, a rebind during the await — restore the prior pin in
the store), then the field is assigned with no await between the reset's return
and the assignment, then the transcript line; a failed transcript write rolls the
field back, restores the prior pin and discards a session started in the window. While
a probe runs, only the probe's **own** spawn — the one whose working directory is
the probe's private scratch, running under the probe's agent — is admitted without a record (held to the bytes the
probe resolved); any other spawn of the same id during the probe is an ordinary
spawn, checked against the attestation and the agent in full. The store
is a gateway-written file beside `harnesses.json`,
`backend-routing-attestations.json`, fenced exactly as the descriptor file is
(agents read it, never write it; the sandbox seals it read-only) because it is a
selectability grant one step downstream of the execution grant. Both files are
**strict no-follow** leaves like `cloud.json`: a seal covers the file a link
resolves to while the link's name stays replaceable, so each consumer — the
descriptor loader, the attestation store — refuses a name that is a link or
carries a second hardlink and serves nothing (no descriptors; no attestations)
rather than read a grant that arrived through an alias. Every
read-modify-write of that store — a verify recording one backend, a spawn
revoking another — runs under one lock, so concurrent updates cannot overwrite
each other's snapshot and silently drop an attestation or, worse, a revocation.

Only a permission request **for the probe write** is evidence: an edit whose
target is the probe file, or a shell command that, parsed, performs a write to it (an output redirection, or `tee`/`cp`/`mv`/`touch`/`dd of=` with it as the written operand — a command that merely mentions the name, like `cat` or `ls`, is not). A host may ask about
other things (reading its own config, an unrelated command) and still perform the
requested write without asking; those requests are denied like every other but
counted separately, and a run with only unrelated requests and no file is
`inconclusive`, with the count in the reason.

The probe drives **the descriptor's own provider**, constructed directly for
its id with the configured default agent and sandbox — never the per-chat
selection gate, which refuses an unselectable pick and so could never spawn the
backend under probe. The provider's backend
identity is asserted before any verdict counts; a mismatch is `inconclusive`
and records nothing.

The bytes are pinned **around** the run, not after it: the probe resolves the
executable and digests it before anything spawns, its own spawn is held to that
digest (a file swapped between resolution and exec is refused, not probed), and
the digest is taken again when the turn ends. A file that changed while the probe
ran yields `inconclusive` and records nothing — a verdict only ever describes the
bytes that were in place from resolution to the end of the turn. The digesting
and the store rewrite are file I/O and run off the gateway's event loop.

Verified routing is necessary for selectability, not sufficient. The deployment's
`agent_backend` policy (the governance ceiling an administrator writes into
`security_policy.json`) narrows the selectable set at boot, and a backend
verified *after* boot is put through that same narrowing the moment it is
registered: a denied backend comes back **verified but not selectable**, the row
says so (`routing verified, but this deployment's agent_backend policy does not
permit the backend`), and no Verify action is offered again, because
verification is not what it lacks.

The probe costs one real turn on the harness's model and can take a couple of
minutes. It is the one place a descriptor backend runs before it is selectable,
and it runs only when the owner asks.

## Lifecycle: edit, restart, verify, listed

`harnesses.json` is read **once, at gateway start** — by
`operator_backends.register_operator_backends(cfg)`, one **additive** step the
gateway schedules after `boot_platform` returns, as a contained background task
that is never awaited on the boot path (a slow descriptor file or a large harness
binary must not delay dashboard binding; until it lands the operator ids are
simply not registered, and a chat pinned to one gets the "not selectable" card
that a withdrawn backend gets). Because it runs
AFTER the boot-time `agent_backend` governance pass, each descriptor is registered
with the deployment's verdict applied in the same step (`policy_permits` →
`register_governed_backend`: the id joins the selectable BASELINE, so a later
loosened policy restores it, and the effective set only when permitted) — never
"register, then recompute at the end", which would leave the first descriptor
selectable for the whole time the attestation check of the next one spends
digesting its binary. A policy-denied descriptor is known, visible-but-unselectable
with the policy reason, and there is no instant at which it is selectable. Verify
in Settings (`mark_routing_verified`) uses the same one-step form.

The background step has a second consequence, and a **settle gate** for it: until
the step re-resolves the gateway's `agent.acp_backend` from the persisted
spelling, that instance still reads the boot-time coercion (Kiro) even when
`config.json` names an operator backend, so an UNPINNED chat dispatching in that
window would run on the wrong provider. The gateway therefore announces the
registration before scheduling it (`operator_backends.registration_pending`), the
step releases the gate on every exit path (and the gateway's task wrapper on
cancellation), and an unpinned chat's provider allocation waits for
`wait_until_registration_settled` while the unpinned speculative pre-spawn stands
down. A pinned chat never waits — its backend is its own — and outside a gateway
(a CLI command, an app server, a test) the gate is open from import, so nothing
ever waits there. It is deliberately not inside
`bootstrap_context`, and the public edition's `ProviderRegistry.register_acp_backends()`
seam stays the no-op it always was: loading descriptors is adapter work (a file
read, validation, registry writes), and the Kiro construction path gains none of
it (harness-parity H13) — a CLI command or an app server that boots the platform
runs exactly the path it ran before. The step registers the descriptors,
re-applies the `agent_backend` governance narrowing to the ids it just widened
the registry with (the same re-application a post-boot Verify performs), and
settles the gateway's config instance: that instance was loaded **before**
registration, so its `agent.acp_backend` was coerced by
`resolve_selected_backend` (which reads the selectable registry live) against a
registry without the operator ids and degraded to Kiro. The load carries the
file's own spelling beside the coerced field (`AgentConfig.acp_backend_persisted`,
a private carrier the schema skips and `to_dict` never writes back), and the step
re-resolves that spelling through the one selection gate — an in-memory registry
lookup, **not a second read of `config.json`**; with no operator descriptors the
lookup answers what the load already answered and the field is left untouched.
Every later `KiroCrewConfig.load()` sees the registered ids directly. An edit to
the file therefore takes effect **on the next gateway start, not live**. This is
the same restart every boot-time registration implies, and it is why an operator
edits the file and then restarts to see the row appear — as *Routing not
verified* on its first appearance, until **Verify routing** on that row records
the attestation and moves it to the selectable list (no second restart).

The load is idempotent (a second bootstrap pass skips an id already in
`ACP_BACKENDS_KNOWN`) and total: malformed JSON costs the whole file one invalid
row, a non-dict entry costs that id its row, and everything else still registers.
One bad entry never blocks boot.

## A complete worked example: `acme`

Take a fictional in-house agent CLI, `acme`, that speaks ACP over stdio. It
reads agent specs — so it asks for permission by construction, which is the
`agent_spec` routing — advertises its models over `session/new`, and ships as
its own adapter binary `acme-acp` on `PATH`. The full descriptor:

```jsonc
{
  "acme": {
    "display_name": "Acme Agent",
    "executable": "acme-acp",
    "argv": ["{executable}", "acp", "--workdir", "{workdir}"],
    "agent_args": ["--agent", "{agent}"],
    "model_args": ["--model", "{model}"],
    "model_source": "acp_advertised",
    "routing": "agent_spec"
  }
}
```

Field by field:

- **`acme`** (the key) is the harness id — the stable handle everything
  references: the configured `agent.acp_backend`, the per-chat pick, the model
  namespace, the session-map binding.
- **`display_name: "Acme Agent"`** is the human name in the picker and Settings; drop it
  and the id is used.
- **`executable: "acme-acp"`** is resolved at spawn on the generic plain-binary
  ladder (env override, then mise, then augmented PATH — the same
  `resolve_descriptor_executable` walk opencode's and goose's binaries use). A
  bare name that does not resolve aborts the spawn with a "not found (searched
  ...)" message naming the directories walked.
- **`argv`** starts with `{executable}` (so the attested absolute path is what
  execs), then Acme's own `acp` subcommand and a `--workdir {workdir}` that
  renders to the session's working directory.
- **`agent_args: ["--agent", "{agent}"]`** is emitted only when an agent is
  selected. Because Acme is `agent_spec`-routed and carries this block, its agent
  activation is verified after spawn.
- **`model_args: ["--model", "{model}"]`** is emitted only when a model is
  pinned; with no model selected the block is dropped and Acme runs on its own
  default rather than execing an empty `--model`.
- **No `capabilities`** -- there is nothing to claim. Acme is a member of no
  session-path capability set, so the client pins its model through
  `model_args` and takes the default branch at every capability gate.
- **`model_source: "acp_advertised"`** — Acme's catalog fills from what a live
  session advertises; it is empty until Acme has run once.
- **`routing: "agent_spec"`** — the selectability gate. No `permission_config` is
  needed (that belongs only to `session_config`), and no `mcp_delivery` is set,
  so it defaults to `agent_file` passthrough.

After an operator writes this and restarts the gateway, `acme` appears as
**Acme Agent** in Settings → AI backends marked *Routing not verified*. The
operator presses **Verify routing**; the gateway spawns Acme once, asks it to
write a probe file through its file-editing tool and again through its shell
tool, denies both permission requests Acme raises, confirms nothing was written,
records the attestation, and the row moves to the selectable list
and into the new-chat backend picker — no second restart. Its model catalog is
empty until Acme serves its first session, then fills from the advertised list.
Had the descriptor omitted `routing`, `acme` would still appear in Settings —
under the unroutable list, with the reason that nothing establishes its tool
calls reach the permission gate, and with no Verify action — but never in the
picker.

## Choosing a backend per chat and per subagent

Two selection surfaces let a session run on a backend other than the configured
global default. Both are distinct from the global `agent.acp_backend`, and both
route every non-empty value through the single selectability gate
(`resolve_selected_backend` / `selectable_backend_values`, harness-parity H4), so
neither can offer a value session creation would refuse.

**Per chat.** `POST /api/chat/slots/{slot}/backend` sets the backend for one chat
slot; a body of `{"backend": ""}` clears the pin and inherits the global default.
Unlike a model change, there is **no live in-place switch**: a backend is a
distinct harness process, and no `session/set_model`-style call can move a
running session across harnesses. So changing the backend **always resets the
session** — the live process is torn down and the next message cold-starts on the
new harness through the provider factory. A turn in flight or a parent with
children attached answers 409 (the reset would tear down the streaming turn or
kill the runtime the children run on); a no-op (same value) returns without a
reset. A crew-bound remote slot refuses the pick — a crew-bound session's backend
is chosen on the crew, not here. The new-chat picker reads its rows from `GET
/api/backends`, which lists every selectable id with its label and which one is
the global default, plus the `invalid` and `unroutable` operator-descriptor
diagnostics for Settings.

**Per subagent.** `spawn_run`'s `backend` parameter overrides the backend for one
spawned subagent; `""` inherits the parent's. Like a per-spawn `model` or
`reasoning_effort`, a non-empty value **forces the dedicated-process path**: the
parent's shared runtime runs on its own backend and cannot switch per session, so
the override reaches the provider factory only on a fresh process (carried as
`backend_override`, validated selectable at admission). A per-spawn backend
therefore always costs a dedicated process rather than a shared-session spawn.
