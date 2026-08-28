/** Model-id helpers shared by every surface that displays a slot's model.
 *
 *  The picker's list and the slot's pinned model come from two different places
 *  (`GET /api/models` vs. the slots payload), so they can disagree — most
 *  visibly after a plan downgrade, where the slot stays pinned to a premium
 *  model the account can no longer run. The backend withholds such a model at
 *  spawn and runs the session on its own default, so displaying the pin would
 *  name a model no turn will use.
 */

import { canonicalKey } from '../providers/modelRegistry'

/** Canonical key for comparing model ids across spelling variants.
 *
 *  Mirrors `_normalize_model_key` in `dashboard/handlers/agents.py`: both route
 *  a model id through the shared canonical registry (`model_registry.json`) so
 *  "same model?" has ONE definition across the dashboard (picker, slot display,
 *  and the #5306 subagent downgrade flag).
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
 *  Returns `'auto'` when the pin is absent from `models` — the picker's list is
 *  narrowed to what the live session says the account can run, so a pin that is
 *  not on it is one the backend withholds.
 *
 *  `degraded` is the authority on whether the list can be trusted, and it must
 *  come from `modelsDegraded(providerId)` — NOT from the list's shape. A cached
 *  multi-row list served while `/api/models` is failing looks perfectly healthy
 *  by length while being arbitrarily stale, so length alone would relabel a pin
 *  the account has (re)gained access to. When `degraded` is true the pin is
 *  returned untouched: entitlement unknown is not entitlement denied.
 *
 *  This is a DISPLAY decision only. Never feed the result into a write — a
 *  lossy label must not become persisted state (see ChatPage's pin-to-agent
 *  row, which writes the slot's real model).
 */
export function displayModel(
  pinned: string,
  models: { name: string }[],
  degraded = false,
): string {
  const key = normalizeModelKey(pinned)
  if (!key || key === 'auto') return 'auto'
  if (degraded || models.length === 0) return pinned
  // Return the LIST's spelling of the match, not the caller's. Matching is
  // normalized (dotted vs dashed, case) but `ModelDropdownList` highlights on
  // exact `activeModel === m.name`, so handing back the raw pin would show a
  // model in the chip that checks no row — e.g. a config pin `claude-opus-4.8`
  // against an advertised `claude-opus-4-8`.
  const match = models.find(m => normalizeModelKey(m.name) === key)
  return match ? match.name : 'auto'
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
