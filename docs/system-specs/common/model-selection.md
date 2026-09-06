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

## An explicit user pick is the opposite

A model the user chose raises `AcpModelUnavailable` instead of resolving. Never
silently swap a model a user picked: the substitution is invisible, and the user reads
the cheaper model's output as the one they asked for.

## Where each choice comes from

- **Pickers** MUST list options from `GET /api/models`, the advertised set, never a
  static in-code list. A hand-maintained list offers models the account cannot run and
  hides the ones it can.
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
