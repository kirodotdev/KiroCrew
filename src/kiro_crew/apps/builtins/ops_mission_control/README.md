# Ops Mission Control

An autonomous ops first responder, shipped as a built-in Kiro Crew app. It polls your
signal providers, claims what is firing, investigates it in a chat session you can watch
and reply to, matches it against a knowledge ledger that gets better the longer you run
it, and proposes a fix.

**Read-only by default.** Nothing is written to any provider until you say so, per
signal pattern. See "Autonomy" below.

## Quick start

1. Enable the app (App Store → Discover → Ops Mission Control).
2. **Settings → Providers** → turn on a source. AWS CloudWatch needs no credential — it
   uses your ambient AWS profile chain and stores no key.
3. Wait one heartbeat, or press **Check now**.

If nothing happens, the board and the dispatch response both say why. "Quiet" and
"nothing is watching" are different states and the app never conflates them.

## The four tabs

| Tab | Answers |
|---|---|
| **Board** | What is being worked right now, its status, and the live investigation chat |
| **Signals** | Per-source health, what the *last poll actually returned* (including errors), firing signals not yet claimed, and signals a human **parked at the provider** (an Alertmanager silence or inhibition) — listed with who parked them, because otherwise "the app ignored my alarm" and "someone silenced it" look identical. A source that failed its last poll is shown as failed, never as quiet — the app will not resolve work on a signal it could not read |
| **Handover** | What an incoming responder needs: what is waiting on a person, what stopped without a diagnosis, and what keeps recurring |
| **Settings** | Providers, credentials, autonomy, Slack, instance role |

## Autonomy

Three modes, and `act` is deliberately hard to reach:

| Mode | What it does |
|---|---|
| `observe` | Reads and investigates. Writes nothing anywhere. **Default.** |
| `propose` | Drafts the acknowledge/resolve/comment/silence and asks first. |
| `act` | Executes — but only for signal patterns you allowlisted with a rule. |

Four verbs are available to a sink: `ack`, `comment`, `silence`, `resolve`. **`silence`
is the one to grant first** — it always carries a bounded expiry (4h default, 24h
ceiling), so a wrong silence undoes itself, where a wrong `resolve` hides a live fault
until somebody notices. `comment` is next safest: append-only and attributed.

`act` requires **both** the app-level mode *and* a rule matching that specific signal. A
rule must name a source plus a resource glob or label match, so "act on everything" is
not expressible. Every execution is audited.

This is a deliberate divergence from a common shortcut: auto-resolving alert types you
believe are always benign. A team that built those alerts itself can reason about which
are safe to close unread; a stranger's first install cannot.

## Credentials

