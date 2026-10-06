# Dashboard templates

A dashboard page is half of a template. This document is the other half, and the reason
the two are shipped together.

The host renders a card as `{html, data}`. The `html` is inert layout it sanitizes; the
`data` is a flat map of text it binds into that layout by `data-dashboard-field`, setting
each matching element's text. The host is two files, and both are worth reading before
authoring a page: `src/kiro_crew/dashboard/dynamic_cards.py` (`normalize_card`, which
accepts or refuses the card) and `website/src/pages/chat/command-center/dashboardDocument.ts`
(which queries `[data-dashboard-field]` and does the binding). No type checker sees that
binding, because one half is markup. Two tests stand in for one: `parity.py` checks the
page against its contract, and `website/src/test/binderParity.test.ts` pins how far the
real `dashboardDocument` binder reaches (a `style` element is skipped, only body
descendants are filled, the attribute value is trimmed, `FORBID_TAGS` elements are
removed). So
a page reading `settled` whose provider fills `settled_count` renders an empty cell — on a
status board, indefinitely, with nothing red anywhere.

A **template** is therefore four artifacts and one registration, and a change to any one
of them belongs in the same pull request as the others:

| Artifact | Where | What it is |
|---|---|---|
| the page | `src/kiro_crew/dashboard_templates/` | inert html, one `data-dashboard-field` per value, no controls |
| the contract | `src/kiro_crew/dashboard_templates/` | a flat `TypedDict` naming exactly those fields, plus the judgment type and `CONTRACT_VERSION` |
| the provider | `src/kiro_crew/dashboard_templates/` | `build_<slug>`, whose **return type is the contract** |
| the gate | `test/test_dashboard_templates.py` and one test per template | asserts the page and the contract agree |
| the registration | `src/kiro_crew/dashboard_templates/registry.py` | `slug -> (contract, provider, source fold, version)` |

Shared pieces live in `src/kiro_crew/dashboard_templates/__init__.py` (the `Unsaid`
sentinel, the fold readers, the single exit to a card) and
`src/kiro_crew/dashboard_templates/parity.py` (the page reader and the two structural
rules). The authoring procedure is the `dashboard-template` skill, and its
`scaffold.py` emits all four artifacts from one field list so they cannot disagree about
what the fields are.

## Authoring is dev time

A template lands through a pull request. `mypy` over the source tree is blocking and is
what makes the provider's return type mean something; the parity test covers the half
mypy cannot see. The gateway never runs agent-authored fold code and never evaluates an
agent-authored expression. One `parity.py` reader runs at run time:
`filled_fields`, which never raises, and which `card_lifecycle._layout_hides_a_fact`
calls on model-authored html at publish. A binding the host's binder steps over is left
out of its answer by design, so a required field on such an element reads as missing.

At run time a publisher supplies exactly three sentences — `lede`, `you`, `notes` — and
no numbers. Numbers are absent from the publisher's surface deliberately: a publisher
that can type a count can make the page contradict the log it claims to summarise. A live
board once rendered an owner's name and a session id in the cell that must say what a
person should DO, and nothing could refuse it, which is why `you` is gated to values that
read as a phrase at all.

## The three invariants

Each is a test, and each exists because its absence produced a wrong page that nothing
reported.

**1. Parity.** The set of `data-dashboard-field` names in the page equals the set of keys
in the contract, in both directions. Strict equality with no exemption list: a list is a
hiding place, because a field inconvenient to render can be declared, listed, and still
pass every check. A field the page reads and the contract omits is an unowned empty cell;
a key the contract declares and the page never reads is a provider deriving something
nobody sees.

**2. Every number carries its denominator.** `7` on a status page reads as complete
information and is not — a reader cannot tell 7 of 7 from 7 of 700. A count reaches the
page as `N/M`, which is what the `fraction` field kind produces and the only numeric form
the scaffold emits. A hand-written contract may declare a bare `int` where the template
wants the pair rendered apart, and the gate then requires its `<name>_of` sibling.

