# Onboarding a new ACP harness

[harness-parity.md](harness-parity.md) says what a new harness may **not** do to
the Kiro path. This file says what it **must** do to land at all, in the order
the work actually falls out.

The sequence below is derived from onboarding Codex (`ACP_BACKEND_CODEX`), not
reconstructed from KAS and Claude Code after the fact. That matters, because two
of the stages here did not exist as stages until a third harness needed them:
Stage 2 had five capability sets and no tuning channels at all, and Stage 6
was invisible while every known id happened to be selectable. A harness
that walks this list will find gaps the list does not predict; when it does, the
gap belongs here in the same change, as a stage — not in the harness's own
module as a special case.

## The two landing states

A harness lands in one of two states, and choosing between them is Stage 7, not
Stage 1:

- **Dormant.** The core can *spell* the id — it is in `ACP_BACKENDS_KNOWN`, it
  has a provider label, a policy name, and a decided membership in every
  capability set — but no operator can *choose* it. A dormant harness is not a
  stub: its spawn path can be complete. It is dormant because something a real
  session depends on cannot yet answer for it.
- **Selectable.** The id is in `BASELINE_SELECTABLE_BACKENDS`, or an edition
  called `register_selectable_backend`, so it renders in the dashboard switch
  and survives a config load.

Dormant is a legitimate destination, and shipping there deliberately is cheaper
than a long-lived branch. But it must be *named* as an exception (Stage 7), or
the narrowing check fails.

## Stage 1 — the vocabulary, in the leaf

Everything a consumer needs to *name* your harness goes in
`src/kiro_crew/acp_backends.py`, which imports no ACP and therefore may be
imported by anything:

| Add | Why there |
|---|---|
| `ACP_BACKEND_<NAME>` | The id. Never a bare literal at a call site (H5). |
| membership in `ACP_BACKENDS_KNOWN` | `AcpProvider.__init__` rejects anything outside it (H8), and every capability set is asserted a subset of it. |
| `PROVIDER_LABEL_<NAME>` in `acp/types.py` | A closed mapping; an absent label means Kiro, so a harness without one persists as a Kiro session and has its transcript pruned for want of a Kiro session file (H11). |
| an entry in `POLICY_ID_BY_BACKEND` | A governance rule is written by a human as an identifier. The mapping is what makes the id nameable in a deny rule **before** anything registers it — so this is required even for a dormant harness. |

`acp/types.py` re-exports the vocabulary, so existing callers keep their import
site. Do not define the constants there: it is a forbidden root for the SDK
boundary gate, and a definition there is a definition consumers cannot reach
without crossing it.

## Stage 2 — an explicit decision for every capability set

Every capability set needs a decision. **"Inherited the default" is not a decision** — a
capability is granted by opt-in membership, never by negation (H6), so a set you
do not think about is a set you have silently opted out of. That is usually
right, and it must still be deliberate, because the review lane and the tests
both read the membership as a claim. `ACP_BACKENDS_KNOWN` is not one of them: it
is the membership floor, not a capability. Neither is
`backends_retired_by_host_logout()`: whether a host logout may retire your running
child is a fact about how you sign in, so it is declared in Stage 5 and projected
from there rather than decided here. It is deliberately not an `ACP_BACKENDS_*` set
— that naming is vocabulary this module owns, and a derived answer is not
vocabulary.

| Set | Grants |
|---|---|
| `ACP_BACKENDS_SESSION_SHARING` | One process may serve several sessions. Wrong membership hands a second session to a process that cannot hold it. |
| `ACP_BACKENDS_STEER` | The `_session/steer` extension. A steer sent to a non-implementer answers `-32601`. |
| `ACP_BACKENDS_INTERNAL_SANDBOX` | The harness sandboxes itself, so Kiro Crew's own wrapper stands down. Security-relevant: wrong membership hands isolation to a layer that never starts (H7). |
| `ACP_BACKENDS_ACP_RUNTIME` | Driven through `AcpRuntime` — one process demultiplexing N sessions — rather than its own per-session `AcpClient` spawn branch. Every reader takes the frozenset itself: `AcpProvider.is_acp_runtime_backend` for the FOREGROUND start path, and `session._bg_runtime_backends`, which intersects it with the set below and with selectability. Membership states the TRANSPORT and nothing more — the kiro-family `cli.json` effort and Tool Search overlay is gated on `ACP_BACKENDS_KIRO_SLASH_COMMANDS` at every site that writes, reads or clears it, so a member reading no such file never collects one. |
| `ACP_BACKENDS_SESSION_EVICTION` | The teardown verb Crew SENDS this harness evicts the session from the adapter's own session map, freeing what it held. Multiplexing is not that claim: a harness can serve N sessions perfectly and still have no verb that disposes one, and the gap shows only on a process that outlives many sessions, where every non-evicting teardown leaves its session addressable with its context resident. Every path that creates and destroys sessions on a shared process reads this set — `session._bg_runtime_backends` (title generation, suggestions, folders and nav each take their own ephemeral `sessionId`, many per conversation, at a rate the operator never controls), `AcpSessionProvider.new_conversation` (warm pooled reuse) and the runtime's entitlement probe — so a harness that has not declared eviction reaches none of them and leaks on none of them. Declare it from the verb Crew SENDS, measured, rather than from what the adapter advertises, and mind the delivery: codex-acp advertises `session/close` and `session/delete`, and for as long as Crew sent it `session/cancel` the same sessionId kept serving a prompt whose `cachedReadTokens` showed the context survived, so codex was out. Crew now sends `session/close` as a request, after which the sessionId stops answering (measured live against codex-acp 1.11.0; the same verb as a notification is ignored and evicts nothing), so codex is in. A gated live test re-runs that measurement on every install with the adapter, which is what lets the membership stand on a fact rather than a memory. |
| `ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION` | Model switching lands as a config option rather than a protocol call. |
| `ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION` | Reasoning-effort push, same channel shape. |
| `ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS` | The ids the harness ADVERTISES are `<model>[<effort>]` pairs its `model` option does not accept whole, so an exhausted spelling ladder falls through to two writes (bare model, then the effort). A non-member's refused bracketed id stays refused: claude's `[1m]` is a context WINDOW that must reach the wire intact, and opencode's `provider/model` ids carry no suffix at all, so neither may inherit a split it never advertised. Membership also gates the "adapter mismatch, not an account restriction" wording in `AcpModelUnavailable`, because "advertised implies entitled" is established only for a harness whose advertised list IS its entitlement. |
| `ACP_BACKENDS_KIRO_SLASH_COMMANDS` | Receives `_kiro.dev/commands/execute`, **and** gets the workspace `cli.json` overlay written for it. Membership decides both, so a non-member must not collect an overlay it never reads and the membership-gated clear can never remove. |
| `ACP_BACKENDS_MARKDOWN_AGENT_SPECS` | The harness loads an agent defined as ONE markdown file (`~/.kiro/agents/<name>.md`, YAML frontmatter + body as prompt), the v3 / Kiro IDE form Crew's roster lists for every backend. Answered through the harness seam `reads_markdown_agent_specs` (`MembershipHarness`). A non-member that fails to activate such an agent has the activation guard explain the markdown file and name the members, instead of the generic "rewrite the JSON spec" advice; it is never refused BEFORE the spawn, so the Kiro path gains no gate (H13). KAS is the only member today: Crew parses the file and hands it over the wire. |
| `ACP_BACKENDS_SESSION_MCP_ARRAY` | The harness reads its MCP surface from the `session/new` array rather than from Crew's agent spec. A non-member that is added here gets an empty array and works with every Crew tool silently absent. |
| `ACP_BACKENDS_MEMBER_DISPATCH` | Crew's member-dispatch tools are mounted into a channel-member session, with the auto-approve grant that goes with them. A harness with no per-session mount to ride is excluded, which withholds only the extra grant. |
| `ACP_BACKENDS_PRIVATE_MEMORY_MCP` | Direct private member MCP tools execute inside the member's OS sandbox. Kiro, Claude Code and KAS are members. Codex and unknown or merely selectable backends fail before private runtime creation. Membership does not waive the separate OS sandbox checks. |
| `ACP_BACKENDS_COMPACT` | The manual `/compact` entry points are offered. A non-member refuses the manual command up front rather than stranding the status waiter on a harness that emits no compaction status of its own. |
| `ACP_BACKENDS_INLINE_COMPACTION` | A strict subset: the compaction finishes INSIDE the `session/prompt` turn, so `wait_for_compaction()` answers `completed` from the capability instead of from the queue. A non-member's result arrives separately and must be awaited. Awaiting a member strands for the full timeout; telling a non-member it is done acknowledges a compaction that has not happened. |
| `ACP_BACKENDS_HARNESS_MANAGED_COMPACTION` | The harness compacts on its OWN initiative and reports it on its ACP surface, so Crew's context meter falls back below the threshold without Crew acting. This is what makes declining a non-member of `ACP_BACKENDS_COMPACT` honest rather than merely quiet. |
| `ACP_BACKENDS_CONTEXT_RECYCLE` | A full context is answered by recycling the session. The only destructive arm, so it is granted by membership and never by exclusion — a harness in none of the three compaction sets declines and is logged at WARNING, rather than inheriting a behaviour that ends conversations. |
| `ACP_BACKENDS_ADVERTISED_MODEL_SELECTION` | Membership buys two things, and a harness can need only one. First the CAPTURE: the list the harness advertises at `session/new` is written to the cross-session provider-model cache under the harness's own namespace, which is what `GET /api/models` reads back. Second the FOLD: a stored id is rewritten to the served spelling, at spawn and on a warm-pool `set_model`. A harness whose wire ids are already exact gets a no-op fold, so it joins for the capture alone — which is the whole point when its advertised select is the only source of ids it accepts back (codex). claude joins for both. |
| `ACP_BACKENDS_SEED_LOCAL_SETTINGS` | A local settings file is seeded at spawn **and re-seeded on `set_model`**, so a warm-pool claim does not leave a stale model or allowlist behind. A harness with no such file is not a member. |
| `ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD` | The dashboard's MCP sync leaves running sessions alone after a config write, because the harness reconciles the agent file itself. Membership is version-gated per process by `mcp_hot_reload_supported`, not granted by the harness name alone. |
| `ACP_BACKENDS_STRUCTURED_REFUSAL` | The harness reports a model-side refusal with a **reason** — on the Kiro path a `_kiro.dev/metadata` frame with `stopReason: CONTENT_FILTERED` and a `refusal {category, explanation, recommendedModel}` object — and `acp/_dispatch.parse_refusal` is consulted on that frame. Every harness still lands on the same `RefusalInfo` and the same dashboard card; a non-member's card just has no category line. A harness whose refusal wire carries a reason in a different shape adds a parser and joins here — it must not widen the metadata reader to guess. |

