/**
 * The surfaces the Phase 5 render gate asserts on.
 *
 * WHY A REGISTRY AND NOT "every route". The gate renders each entry once per
 * locale, so cost is linear in this list. More importantly the ledger is keyed on
 * `id`, which makes a per-surface ceiling meaningful: adding untranslated copy to
 * `/schedule` raises `schedule`'s own number and fails, even if some other surface
 * improved in the same branch. A single global total would let that trade happen
 * silently — which is exactly the bidirectional-ratchet failure #1060 removed.
 *
 * SELECTION. Ranked by catalog text volume x traffic x label density. Every entry
 * is reachable by URL alone: the settings/capabilities/developer sub-panels are
 * query-param driven, so the gate needs no click choreography and cannot fail
 * because a selector moved. Modal and portal surfaces (command palette, credits
 * modal, composer dropdowns) are deliberately NOT here — they need interaction, and
 * a gate that flakes on a dropdown that did not open is a gate people disable.
 * `fixed-overlay-off-viewport` still covers the portals that mount on load.
 *
 * `settle` is extra idle time in ms for surfaces that fetch after first paint.
 */
export const SURFACES = [
  { id: 'chat', url: '/chat', settle: 400 },
  { id: 'settings-display', url: '/settings?tab=display' },
  { id: 'settings-overview', url: '/settings?tab=overview' },
  { id: 'schedule', url: '/schedule' },
  { id: 'settings-chat', url: '/settings?tab=chat' },
  { id: 'settings-privacy', url: '/settings?tab=privacy' },
  { id: 'settings-computer-use', url: '/settings?tab=computer-use' },
  { id: 'settings-security', url: '/settings?tab=security' },
  { id: 'settings-notifications', url: '/settings?tab=notifications' },
  { id: 'settings-about', url: '/settings?tab=about' },
  { id: 'capabilities-crews', url: '/capabilities?tab=crews' },
  { id: 'capabilities-mcp', url: '/capabilities?tab=mcp' },
  { id: 'capabilities-skills', url: '/capabilities?tab=skills' },
  { id: 'projects', url: '/projects' },
  { id: 'knowledge', url: '/knowledge' },
  { id: 'artifacts', url: '/artifacts' },
  { id: 'apps', url: '/apps' },
  { id: 'notifications', url: '/notifications' },
  { id: 'logs', url: '/logs' },
  { id: 'developer-system', url: '/developer?tab=system' },
]

/**
 * Locales the gate renders.
 *
 * `en-XA` is the only one the text assertions run against — it is the locale where
 * un-accented Latin is provably untranslated. The real locales are there for the
 * layout and DNT halves, which the pseudolocale cannot answer:
 *
 *   - `de` is the widest Latin script shipped (long compounds, the classic overflow
 *     source), so it grades the pseudolocale's synthetic padding against a real one.
 *   - `bn` is the tall script. The plan names `th`/`hi`/`ar` for vertical growth;
 *     of those only `hi` ships, and `bn` has the taller line box, so both Indic
 *     locales are cheaper to reason about than either alone. `ar` is deliberately
 *     absent: no RTL locale ships until Track C.
 *   - `zh-CN` exercises the CJK font fallback, which a pseudolocale cannot simulate.
 *
 * DNT integrity runs ONLY here and never on `en-XA`: `gen-pseudolocale.mjs` accents
 * every ASCII letter outside a preserved region, and DNT terms are not preserved,
 * so in the pseudolocale all 19 render mangled by design.
 */
export const LOCALES = [
  { code: 'en-XA', mode: 'pseudo' },
  { code: 'de', mode: 'real' },
  { code: 'zh-CN', mode: 'real' },
  { code: 'bn', mode: 'real' },
]

/**
 * Viewports. The narrow one is where fixed-width ancestors actually bite; the wide
 * one is the default desktop shell and the only place the topbar readout capsule and
 * the rail community row are laid out at all.
 */
export const VIEWPORTS = [
  { id: 'wide', width: 1440, height: 900 },
  { id: 'narrow', width: 1024, height: 768 },
]
