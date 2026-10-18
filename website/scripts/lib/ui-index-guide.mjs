/**
 * The generic `ui.show` guide's plans: the reveal graph a guide may have to
 * open (`compileRevealScopes`), the selection and gate vocabulary it may use
 * (`compileGuideNeeds`), the step lists of a curated location (`guidePlanFor`)
 * and of a guidable auto location (`autoGuidePlan`), and the registered ids a
 * guide points at only with a caution or never points at. A new rule about what
 * a guide plan holds, or which curated location may have one, goes here.
 *
 * `ui-index.mjs` re-exports the public names, and callers import them from
 * there. This module imports only `ui-index-auto.mjs`.
 */

import { SHELL_SURFACE, TRUST_ROOT_PARENTS } from './ui-index-auto.mjs'

// ---------------------------------------------------------------- guide plans (ui.show)

/**
 * Registered destructive or data-replacing controls. The generic `ui.show`
 * guide may point at each one, with a caution: its last step carries
 * `caution: true`, the panel says what it removes is permanent, and pressing
 * the control never finishes the guide by itself (its own confirm decides).
 * Pointing is not clicking; the person still presses it. The generator script
 * fails when an id here is not a registered location (or, for an `auto:` id,
 * a location or render site of this tree), so the list cannot go stale.
 */
export const GUIDE_CAUTION_IDS = Object.freeze([
  'agents.delete',
  'apps.detail.uninstall',
  'apps.library.tile-uninstall',
  'backup.import-file',
  'composer.automation.stop-monitor',
  'notifications.clear-all',
  'notifications.page-clear-all',
  'schedule.cancel-run',
  'schedule.delete',
  'sessions.list-menu.clean-up',
])
/**
 * Registered locations no guide ever points at: a control of the agent's own
 * ceiling (the security policy, profiles, admission, Computer Use, denied
 * commands, credentials, the composer's approval mode), which a guide an
 * agent proposed must never walk the person to. The tabs themselves are
 * refused by TRUST_ROOT_PARENTS and by `settings.show`. A descriptor can also
 * opt out with `guide: false`, which makes it search-only. Checked like
 * GUIDE_CAUTION_IDS.
 */
export const GUIDE_DENY_IDS = Object.freeze(['composer.approval-mode', 'members.permissions'])
/** The only prerequisites a guided location may carry: the guide can satisfy
 *  or check each one. A viewport picks the placement; a reveal is walked; a
 *  preview flag or a gate condition (`UI_GATES`) becomes a `gate` step that
 *  pauses on a blocker naming its setting; a selection condition
 *  (`UI_SELECTION_SCOPES`) becomes a `select` step at its picker; a runtime
 *  predicate (`UI_RUNTIME_PREDICATES`) travels with the reveal step whose
 *  control needs it and shows as a blocker when unmet (one on the location
 *  itself only when an earlier step carries it too). Any other condition on the
 *  location itself (a job is running, an editor section is open) is state the
 *  guide can neither set up nor check, so such a location is never guided. */
const GUIDE_REQUIREMENT_KINDS = new Set(['viewport', 'shown_by', 'preview_flag', 'condition'])
/** The gate id a preview flag compiles to. */
export const previewGateId = (flag) => `preview_flag:${flag}`
/** Reveal steps nest at most this deep (a reveal control revealed by another). */
const GUIDE_MAX_REVEAL_DEPTH = 3
/** Ceiling on one plan's steps, reveals, menus and a preview gate included:
 *  six pointing steps, plus the gate step a preview-held page opens with. */
export const GUIDE_MAX_STEPS = 7
/** The `ui.show` plan format: per-placement step lists with step ids. */
export const GUIDE_PLAN_VERSION = 2

/**
 * Compile the reveal graph: one scope per thing a guide may have to open.
 *
 * - A `shown_by` with `when: <state>` uses the scope DECLARED for that state
 *   (`UI_REVEAL_SCOPES`, whose owner `<GuideRevealScope>` reports it live).
 * - A `shown_by` without `when` is `reveal:<revealer>`.
 * - A registered parent opens `tab:<id>` when its children are reached as a
 *   tab, `menu:<id>` when any is a menu entry, else `open:<id>` (a sheet, a
 *   panel). Compiled ids carry their kind as a prefix.
 *
 * Rejected: a declared scope with an unknown state or a colon in its id, a
 * state with two scopes, a reveal state used with no scope, one state revealed
 * by two different controls, a parent whose children disagree about how it
 * opens, a cycle through a `shown_by`, a placement needing two viewports or two
 * opposite conditions at once.
 */
