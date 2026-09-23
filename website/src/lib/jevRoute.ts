/** The chat model picker's `Auto (Jev)` entry: when it is offered, and when it is on.
 *
 *  The entry is not a model. Picking it asks the `model.route` decision point to
 *  put each turn in a difficulty tier and to run that turn on the model the tier
 *  maps to (`decisions.model_route`); until it answers, the session runs on the
 *  backend's own default. So the id below never reaches a provider — the gateway
 *  resolves it to `auto` and records the choice as the slot's `jev_route` flag —
 *  and the slot's own `model` keeps meaning "a provider model id" everywhere.
 *
 *  Two conditions gate the row, and BOTH are required, because they answer
 *  different questions:
 *
 *  - `decisions_enabled` on `GET /api/dashboard/config` is the FLEET's answer
 *    (`capabilities.decisions`, resolved server-side). Fail closed on `=== true`:
 *    an absent field is an older gateway or a read that has not landed.
 *  - `permits` on `GET /api/decisions/consent` is the OWNER's answer as the GATE
 *    will read it. Strictly stronger than the keystone's `enabled`, and that
 *    difference is the whole reason it is the field used: `permits` additionally
 *    holds the consented endpoint against the one config names NOW, and folds in
 *    the governance denial. Consent recorded for one provider address while
 *    `provider.endpoint` names another is a refusal at the gate, so a surface
 *    reading `enabled` alone offers a control that routes nothing and a chip that
 *    claims a routed turn the backend declined.
 *
 *  Neither read is an enforcement point — the gate re-checks both, and a request
 *  carrying the sentinel against an unconsented keystone simply routes nothing —
 *  so this module is presentation only. It lives here rather than in one page
 *  because two surfaces draw the picker (ChatPage and each split ChatPane), and a
 *  second copy of the condition is a second place for them to disagree about
 *  whether the row is offered.
 */

/** The id a client sends for the `Auto (Jev)` row. Mirrors `JEV_ROUTE_MODEL` in
 *  `dashboard/chat_handlers.py`, which is the only reader. */
export const JEV_ROUTE_MODEL = 'auto:jev'

/** Whether a slot names no model, i.e. Jev's to pick when the preview is on.
 *
 *  Mirrors `_JEV_ROUTE_AUTO_MODELS` in `dashboard/chat_runner.py`, which is the
 *  gate that actually routes: `auto` is the picker's inherit spelling and `''` is
 *  what a freshly dispatched slot carries, and both mean the same thing here.
 *
 *  Takes the slot's RAW `model`, never the composer's displayed one: the display
 *  value substitutes the backend's served id for an inheriting slot, so it is
 *  almost never `auto` and a chip keyed off it would claim every routed turn was
 *  pinned. Stripped and lower-cased for the same reason the gate is.
 */
export function isUnpinnedModel(model: string | undefined): boolean {
  const named = (model || '').trim().toLowerCase()
  return named === '' || named === 'auto'
}

/** Whether the picker may offer the row at all.
 *
 *  `hasSlot` is the third required answer and it is not cosmetic: a pick made
 *  with no live slot is held as the pending model and forwarded verbatim into
 *  slot creation, which would store the sentinel as the new slot's model -- an
 *  id no provider serves -- with routing off. It is a parameter rather than a
 *  check inside each page so both surfaces answer the same question here.
 */
export function jevRouteOffered(
  dashboardConfig: { decisions_enabled?: boolean } | undefined,
  consent: { permits?: boolean } | undefined,
  hasSlot: boolean,
): boolean {
  return (
    dashboardConfig?.decisions_enabled === true && consent?.permits === true && hasSlot === true
  )
}

/** The picker's list with the row prepended when it is offered.
 *
 *  Prepended before `auto` rather than appended: it is the Auto row's sibling —
 *  both mean "do not pin a model" — and a long advertised list would otherwise
 *  put it below the fold of a menu whose first row it belongs beside.
 *
 *  The list is returned UNCHANGED when the row is not offered, so a caller can
 *  pass it through unconditionally and React Query's cached array identity is
 *  preserved for the overwhelmingly common case.
 */
export function withJevRoute<T extends { name: string; description?: string }>(
  models: T[],
  offered: boolean,
  description: string,
): T[] {
  if (!offered) return models
  if (models.some(m => m.name === JEV_ROUTE_MODEL)) return models
  return [{ name: JEV_ROUTE_MODEL, description } as T, ...models]
}

/** The id the picker should highlight for a slot: the sentinel when it is routed.
 *
 *  Takes the already-resolved display model so the routed case is the ONLY thing
 *  it changes. `jev_route` wins over the model, and that is not a display
 *  preference: a routed slot stores `auto` and would otherwise highlight the Auto
 *  row, i.e. name a behaviour the session does not have.
 */
export function jevRouteShownModel(
  shown: string,
  slot: { jev_route?: boolean } | undefined,
): string {
  return slot?.jev_route === true ? JEV_ROUTE_MODEL : shown
}