Not every per-harness fact is a membership SET. Which `configId` carries the
reasoning effort is a per-harness *spelling* -- `effort` for claude-agent-acp,
`reasoning_effort` for codex-acp -- and it is answered by
`effort_config_option_id(backend)` in the vocabulary module, defaulting to
`effort` with a row only for the exception. A new harness that spells it
differently adds one row there and needs no call-site change, because every
effort site reads the resolver: the dashboard's live change and the startup
application of a persisted slot level, the knowledge pool's apply, both
`get_valid_effort_levels` readers, and the pair split above. Getting this wrong
fails toward silence rather than an error -- an adapter answering "unknown
config option" is indistinguishable from one with no effort selector, so every
one of those callers skips and the session runs an effort the UI does not
report. Only the function is exported; the table behind it is not, because a
caller indexing it takes a `KeyError` for exactly the harnesses the default
exists to serve.

The tuning channels are one set each rather than one "tuning" set, because a
harness can implement one and not another. If your harness needs a tuning
channel none of them describes, add a set — do not widen an existing one.

### And this appears on the card

Every decision in this stage is READ BACK to the operator. Developer > Agent
Backend is a LIST of harnesses and a DETAIL for whichever row is highlighted, and
the detail is that harness's capability card — each line projected from these
memberships by `agent_sdk/backend_cards.py`. So a membership is not only what the
code branches on, it is what an operator comparing two harnesses is shown before
they pick one.

Exactly one card is ever on screen, which is why the capability list is not behind
a disclosure: it was collapsed when every harness's card rendered stacked down the
page, and with one card there is nothing to bury. Highlighting a row shows its
card and never switches the backend — the one **Use \<name\>** button does that —
so a harness this machine cannot run still gets a row and a full card. Under the
old control an unselectable harness had no chip at all, which meant the harnesses
an operator most needed to read about were the ones the page had least room for.

That projection is why this stage costs a new harness nothing beyond the decisions
it already owes. A harness that joins `ACP_BACKENDS_KNOWN` and decides every set
renders a complete card with no edit to any card file, no frontend edit and no
locale edit: labels are written once per CAPABILITY and reused by every harness.
`test_backend_cards.py` holds the server half of that, and
`AgentBackendTab.test.tsx`'s "renders a complete detail for a backend the panel has
never heard of" holds the panel half — a projection nothing renders is not a
feature, so both halves are asserted.

Each set reaches one of four buckets, and a new set must be put in one of them or
`test_backend_cards` fails — the same forcing function the disposition table applies
to a set's consumers:

| Bucket | What it means | Where |
|---|---|---|
| user-facing | Switching harness changes what the user can DO, and the absence is a LOSS: a control disappears, a command is refused, tools are missing from a session. | `USER_FACING_LINES`, rendered available / not available |
| security | It moves a confinement or credential boundary: which layer confines the agent, whether Crew hands its own credential to the child, how an unclassifiable approval is answered. | `SECURITY_LINES`, stated only when it HOLDS, rendered OUTSIDE every disclosure |
| operator | It says where something LIVES: whose disk holds the transcript, which side supplies the model list, which channel carries a command. | `OPERATOR_LINES`, stated only when it HOLDS |
| off-card | The only difference is which code path runs, OR the card cannot honestly project the membership. Two tests: if the membership were wrong, would the user see a missing feature or a BUG? A defect is not a capability. And can this card establish the fact at all? A version-gated membership cannot be marked available by a projection that holds no version. | `OFF_CARD_SETS`, with the reason per set |

Two rules decide the bucket, and both are about what a mark on a card MEANS.