export function compileRevealScopes({ registered, registeredIds, finalPlacements, inputs, errors }) {
  const declared = inputs.revealScopes ?? {}
  const scopes = {}
  const scopeForState = new Map()
  for (const [scopeId, state] of Object.entries(declared)) {
    if (scopeId.includes(':')) errors.push(`reveal scope '${scopeId}': a declared scope id has no ':' (compiled ids own the 'kind:' prefix)`)
    if (!Object.hasOwn(inputs.revealStates ?? {}, state)) errors.push(`reveal scope '${scopeId}' names unknown reveal state '${state}' (see src/uiLocations/conditions.ts)`)
    else if (scopeForState.has(state)) errors.push(`reveal state '${state}' has two scopes ('${scopeForState.get(state)}', '${scopeId}'); one owner reports it`)
    else scopeForState.set(state, scopeId)
  }
  const revealerOf = new Map()
  const parentKind = new Map()
  const add = (id, entry, where) => {
    const held = scopes[id]
    if (held && held.revealer !== entry.revealer) {
      errors.push(`${where}: reveal scope '${id}' is opened by both '${held.revealer}' and '${entry.revealer}'; one scope has one revealer`)
      return
    }
    scopes[id] = entry
  }
  for (const r of registered) {
    r.d.placements.forEach((p, i) => {
      const where = `ui location '${r.id}' placement ${i}`
      for (const q of p.requires ?? []) {
        if (q.kind !== 'shown_by') continue
        if (q.when === undefined) {
          add(`reveal:${q.location}`, { kind: 'reveal', revealer: q.location }, where)
          continue
        }
        const sid = scopeForState.get(q.when)
        if (sid === undefined) {
          if (Object.hasOwn(inputs.revealStates ?? {}, q.when)) errors.push(`${where}: reveal state '${q.when}' has no scope in UI_REVEAL_SCOPES (src/uiLocations/conditions.ts); every reveal step names one`)
          continue
        }
        const prior = revealerOf.get(q.when)
        if (prior !== undefined && prior !== q.location) {
          errors.push(`${where}: reveal state '${q.when}' is revealed by both '${prior}' and '${q.location}'; one state has one revealer`)
          continue
        }
        revealerOf.set(q.when, q.location)
        add(sid, { kind: 'state', revealer: q.location, state: q.when }, where)
      }
      if (p.parent !== undefined && registeredIds.has(p.parent)) {
        // A tab selects a panel; every other entry is inside the one container
        // the parent opens (a menu, a sheet), whatever part of it the child is in.
        const kind = p.entry === 'tab' ? 'tab' : p.entry === 'menu' ? 'menu' : 'open'
        const prior = parentKind.get(p.parent)
        if (prior !== undefined && (prior === 'tab') !== (kind === 'tab')) {
          errors.push(`${where}: '${p.parent}' is reached both as a tab and as an opened container by its children; one control opens one way`)
          return
        }
        // Inside one container, a menu entry names it a menu.
        parentKind.set(p.parent, prior === 'menu' ? 'menu' : kind)
      }
    })
  }
  const scopeForParent = new Map()
  for (const [pid, kind] of parentKind) {
    const sid = `${kind}:${pid}`
    scopeForParent.set(pid, sid)
    add(sid, { kind, revealer: pid }, `ui location '${pid}'`)
  }
  // A disclosure opens the pane it heads (Older Sessions): its owner, the
  // shared `<GuideDisclosureScope>`, reports `open:<id>` whether or not a
  // registered location sits inside yet.
  for (const r of registered) {
    if (r.d.kind !== 'disclosure' || scopeForParent.has(r.id)) continue
    add(`open:${r.id}`, { kind: 'open', revealer: r.id }, `ui location '${r.id}'`)
  }
  // Cycles through a reveal: A needs B clicked, and B is only reachable through A.
  const edges = new Map()
  for (const r of registered) {
    const out = []
    for (const p of r.d.placements) {
      for (const q of p.requires ?? []) if (q.kind === 'shown_by' && registeredIds.has(q.location)) out.push({ to: q.location, reveal: true })
      if (p.parent !== undefined && registeredIds.has(p.parent)) out.push({ to: p.parent, reveal: false })
    }
    edges.set(r.id, out)
  }
  const state = new Map()
  const reported = new Set()
  const visit = (id, path) => {
    state.set(id, 'open')
    for (const e of edges.get(id) ?? []) {
      if (e.to === id) continue // reported as "shown by itself"
      const at = path.findIndex((s) => s.id === e.to)
      if (state.get(e.to) === 'open' && at >= 0) {
        // Each entry is a node and the edge it leaves by.
        const loop = [...path.slice(at), { id, reveal: e.reveal }]
        // Parent-only loops are already reported as parent cycles.
        if (loop.some((s) => s.reveal)) {
          const names = [...loop.map((s) => s.id), e.to].join(' -> ')
          if (!reported.has(names)) {
            reported.add(names)
            errors.push(`reveal cycle: ${names}`)
          }
        }
        continue
      }
      if (!state.has(e.to)) visit(e.to, [...path, { id, reveal: e.reveal }])
    }
    state.set(id, 'done')
  }
  for (const r of registered) if (!state.has(r.id)) visit(r.id, [])
  // Contradictions inside one resolved placement.
  const opposites = inputs.conditionOpposites ?? []
  for (const r of registered) {
    for (const [i, p] of (finalPlacements.get(r.id) ?? []).entries()) {
      const where = `ui location '${r.id}' placement ${i}`
      const views = new Set(p.requires.filter((q) => q.kind === 'viewport').map((q) => q.value))
      if (views.size > 1) errors.push(`${where}: needs both a desktop and a mobile viewport at once`)
      const conds = new Set(p.requires.filter((q) => q.kind === 'condition').map((q) => q.id))
      for (const [a, b] of opposites) if (conds.has(a) && conds.has(b)) errors.push(`${where}: needs opposite conditions '${a}' and '${b}' at once`)
    }
  }
  const sorted = Object.fromEntries(Object.entries(scopes).sort(([a], [b]) => (a < b ? -1 : 1)))
  return { scopes: sorted, scopeForState, scopeForParent }
}

