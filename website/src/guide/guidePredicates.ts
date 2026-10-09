/**
 * Live runtime predicates for `ui.show` guide steps.
 *
 * A reveal control can need a runtime condition to be drawn at all (the
 * sessions sidebar toggle exists only with an open session, in the full
 * dashboard). The generator carries such a condition on the step as
 * `requires` (`UI_RUNTIME_PREDICATES` in `uiLocations/conditions.ts`, the
 * closed vocabulary); this module is the browser's evaluator for each one. A
 * step whose predicate is unmet shows a blocker instead of pointing, and its
 * report to the gateway says `predicate_unmet`.
 *
 * Facts come from the components that already know them, through
 * {@link useGuidePredicate}: one entry per mounted reporter, like scope owners.
 * A predicate is met when any mounted reporter says so, unmet when every one
 * says not, and unknown with none mounted. Nothing here reads the page or
 * leaves the tab except the met/unmet/unknown state of an id.
 */
import { useEffect, useState } from 'react'
import {
  PREVIEW_GATE_PREFIX,
  UI_GATES,
  UI_RUNTIME_PREDICATES,
  UI_SELECTION_SCOPES,
  type UiGateId,
  type UiRuntimePredicateId,
  type UiSelectionId,
} from '../uiLocations/conditions'
import { readPreviewFlag } from '../utils/previewFlags'

export type PredicateState = 'met' | 'unmet' | 'unknown'

/**
 * A selection scope's state: `selected` (an entity is picked), `none` (the
 * picker has entries but none is picked), `empty` (nothing to pick), or
 * `unknown` (no owner of the picker is mounted).
 */
export type SelectionState = 'selected' | 'none' | 'empty' | 'unknown'

const facts = new Map<string, Map<symbol, boolean>>()
const selections = new Map<string, Map<symbol, { selected: boolean; available: boolean; name?: string; aliases?: readonly string[]; identity?: string }>>()

function reported(id: string): boolean | null {
  const held = facts.get(id)
  if (!held || held.size === 0) return null
  for (const v of held.values()) if (v) return true
  return false
}

/**
 * One evaluator per runtime predicate. The `Record` type makes a predicate
 * added to `UI_RUNTIME_PREDICATES` without an evaluator a compile error.
 */
export const GUIDE_PREDICATE_EVALUATORS: Record<UiRuntimePredicateId, () => boolean | null> = {
  has_open_sessions: () => reported('has_open_sessions'),
  full_dashboard: () => reported('full_dashboard'),
  not_on_sessions_page: () => reported('not_on_sessions_page'),
  schedule_list_view: () => reported('schedule_list_view'),
  has_schedules: () => reported('has_schedules'),
  has_crewmates: () => reported('has_crewmates'),
  crewmate_danger_zone_open: () => reported('crewmate_danger_zone_open'),
  crewmate_place_pane_open: () => reported('crewmate_place_pane_open'),
  crewmate_model_pane_open: () => reported('crewmate_model_pane_open'),
  crewmate_memory_manageable: () => reported('crewmate_memory_manageable'),
  no_response_running: () => reported('no_response_running'),
  mouse_input: () => reported('mouse_input'),
  phone_connect_available: () => reported('phone_connect_available'),
  goal_loop_running: () => reported('goal_loop_running'),
  monitor_running: () => reported('monitor_running'),
}

/** One evaluator per gate, like predicates; a preview flag is read from storage directly. */
export const GUIDE_GATE_EVALUATORS: Record<UiGateId, () => boolean | null> = {
  developer_mode: () => reported('developer_mode'),
  terminal_enabled: () => reported('terminal_enabled'),
}

const KNOWN: ReadonlySet<string> = new Set(UI_RUNTIME_PREDICATES)

export function isRuntimePredicate(id: string): id is UiRuntimePredicateId {
  return KNOWN.has(id)
}

export function isSelectionScope(id: string): id is UiSelectionId {
  return Object.hasOwn(UI_SELECTION_SCOPES, id)
}

/** A `UI_GATES` id or a `preview_flag:<flag>` gate. */
export function isGate(id: string): boolean {
  return Object.hasOwn(UI_GATES, id) || (id.startsWith(PREVIEW_GATE_PREFIX) && id.length > PREVIEW_GATE_PREFIX.length)
}

/** Live state of selection *id*: any mounted owner with a pick wins, else empty only when every one is empty. */
export function selectionState(id: string): SelectionState {
  const held = selections.get(id)
  if (!isSelectionScope(id) || !held || held.size === 0) return 'unknown'
  let available = false
  for (const v of held.values()) {
    if (v.selected) return 'selected'
    if (v.available) available = true
  }
  return available ? 'none' : 'empty'
}

/**
 * The name of the entity picked in selection *id*, as its picker's rows are
 * registered with it (`guidePick`): the one mounted owner reporting a pick, else
 * undefined (none, several, or an owner that names none). Read in this tab
 * only, to bind a guide's named pick to what is actually open and to name it
 * in the confirm line; never part of a report.
 */