**A membership whose two states are both correct behaviour is a note, never a
line.** Three sets are that kind today. Crew holds the kiro family's transcript and
serves its model ids from its own registry, so a not-available mark on either would
report the default harness as missing something it never needed. And
`ACP_BACKENDS_KIRO_SLASH_COMMANDS` names an RPC rather than the feature: a
non-member is not command-less — opencode and pi both publish their own built-ins as
an `available_commands_update` — so the card states which CHANNEL carries a command
and claims nothing about a harness that carries its own. If your harness has an
equivalent mechanism under another name, say so in the set's comment: that is what
decides whether its line is a loss or a note.

**The card is two-level on purpose.** A `frozenset` carries one bit, so "does it
differently" and "nobody measured it" cannot be told apart from "cannot", and a
graded level would have to be authored per harness per capability. The one
genuinely graded fact is Stage 4's routing, rendered from `Routing`'s own five
mechanisms — and it, like the security notes, is never hidden behind a disclosure.

## Stage 3 — the spawn path

This is the irreducible new code, and on the harnesses measured so far it is the
largest single piece: `acp/client.py` grew between +194 and +806 lines per
harness. It is not reducible by refactoring, because it is the part that is
genuinely different.

**Ask first whether your harness serves ACP from its own binary.** If it does,
most of this stage is already written. `ACP_BACKEND_LAUNCH` in
`agent_sdk/backends.py` holds one record per such harness — the display label, the
binary, the ACP args, the override variable, the install command, the protocol
version, and one prose hint for what an operator might otherwise try to install.
Add a row and the shared paths resolve you from it: one resolver
(`_resolve_self_served_bin`), one cache keyed by backend, the `_spawn` prologue
(`_resolve_self_served_launch`, which answers binary, argv, spawn label and stderr
label), the handshake dialect table, the install probe and the tool-gate label.

Membership is `ACP_BACKENDS_SELF_SERVED_ACP`, derived from the table's own keys.
There is nothing else to register.

What stays yours in that case is only your ROUTING — a config read-back, an
environment seed, a gate extension — because those are decisions rather than
values. The three harnesses in the table each keep exactly that and nothing more.

A harness that is **not** a member — a Node adapter, two independently-absent
components, an argv that carries an agent spec — writes this stage by hand, and the
list below is the shape. Do not add a row for it: a record whose fields do not
describe the launch is worse than no record, because the shared resolver would spawn
the wrong thing rather than say so.

Where that hand-written code lives follows the transport. A harness driven through
`AcpRuntime` declares its spawn at Seam 1 of its own `acp/harness/<name>.py` —
`CodexHarness.resolve_spawn` is the worked shape — while a per-session harness
carries its arm in `acp/client.py`. The resolver ladder is shared either way:
`CODEX_ACP_BIN`, `CODEX_ACP_NPM_PKG`, `_resolve_codex_acp_bin` and
`codex_acp_not_found_message` sit in `client.py` with callers in the harness and in
the install probe, so one wording answers "the adapter is not installed" wherever
the question is asked.

What a hand-written harness needs, using the Codex adapter as the shape:

- **The adapter, and whether one is needed at all.** `codex-acp` exists because
  the `codex` CLI does not serve ACP — it reads `acp` as a prompt. The adapter is
  the transport, not an optimization. Establish this before anything else; a
  harness that speaks ACP natively skips most of this stage.
- **Binary and package constants** (`CODEX_ACP_BIN`, `CODEX_ACP_NPM_PKG`) and
  the package entry path.
- **A hoisted-dependency marker.** An adapter whose own dependencies are missing
  dies at ESM import time — *after* the child is spawned, which is the worst
  place to find out.
- **An explicit env override** (`CODEX_ACP_BIN`), spelled the way the adapter's
  own documentation spells it.
- **Resolution order**: project-local `node_modules` first, then global/PATH.
  Share the root discovery (`_vendored_acp_roots`) and join your own package
  path onto it. Generalizing that helper is allowed; it is harness-neutral and
  belongs to no harness. Adding a branch to the Kiro path is not (H13).

Constants an adapter reads *itself* from the ambient environment do not get a
constant here. Naming one implies a forwarding that does not exist — the Codex
seam documents exactly this asymmetry against its Claude counterpart, which *is*
explicitly forwarded.

**If the harness has no permission setting to read back** — it runs every tool
call unasked and only an extension inside it can raise the dialog the adapter
forwards — the routing is `VERIFIED_GATE_EXTENSION`, and the spawn path carries a
fixed sequence that the next such harness repeats verbatim rather than rediscovers
(the Pi worked example is the first instance; `acp/client.py` names each step):

1. **Ship the extension as package data** (`agent_sdk/gate_extensions/<harness>/`,
   covered by the existing `setup.cfg` / `MANIFEST.in` globs) and **pin its digest**
   in the driver (`<HARNESS>_GATE_EXTENSION_SHA256`). Hash and seal the **LF form** of
   the bytes (`_pi_gate_extension_bytes`): a Windows checkout rewrites the text file
   CRLF and a raw-bytes digest would refuse every session there. Pin the checkout LF
   in `.gitattributes` as well; the normalization is what keeps the property off a
   repo-config line. Editing the extension is a deliberate two-file edit (bytes and
   pin), and the test that hashes both renderings to the pinned digest is the ratchet.
2. **Seal a copy** into the sandbox run directory (read-only, per gateway process,
   rewritten when the bytes differ) and **refuse the temp-dir fallback**
   (`_pi_gate_run_dir`): the package path is agent-writable on a source install,
   and a shared directory is rewritable by any same-UID process between seal and
   exec. Teach the run-dir sweep the artifact family (owner-PID rule, never age).
3. **Load it through a launcher** the adapter is told to run in place of the harness
   (its own override variable, `PI_ACP_PI_COMMAND` for pi-acp), written under
   `mkstemp` and published only after the mode change.
4. **Read the load back** from the harness's own registry before the first prompt,
   requiring the probe command AND its source path to be the sealed copy, compared as
   the same file (`realpath` + `normcase`), and refuse the session otherwise
   (`gate_extension_issue`).
5. **Bind the dialog to the session** with a per-session nonce in the child's
   environment that the extension echoes in its envelope; the dispatch parser trusts
   an envelope only under that nonce. Forward a shell command and a document body
   WHOLE; bound other values and mark the call untrusted when cut.
6. **Guard the unpinned links in band.** The read-back proves the extension LOADED;
   three links stay the adapter's and the harness's: the adapter honouring its
   command override, the adapter forwarding the dialog per call, and the harness
   honouring the extension's block. A `completed` update for a call the gate never
   asked about, or for one the host DENIED, kills the harness and fails the turn
   (`_tripwire_pi_gate`); the adapter version is named against the one the contract
   was observed on, once per process (`_note_pi_adapter_version`).

## Stage 4 — the handshake, as your own literal

Protocol version and client capabilities stay per-harness literals (H10). Give
your harness its own `PROTOCOL_VERSION_<NAME>` **even when the number is
identical to an existing one.** That is not duplication: it makes a future
divergence a one-line edit here instead of a silent downgrade of whichever
harness happened to move first.

The literal belongs beside the harness that answers with it — `PROTOCOL_VERSION_CODEX`
in `acp/harness/codex.py`, returned from Seam 2 — so one module holds a host's whole
dialect and the shared driver keeps no per-host table to fall out of step with it.

