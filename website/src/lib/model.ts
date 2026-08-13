/** Model-id helpers shared by every surface that displays a slot's model.
 *
 *  The picker's list and the slot's pinned model come from two different places
 *  (`GET /api/models` vs. the slots payload), so they can disagree — most
 *  visibly after a plan downgrade, where the slot stays pinned to a premium
 *  model the account can no longer run. The backend withholds such a model at
 *  spawn and runs the session on its own default, so displaying the pin would
 *  name a model no turn will use.
 */

/** Canonical key for comparing model ids across spelling variants.
 *
 *  Mirrors `_normalize_model_key` in `dashboard/handlers/agents.py`: adapters
 *  advertise dashed ids (`claude-opus-4-8`) while curated/config entries may be
 *  dotted (`claude-opus-4.8`), and case can differ. `default` and `auto` both
 *  mean "let the backend pick", so they fold to one key.
 */
export function normalizeModelKey(name: string): string {
  const key = (name || '').trim().toLowerCase().replace(/\./g, '-')
  return key === 'default' || key === 'auto' ? 'auto' : key
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
