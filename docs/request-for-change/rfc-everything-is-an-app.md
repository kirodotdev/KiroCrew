---
title: Everything is an App — the core/app boundary and what makes a surface replaceable
status: draft
author: zezhexu
created: 2026-08-18
last-audited: 2026-08-18
audited-at: e6b06685e
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: Everything is an App — the core/app boundary and what makes a surface replaceable

- Status: draft — nothing implemented. Every phase is a proposal.
- Author: zezhexu
- Created: 2026-08-18
- Measured at: `e6b06685e`. Code line numbers below were verified against that
  commit. Quotations from `TENETS.md` and
  [`../architecture/overview.md`](../architecture/overview.md) are from this
  branch, which is one docs-only commit ahead of it.
- Related: [`rfc-amend-tenets-everything-is-an-app.md`](rfc-amend-tenets-everything-is-an-app.md) (owns the
  `TENETS.md` edit this document's boundary serves),
  [`rfc-federated-app-platform.md`](rfc-federated-app-platform.md) (how
  an app's UI loads), [`rfc-navigation-placement-seam.md`](rfc-navigation-placement-seam.md)
  (where an app may sit in the rail),
  [`rfc-app-sandbox-isolation.md`](rfc-app-sandbox-isolation.md) (what an app is
  authorized to do), [`rfc-channel-plugin-architecture.md`](rfc-channel-plugin-architecture.md)
  (the same argument applied to channels)

## 1. Summary

Adopt one rule for deciding which side of the core/app line a surface belongs on:
**a surface that renders or interprets is built as an app; the core keeps the
trust boundary and the state every app shares.** Where a surface cannot be built
that way, the missing seam is the defect to file, and building it into the core
instead is the thing this rule forbids.

The rule is a statement about **how the core is built**, not a grant of new
territory to third parties. A surface qualifies when it stands on app-facing
seams and can be replaced whole. Who is permitted to replace it is a separate,
trust-graded question that [`rfc-navigation-placement-seam.md`](rfc-navigation-placement-seam.md)
and [`rfc-app-sandbox-isolation.md`](rfc-app-sandbox-isolation.md) already own.

Two things make this worth writing down rather than asserting once. First, the
argument it replaces is unwinnable: "is this core?" is a matter of taste, and as
more people contribute there is no evidence anyone can bring to it. "Why can this
not be an app?" is decidable against the code. Second, the decomposition pays for
itself in findings before it pays for itself in flexibility — §2.3 lists eleven
manifest fields that are declared and read by nothing, found by inventory rather
than by usage, and one of them is named `jobFamilies`.

## 2. Motivation

### 2.1 The boundary is undeclared, so it is settled by whoever writes first

There is no statement anywhere in the repository of what belongs in the core. The
closest thing is a tier table in
[`rfc-federated-app-platform.md`](rfc-federated-app-platform.md) §5.2, which lists
"Chat, Overview, Settings" as **Built-in**, described as part of the core with
"no dynamic loading", without a test for why those three and not others. In
practice the boundary is wherever the last contributor put a file, and a reviewer
objecting has only taste to object with.

### 2.2 The concrete case: there is no overview surface to customize

The motivating problem is that an overview or summary view wants to be built
differently for different job families, and a single product owner cannot satisfy
every group. Measured at `e6b06685e`, the situation is worse than "hard to
customize":

- **There is no overview surface.** `/overview` is a redirect to
  `/settings?tab=overview` (`website/src/App.tsx:2712`). Overview is a settings
  tab reached through `website/src/pages/settings/OverviewPanel.tsx:1-4`.
- **It is one fixed layout for everyone.** The six stat tiles are a hardcoded
  literal with no conditional (`website/src/pages/OverviewPage.tsx:193-198`).
  There is no per-user, per-role, or per-workspace variation on it or on any
  sibling summary surface. The four variation mechanisms that exist are all
  install-global: an edition registry, a per-provider capability flag, a
  self-hiding tunnel card, and one global config flag.
- **A seam for this already half-exists and apps cannot reach it.**
  `registerOverviewStatCards` is documented edition seam 7
  (`website/docs/extension-seams.md:26`), populated at module load, explicitly
  non-reactive (`website/docs/extension-seams.md:371`), and **empty in the stock
  build** (`website/src/pages/overviewStatCards.tsx:32`). It composes cards into a
  page rather than letting anyone own the page, and it is build-time, so an
  installed app cannot contribute to it.
- **Job-family overviews are already being built — inside apps.** `ops-mission-control`
  ships `HandoverPanel.tsx` (348 lines) and a 1,403-line page;
  `issue-radar` ships `views/OverviewView.tsx` (344 lines). The pattern this RFC
  proposes is what contributors already do when the core surface does not fit.
  The core has no per-group overview at all.

So the customization problem is not a fight over one page's layout. The page that
would be customized does not exist as a surface, and the one seam pointed at it is
the wrong shape and closed to apps.

### 2.3 Decomposition finds defects before it delivers flexibility

An inventory of the manifest for fields with **no reader** — grepping callers, not
definitions — returns eleven, at `e6b06685e`:

| Field | Declared | Status |
|---|---|---|
| `jobFamilies` | `src/kiro_crew/apps/manifest.py:1010` | Parsed, serialized, echoed to the client. No consumer. |
| `sops` | `manifest.py:976` | `bridges.register_app` (`bridges.py:2324-2410`) registers MCP servers, agents, skills, crons. There is no `_register_sops`. Declaring `sops` copies nothing. |
| `backend.routes` | `manifest.py:386` | No reader. `apps/routes.py:2336` still carries a comment describing it. The prefix is hardcoded at `apps/route_registry.py:122`. |
| `permissions.mcpTools` | `manifest.py:442` | Declarative only. `check_tool_permission` (`apps/permissions.py:71`) has no non-test caller. |
| `permissions.network` | `manifest.py:444` | Not enforced. An app makes outbound calls regardless. |
| `permissions.memory` | `manifest.py:445` | Not enforced. There is no `ctx.memory` on `AppContext` (`apps/context.py:51-67`). |
| `setup.configSchema` | `manifest.py:515` | Parsed, serialized, no reader. |
| `platform.arch` | `manifest.py:712` | No reader. Only `os` is gated (`manifest.py:718-763`). |
| `platform.clientInstall.postInstall` | `manifest.py:664` | Parsed, serialized, no reader. |
| `ui.pages[].iconInactiveUrl` | `manifest.py:247` | No reader outside serialization. |
| `ui.pages[].mountFunction` | `manifest.py:249` | No reader outside serialization. |

The whole of `src/kiro_crew/apps/permissions.py` is dead in production:
`validate_permissions` (`:34`), `check_tool_permission` (`:71`) and
`format_permissions_summary` (`:82`) have zero non-test callers, so the
install-time permission warnings and the permission summary are never shown.
`governance.register_matcher` (`platform/governance.py:599`) and
`register_scope` (`:1119`) likewise have zero call sites repository-wide.
Separately, `lifecycle`, `resources`, `openCommand` and `heroImage*` are
documented as first-class in
[`../app-kit/manifest-reference.md`](../app-kit/manifest-reference.md) but are not
`AppManifest` fields at all — they land in `extra` and are read as raw dicts with
no validation and no traversal check.

A field that is parsed and ignored is the failure mode the manifest's own
"Forward Compatibility" section cannot detect, because it only promises that
*unknown* fields survive a round trip (`../app-kit/manifest-reference.md:499`).
`ui.sidebar.section` and `ui.sidebar.order` are the case
[`rfc-navigation-placement-seam.md`](rfc-navigation-placement-seam.md) already
documents. None of these were found by an app hitting them. They were found by
looking.

### 2.4 The boundary that exists is agent-scoped, and does not hold against an app

This is the finding that most changes what this RFC can safely propose.

The five controls a core keeps — the PreToolUse gate, the governance ceiling, the
keystone sensitive-path list, app admission, and the SEL audit log — are correctly
built **against a prompt-injected agent operating through the tool surface**. They
are not built against app code, because app code is not on the other side of
them:

> `src/kiro_crew/apps/module_loader.py:34-39` — "The app permission system only
> gates the SDK tool surface passed to the app context — it does NOT restrict
> `import`, filesystem, network, or access to in-memory credentials. Installing an
> app is therefore equivalent to granting it full gateway-process privileges."

Consequences, each grep-verified rather than demonstrated:

- The keystone list is a module-level Python list mutated at import
  (`src/kiro_crew/security.py:4436`), not frozen. In-process code can read and
  write the keystone files directly, `security_policy.json` included.
- The SEL chain's integrity rests on the HMAC key living outside the log
  directory (`src/kiro_crew/sel.py:58-63`). That defeats an actor with
  log-directory write access, which is narrower access than an app has.
- Audit failure never changes a decision, by explicit policy
  (`apps/execution.py:459-460`, `admission.py:154-156`), so suppressing the record
  is free.
- App admission (`apps/admission.py:200`) is the one control that acts while an
  app is still inert, and an **absent** policy file admits, documented as accepted
  risk at `apps/admission.py:25-30`.

Meanwhile the *declarative* surface is strictly add-only. An app cannot take a
position in any core flow: `HookManager` is constructed solely from the `hooks`
section of `config.json` (`src/kiro_crew/hooks.py:931`, callers at
`cli_server.py:1507`, `slack/gateway.py:653`) and has no registration path; the
app `EventBus` is publish-only, with no `subscribe` anywhere in
`apps/event_bus.py`; notification **routing** transports register through
`DashboardState.register_channel_transport` (`dashboard/state.py:3200`) whose only
callers are first-party gateways; HTTP routes are confined to
`/api/apps/<own-name>/*` by a hardcoded prefix (`apps/route_registry.py:122`).

So the current state is inverted from the intuition. The declarative surface is
too narrow to build a real surface on, and the executable surface is unbounded.
**Moving product surface across a line that is not enforced does not make the
product safer or more modular; it just moves code.** Every phase below is ordered
around that fact.

### 2.5 What the tenet has to be, given the above

A single sentence cannot carry both halves, so the rule splits:

1. **A surface is built as an app when it renders or interprets.** The test is
   whether it stands on app-facing seams and can be replaced whole. This is a
   constraint on us, checkable in review.
2. **Who may replace a given surface is graded by trust, not by function.**
   First-party code decomposed into an app is the same trust tier as the core it
   came from. A third party replacing a core surface requires an authorization
   model that does not exist yet.

Keeping these separate is what resolves the conflict in §6.

## 3. Goals

- State the core/app boundary once, in a form a reviewer can apply to a PR without
  appealing to taste.
- Make "why can this not be an app?" answerable with evidence, and make the
  answers accumulate into a list that only shrinks.
- Let a group of users get an overview surface built for their work, by replacing
  the surface rather than by negotiating its contents.
- Keep the tenet from being usable as a polite refusal: an app must be able to do
  what the surface it replaces did.
- Keep the tenet from being usable to argue the trust boundary into a plugin.

## 4. Non-goals

- **Rail placement.** Which groups an app may occupy, `order` semantics, the
  `APPS_NAV_LIMIT` interaction, and the app-label i18n path belong to
  [`rfc-navigation-placement-seam.md`](rfc-navigation-placement-seam.md).
- **The loading model.** Import maps, vendored ESM shims, `AppHost`, and the
  `@kirocrew/app-sdk` surface belong to
  [`rfc-federated-app-platform.md`](rfc-federated-app-platform.md).
- **The authorization model.** `permissions` semantics, `owner_app`, namespace
  isolation, and quotas belong to
  [`rfc-app-sandbox-isolation.md`](rfc-app-sandbox-isolation.md). This RFC
  consumes that work; it does not redesign it.
- **Channels.** [`rfc-channel-plugin-architecture.md`](rfc-channel-plugin-architecture.md)
  applies the same argument to messaging and owns the session address model.
- **Making Chat an app.** Chat is the interpreter of the session model, which is
  core state, and the surface is roughly 24,500 lines across `ChatPage.tsx`,
  `ChatSidebar.tsx` and `pages/chat/`. It is neither the right first case nor
  obviously ever the right case.
- **Per-widget composition.** Replacement is whole-surface (§5.2).

## 5. Design

### 5.1 What the core keeps

The core keeps the trust boundary and the state every app shares. Concretely:
sessions and transcripts, memory and lessons, approvals and the PreToolUse gate,
the governance ceiling, app admission, the SEL audit log, and the event bus and
identity. The argument is the same in every row — a second implementation means a
second truth, and a control whose value is being unavoidable cannot have an
alternative.

This list describes an **intended** boundary. §2.4 establishes that it is enforced
against the agent and not against app code, so the honest statement of today's
position is: *these are the things no app may be asked to supply, and the
mechanism that would stop one from supplying them anyway does not exist yet.*
Phase 3 is where that stops being true, and nothing in this RFC treats it as
already true.

### 5.2 Replacement is whole-surface

An app owns a named surface and everything that appears on it. Several apps each
contributing a card into one page requires layout negotiation between parties who
cannot see each other, and produces a page nobody owns. `registerOverviewStatCards`
is the card-composition shape, and it is instructive that after being built it has
no registrants in the stock build.

Whole-surface replacement also makes the contract small enough to state: one
surface, one owner, one default.

### 5.3 The test a surface has to pass

A surface is app-shaped when all four hold. Each is checkable:

1. Everything it renders comes through published app-facing surface — declared
   manifest fields with live readers, `permissions.api` allowlisted endpoints, the
   app SDK.
2. It reads no private core wiring. Of the four endpoints the overview page calls
   today, three are private core wiring: `/api/status` returns `yolo`,
   `owner_id_hash`, `update_progress` and host facts, and **mutates on read** by
   firing a background update check (`dashboard/handlers_system.py:168-172`);
   `/api/tunnel/status` and `/api/memory/settings` are gateway internals. A
   surface that needs these needs a narrower public endpoint first, and that is
   the defect to file.
3. Its label and every user-facing string it renders can be translated. Today an
   app's rail label falls back to raw `page.label` outside a hardcoded first-party
   table, which is the hole `rfc-navigation-placement-seam.md` N3 closes.
4. Removing it degrades the product without breaking it.

### 5.4 The default set is a curated opinion

Built-in apps are defaults, and the set we ship is replaceable wholesale rather
than the definition of the product. Because users take defaults, this moves the
argument about what is central from an architecture argument to a product
argument, which is the point. It does not make the argument disappear.

### 5.5 Selecting a surface per group

`jobFamilies` already exists as a manifest field with no reader
(`apps/manifest.py:1010`). Whether it is the right selector is open (§10), but the
shape it implies — an app declares which job families it serves, and the host
resolves one owner per surface — is the mechanism this RFC needs, and it was
declared before it was needed.

## 6. Relationship to the RFCs this contradicts

Three documents on main disagree with the rule as stated. Two are resolved by the
split in §2.5; one is amended.

**`rfc-navigation-placement-seam.md` grades by who wrote the code.** It keeps
`Main` and `Bottom` closed to apps because they are "the two pinned regions users
read as 'the product itself'", lists "letting an app replace a core surface" as a
non-goal, and distinguishes an edition (first-party, build-time) from an app
(third-party, runtime). This RFC grades by what a thing does. **Both stand,
because they answer different questions.** This RFC constrains how a surface is
*built*; that RFC governs who may *place* one. A first-party surface decomposed
into an app is the same trust tier as the core it came from and may occupy `Main`;
a third-party app is clamped exactly as N1 specifies. Neither document needs to
change.

**`rfc-app-sandbox-isolation.md` documents the absent boundary.** Dashboard tokens
bypass app restrictions, enforcement is opt-in per gateway version, and apps run
in-process with full privileges. That is not a contradiction, it is the
prerequisite: this RFC's Phase 3 is gated on that RFC's Phases 1–3, and its own
table shows only Phase 1 rows done.

**`rfc-federated-app-platform.md` §5.2 is amended.** Its Built-in tier —
"Chat, Overview, Settings", core-resident with "no dynamic loading" — is a
list without a test. This RFC supplies the test (§5.3) and does not preserve the
list. Chat stays core on the §4 reasoning, not on tier membership. The rest of
that RFC, including its loading model and its trust tiers, is untouched.

`rfc-channel-plugin-architecture.md` reaches the same conclusion from the other
end and should be read as a sibling: its §3.2 has the core owning turn
interpretation on purpose, because "a plugin cannot skip `channel_inbound_permitted`
because it never owns that code." That is §5.1 stated for channels.

## 7. Migration plan

Each phase is independently shippable and independently abandonable. Phases 0 and
1 deliver value with no decomposition at all.

### Phase 0 — declare the boundary and ratchet the inventory

Land the boundary in [`../architecture/overview.md`](../architecture/overview.md)
and add a test that records today's declared-but-unread manifest fields as a
frozen list, failing when a new one appears. The list may shrink freely.

The tenet itself is **not** this RFC's to land. The wording, the ordering argument,
and the reading notes that keep the sentence from being misquoted belong to
[`rfc-amend-tenets-everything-is-an-app.md`](rfc-amend-tenets-everything-is-an-app.md), which owns the
`TENETS.md` edit. This RFC supplies the architecture the tenet points at, and the
two are independent: the boundary section and the ratchet test stand on their own
if the tenet is declined, and the tenet stands on its own if these phases are.

- Exit: the architecture doc states the boundary including the §2.4 caveat that it
  is not enforced against app code.
- Exit: a test asserts the set of `_KNOWN_FIELDS` entries with no non-test,
  non-serialization reader equals a recorded list, and fails on any addition.
- Exit: the same test's list is referenced from
  [`../app-kit/manifest-reference.md`](../app-kit/manifest-reference.md) so a
  manifest author can see which documented fields do nothing.

### Phase 1 — subtract what nothing will read

Remove `sops`, `backend.routes`, `setup.configSchema`, `platform.arch`,
`platform.clientInstall.postInstall`, `ui.pages[].iconInactiveUrl`,
`ui.pages[].mountFunction`, the dead `apps/permissions.py` module, and
`governance.register_matcher` / `register_scope`. Give `lifecycle`, `resources`,
`openCommand` and `heroImage*` real dataclass fields with validation, since they
are documented as first-class and currently bypass it. Hand
`permissions.mcpTools` / `network` / `memory` to
[`rfc-app-sandbox-isolation.md`](rfc-app-sandbox-isolation.md) rather than
deleting them, and say so in the manifest reference.

- Exit: every `_KNOWN_FIELDS` entry has a reader, or a comment naming the RFC that
  owns wiring it.
- Exit: no field documented in `manifest-reference.md` is read as a raw `extra`
  dict.
- Exit: removals appear in `CHANGELOG.md`, since a removed manifest field is a
  breaking change for any manifest that declares it, however inert.

### Phase 2 — one surface, one owner: the overview slot

Define a single named overview surface with exactly one owning app, defaulting to
the built-in one, and delete `registerOverviewStatCards` rather than promoting it.
Wire a selector so a group gets the overview app built for it. Ship a second
first-party overview app so the slot has two real occupants and the seam is proven
by use rather than by intent.

- Exit: two first-party overview apps exist, and which one renders is determined
  by declared data rather than by a build.
- Exit: the built-in overview is one of them, installed as the default, and
  removing it leaves the product usable.
- Exit: `registerOverviewStatCards` and its docs row are gone, or the phase
  records why it had to stay.
- Exit: the overview app reads no endpoint on §5.3's private-wiring list; any
  gap is closed by a narrower public endpoint, not by widening `permissions.api`
  to `/api/status`.
- Blocked on: nothing. First-party occupants are the same trust tier as core.

### Phase 3 — third-party replacement

Let an app we did not write own a core surface.

- Exit: an installed third-party app can own the overview slot without holding
  gateway-process privileges.
- Exit: app admission fails closed on an absent policy file, reversing the
  accepted risk at `apps/admission.py:25-30`.
- Blocked on: [`rfc-app-sandbox-isolation.md`](rfc-app-sandbox-isolation.md)
  Phases 1–3. Do not start this phase while §2.4 holds.

## 8. Backward compatibility

Phase 0 is documentation and a test. Phase 1 removes manifest fields, which is
breaking for any manifest that declares them even though none of them does
anything today; the parser must keep accepting an unknown field, which
`AppManifest.from_dict`'s `extra` handling already guarantees
(`apps/manifest.py:1214-1215`), so a stale manifest degrades to a warning rather
than a failed install. Phase 2 changes where the overview renders and must keep
`/settings?tab=overview` resolving. Phase 3 changes no existing app's behavior.

One gap is worth naming because this RFC makes it load-bearing: the compatibility
contract is one-directional. `minKiroCrewVersion` is a floor an app declares about
the gateway, checked only at install and update (`apps/manager.py:281`,
`apps/routes.py:503`, `apps/registry.py:4053`) and never at enable or boot, and it
fails **open** on a malformed value (`apps/version.py:32-33`). The platform
declares no version for its own app-facing surface, so withdrawing a seam carries
no signal in the other direction. Phase 1 removes seams. If that ordering feels
uncomfortable, that discomfort is the argument for a platform-side version, and it
is open question 4.

## 9. Security considerations

The honest summary is in §2.4 and it is the reason Phase 3 exists as a separate,
blocked phase.

- **This RFC must not be read as a reason to pluginize a control.** §5.1 is the
  closed list, and the tenet's own text names the trust boundary as what stays.
- **Decomposition does not reduce privilege today.** First-party code moved from
  core into a built-in app runs in the same process with the same privileges. The
  gain in Phases 0–2 is modularity and replaceability, not containment, and
  claiming otherwise would be false.
- **Third-party replacement genuinely increases exposure** and is why Phase 3 is
  gated. A third party owning a surface users read as the product is a
  supply-chain question first and a UI question second.
- Two pre-existing weaknesses are relevant enough to name: a governance policy
  cannot deny by omission, since an absent scope key permits
  (`platform/governance.py:955-963`), and policy signature verification is off
  when the trust root is absent or unreadable (`platform/governance.py:1803-1807`).
  Neither is created by this RFC; both bound what Phase 3 can promise.

## 10. Alternatives considered

**Leave the boundary implicit.** Costs nothing to write and settles nothing. The
overview problem stays a taste argument, and §2.3's eleven dead fields stay
invisible because nothing forces the inventory.

**Declare the tenet without the phases.** This is what a slogan looks like: it
would be quoted to justify pluginizing the governance ceiling within a release,
because the sentence alone does not carry §5.1 or §2.4.

**Adopt the plugin-kernel model, where the core itself is plugins.** This is the
DeepSeek Harness shape, and `rfc-navigation-placement-seam.md` already rejected it
for this codebase. The reason to restate the rejection here: it converts "is this
core?" into "is this seam core?", which is a harder question with a worse failure
mode, because every seam is a permanent compatibility promise and a missing one
blocks a whole class of app. The rule in §1 keeps the question decidable without
making the plugin API the product.

**Fix the overview page instead.** Add config-driven card visibility and let each
group toggle tiles. This is the card-composition shape from §5.2, it does not
generalize past one page, and it leaves the next surface to have the same
argument from scratch.

**Grade everything by trust rather than by function** — that is, keep the core as
whatever first-party code we choose to compile in. This is the status quo, and it
is exactly the position that cannot be defended in review, because "first-party"
is not a property of the surface.

## 11. Open questions

1. **Does Settings become an app?** It is the largest surface after chat
   (`website/src/pages/settings/` is 16,519 lines) and it is where the keystone
   toggles live, so it may be the clearest case of a surface that renders and
   still cannot leave. Answering it tests whether §5.3 is a real test or a
   rationalization.
2. **Is `jobFamilies` the right selector?** A per-user or per-workspace choice may
   be better than a manifest-declared one, and the field's own lack of a reader
   means nothing depends on the answer yet.
3. **Who curates the default set, and by what process?** §5.4 moves this argument
   into product; it does not say who wins it. `GOVERNANCE.md` covers RFC
   acceptance, not bundle curation.
4. **Does the platform need to declare a version for its app-facing surface?**
   Phase 1 removes seams with no signal available to an app that depended on one.
5. **Does §5.2's whole-surface rule survive a surface that genuinely wants
   contributions**, such as a notification feed aggregating several apps? The
   event bus already carries per-app events; a feed may be the counterexample that
   forces a second composition shape.
