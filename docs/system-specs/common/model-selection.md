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

## Resolve, don't guess

For a model chosen on the caller's behalf — background one-liners, tips, inherited or
cold-start applies — route through
`acp.client.resolve_usable_model(preferred, advertised)`. It answers with a served id,
or `"auto"` only when the backend advertises it, or `""` meaning **inherit the
session's served backend default**. Returning `""` rather than substituting a guess is
the whole point: the wire never receives a model the partition does not serve.

Two behaviours of the resolver are worth knowing before writing a call site:

- An **unknown or empty advertised set** means entitlement is unknowable. `"auto"`
  degrades to `""` because it cannot be verified, while a concrete caller-supplied id
  is trusted because there is nothing to check it against.
- A persisted pin can carry a stale `<namespace>::<bare-id>` qualifier while the
  session advertises the bare id. The resolver retries the miss through
  `resolve_pin_spelling` and puts the **advertised** spelling on the wire, not the
  caller's, because the qualified spelling is one the backend never advertised.

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
- The predicate is only meaningful where the advertised ids share a namespace with the
  id being tested, and callers gate on that. Comparing ids across two harnesses'
  namespaces calls every legitimate model unusable (harness-parity invariant `H12`).

## The one allowed concrete fallback

The `claude_code` seam's `cc_model` (`_BACKGROUND_CC_MODEL` in `agent.py`) is the one
allowed concrete fallback, because that backend cannot resolve `"auto"`. Keep it off
the default path.

## The gate

`code-review.yml` fails on a newly added hardcoded model literal outside
`model_registry*`, the config schema, and tests. It reports on the lines a change adds,
so an existing literal elsewhere in a file does not exempt a new one.