**3. A value nobody supplied says so, in words.** The host renders a field it was NOT
given as the empty string, and on a page of numbers a blank and a zero are one reading. So
a value that may be unknown is typed `str | Unsaid`, the provider writes the sentinel
explicitly, and one exit function turns it into the words "not said". `mypy` then refuses
both the missing key and a stray `None` from a lookup that found nothing.

`Unsaid` is a single-member enum rather than a string sentinel, and that is load-bearing
in both directions. A string token is forgeable: real content equal to it would be
rewritten as "not said", and a publisher could print what looks like a system marker onto
a status page. Being a single member also lets a type checker narrow `str | Unsaid` to
`str` once the sentinel is ruled out, so every reader says what it means without a cast.

The corollary is
that a provider never reads a fold with a `0` default — the package's readers answer the
sentinel for absent, wrong-typed and unparsable values alike, including `bool`, which
passes an `int` check and would render a flag as `1`.

## How parity is enforced

`parity.py` reads the page with the standard library html parser, not a regular
expression. An attribute scan by regex mis-reads an attribute inside a comment, a value in
the other quote character, and a tag name in upper case — and each of those makes the gate
quietly incomplete, which is worse than a gate that fails: a field the reader missed looks
like a field the page does not use, so the equality assertion passes over a real mismatch.

Every dev-time reader there (`html_fields` and the structural rules) **refuses** rather
than returning a partial answer, for the same reason. A short set is indistinguishable from a template that genuinely uses fewer
fields. A page that binds nothing, a binding with no field name, and a contract that nests
are all errors, not smaller answers.

The registry is allowed to be empty: the machinery ships first and templates arrive with
the work that needs them. An empty registry cannot make the gates read as passing, because
each rule is also asserted against a deliberately broken input in the same run.

## How to add one

1. Write the question the page answers, as one sentence a person would ask.
2. Pick the fold whose answer that sentence needs, from the generated catalogue below
   (`FOLDS.md` beside the skill).
3. Write the field list: `name:str|unsaid` for text, `name:fraction:<total>` for a count, naming the fold key holding its total. Both halves are checked against the chosen fold; a fourth token names the numerator's key when the card field is renamed. There
   is no plain `str` kind — every declared field is read out of the fold, and a fold
   value can always be absent, so a text field is `str | Unsaid` or the provider cannot
   satisfy the contract; `contract_version` and `captured_at` are plain `str` only
   because the scaffold supplies them and the provider writes them. At most 24 fields —
   the host refuses an over-cap card whole, so a page one field too large stops appearing
   rather than degrading.
4. Run the skill's `scaffold.py` with that list. Edit the provider where the fold spells a
   value differently; lay the page out properly.
5. Add the registration line, run `mypy` and the scoped tests, open the pull request
   naming the fold you read.

## The fold catalogue

A template's numbers come from a fold — a durable projection of the append-only crew log,
described in [crew-log-projection.md](crew-log-projection.md). Picking an existing fold
from the generated catalogue is the rule; a new fold is the exception and argues for itself in the pull request
that adds it, because it moves the stored-state version and retires every saved checkpoint
to a cold refold.

The catalogue is **generated from the code** by `scripts/fold_catalogue.py`, which walks
the fold registry and emits two committed artifacts beside the authoring skill: a markdown
section the skill points a reader at, and a JSON document the scaffold reads to validate
`--fold`. For each fold it records the name, whether it is keyed by session or by slot,
the entry types that move it, one sentence saying what it answers, and every field of its
rendered value with that field's Python type.

A hand-written list was the alternative and is the failure being replaced. It is right on
the day it is written: a fold added, renamed, or given a new rendered field leaves it
silently stale, and stale is worse than absent, because an agent that trusts it writes a
provider reading a field no fold produces. `--check` is the gate, and
`test/test_fold_catalogue.py` runs it alongside the generator's own planted-drift
self-test, so "nothing differs" cannot mean "the comparison is broken".