/**
 * The selection and gate vocabularies a plan may use, checked against the
 * conditions table, the registered locations and the settings registry.
 * `selections`: condition id -> `{ picker, entity }`. `gates`: gate id ->
 * `{ setting_id }` (null when no setting turns it on), including one
 * `preview_flag:<flag>` gate per flag in `PREVIEW_FLAG_ENABLERS`.
 */
export function compileGuideNeeds({ inputs, registeredIds, predicates, errors }) {
  const conditions = inputs.conditions ?? {}
  const settingIds = new Set((inputs.settingsEntries ?? []).map((e) => e.id))
  const gates = new Map()
  for (const [id, g] of Object.entries(inputs.gates ?? {})) {
    if (!Object.hasOwn(conditions, id)) errors.push(`gate '${id}' is not a condition in src/uiLocations/conditions.ts`)
    if (predicates.has(id)) errors.push(`gate '${id}' is also a runtime predicate; a condition is one or the other`)
    const sid = g?.setting ?? null
    if (sid !== null && !settingIds.has(sid)) errors.push(`gate '${id}' names unknown setting '${sid}' (not in the settings registry)`)
    gates.set(id, { setting_id: sid })
  }
  for (const [flag, sid] of Object.entries(inputs.previewEnablers ?? {})) {
    if (!settingIds.has(sid)) errors.push(`preview flag '${flag}' enabler '${sid}' is not in the settings registry`)
    gates.set(previewGateId(flag), { setting_id: sid })
  }
  const selections = new Map()
  for (const [id, s] of Object.entries(inputs.selectionScopes ?? {})) {
    if (!Object.hasOwn(conditions, id)) errors.push(`selection '${id}' is not a condition in src/uiLocations/conditions.ts`)
    if (predicates.has(id) || gates.has(id)) errors.push(`selection '${id}' is also a predicate or a gate; a condition is one of the three`)
    if (!registeredIds.has(s.picker)) errors.push(`selection '${id}' picker '${s.picker}' is not a registered location`)
    if (!/^[a-z]+$/.test(s.entity ?? '')) errors.push(`selection '${id}' entity '${s.entity}' must be one lowercase word (it names catalog keys)`)
    selections.set(id, { picker: s.picker, entity: s.entity })
  }
  return { selections, gates }
}

