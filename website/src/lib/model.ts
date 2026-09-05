/** Model-id helpers shared by every surface that displays a slot's model.
 *
 *  The picker's list and the slot's pinned model come from two different places
 *  (`GET /api/models` vs. the slots payload), so they can disagree — most
 *  visibly after a plan downgrade, where the slot stays pinned to a premium
 *  model the account can no longer run. The backend withholds such a model at
 *  spawn and runs the session on its own default, so displaying the pin would
 *  name a model no turn will use. The slots payload carries the backend's
 *  effective model id (`effective_model`) — the normalized wire id the next
 *  turn will actually send — so the answer is read rather than inferred from
 *  picker-list membership; the legacy boolean (`model_withheld`) is kept for
 *  backward compat. Unknown (null) fails open and shows the pin.
 */

import { canonicalKey } from '../providers/modelRegistry'

/** Canonical key for comparing model ids across spelling variants.
 *
 *  Kept for the unknown fallback and for `pinIsWithheld`; the effective-model
 *  path no longer needs it because the backend hands over an already-resolved
 *  wire id (deprecated aliases resolved, withheld mapped to `auto`), removing
 *  the mirrored `_normalize_model_key` predicate (#7575).
 *
 *  Resolution order:
 *  1. `auto`/`default`/unset -> the `auto` sentinel (both mean "let the backend
 *     pick"); an empty id stays `''` (no pin, distinct from Auto).
 *  2. Registry canonical key: a canonical key, a registry alias, or a
 *     claude_code provider id — with or without a region/vendor routing prefix
 *     (`us.anthropic.…`, `global.anthropic.…`) — folds to its canonical key.
 *     This is what makes an alias and its provider-prefixed canonical id equal
 *     (`us.anthropic.claude-opus-4-8[1m]` ≡ `claude-opus-4.8` -> `opus-4.8-1m`)
 *     while keeping DISTINCT registry entries distinct — notably the advertised
 *     dashed `claude-opus-4-8` (200K, `opus-4.8`) does NOT fold onto dotted
 *     `claude-opus-4.8` (1M, `opus-4.8-1m`); the old dot->dash fold conflated
 *     those two genuinely different context-window models (#5339).
 *  3. Fallback for an id the registry does not list (GPT/DeepSeek/Qwen, future
 *     models, operator-typed ids): the historical lossless fold — trim,
 *     lowercase, `.`->`-` — so behavior is identity-preserving off the
 *     registered set, matching the backend's pass-through contract.
 */
export function normalizeModelKey(name: string): string {
  const stringFold = (name || '').trim().toLowerCase().replace(/\./g, '-')
  if (!stringFold) return ''
  if (stringFold === 'default' || stringFold === 'auto') return 'auto'
  const canonical = canonicalKey(name)
  if (canonical !== null) return canonical
  return stringFold
}

/** The model id to DISPLAY for a slot pinned to `pinned`.
 *
 *  `effective` is the slot's effective model id (`effective_model` from the
 *  slots payload) — the normalized wire id the next turn will actually send —
 *  or the legacy boolean (`model_withheld`) for backward compat. It wins
 *  whenever it is known: it was computed at spawn against the live session's
 *  advertised list (or, for a deprecated alias, resolved even before the first
 *  turn), so it answers "will a turn use this pin?" directly. A string
 *  `"auto"` means withheld, any other string is the runnable wire id (for a
 *  deprecated pin, its replacement, so the chip names the replacement rather
 *  than `auto` or the deprecated spelling). `true`/`false` are the legacy
 *  boolean encoding.
 *
 *  `null`/`undefined` is the third state — no verdict yet — and it must fail
 *  open: return the pin itself. The previous membership heuristic (absent from
 *  `models` => `auto`, gated by `modelsDegraded`) is removed; every
 *  `/api/models` filter (deprecation, curation) would otherwise become an
 *  entitlement signal (#7575). `degraded` is kept as a param for backward
 *  compat but no longer gates the display.
 *
 *  This is a DISPLAY decision only. Never feed the result into a write — a
 *  lossy label must not become persisted state (see ChatPage's pin-to-agent
 *  row, which writes the slot's real model, and the firewall note in
 *  `DashboardState.effective_model`).
 */
export function displayModel(
  pinned: string,
  models: { name: string }[],
  degraded = false,
  effective?: string | boolean | null | undefined,
): string {
  const key = normalizeModelKey(pinned)
  if (!key || key === 'auto') return 'auto'
  // New path: backend-supplied effective wire id.
  if (typeof effective === 'string') {
    const effKey = normalizeModelKey(effective)
    if (!effKey || effKey === 'auto') return 'auto'
    const match = models.find(m => normalizeModelKey(m.name) === effKey)
    return match ? match.name : effective
  }
  // Legacy boolean path.
  if (effective === true) return 'auto'
  if (effective === false) {
    const match = models.find(m => normalizeModelKey(m.name) === key)
    return match ? match.name : pinned
  }
  // Unknown (null/undefined): fail open, show the pin. No membership
  // inference — absence from `models` is not evidence of withholding (it
  // conflated every unrelated picker filter with entitlement). Still return
  // the list's spelling when the pin matches a row, so the dropdown row
  // highlights; only the `auto` fallback for absent rows is gone.
  void degraded
  const match = models.find(m => normalizeModelKey(m.name) === key)
  return match ? match.name : pinned
}

/** True when a real model is pinned but display fell back to `auto` — i.e. the
 *  backend withholds it and no turn will use it.
 *
 *  Deliberately NOT `shown !== pinned`: `displayModel` returns the list's
 *  spelling, so a config pin of `claude-opus-4.8` against an advertised
 *  `claude-opus-4-8` differs as a string while naming the same model. Comparing
 *  normalized keys against `auto` states the condition directly instead of
 *  inferring it from inequality.
 */
export function pinIsWithheld(pinned: string, shown: string): boolean {
  const key = normalizeModelKey(pinned)
  // An unset pin normalizes to '' rather than 'auto', so it needs its own guard:
  // without it "nothing pinned" would read as withheld and disable the row.
  if (!key || key === 'auto') return false
  return normalizeModelKey(shown) === 'auto'
}