- **AWS** uses your ambient credential chain. No key is ever stored.
- **Other providers'** tokens go to a keystone file the agent cannot read or overwrite
  (it is on Kiro Crew's sensitive-path floor). The API never returns a stored secret —
  only whether a field is set.
- That file lives outside the app's folder, which is what makes it unreachable to the
  agent. It therefore **survives uninstalling the app** — use Revoke in Settings first
  if you want a credential gone.

## Slack

Mirrors incidents to a channel as a live board: one message per incident whose state
updates in place, diagnosis in the thread. It uses the Slack connection **Kiro Crew
already has** and stores no token of its own — so if Slack is not set up for Kiro Crew,
this channel is unavailable and Settings says so.

**Replies reach the investigation.** Once an incident has a chat slot, its board thread is
registered with Kiro Crew's session map, so answering in the thread steers the running
agent. `POST /incident/transition` reports `slack_thread_replyable` so you can tell
whether that link is live rather than assuming it — before this the ts was recorded only
on the app's own record, and a reply resolved to no session and was dropped in silence.

## The knowledge ledger

Each investigation that finds a reusable fix records it, and a repeat failure starts from
what you already know instead of re-deriving it. Matching uses two keys:

- **The provider's own identity** (`provider_key`) when it publishes one — an
  Alertmanager fingerprint, a Datadog monitor id, a CloudWatch alarm name. A hit here is
  *exact*: the system that owns the grouping says this is the same failure.
- **A shape fingerprint** otherwise — a normalized signal shape that strips timestamps,
  ids, and bare numbers so a recurrence matches its ancestor.

Exact matches rank above shape matches, and the investigation brief says which kind it
found. That distinction matters: because the shape hash strips every bare number, a
"4xx rate above 5" alarm and a "5xx rate above 1" alarm on one resource hash identically,
so a shape match means *looks like this*, not *is this*.

Entries carry `confidence` and `trust`, and they carry a **track record**. The fast path —
where the agent proposes a remembered fix directly instead of re-deriving it — needs all
four of `verified`, `high`, at least two uses, and no recorded failure. Anything weaker is
still handed to the agent in full, just framed as a hypothesis to test. A knowledge base
that overstates itself does harm.

`verified` and `high` alone were not enough because both are hand-settable: an entry could
claim them having never been applied to anything. And the record moves DOWN as well as up —
when an action this app took is followed by the signal still firing, every entry it cited
gets a `miss_count`, the nightly hygiene pass demotes one confidence step, and the board
and the handover digest both say the fix was tried and did not hold. A fix that failed is
never deleted (it may still work sometimes) but it stops being presented as the answer.

That downward path only means anything because actions are **verified**: after this app
resolves or silences something, the next heartbeat re-reads the signal and records whether
it actually cleared. A provider's 2xx means "your request arrived", not "it worked" — and a
recheck against a source that did not answer records "could not check", never "it worked".

## Extending it: the companion contract

Internal or bespoke adapters live in a **separate package** you install alongside
Kiro Crew. The public core never imports it and never branches on which edition is
running.

Register an entry point:

```toml
[project.entry-points."kirocrew.ops_providers"]
my-company = "my_pkg.ops:register_adapters"
```

```python
def register_adapters(registry) -> None:
    registry.register_signal_source(MyTicketSource())
    registry.register_action_sink(MyTicketSink())
```

Implement one or more of four narrow Protocols (`backend/providers/base.py`). Each needs
`id` and `display_name` properties plus `configured() -> bool`:

| Protocol | Extra method |
|---|---|
| `SignalSource` | `async poll() -> list[Signal]` |
| `RotationSource` | `async on_shift() -> ShiftStatus` |
| `ActionSink` | `supported_actions() -> frozenset[str]`, `async execute(signal, action, payload) -> ActionResult` |
| `EvidenceSource` | `async gather(signal, budget) -> list[Evidence]` |

Build signals with `Signal.create(source=..., native_id=..., title=..., severity=...,
state=..., resource=..., url=..., labels=...)` so the fingerprint is computed the same
way as for every built-in adapter.

Rules that will not bend:

- **Registration is ADD-only.** An id that already exists is refused and the incumbent
  wins, so auditing what the public core does never requires auditing your package.
- **Every candidate passes the fleet admission policy before its code is imported.** A
  package the policy rejects never runs; each decision is audited.
- **Evidence is redacted for you** at a single chokepoint. Return raw text; the core
  scrubs credentials before anything reaches a model prompt or Slack.
- **Do not police your own authority.** The autonomy gate is resolved before `execute`
  is called.

## Layout

```
app.json                  manifest: crons, permissions, store listing
backend/
  models.py               Signal, Incident, LedgerEntry, the status grammar
  registry.py             ADD-only adapter registry + fan-out
  companion.py            entry-point discovery for out-of-tree packages
  dispatch.py             the cycle: poll -> claim -> ledger-match -> sweep
  store.py                atomic claim index (one incident per signal)
  ledger.py               append-only knowledge ledger + hygiene
  rotation.py             autonomy gate + rotation-driven tier arming
  slot_watch.py           derives "waiting on a person" from the live chat
  handover.py             the shift digest
  slack_out.py            the Slack pin board
  notify_out.py           local desktop notifications (no credential, no inbound URL)
  secrets.py              keystone credential store
  providers/              cloudwatch, pagerduty, datadog, github_issues, webhook, noop
tests/                    unit + contract tests
```

The agent-facing skill and its SOPs are **not** here — they ship in
`src/kiro_crew/builtin_skills/ops-mission-control/`, because a builtin app's own
directory is never copied into the data home. The dashboard UI is in
`website/src/apps/ops-mission-control/`.

Full design rationale and every durable contract:
`docs/system-specs/modules/ops-mission-control.md`.