/**
 * The `ui.show` plan for one registered location, or why it has none.
 *
 * Deny by default: only a curated location (a render-site marker proved where
 * it is drawn) whose every placement needs nothing but a viewport, reveal steps
 * and preview flags, and which is neither destructive nor already covered by
 * its own guide action. Each placement's steps are the controls a person
 * clicks to get there, in order: an ancestor's reveal steps, the registered
 * ancestor itself (a menu button, a sub-tab), the location's own reveal steps,
 * then the location. A page or tab in the path is reached by the placement's
 * route, not a step.
 *
 * Version 2: placements may take different numbers of steps. Each placement
 * has an id (its viewport, or `any`) and each step an id unique in the plan
 * (`<placement>:<key>`); every reveal step names the reveal scope it opens,
 * and every step carries the runtime predicates its control needs.
 *
 * Outermost first: the page (the placement's route), then a `gate` step per
 * gate or preview flag the path needs, then a `select` step per selection
 * (each after its picker's own reveal steps), then the reveal and menu steps,
 * then the location.
 *
 * `ctx`: `{ locations, registeredIds, describedIds, scopeForState,
 * scopeForParent, predicates, selections, gates, deny }`.
 */
/**
 * Runtime predicates the page that draws the location reports as soon as it
 * has loaded (MembersPage: whether the roster has a crewmate). Unlike a
 * control's own transient state, the guide knows it on arrival, so a location
 * needing one may still be planned: its last step carries it, and unmet it is
 * a blocker that says what to do first.
 */
const PAGE_FACT_PREDICATES = new Set(['has_crewmates', 'phone_connect_available'])
/**
 * Runtime predicates the owner of a location's opener reports (the goal and
 * monitor button's popover owner knows whether anything is running). On the
 * location they move to the opener's step, so the guide says there is
 * nothing to stop before it asks anyone to open the panel. Mirrors
 * `UI_OPENER_FACT_PREDICATES` in `src/uiLocations/conditions.ts`.
 */
export const OPENER_FACT_PREDICATES = new Set(['goal_loop_running', 'monitor_running'])

