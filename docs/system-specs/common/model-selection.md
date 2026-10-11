# Model selection: never hardcode a model id

Never hardcode a model id (`claude-*`, `opus*`, `sonnet*`, `haiku*`, `gpt-*`,
`fable*`) as a default or a fallback. Accounts differ in entitlement and even
`"auto"` is not served in every partition, so a hardcoded id fails at runtime — and
silently, until the first prompt — for anyone not entitled to it.

This spec covers **choosing** a model before the wire. What happens when a model that
was already chosen stops working mid-session is
[model-fallback.md](../modules/model-fallback.md).

## The default is `"auto"`

`agent.model` defaults to `"auto"` in `config/defaults.json`. Do not replace it with a
concrete model. `"auto"` is validated like any other id and is not assumed usable: a
partition that does not serve it makes it as unusable as any other unentitled id.

## Model-name format

The shared spawn and cron model-name validator accepts an alphanumeric first
character followed by alphanumerics, dots, underscores or hyphens, optionally
ending in one nonempty bracketed qualifier using those same characters.
Empty, unbalanced, nested, repeated or non-trailing qualifiers are rejected;
shell metacharacters remain invalid. The existing field length limit still
applies. This format check does not establish entitlement or alter the separate
`MODEL_ID_RE` grammar or ACP runtime validation.

## Resolve, don't guess

For a model chosen on the caller's behalf — background one-liners, tips, inherited or
cold-start applies — route through
`acp.client.resolve_usable_model(preferred, advertised)`. It answers with a served id,
or `"auto"` only when the backend advertises it, or `""` meaning **inherit the
session's served backend default**. Returning `""` rather than substituting a guess is
the whole point: the wire never receives a model the partition does not serve.

These behaviours of the resolver are worth knowing before writing a call site:

- An **unknown or empty advertised set** means entitlement is unknowable. `"auto"`
  degrades to `""` because it cannot be verified, while a concrete caller-supplied id
  is trusted because there is nothing to check it against.
- A persisted pin can carry a stale `<namespace>::<bare-id>` qualifier while the
  session advertises the bare id. The resolver retries the miss through
  `resolve_pin_spelling` and puts the **advertised** spelling on the wire, not the
  caller's, because the qualified spelling is one the backend never advertised.
- `resolve_pin_spelling` never folds a pin onto a different reasoning-effort
  suffix: a pin's effort half must match the advertised id's.
- A wire site passes the harness it is sending to (`backend=` on
  `resolve_usable_model`, or `resolve_pin_spelling_on` in `acp/runtime_models.py`).
  On an `ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS` harness, a pin the fold could not
  resolve widens to the bare model half, never for a `[1m]` window pin. A caller
  not choosing a wire spelling leaves `backend` empty and keeps the plain fold.

`run_bg_oneliner` adds a one-shot reactive retry on a wire rejection as a backstop.
Treat it as a backstop, not as permission to skip the resolver.

### `""` only inherits a *served* default

`""` promises the session's **served** backend default, and the backend does not
always keep that promise on its own: `session/new` can answer with a
`currentModelId` the account is not entitled to (the classic case is `auto` on a
partition that does not serve it), and the first prompt then fails with "no access
to model". `acp.client.pick_served_default(current, advertised)` closes that gap:
given the backend's current id and its advertised list it returns `""` when the
current id is served (or the list is unknown), otherwise `"auto"` if advertised,
otherwise the first served id. `AcpClient._ensure_served_default` and
`AcpSessionHandle.ensure_served_default` run it on every inherit exit of the
startup model apply and `session/set_model` the pick, correcting only the wire
(`_resolved_model_id`); the session's intent (`_model` as `""`/`"auto"`) is left
alone so the warm-pool re-apply and the slot backfill still read "inherit". The
dashboard carries the corrected id as the slot's `served_model` so the composer
chip names the model a turn will run on instead of `auto`.

KAS sends no `models` object; its list is only the `configOptions` `model`
select, and it defaults a new session to `auto`. The pooled check asks that select
one question: is the session on an `auto` the select does not list? If so it moves
to a listed id. The select never becomes the session's advertised list, so the
picker and the explicit-pick guard are unchanged for KAS, and a concrete current
model is never judged against a list that may be incomplete.