export function selectedName(id: string): string | undefined {
  const held = selections.get(id)
  if (!held) return undefined
  const picked = Array.from(held.values()).filter(v => v.selected)
  return picked.length === 1 ? picked[0].name : undefined
}

/**
 * What the picked entity is, to tell it from another one with the same name:
 * the `identity` its owner reported (an open session's key), else its name.
 * Read in this tab only, to freeze a choice; never shown, never reported.
 */
export function selectedIdentity(id: string): string | undefined {
  const held = selections.get(id)
  if (!held) return undefined
  const picked = Array.from(held.values()).filter(v => v.selected)
  return picked.length === 1 ? picked[0].identity ?? picked[0].name : undefined
}

/**
 * The other names the picked entity was reported with (`useGuideSelection`
 * `aliases`: a built-in app's manifest name beside its translated one), so a
 * pick given in either name binds to it. Empty unless exactly one is picked.
 */
export function selectedAliases(id: string): readonly string[] {
  const held = selections.get(id)
  if (!held) return []
  const picked = Array.from(held.values()).filter(v => v.selected)
  return picked.length === 1 ? picked[0].aliases ?? [] : []
}

/** Whether gate *id* is on: true / false, null with no reporter mounted. */
export function gateOn(id: string): boolean | null {
  if (id.startsWith(PREVIEW_GATE_PREFIX)) return readPreviewFlag(id.slice(PREVIEW_GATE_PREFIX.length))
  return Object.hasOwn(UI_GATES, id) ? GUIDE_GATE_EVALUATORS[id as UiGateId]() : null
}

/**
 * Live state of *id*: a runtime predicate, a selection (met once picked) or a
 * gate (met once on). An id this build has no evaluator for is `unmet`: fail
 * closed. Only this enum ever leaves the page, never what was picked.
 */
export function predicateState(id: string): PredicateState {
  if (isSelectionScope(id)) {
    const s = selectionState(id)
    return s === 'unknown' ? 'unknown' : s === 'selected' ? 'met' : 'unmet'
  }
  if (isGate(id)) {
    const on = gateOn(id)
    return on === null ? 'unknown' : on ? 'met' : 'unmet'
  }
  if (!isRuntimePredicate(id)) return 'unmet'
  const v = GUIDE_PREDICATE_EVALUATORS[id]()
  return v === null ? 'unknown' : v ? 'met' : 'unmet'
}

/** The predicates of *ids* that are known to be unmet right now (unknown is not a blocker). */
export function unmetPredicates(ids: readonly string[] | undefined): string[] {
  return (ids ?? []).filter(id => predicateState(id) === 'unmet')
}

export function reportPredicate(id: UiRuntimePredicateId | UiGateId, owner: symbol, value: boolean): void {
  let held = facts.get(id)
  if (!held) facts.set(id, (held = new Map()))
  held.set(owner, value)
}

export function dropPredicate(id: UiRuntimePredicateId | UiGateId, owner: symbol): void {
  const held = facts.get(id)
  if (!held) return
  held.delete(owner)
  if (held.size === 0) facts.delete(id)
}

/**
 * A component that knows *id* reports it while mounted. `null` reports
 * nothing (the fact is not known yet, e.g. a list still loading), so the
 * predicate reads unknown instead of a blocker flashing up.
 */
export function useGuidePredicate(id: UiRuntimePredicateId, value: boolean | null): void {
  const [owner] = useState(() => Symbol('guide-predicate'))
  useEffect(() => {
    if (value === null) return
    reportPredicate(id, owner, value)
    return () => dropPredicate(id, owner)
  }, [id, value, owner])
}

/** The shell, which knows whether gate *id* is on, reports it while mounted. */
export function useGuideGate(id: UiGateId, on: boolean): void {
  const [owner] = useState(() => Symbol('guide-gate'))
  useEffect(() => {
    reportPredicate(id, owner, on)
    return () => dropPredicate(id, owner)
  }, [id, on, owner])
}

export function reportSelection(id: UiSelectionId, owner: symbol, selected: boolean, available: boolean, name?: string, aliases?: readonly string[], identity?: string): void {
  let held = selections.get(id)
  if (!held) selections.set(id, (held = new Map()))
  const also = selected && name ? (aliases ?? []).filter(a => a && a !== name) : []
  held.set(owner, {
    selected, available,
    ...(selected && name ? { name } : {}),
    ...(also.length ? { aliases: also } : {}),
    ...(selected && identity ? { identity } : {}),
  })
}

export function dropSelection(id: UiSelectionId, owner: symbol): void {
  const held = selections.get(id)
  if (!held) return
  held.delete(owner)
  if (held.size === 0) selections.delete(id)
}

/**
 * The page that owns a selection scope's picker reports, while mounted,
 * whether one entity is picked, whether there is any to pick, and the picked
 * one's `name`: the same string its row carries as `data-guide-pick` (and
 * its `alias`, the other name its row is registered with, if any). The
 * name stays in this tab (`selectedName`); only the selection's state is ever
 * reported to the gateway.
 */