export function guidePlanFor(r, loc, ctx) {
  if (ctx.deny.has(r.id) || placesUnderTrustRoot(loc)) return { reason: 'denied' }
  if (r.d.guide === false) return { reason: 'opted_out' }
  if (r.d.guide) return { reason: 'own_guide_action' }
  if (r.description || loc.label_kind === 'description') return { reason: 'no_label' }
  if (loc.tier && loc.tier !== 'curated') return { reason: 'not_curated' }
  if (!Array.isArray(loc.placements) || loc.placements.length === 0) return { reason: 'no_placement' }
  const selections = ctx.selections ?? new Map()
  const gates = ctx.gates ?? new Map()
  const handled = (id) => !!ctx.predicates?.has(id) || selections.has(id) || gates.has(id)
  const viewportOf = (reqs) => reqs.find((q) => q.kind === 'viewport')?.value
  // A step's control is drawn where the target is: the same route (or the
  // shell, on every page), and no viewport the target placement cannot be in.
  const sameView = (q, route, v) => {
    const vq = viewportOf(q.requires)
    return (q.surface_id === SHELL_SURFACE || q.route === route) && (vq === undefined || vq === v)
  }
  const out = []
  // A control's own runtime conditions become the step's predicates; one the
  // page cannot evaluate makes the whole location search-only.
  let unknownPredicate = false
  const withExtras = (base, scope, requires) => ({
    ...base,
    ...(scope !== undefined ? { scope } : {}),
    ...(requires.length ? { requires } : {}),
  })
  for (const p of loc.placements) {
    if (p.requires.some((q) => !GUIDE_REQUIREMENT_KINDS.has(q.kind) || (q.kind === 'condition' && !handled(q.id)))) return { reason: 'condition' }
    const own = viewportOf(p.requires)
    // A placement drawn at every width whose path differs by viewport (the
    // session picker sits in the sidebar on a desktop, in the drawer on a
    // phone) is planned once per viewport instead.
    let planned = [planPlacement(p, own)]
    if (own === undefined && !planned[0].entry && planned[0].reason === 'unresolvable_step') {
      const split = ['desktop', 'mobile'].map((v) => planPlacement(p, v))
      if (split.every((x) => x.entry)) planned = split
    }
    for (const x of planned) {
      if (!x.entry) return { reason: x.reason }
      out.push(x.entry)
    }
  }
  function planPlacement(p, v) {
    const placementId = v ?? 'any'
    const route = p.surface_id === SHELL_SURFACE ? null : p.route
    let ok = true
    let unknownPredicate = false
    const stepId = (key) => `${placementId}:${key}`
    // Gates and selections any step's control needs, in the order first met.
    const needGates = []
    const needSelections = []
    const need = (id) => {
      const list = gates.has(id) ? needGates : needSelections
      if (!list.includes(id)) list.push(id)
    }
    // `implied`: the control belongs to a conditional reveal (`when`), which is
    // walked only while its state holds; that state already implies the
    // control's own selections and gates (the folded roster means a crewmate
    // is open), so they add no select or gate step of their own.
    const predicatesOf = (reqs, implied = false) => {
      const ids = []
      for (const x of reqs) {
        if (x.kind === 'preview_flag') {
          if (gates.has(previewGateId(x.flag))) need(previewGateId(x.flag))
          else unknownPredicate = true
          continue
        }
        if (x.kind !== 'condition') continue
        if (implied && (gates.has(x.id) || selections.has(x.id))) continue
        if (gates.has(x.id) || selections.has(x.id)) need(x.id)
        else if (!ctx.predicates?.has(x.id)) unknownPredicate = true
        else if (!ids.includes(x.id)) ids.push(x.id)
      }
      return ids
    }
    const pushReveal = (sid, when, depth, into, seen, implied = when !== undefined) => {
      if (seen.has(sid)) return true
      const s = ctx.locations.get(sid)
      if (depth > GUIDE_MAX_REVEAL_DEPTH || !s || !ctx.registeredIds.has(sid) || ctx.describedIds.has(sid)) return false
      const qs = s.placements.filter((q) => sameView(q, p.route, v))
      // A conditional reveal whose control this viewport never draws (the
      // phone's Back to roster on a desktop) is a state this viewport cannot
      // be in, so it is no step here.
      if (qs.length === 0 && when !== undefined && v !== undefined
        && s.placements.length > 0 && s.placements.every((q) => viewportOf(q.requires) !== undefined && viewportOf(q.requires) !== v)) return true
      if (qs.length !== 1) return false
      // A reveal control drawn inside a registered menu (the switcher's "Show
      // the full roster") is reached through that menu: its opener is a step
      // first, with the same scope a menu parent of the target would get.
      const menuParents = qs[0].parent_ids.filter((pid) => ctx.registeredIds.has(pid))
      for (const [k, pid] of menuParents.entries()) {
        if (seen.has(pid)) continue
        const a = ctx.locations.get(pid)
        const prefix = JSON.stringify(qs[0].parent_ids.slice(0, qs[0].parent_ids.indexOf(pid)))
        const aqs = (a?.placements ?? []).filter((q) => sameView(q, p.route, v) && JSON.stringify(q.parent_ids) === prefix)
        const pscope = ctx.scopeForParent?.get(pid)
        if (k > 0 || !a || ctx.describedIds.has(pid) || aqs.length !== 1 || pscope === undefined
          || aqs[0].requires.some((x) => x.kind === 'shown_by')) return false
        seen.add(pid)
        into.push(withExtras({ id: stepId(pid), location: pid, label_key: a.label_key }, pscope, predicatesOf(aqs[0].requires, implied)))
      }
      for (const x of qs[0].requires) {
        if (x.kind === 'shown_by' && !pushReveal(x.location, x.when, depth + 1, into, seen, implied || x.when !== undefined)) return false
      }
      // Every reveal step names the scope it opens (see compileRevealScopes).
      const scope = when !== undefined ? ctx.scopeForState?.get(when) : `reveal:${sid}`
      if (scope === undefined) return false
      seen.add(sid)
      const base = { id: stepId(sid), location: sid, label_key: s.label_key, ...(when !== undefined ? { when } : {}) }
      into.push(withExtras(base, scope, predicatesOf(qs[0].requires, implied)))
      return true
    }
    // The reveal and menu steps, walked first: their controls' conditions say
    // which gates and selections come before them.
    const rest = []
    const seenRest = new Set()
    p.parent_ids.forEach((pid, k) => {
      if (!ok || !ctx.registeredIds.has(pid)) return
      const a = ctx.locations.get(pid)
      const prefix = JSON.stringify(p.parent_ids.slice(0, k))
      const qs = (a?.placements ?? []).filter((q) => sameView(q, p.route, v) && JSON.stringify(q.parent_ids) === prefix)
      const scope = ctx.scopeForParent?.get(pid)
      if (!a || ctx.describedIds.has(pid) || qs.length !== 1 || scope === undefined) {
        ok = false
        return
      }
      for (const x of qs[0].requires) {
        if (x.kind === 'shown_by' && !pushReveal(x.location, x.when, 1, rest, seenRest)) ok = false
      }
      if (ok && !seenRest.has(pid)) {
        seenRest.add(pid)
        rest.push(withExtras({ id: stepId(pid), location: pid, label_key: a.label_key }, scope, predicatesOf(qs[0].requires)))
      }
    })
    for (const x of p.requires) {
      if (ok && x.kind === 'shown_by' && !pushReveal(x.location, x.when, 1, rest, seenRest)) ok = false
    }
    // The location's own conditions: predicates not already carried by an
    // earlier step (an inherited menu's), and the gates and selections it needs.
    const carried = new Set(rest.flatMap((s) => s.requires ?? []))
    const ownPredicates = predicatesOf(p.requires).filter((id) => !carried.has(id))
    // Each selection: its picker's reveal steps, then the select step.
    const front = []
    const seenFront = new Set()
    for (let i = 0; ok && i < needSelections.length; i++) {
      const selId = needSelections[i]
      const { picker, entity } = selections.get(selId)
      const s = ctx.locations.get(picker)
      const qs = s && ctx.registeredIds.has(picker) && !ctx.describedIds.has(picker)
        ? s.placements.filter((q) => sameView(q, p.route, v))
        : []
      // One picker placement on this page, directly on it, behind no other
      // gate or selection (a chain of those is not modelled). The preview that
      // holds the whole page is not another gate: the location needs it too,
      // so its gate step already runs before the select step.
      if (qs.length !== 1 || qs[0].parent_ids.some((pid) => ctx.registeredIds.has(pid))
        || qs[0].requires.some((x) => (x.kind === 'preview_flag' && !needGates.includes(previewGateId(x.flag))) || (x.kind === 'condition' && (gates.has(x.id) || selections.has(x.id))))) {
        ok = false
        break
      }
      for (const x of qs[0].requires) {
        if (ok && x.kind === 'shown_by' && !pushReveal(x.location, x.when, 1, front, seenFront)) ok = false
      }
      if (!ok) break
      const base = { id: stepId(`select:${selId}`), kind: 'select', location: picker, label_key: s.label_key, selection: selId, entity }
      front.push(withExtras(base, undefined, predicatesOf(qs[0].requires)))
    }
    if (!ok) return { reason: 'unresolvable_step' }
    const gateSteps = needGates.map((gid) => {
      const settingId = gates.get(gid).setting_id
      return { id: stepId(`gate:${gid}`), kind: 'gate', gate: gid, ...(settingId ? { setting_id: settingId } : {}) }
    })
    // A reveal a picker needed is walked before the select step, not again after.
    const steps = [...gateSteps, ...front, ...rest.filter((st) => !seenFront.has(st.location))]
    const carriedAll = new Set(steps.flatMap((st) => st.requires ?? []))
    // A runtime predicate on the location itself that no earlier step carries
    // is state the guide only finds out about after pointing at nothing: such
    // a location stays search-only, as before selections and gates.
    // Behind a select step it is different: the pick is what makes the state
    // knowable (the open crewmate keeps a private memory or not), so the
    // predicate travels on the last step and an unmet one is a blocker there.
    // So does a page fact its page always reports once mounted (`PAGE_FACT_PREDICATES`).
    const uncarried = ownPredicates.filter((id) => !carriedAll.has(id))
    // A fact the opener's own owner reports (the goal button knows whether a
    // loop runs) is checked before the opener is pressed: it travels on the
    // opener's step, so an unmet one is a blocker there and the panel is
    // never opened onto nothing to stop.
    const openerId = p.parent_ids[p.parent_ids.length - 1]
    const opener = steps.findLast((st) => st.location === openerId && st.kind === undefined)
    for (const id of uncarried.filter((x) => OPENER_FACT_PREDICATES.has(x))) {
      if (!opener) return { reason: 'condition' }
      opener.requires = [...(opener.requires ?? []), id]
    }
    const stillUncarried = uncarried.filter((id) => !OPENER_FACT_PREDICATES.has(id))
    const afterSelect = steps.some((st) => st.kind === 'select')
    if (stillUncarried.some((id) => !afterSelect && !PAGE_FACT_PREDICATES.has(id))) return { reason: 'condition' }
    steps.push(withExtras({ id: stepId(r.id), location: r.id, label_key: loc.label_key, ...(ctx.caution?.has(r.id) ? { caution: true, ...(loc.caution_key ? { caution_key: loc.caution_key } : {}) } : {}) }, undefined, stillUncarried))
    if (unknownPredicate) return { reason: 'unknown_predicate' }
    if (steps.length > GUIDE_MAX_STEPS) return { reason: 'too_many_steps' }
    return { entry: { id: placementId, route, ...(v !== undefined ? { viewport: v } : {}), steps } }
  }
  // Identical placements (one route reached two equivalent ways) are one plan.
  const placements = out.filter((x, i) => out.findIndex((y) => JSON.stringify(y) === JSON.stringify(x)) === i)
  // The page picks a placement by viewport alone, so each viewport has at most
  // one, and a placement for every viewport stands alone.
  const views = placements.map((x) => x.id)
  if (new Set(views).size !== views.length || (views.includes('any') && views.length > 1)) return { reason: 'ambiguous_placement' }
  return { plan: { version: GUIDE_PLAN_VERSION, label_key: loc.label_key, placements } }
}