The claude backend has a different gap on the same exits: the model it reports is
not always the model Claude Code runs. claude-agent-acp resolves an inheriting
session's model from `ANTHROPIC_MODEL` or the user's `settings.model` and reports it
as the `model` option's current value. When that value is the setting verbatim, the
adapter does not pass it on and trusts Claude Code to have read the same setting.
After a resume, Claude Code can instead run its own built-in default while the
report still names the settings model. A custom gateway that does not serve that
default then refuses every turn until the user runs `/model`.
`AcpClient._reassert_adapter_resolved_model` runs from `_ensure_served_default` on
the claude backend. It sends the reported id back over `session/set_config_option`,
the same write the picker and `/model` make, which the adapter always passes on.

- **When nothing is sent.** The reported id is the head of the advertised list (the
  adapter's `default` pseudo-model, reported when no setting applies), or the list
  does not carry it.
- **What stays unchanged.** As with the kiro check, `_model` keeps `""`/`"auto"`.
  An explicit pin never reaches this path: it is pushed as the pin.
- **Failure handling.** It is best effort: a refused value or a failed request only
  logs, and a dead process still fails the session.

The INFO line on the inherit exit names the model the backend reports, so a log
shows what an `auto` session was started on.

## A pin belongs to the harness it was chosen in

A stored pin records WHAT was picked and never WHERE. Switching `agent.acp_backend`
changes which adapter the next session starts, so an unscoped pin reaches a harness
that never served it: the adapter refuses the id, the session lands on that
harness's default, and the user reads a warning about a model they did not pick
this turn.

`model_scope.pin_applies(pin, namespace)` decides whether a pin may be applied,
and `model_scope.scoped_pin(pin, namespace)` returns the pin or `""`. The namespace
is the model-registry namespace of the backend that will run the session
(`agent_sdk.backends.model_registry_namespace`), so two harnesses sharing one
vocabulary — `kiro` and `kas`, both `acp` — share pins, and a harness added through
the `ACP_BACKEND_*` seam is covered by having a namespace rather than by a branch.

A pin is refused only when BOTH hold:

- the session's own harness has advertised a list and the pin is not in it. An
  absence from a warm advertised list is evidence. An absence from the STATIC
  `model_registry.json` index is not: that file names `acp` and `claude_code` only,
  so every other harness is missing from it by construction.
- some OTHER namespace's catalog claims the pin, which is what makes it
  attributable rather than merely unrecognized. An id no catalog claims — a
  regional Bedrock profile, a model newer than every catalog — reaches the wire
  unchanged.

`model_registry.namespace_vocabulary(id, namespace, advertised)` answers the
per-namespace half, and it answers a question distinct from entitlement. "Is this
id in my vocabulary" decides whether a pin was chosen for another harness. "Can
this account run it" belongs to `model_is_unusable`. Conflating them misnames the
cause: a native pin the account is not entitled to would be reported as belonging
elsewhere, and its entitlement warning suppressed.

So PRESENCE is taken from any of three sources while ABSENCE is never proof on its
own:

- the `advertised` list this session's harness sent, which is the freshest and
  sometimes the only source; the wire sites pass theirs for exactly that reason.
- the cross-session advertised cache for that namespace. `kiro` and `kas` are NOT
  members of `ACP_BACKENDS_ADVERTISED_MODEL_SELECTION`, so no `session/new` payload
  fills the `acp` bucket; `GET /api/models` fills it from the `chat --list-models`
  catalog instead, the same rows that seed the window authority.
- the static index, but ONLY where the id round-trips through `to_provider_id` to
  itself. An entry can be an ALIAS folding onto a different model — kiro serves
  `claude-haiku-4.5` while the `claude_code` index maps that spelling to Sonnet —
  and reading such an alias as vocabulary lets a pin survive into a silent
  substitution.

A pin that IS one of a namespace's canonical registry keys is native by identity,
checked before the round-trip. `catalog_key` folds a `[1m]` bracket but not a `-1m`
suffix, so `opus-4.8-1m` does not match its own provider id
`global.anthropic.claude-opus-4-8[1m]`; the identity check admits it without adding
a spelling rule. An alias resolves to a DIFFERENT canonical key, so it still takes
the round-trip and is still rejected.

`model_scope.foreign_namespaces(id, namespace)` names the OTHER namespaces whose
vocabulary holds the id, excluding the session's own. The exclusion is
load-bearing: two namespaces list one model family, so without it a harness is
reported as foreign to itself.

### Scoping is a read, and every tier takes it

Nothing on disk is rewritten. The stored pin stays as the user picked it and is
simply not read by a harness that cannot claim it, so switching back restores it
with no migration and no second field.

Both resolvers scope EVERY tier and let an out-of-scope tier defer to the next, so
an out-of-scope pin reads exactly like an unset one:

- `KiroCrewConfig.acp_effective_model` — the provider factory's selection, which
  every surface routes through. It takes the per-session `backend` because
  `create_provider_factory` resolves that before the model; a member-DM thread
  auto-routed to another harness is judged against the harness it runs.
- `config.loader.resolve_effective_model` — the display resolver behind the model
  chip. It scopes against the configured backend.

The two MUST agree on whether a pin survives, or the chip names a model no turn
runs. `test_model_scope_wire_paths.py` pins that agreement.

The provider factory judges from the catalogs alone because it runs before any
session exists. The three WIRE sites each hold this session's advertised list and
pass it in, so they decide from fresher evidence than the factory can. That split
is deliberate: the factory gives breadth across every surface, the wire sites add
freshness.

The cache is warm for every backend, but from two different sources. For a
backend in `ACP_BACKENDS_ADVERTISED_MODEL_SELECTION` the client captures the
`session/new` list. For `kiro` and `kas` the `acp` bucket is filled by
`GET /api/models` from the `chat --list-models` catalog — the UNFILTERED rows,
before the deprecation and entitlement narrowing, because a deprecated or
unentitled id is still a kiro id and dropping it would make a native pin read as
foreign. That source is kiro's own ground truth and is deliberately NOT a
`session/new` payload: the registry attributes that payload to `claude-agent-acp`,
and a kiro session's list is scoped to the agent that session started. With the
bucket warm, the chip and the provider factory reach the same foreign-pin verdict
the wire does, instead of naming a pin the wire then withholds. The catalog is a
vocabulary, never an entitlement: `model_scope.pin_applies` reads it only for
presence and non-emptiness, and the two readers that fold a pin onto an advertised
spelling (`seed_available_models`, `resolve_wire_model_id`) are gated to the
advertised-selection backends and never see the `acp` bucket for kiro or kas.
Entitlement stays with the live `session/new` list (`_entitled_kiro_models`,
`model_is_unusable`). The cache is cold until the first `GET /api/models` of an
install, and in that window the catalogs alone cannot call any pin foreign; every
send is still a wire decision, so no turn runs the wrong model.

A catalog row's wire id is its `model_id`. kiro-cli prints a `model_id` beside each
`model_name`, and the two can differ: a model can carry a display name that is not
its id. `session/new` advertises the id, and every reader of a picker row (the
entitlement narrowing, the pin validators, `session/set_model`) treats
`model_name` as the id, so `GET /api/models` serves such a row with `model_name`
set to the `model_id` and the printed name kept as `display_name`
(`_fetch_kiro_catalog`). Left under its printed name, the row would never match an
advertised id and the picker would hide a model the account can run.

That live list is revalidated on the read path before it narrows anything. A
`session/new` snapshot is one answer captured at one instant, and an entitlement
lookup racing a token refresh can answer with the free tier; no explicit pick is
ever refused on the picker read path, so the refresh-before-refuse heal never
fires there. `_entitled_kiro_models` therefore first calls the newest kiro
session's `maybe_refresh_available_models(catalog_ids)` and narrows with what it
returns. The ACP handle re-probes only when the snapshot would drop a catalog row
-- judged by `catalog_row_would_drop`, the same per-row verdict the endpoint
applies, and not when the endpoint would fail open to the full catalog -- AND the
snapshot is suspect: never probe-confirmed, captured within
`_READ_PATH_SPAWN_RACE_SECS` of runtime spawn, or advertising only `auto`. The
probe is single-flight per handle and shielded under
`_READ_PATH_PROBE_DEADLINE_SECS` (3s). A deadline miss raises
`EntitlementRevalidating`; `GET /api/models` answers it with
`503 model_list_revalidating`, the client keeps its last-good list and re-polls,
and the shielded probe lands so the next read serves its result. A probe that
fails, rather than times out, fails open: the current snapshot narrows as before.
Underneath, `probe_advertised_models` keeps two clocks -- a result TTL that
replays a recent non-empty answer and an attempt TTL that replays a recent empty
or failed attempt as no evidence -- and `force=True` (an explicit `set_model`
pick, the spawn-time pin check) bypasses only the failed-attempt replay, so a user
action always earns a real answer. Either replay is served only if its clock is at
least as new as the snapshot the caller holds (`not_before`): a cached broader
answer can never replace a session's newer narrower one, and a failed attempt that
predates the snapshot never stands in for the probe it has yet to receive. The
handle dates the snapshot it stores by the answer's own clock (the runtime's
result clock, `entitlement_probe_result_at`), not by its call time, so its floor
never rises above the data it holds and a replayed answer is never re-dated out
of the spawn-race window it was captured in.