export function useGuideSelection(id: UiSelectionId, { selected, available, name, alias, identity }: { selected: boolean; available: boolean; name?: string; alias?: string; identity?: string }): void {
  const [owner] = useState(() => Symbol('guide-selection'))
  useEffect(() => {
    reportSelection(id, owner, selected, available, name, alias ? [alias] : undefined, identity)
    return () => dropSelection(id, owner)
  }, [id, selected, available, name, alias, identity, owner])
}

/** How often a shown blocker re-reads its predicates. */
export const GUIDE_PREDICATE_TICK_MS = 250

/** The unmet predicates of *ids*, re-read on a short tick while *enabled*. */
export function useUnmetPredicates(ids: readonly string[] | undefined, enabled: boolean): readonly string[] {
  const [unmet, setUnmet] = useState<readonly string[]>([])
  const key = (ids ?? []).join('|')
  useEffect(() => {
    if (!enabled || !key) {
      setUnmet(prev => (prev.length ? [] : prev))
      return
    }
    const list = key.split('|')
    const read = () => {
      const next = unmetPredicates(list)
      setUnmet(prev => (prev.join('|') === next.join('|') ? prev : next))
    }
    read()
    const t = setInterval(read, GUIDE_PREDICATE_TICK_MS)
    return () => clearInterval(t)
  }, [key, enabled])
  return unmet
}

/** The catalog key that says, in the person's words, what each predicate needs. */
export const GUIDE_PREDICATE_TEXT_KEYS: Record<UiRuntimePredicateId, string> = {
  has_open_sessions: 'components.guideLayer.predicate_has_open_sessions',
  full_dashboard: 'components.guideLayer.predicate_full_dashboard',
  not_on_sessions_page: 'components.guideLayer.predicate_not_on_sessions_page',
  schedule_list_view: 'components.guideLayer.predicate_schedule_list_view',
  has_schedules: 'components.guideLayer.predicate_has_schedules',
  has_crewmates: 'components.guideLayer.predicate_has_crewmates',
  crewmate_danger_zone_open: 'components.guideLayer.predicate_crewmate_danger_zone_open',
  crewmate_place_pane_open: 'components.guideLayer.predicate_crewmate_place_pane_open',
  crewmate_model_pane_open: 'components.guideLayer.predicate_crewmate_model_pane_open',
  crewmate_memory_manageable: 'components.guideLayer.predicate_crewmate_memory_manageable',
  no_response_running: 'components.guideLayer.predicate_no_response_running',
  mouse_input: 'components.guideLayer.predicate_mouse_input',
  phone_connect_available: 'components.guideLayer.predicate_phone_connect_available',
  goal_loop_running: 'components.guideLayer.predicate_goal_loop_running',
  monitor_running: 'components.guideLayer.predicate_monitor_running',
}

/**
 * A select step's instruction, its empty-picker blocker, and the confirm line
 * for a pick already made -- `confirm` while its picker is drawn, and
 * `confirm_unseen` (no "in the list") while the picker is folded away.
 */
export const GUIDE_SELECTION_TEXT_KEYS: Record<typeof UI_SELECTION_SCOPES[UiSelectionId]['entity'], { choose: string; empty: string; confirm: string; confirm_unseen: string }> = {
  session: { choose: 'components.guideLayer.select_session', empty: 'components.guideLayer.select_empty_session', confirm: 'components.guideLayer.select_confirm_session', confirm_unseen: 'components.guideLayer.select_confirm_unseen_session' },
  crewmate: { choose: 'components.guideLayer.select_crewmate', empty: 'components.guideLayer.select_empty_crewmate', confirm: 'components.guideLayer.select_confirm_crewmate', confirm_unseen: 'components.guideLayer.select_confirm_unseen_crewmate' },
  job: { choose: 'components.guideLayer.select_job', empty: 'components.guideLayer.select_empty_job', confirm: 'components.guideLayer.select_confirm_job', confirm_unseen: 'components.guideLayer.select_confirm_unseen_job' },
  // Ticking one job's box (a move of that job alone). No step that removes
  // something follows it, so its confirm lines are the job's own.
  tickjob: { choose: 'components.guideLayer.select_tickjob', empty: 'components.guideLayer.select_empty_job', confirm: 'components.guideLayer.select_confirm_job', confirm_unseen: 'components.guideLayer.select_confirm_unseen_job' },
  app: { choose: 'components.guideLayer.select_app', empty: 'components.guideLayer.select_empty_app', confirm: 'components.guideLayer.select_confirm_app', confirm_unseen: 'components.guideLayer.select_confirm_unseen_app' },
  artifact: { choose: 'components.guideLayer.select_artifact', empty: 'components.guideLayer.select_empty_artifact', confirm: 'components.guideLayer.select_confirm_artifact', confirm_unseen: 'components.guideLayer.select_confirm_unseen_artifact' },
}

/** A gate with no setting to name says what it needs in its own words. */
export const GUIDE_GATE_TEXT_KEYS: Record<UiGateId, string> = {
  developer_mode: 'components.guideLayer.gate_developer_mode',
  terminal_enabled: 'components.guideLayer.gate_terminal_enabled',
}