Two honest limits, both visible in the output. Each fold is rendered from its own empty
start state, because that is reachable with no fixtures at all and a fixture per fold
would be the second source of truth this removes. An empty fold leaves some fields
`None`; those rows say `unknown` and nothing else. The generator deliberately does not
infer a type from the entry field of the same name: a rendered field name is owned by no
one entry type, so that lookup answered `status.previous` with `dict` and `status.turn`
with `int` -- confident and wrong, and a reader could not tell which rows to trust. One
wrong row costs more than every missing one. That is not a gap to apologise for either: a
field that is `None` on an empty fold is exactly a field a provider must treat as possibly
absent, so its row is marked optional and the contract types it `... | Unsaid`. The safe
answer and the honest one are the same answer.

The fold names are restated in exactly one place, the generated catalogue, and a test
asserts it against the kernel that owns them — so a fold added or renamed there fails
rather than drifting. `registry.py` keeps no copy of the names; it exports only
`REGISTRY`, `html_path` and `spec_for`.

## The crewmate

The four artifacts are produced by a dedicated agent, `kirocrew-dashboard-author`, whose
built-in prompt is this procedure. Its spec ships as a markdown agent spec beside the
skill: the charter — outputs land as a pull request and never at run time, pick an existing
fold first, every field a writer could omit is `Unsaid`, no percentages, zero controls —
plus the narrowed toolset that makes the charter true against a tool list rather than only
against prose.

`fs_read`, `fs_write` and `execute_bash` are the job. Every omission is deliberate and
recorded in the spec: no session verb (it dispatches nobody, and a session verb is how an
authoring agent grows into a conductor by accident), no work-ledger mount (`work_report`
writes into a parent's record), no dashboard mount (a template is not published at run
time), no `code` (already covered, and governance classes it under `filesystem.write`), no
web tools (every input is on disk or behind `gh`), and no cron, artifact, deploy or
`learn_add` grant. Writing and shell are mounted and never auto-approved, because
`allowedTools` has no argument matching and this agent's whole safety story is that a human
reads its diff before it lands.

The spec is not registered in `OWNED_KIRO_AGENT_FILES`, so the runtime does not install
or rebuild it.

## What a page may not contain

The host's sanitizer strips fourteen of the seventeen outright, so authoring one of those
ships a layout with a hole in it and an author who believes the button exists. The other
three -- `label`, `fieldset`, `option` -- the host keeps, and this package refuses them on
its own ground: a dashboard states facts rather than offering a choice. No `form`, `input`, `button`, `textarea`,
`select`, `option`, `label`, `fieldset`, `script`, `iframe`, `object`, `embed`, `link`,
`meta`, `base`, `template`, `noscript`. That list is `CONTROL_TAGS` in `parity.py`, the
one authoritative copy; the skill body and the crewmate's spec restate it, and a test in
`test_dashboard_templates.py` holds all three to the set so an addition cannot drift.
Separately, and read by its own function `outbound_references`: no `src`, so no images;
no `href` that is not a same-document fragment, matched on the attribute's local name so
an SVG `xlink:href` counts. `style`, `details`/`summary`, inline SVG and CSS are the
template's own, with one limit on the SVG: a binding may not sit inside it. Whether SVG
paints a string depends on the whole ancestor chain and not on the element carrying the
value -- `text` paints, the same `text` under `defs` or `clipPath` paints nothing -- so
the reader refuses the foreign subtree rather than decide it per tag. A value that belongs
over a drawing goes in a sibling `span` positioned above it.

A dashboard states facts and offers no actions. A decision goes through the product's own
question and approval surfaces, which live outside the rendered frame and carry the
identity of the session that owns them. An imitation approval control in a page is a claim
about authority that the page does not have.