The same snapshot judges three more decisions, and all revalidate before they
trust a denial or a narrowing. A direct-spawn `AcpClient` (one kiro-cli process
per session, no shared runtime) refuses an explicit `set_model` pick and withholds
a startup pin only after `AcpClient.refresh_available_models` agrees, and the
picker read on that same dedicated transport (`AcpProvider.maybe_refresh_available_models`
with a plain kiro `AcpClient`) is served by the same method whenever the snapshot
would drop a catalog row (`catalog_row_would_drop`): that client has no shared
probe cache, so its own snapshot is the cache -- a snapshot a probe confirmed
within `_ENTITLEMENT_PROBE_TTL_SECS` is fresh and is not re-probed, anything else
(a `session/new` capture, an older confirmation) earns one throwaway `session/new`
on a dedicated short-lived probe process of its own (never this session's stream,
so none of its frames can reach this session), overlapping callers share one
in-flight probe, and a failed probe keeps the snapshot's verdict. The picker read
honours `_READ_PATH_REPROBE_MIN_INTERVAL_SECS` against a probe-confirmed list, and
is bounded by the same `_READ_PATH_PROBE_DEADLINE_SECS` (3s) shielded deadline the
shared read path uses: past it the read raises `EntitlementRevalidating` (the
endpoint's degraded response; the probe keeps running and the next read serves its
landed answer) rather than holding a picker poll or a pin-save for the probe's full
`initialize`+`session/new` timeout. A role pin
(`agent.role_models.*`, `agent.fallback_model`, `agent.refusal_fallback_model`,
the `decisions.*` model pins, a crew's `model`) is judged by the synchronous
`_validate_role_model` against the NEWEST live session in the target namespace
(always scoped: the PATCH path resolves the default harness `agent.acp_backend`
and the crew handlers the member's, so a newer session on another harness can
neither admit nor reject the pin)
-- the same session `_entitled_kiro_models` reads, so a session started before a
downgrade cannot keep admitting the model the account lost. Its callers first
await `_revalidate_role_pin_evidence`, which hands that session to the same
`maybe_refresh_available_models` seam the picker uses (the pin judged beside the
rows the snapshot already serves, so a lone pin never reads as the fail-open
namespace mismatch). The seam heals the snapshot in place, so the validator reads
the fresh answer; a deadline miss is a retryable 400, never an acceptance on no
evidence, and a probe failure proceeds on the snapshot as it was. The crew
handlers run it before taking the config lock, so no probe holds the lock.

The vocabulary side and the spelling side fold ids with ONE function. A pin can be
native to a harness while spelled in another namespace's provider-id form:
`global.anthropic.claude-opus-4-8[1m]` folds through `catalog_key` onto kiro's
advertised `claude-opus-4.8`, so `namespace_vocabulary` calls it native. The wire
then has to send the ADVERTISED spelling, and `resolve_pin_spelling` answers it:
after the literal match and the one `<namespace>::` peel miss, it folds both sides
with the same `catalog_key` and returns the advertised id, tie-breaking several
candidates through `preferred_advertised_spelling` exactly as `resolve_wire_model_id`
does. Folding the two sides with different functions is how the entitlement
warning came to name a spelling problem. It is a SPELLING fold, never a model fold:
`catalog_key` folds the window marker away, so the 200K `claude-opus-4-8` and the 1M
`claude-opus-4.8` share a key, but the registry lists them as two canonical models
and `same_registered_model` refuses to fold one onto the other -- a pin never
resolves to its neighbour with a different context window. Two ids the registry
cannot both place are unknown, not different, and fold on spelling alone.

`resolve_wire_model_id` has one fallback past the spelling fold. An adapter can
advertise a bare family alias (`fable`, `opus`) where a session stored the dotted
provider id, and those share no normalized key. When the key compare misses, an
advertised id that is a VERSION-LESS alias of the stored id's own registry entry
is accepted. Versioned aliases never qualify: an entry also lists substitution
aliases (`claude-haiku-4.5` under Sonnet), and an adapter advertising one is
serving that other model.

Three more sites apply the same rule on the wire, and one on the picker:
`AcpClient._apply_startup_model`, the shared-runtime cold start in
`providers/acp.py`, the warm-pool post-claim switch in `session_allocation.py`
(through the injected `model_pin_applies` dep), and `_scoped_default` behind
`GET /api/models`. A harness-scope refusal logs at INFO on wire paths and at
DEBUG on display paths, and MUST NOT take the entitlement warning path: harness
ownership and account entitlement are separate
questions, and reporting one as the other names the wrong cause.

**Every site scopes the pin BEFORE translating it into a backend's namespace.** For
an alias that translation is already a substitution — `to_provider_id`
turns `claude-haiku-4.5` into Sonnet's id, because the claude backend serves no
Haiku — so a site that scopes the translated value asks about the substitute and
the pin passes. `test_model_scope_wire_paths.py::TestScopeSeesTheUntranslatedPin`
parses `src/kiro_crew` and fails when any scope call receives a value assigned from
`to_provider_id` / `to_acp_id` / `resolve_wire_model_id` in the same function. It
asserts the property rather than freezing a list of sites, so a legitimate new site
needs no edit.

It tracks both flow shapes a translated value takes: a local name, and an attribute
such as `self._model`. Covering only local names leaves the attribute form
invisible, and the client handshake carries the pin in exactly that form.

This check asserts a property rather than enumerating read sites. It fails when any
scope call receives a translated value, whether that value reaches the call through a
local name or through an attribute such as `self._model`. The property holds for a
future site without registering its name, whereas an inventory only observes names that
already exist.

## An explicit user pick is the opposite

A model the user chose reaches the adapter, which raises `AcpModelUnavailable` when
it refuses every accepted spelling. Never silently swap a model a user picked: the
substitution is invisible, and the user reads the cheaper model's output as the one they
asked for.

The two rules disagree in exactly one case, and the disagreement is deliberate: an
inherited pin the account really can run on this backend, absent from its advertised
list, and claimed by another namespace's catalog is withheld by the scope rule, while
an explicit pick of the same id is sent — an inherited pin is a stale value, an
explicit pick is a live intent. Nothing is written to disk, so the pin returns on
its own once the cache refreshes with a list that carries it.

## Where each choice comes from

- **Pickers** MUST list options from `GET /api/models`, the advertised set, never a
  static in-code list. A hand-maintained list offers models the account cannot run and
  hides the ones it can.
- **A pin's save gate accepts every id its picker offers.** The config PATCH gives
  every validated model pin (`agent.role_models.*`, `agent.fallback_model`,
  `agent.refusal_fallback_model` and the `decisions.model_route` tiers) one
  grammar, `_MODEL_PIN_PATTERN` in `dashboard/handlers/core.py`: a bare id, or the
  slash-joined `provider/model` form OpenCode advertises. It refuses empty
  segments and shell metacharacters; `_validate_role_model` then judges
  entitlement as above. The chat default `agent.model` uses the same grammar
  (its picker lists the same names) without the entitlement check. `decisions.nudge_wake.llm_model` keeps the bare-id
  grammar, because the decision gate scrubs any model `is_model_id` refuses.
- Backends with `resolves_model_from_advertised_list` use their own advertised
  model namespace. Claude retains its registry display-name reconciliation.
  Other advertised-selection backends use their live session's model ids, then
  their persisted namespace cache, preserving exact wire ids (including Pi
  provider/model ids) without substituting the Kiro CLI catalog. A cold cache
  offers `auto` and a scoped configured default until that backend advertises
  its choices.
- The chat composer reads `GET /api/chat/slots/{slot}/selection-capabilities` for
  the active ACP session's backend, effort support, and ordered effort levels. A
  missing session answers `known: false`; the composer then uses its existing
  model-name heuristic until ACP reports the session's actual options. A slot the
  gateway has not registered yet answers 404 `slot_not_found`, and the composer
  reads that the same way (`selectionCapabilitiesFailed` in `website/src/lib/effort.ts`):
  only a real fault (403, 503 `peer_unavailable`, transport failure) shows the
  "could not verify effort options" notice and hides the effort control. The same
  endpoint proxies a remote slot to its execution peer. Model and effort are ONE
  composer control (`docs/decisions/2026-06-14-chat-composer-model-and-effort-are-one-control.md`):
  the model chip names the level in force, and the model picker embeds the effort
  slider below its model list whenever the capability read reports support, offering
  exactly the advertised levels in their advertised order, whether the backend is
  Claude, Codex, Pi, or another capable ACP harness. A session that reports no
  effort support gets no effort row inside the picker. The composer never grows a
  second, standalone effort control. A model pick first carries a staged effort
  pick (or waits for an effort write already on the wire) and sends the model
  after it; if the effort write is refused, the model pick aborts rather than
  resetting the owner's previous selection.
- The composer chip's marker (`modelChipMarker` in `website/src/lib/model.ts`)
  reads `default` only when the model shown is the Settings default and the slot
  takes it from there, and `auto` when the session was served a different model
  for a slot that picked Auto or holds a withheld pin. A pin, an agent's own
  model, or a default the surface cannot read carries no marker. When the config
  read or the agent-pin read fails (`useSettingsDefaultModel` reports `failed`),
  the composer shows an inline "failed to load config" notice instead.
- Codex advertises `model[effort]` pairs, but its `model` config option accepts the
  base ID and its `reasoning_effort` option accepts the level. The live capability
  marks only backends in `ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS` for pair grouping;
  before the session exists, the configured backend ID supplies the same Codex
  fallback. The pair shape alone is never sufficient. Existing pair pins display
  as their base model; an unset effort control remains Default until the user
  chooses an override. Picking a model stores the base ID; the backend's existing
  effort reapply path keeps a slot override in force. Other backends' model IDs,
  including Claude window suffixes such as `[1m]`, remain intact.
- `dashboard.model_picker_hidden_models` is a presentation preference over that
  advertised set. It filters only the interactive ChatPage and ChatPane pickers;
  `auto` and each slot's active model remain visible. Settings defaults, role and
  fallback selectors, the bulk switcher, crew editors, and app-specific selectors
  continue to receive the complete advertised list. Storing hidden IDs rather than
  visible IDs means a newly advertised model appears by default. The picker links
  to this setting until the first successful visibility save; merely opening
  Settings or a failed save does not dismiss it. The server stores that fact in
  `dashboard.model_picker_configured`, migrating an existing non-empty hidden list
  as already configured. Select-all and deselect-all update the current advertised
  set with one write, keep `auto` selected, and preserve hidden IDs absent from the
  current catalog. Enabling the configured effort default moves the slider thumb to
  that level before the setting write completes.
- **Pin a cheaper model** only through `agent.role_models.<role>` (`background`,
  `subagent`), read by `AgentConfig.resolve_model(role)` in `config/sections.py`. Roles
  default to `"auto"` and deliberately do NOT inherit `agent.model`, so a user's chat
  model does not silently become the price of every background task.
- **Entitlement checks** always use the shared predicate
  `acp.client.model_is_unusable(id, advertised)` together with
  `advertised_model_ids(...)`. It is one predicate on purpose: two spellings of "can
  this account use it" eventually disagree. An empty or unknown advertised set means
  **allow** — reading it as "nothing is allowed" would withhold every model on a
  backend that simply does not advertise. Never hand-roll a membership test.
- On a backend in `ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS` (codex) the advertised list
  holds only `<id>[<effort>]` pairs while the model option takes the bare id, so the
  question "is this bare id served" belongs to
  `acp.runtime_models.resolve_pin_spelling_on(id, advertised, backend=...)` (and
  `resolve_usable_model(..., backend=...)`, which calls it), not to a second
  predicate. `AcpSessionProvider.set_model(id)` admits a pick on that verdict and
  hands it to `AcpSessionHandle.set_model`, which applies the model config option
  NON-strictly: an adapter that refuses every spelling leaves the session on its
  default and records the refused id in `model_pin_refused` (reset at the start of
  every call, so the field is always the last call's verdict). Substitute, restore
  and startup callers (`llm_helpers.resolve_substitute_set_model`, the Auto route
  and throttle walk in `chat_runner`, the warm-pool claim) read that as "stay on
  the backend default" and say nothing. The explicit dashboard pick
  (`chat_handlers._try_live_model_switch`) reads `provider_model_pin_refused` after
  the call and raises `AcpModelUnavailable` itself, so the handler answers 400 and
  the slot keeps its old model instead of reporting a switch the session never
  made. `AcpClient.set_model` (dedicated runtime) raises inside the call as before.
  On a backend that switches through `session/set_model` (kiro-cli),
  `AcpSessionHandle.set_model` waits briefly for the adapter's reply before
  recording the model. An explicit error reply is recorded in `model_pin_refused`
  and the session keeps its model, the same contract as the config-option branch,
  so the dashboard pick answers 4xx instead of resetting. No reply within the wait
  is read as accepted and the model is recorded, as it was before the wait existed.
  Both refusals carry `advertised_but_refused` from the same verdict, which selects
  the adapter-mismatch wording; `AcpModelUnavailable` requires `backend=` and shows
  the `kiro-cli whoami` hint only for backends that sign in through the host
  kiro-cli identity store (`host_auth.signs_in_separately`).
- The predicate is only meaningful where the advertised ids share a namespace with the
  id being tested, and callers gate on that. Comparing ids across two harnesses'
  namespaces calls every legitimate model unusable (harness-parity invariant `H12`).

Member create and update validation pass the DM slot's backend to the shared
model-pin check. When the configured default harness (`agent.acp_backend`) and
the member DM harness (`agent.member_acp_backend`) share a model-registry
namespace, the default scopes the entitlement evidence; when they diverge the
member backend does, because the DM thread runs on it. Entitlement evidence then
comes only from live providers sharing that scoped backend's namespace: `kiro`
(including the empty default backend) and `kas` share `acp`. Providers with
unknown identity or another namespace supply no evidence, so a pin the scoped
backend has no live catalog for is treated as unknown (allowed), never rejected
by an unrelated harness's advertised ids.

## The one allowed concrete fallback

The `claude_code` seam's `cc_model` (`_BACKGROUND_CC_MODEL` in `agent.py`) is the one
allowed concrete fallback, because that backend cannot resolve `"auto"`. Keep it off
the default path.

## The gate

`code-review.yml` fails on a newly added hardcoded model literal outside
`model_registry*`, the config schema, and tests. It reports on the lines a change adds,
so an existing literal elsewhere in a file does not exempt a new one.