/** Whether any placement of *loc* sits under a tab of the agent's own ceiling. */
function placesUnderTrustRoot(loc) {
  return (loc.placements ?? []).some((p) => p.parent_ids.some((pid) => TRUST_ROOT_PARENTS.includes(pid)))
}

// ---------------------------------------------------------------- auto tier: single-step plans

/** Placement ids of a shared location's plan, one per candidate page: `pa`, `pb`, ... `pz`, `paa`, ... */
function sharedPlacementId(i) {
  let n = i
  let out = ''
  do {
    out = String.fromCharCode(97 + (n % 26)) + out
    n = Math.floor(n / 26) - 1
  } while (n >= 0)
  return `p${out}`
}

/**
 * The single-step `ui.show` plan of a guidable auto location: go to its page,
 * point at its one site. A shared location (drawn by a file several pages
 * share) has one placement per candidate page, in its placement order, each
 * pointing at the same site: the page walks the one the person is on when it
 * is a candidate, else the first. A `caution` location's step says so.
 */
export function autoGuidePlan(loc, siteId, { caution = false } = {}) {
  const shared = loc.placements.length > 1
  return {
    version: GUIDE_PLAN_VERSION,
    label_key: loc.label_key,
    placements: loc.placements.map((p, i) => {
      const id = shared ? sharedPlacementId(i) : 'any'
      return {
        id,
        route: p.surface_id === SHELL_SURFACE ? null : p.route,
        steps: [{ id: `${id}:${loc.id}`, location: siteId, label_key: loc.label_key, ...(caution ? { caution: true } : {}) }],
      }
    }),
  }
}