## Stage 5 — the auth declaration

How your harness signs in is one frozen `AgentAuthDeclaration` in
`src/kiro_crew/agent_sdk/host_auth.py`, plus your column in bucket 3 of
[agent-host-contract.md](agent-host-contract.md). **That is the whole auth cost.**
Everything else is a projection of that one literal: the read-gate floor that
fences your credential and re-anchors it under your own override variables
(`security/paths.py`), the sandbox credential mask and the single leaf it spares so
your own child can still authenticate (`agent_sdk/tool_gate.py`), the
the logout-recycle answer `backends_retired_by_host_logout()`, the `AcpAuthRequired` text
an operator reads when a session cannot start, the `auth` object on
`GET /api/acp-backends` (`dashboard/handlers/acp_backend_status.py`), and the
standing sign-in caveat the backend switch renders from it. You write the
declaration; you edit none of those.

It belongs after the handshake and before the install probe, because it is the
sign-in half of the question Stage 6 answers about files — a harness that is
installed and signed out still dies at `session/new` — and because the floor has
to be fencing your credential before Stage 7 lets an operator choose you.

| Field | What it commits the host to |
|---|---|
| `entitlement_source` | One of `host_identity_store` or `own_credential_file`. The doctor row and the logout policy branch on it, so a third spelling fails a test rather than reading as a harness nobody has an answer for; a harness entitled by the ambient cloud environment adds the third when it exists. |
| `credential_leaves` | The home-relative leaves you STORE, spliced onto the read-gate floor so an agent's file tools can read none of them. Empty when your entitlement is the host store: those locations are the host's, declared in `identity_stores.py`, and re-declaring them would hand a driver a say over the host's own store. |
| `home_override_env_vars` | The variables that relocate your credential `$HOME`. Every declared leaf is re-anchored under each of them, which is what keeps a relocated token fenced. |
| `adapter_own_leaves` | The leaf your own child must still read. A SUBSET of `credential_leaves`, and enforced as one: a driver may only ask the mask to spare a leaf its own declaration put on the floor, and `__post_init__` raises otherwise. Know what you are declaring: the spared leaf is readable by the harness's shell too (same process tree, and the shell gate matches no paths by design), so this is the operator's token for that vendor exposed to the model — the posture every enforced harness ships today, tracked in #10438. Declare `()` if the harness can authenticate without the file (a keyless local model; Linux presents a masked file as empty) and say so in `sign_in_remedy`. |
| `sign_in_remedy` | A finished sentence, server-owned and rendered verbatim wherever it appears. Untranslated on purpose — a translated per-harness string is a per-harness edit to thirteen locale files by construction, and the harness nobody remembers to add is exactly the one that needs the sentence. |
| `host_logout_retires_children` | Whether a host logout may retire your already-running children. True only alongside `host_identity_store`; the other pairing is refused, because a logout says nothing about a store you never read. |

The declaration says what you **store**; the host still decides what is **fenced**.
That is why no field names a path to leave open in general, and why a malformed
declaration raises at import rather than reaching a floor that fences less than its
author believed.

`AgentInteractiveLogin` is the optional half, and the absence is the point. A
harness whose sign-in happens outside the product — in the operator's own terminal,
or in the harness's own CLI — simply does not implement it, and a consumer finds
that out with `isinstance` rather than by reading a boolean and then calling a
method that no-ops. No harness implements it today.

**Silence is not available at this stage.** `test_agent_sdk_host_auth.py` fails
when a member of `ACP_BACKENDS_KNOWN` has no declaration, and
`missing_declarations` names the gap. The failure mode that gate closes is a
harness reaching `BASELINE_SELECTABLE_BACKENDS` without touching the credential
floor, and then serving sessions with a live agent-readable token that nothing
fences.

## Stage 6 — the install probe

`agent_sdk/backend_install.py` answers a question selectability does not: *is
this harness installed on this machine, and if not, what installs it?* `_PROBES`
maps each id to a probe returning a `BackendInstallState` that names the missing
component and the command that fixes it.

**A harness in `ACP_BACKEND_LAUNCH` needs no probe and no row.** `_PROBES` binds
`_probe_self_served` to every member of `ACP_BACKENDS_SELF_SERVED_ACP`, and that
probe reads the component name and the install command out of the record. This is
the whole of Stage 6 for a member: the row you added in Stage 3 is what answers.

A harness outside the table writes its own `_probe_<name>` and its own `_PROBES`
row, because its install shape is a decision — the two Node adapters and Pi each
have two components, and which half is absent changes the remedy.

**This stage is the gate between dormant and selectable**, and it is the one
that is easy to skip because nothing fails without it. Nothing fails; the
operator does. A build that offers a switch with no probe behind it cannot tell
anyone what was missing when the session failed to start — the switch renders,
the session dies, and the dashboard has nothing to say.

## Stage 7 — selectability, or a named exception

With Stages 1–6 done, add the id to `BASELINE_SELECTABLE_BACKENDS`.

If it is not done — most often Stage 6 — then the id is in
`ACP_BACKENDS_KNOWN` but not in the baseline, which is a NARROWING. Name it in
`NOT_SHIPPED_SELECTABLE` in
`test_agent_backend_editable.py::test_baseline_ships_every_known_backend`, with
the reason. An explicit allowlist rather than a relaxed assertion is the point:
a plain `baseline != known` still fails, so an id may sit outside the baseline
only by being named.

**Selectability additionally requires a decided MCP projection.** A harness an
operator can choose is a harness whose sessions have — or provably do not have —
Kiro Crew's own tools, and that answer is a declared kind in
`src/kiro_crew/providers/mirrors/registry.py` (`PROJECTIONS`): `native`, `mirror`,
`external` or `no-channel`. Work the checklist in
[`providers/mirrors/README.md`](../../../src/kiro_crew/providers/mirrors/README.md)
("Adding a backend: checklist") as part of this stage, not after it. The kind is
not a formality and the failure it closes is specific: a session comes up holding
`tools: ["@kirocrew-core", ...]` with nothing defining `kirocrew-core`, so every
Crew tool is absent while the harness works and nothing anywhere is red. That
shipped on four harnesses in a row, because a projection nobody had written was
spelled the same way as a projection nobody needed.

`no-channel` is a legitimate answer here, on the same terms as dormancy: it must be
NAMED. A selectable `no-channel` harness has to name the channel that would have to
exist and its tracking pointer in the declaration, and be named in this document —
`test_provider_mirrors.py` checks both halves, so a gap recorded in only one of them
fails. The reader of this file is the human who writes the code; the declaration is
what the code reads; neither substitutes for the other.

Selectability has exactly one gate, `resolve_selected_backend`, and it logs
(H4). Do not add a static `enum` to `AgentConfig.acp_backend`: a literal frozen
at import cannot see a boot-time registration, and `validate_config_data`
*deletes* an out-of-enum value before the loader ever sees it — which strips a
registered harness from `config.json` with no degrade log at all.

## Stage 8 — what a live harness additionally touches

Stages 1–7 keep a harness inside `acp/`, `providers/`, and `acp_backends.py`. A
harness an operator can actually select spills further. Measured across the two
in-flight live-harness branches, roughly ten files outside those trees:

`dashboard/handlers/agents.py` (the largest, +213 on one branch),
`mcp_gateway/session_servers.py` (+112), `dashboard/kiro_readiness.py`,
`dashboard/handlers/kiro_prerequisite.py`, `dashboard/handlers/sessions.py`,
`agent.py`, `config/loader.py`, `providers/base.py`, `session.py`,
`subagent.py`, `cli_doctor.py`.

Two rules govern that spill. A capability the session layer reads off a provider
is declared on `LLMProvider` with a safe default, so an adapter never forces a
`hasattr` probe onto the Kiro path (H14) — the cost of obeying this is small,
around +11 lines in `providers/base.py` on the branch that needed it. And the
`ProviderRegistry` seam takes the addition without a `CONTRACT_VERSION` bump
(H13); if the Kiro construction path gains a conditional, a required argument,
or a new failure mode in service of your adapter, the design is wrong, not the
invariant.

## Gates and tests

Beyond the ordinary suite:

- **`scripts/check_harness_parity.py`** enforces Group B on the lines your diff
  *adds*, not the whole tree. Six rules, self-tested.
- **`scripts/check_agent_sdk_boundary.py`** is shrink-only. A new import of
  `kiro_crew.acp` or `kiro_crew.providers` from a consumer fails even though the
  existing baseline (`.github/agent-sdk-boundary-baseline.txt`) grandfathers a
  list of them. This is why Stage 1 puts the vocabulary in a
  leaf: a consumer naming your constant must not have to cross the boundary to
  do it.
- **`test_harness_parity.py`** pins the structural invariants (Groups A and C),
  so they fail in the ordinary test job rather than a separate gate.
- **Group D is review-only.** `AUTOSDE.yaml`'s `harness-parity` rule carries
  H13 and H14 to every AI review lane, because the absence of a mechanism is not
  something a source scan can see.
- **`./scripts/docs-lint.sh`** requires every doc to be reachable from its
  directory index, and checks that line citations still point at what they
  claim.
- **`test_acp_launch_goldens.py`** compares what every known harness is LAUNCHED
  as — the argv handed to the process factory, the label the spawn is logged under,
  the label stderr is drained under, and the environment variables the spawn adds —
  against `test/fixtures/acp_launch_goldens.json`. A new id fails it until the
  fixture carries a row for it, which is deliberate: the fixture is what says a
  change to a shared path left every other harness alone.

  The test is read-only. Regenerate with `python3 scripts/update_acp_launch_goldens.py`
  and **commit the rewritten fixture in the same commit as the change**, because the
  fixture diff is what shows a reviewer which harness moved. Never regenerate one to
  turn a red green without saying in the review why the launch moved, and never to
  clear a kiro-cli row — that row is what harness-parity H13 protects.

Never relax a check to make a red invariant green. If a harness genuinely cannot
be adapted within these invariants, the correct outcome is that it does not land
yet — say so in the PR instead of widening a seam.

## Worked example: the Codex seam

The Codex onboarding is a clean instance of stopping at Stage 7:

| Stage | State |
|---|---|
| 1 vocabulary | Done — `ACP_BACKEND_CODEX`, in `ACP_BACKENDS_KNOWN`, `PROVIDER_LABEL_CODEX`, policy name mapped. |
| 2 capability sets | Decided for every set: in the transport set `ACP_BACKENDS_ACP_RUNTIME`, out of `ACP_BACKENDS_SESSION_SHARING` because the shared-subagent path cannot resolve a codex continuation, in `ACP_BACKENDS_SESSION_EVICTION` because the teardown Crew sends it is the standard `session/close`, which evicts (it was out while that verb was `session/cancel`, which does not), in the session MCP array, the advertised-model capture and the model and effort channels, out of every kiro-family set. All three channel sets were *created* by this work, which is why the tuning channels are three sets rather than one. |
| 3 spawn path | Done — adapter, npm package, dep marker, env override, project-local resolution, and the spawn itself at Seam 1 of `acp/harness/codex.py` over the resolver `client.py` shares. |
| 4 handshake | Done — `PROTOCOL_VERSION_CODEX`, its own literal at the same number as Claude's. |
| 5 auth declaration | Done — `own_credential_file`, `~/.codex/auth.json` on the floor with `CODEX_HOME` re-anchored, that same leaf spared for its own child, `.aws/config` re-exposed read-only, not retired by a host logout, and a two-branch remedy every consumer renders verbatim. |
| 6 install probe | Done — `_probe_codex` names `codex-acp` and the command that installs it. One component, not two: the adapter ships its own Codex binary. Credentials are deliberately NOT probed: a `missing` verdict disables the switch, and the checkable paths are not the only ones that authenticate a Codex. The sign-in answer is the Stage 5 declaration instead, and every consumer renders its remedy rather than carrying a string of its own. |
| 7 selectability | Selectable. `NOT_SHIPPED_SELECTABLE` is empty again, which is the healthy state. |
| routing | Done — `SESSION_CONFIG`, verified and applied as `mode=read-only` after session/new and before the first prompt, refusing otherwise. |
| residual | ACP v1 cannot require a prompt for a passive READ, so the sensitive-path block does not see this harness's reads. Mitigated at the OS boundary instead: its child cannot read the credential homes the standard tier leaves open. |
| 8 live spill | Not reached. |

The lesson worth carrying: the seam is dormant for exactly one reason, that
reason is written down where the narrowing check reads it, and closing it is a
single stage rather than a re-litigation. That is the shape to aim for — not
"complete or nothing", but "incomplete at a named stage".

## Worked example: the OpenCode harness

The first onboarding run with every gate in this document already in place, and
the one to read for what the stages cost when nothing can be skipped:

| Stage | State |
|---|---|
| 1 vocabulary | Done — `ACP_BACKEND_OPENCODE`, in `ACP_BACKENDS_KNOWN`, `PROVIDER_LABEL_OPENCODE`, policy name mapped, its own model-registry namespace. |
| 2 capability sets | Decided for every set, and each decision cites what the harness advertised rather than what it resembles: in the model channel and the advertised-model capture, out of the effort channel (its `session/new` advertises a `mode` select beside `model` and no `effort`), out of steer and both compaction sets (its `sessionCapabilities` are close/fork/list/resume), and — the one decision this run got WRONG — out of the session MCP array, on the grounds that it advertises `http` and `sse` MCP transports and no stdio. That is the failure mode Stage 2's own instruction is meant to prevent, arriving through a door the instruction leaves open: the decision DID cite what the harness advertised rather than what it resembles, and it was still wrong, because ACP's `McpCapabilities` has exactly two boolean fields (`http`, `sse`) and no `stdio` field for any conforming agent to set. Citing an advertisement is not enough; the schema that would carry the claim has to be read too, or an absence that cannot exist is mistaken for a refusal. Corrected by measurement against `opencode acp` 1.18.30 — the element Crew already emits is accepted, the child is spawned, its tools are listed and the element's `env` reaches it — so the harness is IN the set, with a mirror at `providers/mirrors/opencode.py` and a ratchet at `test/test_opencode_session_mcp.py`. The cost of the error was one selectable harness serving every session with none of Crew's own tools, and nothing red. |
| 3 spawn path | Done — one binary, `opencode acp`, resolved override → mise → PATH. No adapter package and no Node floor, so the ladder is the plain-binary one rather than the entry-script one. |
| 4 handshake | Done — `PROTOCOL_VERSION_OPENCODE`, its own literal, integer `1`, captured off its own wire. |
| 5 auth declaration | Done — `own_credential_file`, `~/.local/share/opencode/auth.json` on the floor with `XDG_DATA_HOME` re-anchored, that leaf spared for its own child, not retired by a host logout, and a remedy that names an action without asserting a state (a locally served model needs no sign-in at all). |
| 6 install probe | Done — no probe of its own. This harness is a member of `ACP_BACKEND_LAUNCH`, so `_PROBES` binds `_probe_self_served` to it and the component name and install command come from its record. One component, and here that is not a simplification: the thing that would be missing is the thing that serves ACP. `restart_required` is read from the spawn path's own cache (`self_served_cached_negative()`): the binary resolves now, but this process already cached its absence, so a session started right now still fails until the gateway restarts. |
| 7 selectability | Selectable. `NOT_SHIPPED_SELECTABLE` stays empty. |
| routing | Done, by a NEW mechanism — `VERIFIED_SEEDED_SETTINGS`. The setting travels as inline config in the child's environment, which resolves above the project's own config file, and the harness's own resolved configuration is read back before the first prompt; the session is refused when the required value is not in force. |
| residual | The read-back establishes the PRECONDITION, not that the harness honours it per tool call — no client-side read can prove that. And ACP v1 still cannot require a prompt for a passive READ, so the OS-boundary credential mask is the compensating control, as it is for Codex. |
| 8 live spill | Reached — a live turn, and a frame corpus that is live for all seven required classes, `session/request_permission` included: with `permission: ask` in force the harness asked before running `bash`, which is the observation the whole enforcement claim needed. |

Two things this run produced that the checklist did not ask for, and both belong
in the reading of it. The routing mechanism is one: Stage 2's instruction is to
decide every set, and the honest decision here was that neither existing routing
member described this harness — `SEEDED_SETTINGS` is declared-but-unenforced for
want of a read-back, and this harness has one. Adding a member to the vocabulary is
a heavier edit than joining a set, and it is the right one when the alternative is
a guarantee nobody performs.

## Worked example: the DeepSeek Harness

The run to read for what happens when a harness passes every mechanical stage and
fails the one that matters. It is `ACP_BACKENDS_KNOWN` and it is NOT selectable.

| Stage | State |
|---|---|
| 1 vocabulary | Done — `ACP_BACKEND_DEEPSEEK`, in `ACP_BACKENDS_KNOWN`, `PROVIDER_LABEL_DEEPSEEK`, policy name mapped, its own model-registry namespace. |
| 2 capability sets | Decided for every set. In the model channel, the effort channel and the advertised-model capture; in the session MCP array, which is the first membership won by a PROBE rather than by the advertisement (it advertises `mcpCapabilities: {"http": true}`, and stdio is ACP v1's baseline rather than an omission — a stdio entry naming an unrunnable command comes back as a failed MCP handshake, so the transport mounted). Out of steer, both compaction sets, the internal sandbox and member dispatch. |
| 3 spawn path | Done — one binary plus a profile selector, `dsh --profile acp`, resolved override → mise → PATH. The ACP package is a plugin with no executable, so what resolves is the HOST that boots the profile it lives in. |
| 4 handshake | Done — `PROTOCOL_VERSION_DEEPSEEK`, its own literal, integer `1`, captured off its own wire. |
| 5 auth declaration | Done — `own_credential_file`, and less auth than any harness so far: `authMethods: []` and an `authenticate` that returns immediate success, so the ACP layer authenticates nothing and the secret it needs is a PROVIDER key. Two leaves on the floor, `~/.dsh/.credentials.yaml` and the `~/.dsh/.env` fallback, `DSH_HOME` re-anchored, `adapter_own_leaves` EMPTY. |
| 6 install probe | Done — no probe of its own; `_probe_self_served` reads this harness's record, which names `dsh` and `npm i -g @deepseek-ai/dsh`, with `restart_required` from the spawn path's own cache. Naming the HOST binary rather than the ACP package is why the command is data in the record: that package is a plugin with no executable, so advice naming it would not produce a runnable harness. |
| 7 selectability | **Not selectable.** Named in `NOT_SHIPPED_SELECTABLE` with its reason. |
| routing | `UNVERIFIED`, on observation rather than for want of looking. |
| residual | The whole of it. Crew's PreToolUse gate does not run for what a session does, and there is no compensating mask either, because the mask is gated on `ENFORCED_ROUTINGS`. |
| 8 live spill | Reached, and it is what produced the verdict: a live turn, and a corpus live for six of the seven required classes. The seventh is synthesized, because the harness produced no `session/request_permission` frame in four captures. |

Two things this run produced that the checklist did not ask for.

The first is a protocol divergence that turned out not to be one. This harness
REJECTS `session/load` with `-32601` and serves `session/resume` instead. The
reflex is to read that as a quirk and branch on the harness id; the ACP schema says
otherwise — `session/resume` restores "an existing session without returning
previous messages (unlike `session/load`)" and exists "for agents that can resume
sessions but don't implement full session loading". Both are standard, and their
requests and responses carry the same fields. So the cost was one membership set
keying BOTH varying reads (the capability advertised and the verb sent) plus one
method constant, and the two sets that already described a harness owning its own
sessions were reused unchanged. Read a "divergence" against the specification
before writing a branch: a fourth harness lacking `session/load` pays nothing.

The second is the harder lesson, and it is about what Stage 7 is FOR. Every
mechanical stage passed. What failed is the question underneath the switch: does a
tool call reach Crew's gate? This harness's sandbox decides that itself — an
in-policy action runs silently, an out-of-policy one is DENIED with the denial
inside the tool result and a `status` of `completed` — and
`session/request_permission` carries only a MODEL-INITIATED request to escalate
past the sandbox, refused outright when the model omits its justification.

That shape is dangerous to onboard because it looks routable. The harness has a
real approval policy, Crew can pin it, and a read-back can confirm it in force. A
`VERIFIED_SEEDED_SETTINGS` entry would have gone green through every gate in this
document while gating escalations rather than tool calls. The thing that caught it
was Stage 8: four live captures across both non-permissive postures, none of which
raised a permission request. Do not let a setting's existence stand in for the
observation — a harness with a permission vocabulary is not the same as a harness
that asks, and only the wire can tell you which you have.

The onward consequence is worth naming because it is a benefit. `UNVERIFIED` keeps
this harness outside `ENFORCED_ROUTINGS`, so `adapter_own_leaves` must be empty,
so it removes nothing from the OS credential deny list for its process tree. A
harness shipping `bash` with a carve-out is a harness whose shell can `open()` the
carved-out token; retreating to the honest verdict avoided that by construction
rather than by a second control.

**The unselectable state is conditional, not terminal, and it is worth saying what
would lift it.** Stage 7 becomes passable when a tool call reaches Kiro Crew's gate
per call rather than only when the model asks to escalate. Three routes would do it,
in ascending cost. The harness could route its sandbox's own decision through the
approval seam instead of deciding it internally, which would make every in-policy
action ask and put this harness on the same footing as OpenCode — an upstream change,
and the cheapest if it happens. Or a Kiro Crew plugin could be composed into the
harness's `approval/request` waterfall as the terminal answerer, which would make
every request Crew's to decide — that means shipping a plugin into a third-party
composition, a new kind of artifact for this repository. Or the sandbox mode could be
pinned to a posture where the actions Crew cares about are all escalations, which is
the weakest of the three because it depends on the model choosing to ask.

Until one of those exists the verdict stands. What must NOT happen is the fourth
route: reading the approval policy back and calling it routed. That verifies a real
setting about the wrong thing, and it would go green through every gate in this
document.

There is one further gap this run recorded rather than closed, in
`tool_gate.ENFORCED_ROUTINGS`'s own comment: `is_enforced()` answers both "does a
non-ROUTED verdict refuse this session" and "does this harness get the OS credential
mask", and those only coincide for the harnesses carried today.

The other is what onboarding a harness with a *different shape* of credential home
surfaced. Every earlier harness's override variable stood in for its token's parent
directory, so the credential floor re-anchored a relocated token by its final
segment alone. `XDG_DATA_HOME` stands in for `.local/share`, two segments up, so
that anchoring fenced a path this harness never writes while the real relocated
token stayed readable. A harness declares the spelling its file takes under an
override root now. Expect this: the buckets are answered from the harnesses that
existed when they were written, and a new one whose answer has a different shape
finds the seam rather than the gap.

## Worked example: the Pi harness

The second run through every gate, and the one to read for what a harness with NO
permission gate of its own costs — the case none of the routing members described:

| Stage | State |
|---|---|
| 1 vocabulary | Done — `ACP_BACKEND_PI`, in `ACP_BACKENDS_KNOWN`, `PROVIDER_LABEL_PI`, policy name mapped, its own model-registry namespace. |
| 2 capability sets | Decided for every set, each on what the harness advertised or what a capture showed: in the model channel and the advertised-model capture (a `model` select whose values are `provider/model` ids out of pi's own `models.json`), out of the effort channel (the option beside it is `thought_level`, a different id and vocabulary), in the harness-owned-sessions set (a `session/load` replays the conversation and answers with `modes`, so NOT in the load-without-modes set), out of steer, out of both compaction sets (a `/compact` built-in exists but its turn shape is unobserved, so the exclusion is conservative and says so), and OUT of the session MCP array for a reason worse than absence — see below. |
| 3 spawn path | Done — TWO components. The `pi-acp` adapter is resolved on the Node-entry ladder (`PI_ACP_BIN` override → project-local `node_modules` with the SDK marker → mise → PATH) and the `pi` agent on the plain-binary ladder (`PI_ACP_PI_COMMAND` override → mise → PATH). Both are resolved because Crew's gate launcher execs `pi` by absolute path, and the not-found message names whichever half is absent. |
| 4 handshake | Done — `PROTOCOL_VERSION_PI`, its own literal, integer `1`, captured off pi-acp 0.0.33's wire. |
| 5 auth declaration | Done — `own_credential_file`, `~/.pi/agent/auth.json` on the floor with `PI_CODING_AGENT_DIR` re-anchored (it moves the whole agent directory, so the default final-segment spelling is right), that leaf spared for its own child, not retired by a host logout, and a remedy that names an action without asserting a state. Verified on disk: a key planted in that file under a scratch `PI_CODING_AGENT_DIR` is what `pi auth check --credentials` reports back. |
| 6 install probe | Done — `_probe_pi` names whichever of `pi-acp` and `pi` is absent, with the one `npm i -g` that installs both, and reads `restart_required` from BOTH spawn-path caches. |
| 7 selectability | Selectable. `NOT_SHIPPED_SELECTABLE` stays empty. |
| routing | Done, by a NEW mechanism — `VERIFIED_GATE_EXTENSION`. pi runs every tool call unasked by design, and pi-acp sends `session/request_permission` only when an extension inside pi raises a confirm dialog. So Crew ships a pi extension (`agent_sdk/gate_extensions/pi/kiro_crew_tool_gate.ts`, package data) that intercepts every `tool_call` event and raises that dialog with the tool call written into the message as a JSON envelope; a launcher in the sandbox run directory execs the resolved `pi` with `--extension <that file>`, and the adapter is told to run the launcher in place of `pi` through its own `PI_ACP_PI_COMMAND`. The file the launcher names is a sealed copy: the packaged bytes are checked against a digest pinned in the driver at every spawn (over their LF form — a Windows checkout rewrites the text file CRLF, and a raw-bytes digest would refuse every session there; the checkout is pinned LF in `.gitattributes` as well) and written read-only into the sandbox run directory, so a package file rewritten on a source install is refused rather than loaded. Before the first prompt, that exact launcher is run with the adapter's own arguments, asked `get_commands`, and the probe command must be present AND sourced from that copy; the session is refused otherwise. On the client side the dispatch parser reads the envelope back out of the permission frame — only for a session running the extension, and only under the per-session nonce that session put in the pi process's environment for the extension to echo, because on every harness a permission frame's `rawInput` is the model's own tool arguments — so the gate judges the real tool name, kind and arguments rather than a dialog titled "confirm". Both artifacts must live under the real sandbox run directory: the sandbox's temp-dir fallback is a directory any process of the same user can write, so a spawn that would land there is refused rather than gated from a rewritable file. The read-back compares the probe's source path and the sealed copy as the same file (realpath, case-normalized), because pi reports the path in its own spelling. An oversize argument is bounded value by value so `path` always survives; a shell command and a document body (`write`'s `content`, `edit`'s two halves) are never bounded — the deny rules read the command's text verbatim, and the host skips body keys in its command-line scan only while the arguments are intact — so they are forwarded whole, and only an envelope too large to carry at all (200k chars) is refused, never cut. |
| residual | The read-back establishes that the extension LOADED, not that the adapter forwards its dialog per call — the frame corpus carries that observation, on pi-acp 0.0.33 / pi 0.85.1, and a dispatch-side tripwire guards it in band: a `tool_call_update` that reaches `completed` for an id no envelope named kills the harness and fails the turn, and a `completed` update for a call the host DENIED kills it too, so the three links read off the adapter's and harness's source rather than the read-back (`PI_ACP_PI_COMMAND` honoured; dialogs forwarded; the extension's block honoured) each cost one call when they break, never a silent session. The adapter version is named against the one the contract was observed on, once per process. The extension is Crew's code running inside a third-party process with that process's permissions: a new trust boundary, stated in the `Routing` docstring rather than assumed. And an operator extension that BLOCKS a call before Crew's asks is honoured, not overridden — a denial there is theirs. |
| 8 live spill | Reached for the frame corpus and the read-back chain (`test_acp_pi_backend.py` drives the real launcher against the real `pi` and requires the registry to name Crew's file, and refuses a copy of the same file elsewhere). A turn through the gateway itself was NOT reachable on the recording host, whose kernel refuses user namespaces: the sandbox floor refuses every enforced harness there, pi included, exactly as designed. |

Two things this run produced that the checklist did not ask for. The first is the
MCP verdict, which is the reason the array membership matters: pi-acp ACCEPTS the
`session/new` `mcpServers` array without error and never hands it to the agent —
its `initialize` advertises `mcpCapabilities` of `http: false` and `sse: false`, and
a stdio server in the array produced no error and no tool. A harness that refuses
the array is easy; one that accepts it and does nothing is the state that makes a
dashboard report tools as mounted on a session where none can be called. It is
recorded in `providers/mirrors/registry.py` as a `no-channel` projection naming that reason,
and it is why the extension mechanism — not the MCP array — is the channel anything
of Crew's reaches pi through today.

The second is the shape of the routing itself. OpenCode's read-back proved a
SETTING was in force. Pi has no setting, so what is read back is whether Crew's own
CODE loaded, from Crew's own file — and the source check is not decoration: pi loads
extensions from the operator's own directories too, and one of theirs registering
the probe name would otherwise make an absent gate read as present. Expect this
too: a harness that offers less than the ones before it does not fit a weaker
version of an existing member, it needs a member that says what is actually
established.

## Worked example: goose

The run to read for what a MEASUREMENT overturning a written premise looks like, and
for the difference between a hazard at the start of a session and one at its restore.
It is in `ACP_BACKENDS_KNOWN` and it IS selectable.

| Stage | State |
|---|---|
| 1 vocabulary | Done — `ACP_BACKEND_GOOSE`, in `ACP_BACKENDS_KNOWN`, `PROVIDER_LABEL_GOOSE`, policy name mapped. |
| 2 capability sets | Decided for every set. In the session MCP array, won by a ROUND TRIP rather than by an advertisement. Out of steer, both compaction sets, the internal sandbox, member dispatch and session sharing. In NEITHER load-workaround set, because it serves `session/load` and rejects `session/resume` — the exact inverse of the harness onboarded before it. |
| 3 spawn path | Done — one binary and one subcommand, `goose acp`, resolved override → mise → PATH. No adapter package and no Node floor. The argv also names `--with-builtin developer`, because supplying `mcpServers` REPLACES this harness's configured extensions. |
| 4 handshake | Done — integer `1`, captured off its own wire. |
| 5 auth declaration | Done — `own_credential_file`, one leaf (`~/.config/goose/secrets.yaml`), `XDG_CONFIG_HOME` re-anchored, `adapter_own_leaves` non-empty. The CONFIG home rather than the data home, which is the inverse of the sibling single-binary harness's choice and the same rule applied: name the home the secret lives under, and no other. |
| 6 install probe | Done — no probe of its own; `_probe_self_served` reads this harness's record, which names `goose` and the harness's own installer, with `restart_required` from the spawn path's own cache. |
| 7 selectability | **Selectable**, on a routing that is verified rather than declared. |
| routing | `VERIFIED_SEEDED_SETTINGS`, and the cheapest instance of it: the seed is one environment variable and the read-back is a field on the response that opens the session. |
| 8 live spill | Reached. A live turn, and a corpus live for ALL SEVEN required classes — the first onboarding here to synthesize nothing, because this harness emitted a `session/request_permission` frame for both a builtin tool and one of Crew's own MCP tools. |
| verified range | goose **1.50.x** (1.50.1 recorded). Three wire facts of that release, not spec guarantees, each with its own failure direction: the mode read-back (`modes.currentModeId`) and the tool identity channel (`_meta.goose.toolCall`) fail **closed** when absent — refused sessions, refused approvals; the mid-session `current_mode_update` emission the mode tripwire rests on fails **open** — a release that stops emitting it narrows enforcement back to the open/restore read-back with nothing announcing it. All three are pinned by the corpus; re-verify all three, and re-capture the mode move deliberately, before raising the range. A session on a release outside it is named at the handshake (`_note_goose_version`, off `agentInfo.version`), once per process — the one signal before the first prompt that the open-failing fact may not hold. |

Three things this run produced that the checklist did not ask for.

**The first is a premise that measurement overturned.** The prior research on this
harness recorded that it establishes its asking route only AFTER the session exists,
leaving an ungated interval at session start, and concluded that Crew must therefore
withhold its MCP tools rather than create one. Driven against the shipped binary, that
is not what happens: the mode is read from the child's ENVIRONMENT at spawn and above
the harness's own config file, so `session/new` returns with
`modes.currentModeId` already reporting the required mode, before a prompt is sent.
There is no interval in which the session is live and the route is not. The tools are
therefore delivered rather than withheld, and the routing joins an existing vocabulary
member instead of needing a new one. Re-measure an inherited premise before you
inherit the design it justified — this one cost the previous attempt its whole tool
surface.

**The second is the hazard that premise was pointing at, in its real place.** The
environment seed governs a session this harness CREATES. It does not govern one it
RESTORES: `session/load` returns the mode the session was last left in, and
`session/set_mode` ACCEPTS the auto-approving mode on a live session. So a session
moved to that mode and later resumed comes back permissive with the seed still in the
child's environment, and nothing about the spawn would say so. The read-back therefore
runs on the LOAD response as well as the new one, which is one extra call site and no
new mechanism. When a harness owns its own sessions, ask what a RESTORED one carries
that a fresh one does not — a guarantee established at spawn is not automatically a
guarantee at resume.

**The third is a gate this run needed and the corpus did not have.** Reviewing the new
fixtures by hand against the README's redaction rules surfaced that the shipped corpus
carries three classes of recording-host data across nineteen files from every prior
harness: a scratch run id, request and run uuids, and the harness's own command
inventory. None is a credential, which is why every scrubber and scanner passes over
them — the class that gets through is an ENUMERATION, because it reads as ordinary
product data while describing what the recording host HAS.
The ratchet that closes it is eleven marker patterns over every fixture, shrink-only,
and baselined per FILE and per MARKER CLASS so a new fixture cannot inherit an
exemption and an old one cannot acquire a second, with a stale entry itself a failure
because a permission for an absent marker is a permission for the next one. It is a
change to the SHARED corpus rather than to any harness, so it ships on its own branch
with its own revert path and is not counted below.

The lesson for a fifth onboarding is the order rather than the gate: review the new
fixtures by hand BEFORE writing the corpus README, because that review is what finds
whatever the scrubbers cannot name.

### What a fourth harness cost

Thirty-one files, all of them this harness's own: the corpus gate the fixture review
turned up is a change to the shared corpus and ships separately, so it is not counted
here. Thirty-one against thirty-three for the first single-binary harness and
thirty-eight and forty-three for the two adapters. The
reduction is not efficiency; it is two costs this harness did not pay. It needed no
second component, so the install probe names one thing and there is no gate extension
to ship into a foreign process. And it serves `session/load`, so no membership keys a
restore workaround.

Read that margin as small on purpose. Four of the thirty-one are files a review found
rather than the checklist: the harness had been added to three capability sets and one
mirror table whose CONSUMERS were never wired, so it was declared to deliver Crew's
tools, to own its sessions and to take its model over a config option while doing none
of the three. A membership set makes the decision cheap to record and does nothing to
make it true, and every one of those four gaps was contradicted by prose written in the
same change. So the honest reading of a falling file count is that the seam has moved
the cost from writing branches to CHECKING that a declaration has a reader -- and the
checking is not yet mechanical.

What remains is the irreducible part, and it is worth naming because it is what a
fifth harness will pay too: one column in each of nine bucket tables, one frame
corpus, one auth declaration, one install probe, one mirror class, and the three
per-backend sites in `acp/client.py` that every harness has extended — the spawn arm,
the spawn label and the stderr label. Those three are the only recurring edit points
left that a membership set does not already absorb.
