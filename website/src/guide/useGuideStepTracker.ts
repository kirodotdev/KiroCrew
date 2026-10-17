/**
 * Finds the control the current guide step points at and follows it.
 *
 * The target is the ONE element the registry names: a registered
 * `data-guide-anchor`, the exact Settings row (`resolveSettingElementStrict`),
 * or the one visible element carrying a UI location's `data-ui-location`.
 * It must be visibly rendered; nothing near it stands in. While it is absent
 * the tracker waits a bounded time for a panel that mounts late, then reports
 * `target_missing` once. A `reach` step reports `observed` once the UI shows a
 * later registered target — the human moved the form on (or the menu, panel
 * or sidebar it opens is already open, so the step is skipped). A `committed` step is
 * never reported by the browser; after its save was submitted, the target
 * leaving (a dialog closing on success) is not "missing" either. Once a step
 * IS missing, the same hook runs in `recover` mode: the target (or a later
 * anchor of a `reach` step) coming back reports `onFound`, re-offered on a
 * slower cadence a bounded number of times so one failed write does not
 * strand it, and the gateway returns the guide to that same step.
 *
 * A step carrying runtime predicates (`step.requires`) is never pointed past
 * them: with its target absent and a predicate unmet, the step is reported
 * missing with detail `predicate_unmet` after the short settle, and it
 * recovers only once the predicates hold and the target is drawn.
 */
import { useEffect, useRef, useState } from 'react'
import { resolveSettingElementStrict } from '../hooks/useSettingHighlight'
import { atPage, findGuideAnchor, findOpenerLocation, findUiLocation, isPickedName, pickCandidate, pickItems, type GuideStepPlan, type GuideTarget } from './guideActions'
import { predicateState, selectedAliases, selectedIdentity, selectedName, selectionState, unmetPredicates } from './guidePredicates'
import { UI_OPENER_FACT_PREDICATES } from '../uiLocations/conditions'
import { firstShown, isDisplayed, liveTarget, scopeOpen, soleShown, uiLocationCopies } from './liveRegistry'
import { accessibleName, findNeedsPick, findPick, findState, isProbing, resolveContainerPath, resolveFind, searchByName } from './findByName'
import { isSafeOpenerTarget } from './findTargetPolicy'
import { GUIDE_LAYER_SELECTOR } from './probeRegistry'
import { closestRegistered } from '../uiLocations/targetRegistry'

/**
 * How long a page still loading (a skeleton marks its container `aria-busy`)
 * holds off a step's first miss: a session drawn as placeholders has not
 * drawn its controls yet, so "not on this screen" would be said too early.
 */
export const GUIDE_PAGE_SETTLE_MAX_MS = 10_000

/** Whether the page outside the guide's own layer is still loading. */
export function pageStillLoading(root: ParentNode = document): boolean {
  return Array.from(root.querySelectorAll('[aria-busy="true"]')).some(el => !el.closest(GUIDE_LAYER_SELECTOR))
}

/** How long a registered target may take to appear before it is missing. */
export const GUIDE_TARGET_WAIT_MS = 10_000
/** How long the page must show an EARLIER step of the action, with this step's
 *  target absent, before the step counts as missing (and recovers back there). */
export const GUIDE_EARLIER_STEP_WAIT_MS = 1_500
/** Poll cadence while a step is tracked; scroll and resize re-measure at once. */
export const GUIDE_TRACK_TICK_MS = 250
/** A recovered target re-offers `onFound` this often until the guide resumes. */
export const GUIDE_FOUND_RETRY_MS = 2_000
/** How many times a recovered target is offered before the tracker gives up. */
export const GUIDE_FOUND_MAX_ATTEMPTS = 5

export interface GuideRect {
  top: number
  left: number
  width: number
  height: number
}

export function resolveGuideTarget(target: GuideTarget): HTMLElement | null {
  if (target.kind === 'none') return null
  if (target.kind === 'anchor') return findGuideAnchor(target.anchor)
  if (target.kind === 'location') {
    const el = findUiLocation(target.id, isGuideTargetVisible, target.repeated === true)
    return el && target.pickFrom ? pickCandidate(el, isGuideTargetVisible, target.pickName, target.pickControl) ?? el : el
  }
  // While a probe holds a container open, what it shows is the probe's, not the person's.
  if ((target.kind === 'find' || target.kind === 'find-container') && isProbing()) return null
  if (target.kind === 'find') return resolveFind(target.query, target.key, isGuideTargetVisible)
  if (target.kind === 'find-container') {
    const s = findState(target.key)
    if (s?.status === 'opener') {
      // Re-judged on the element shown now: the trigger can have changed
      // since the probe chose it.
      const el = findOpenerLocation(s.location, isGuideTargetVisible)
      return el && isSafeOpenerTarget(el, accessibleName(el)) ? el : null
    }
    return s?.status === 'found' ? resolveContainerPath(s.path, isGuideTargetVisible) : null
  }
  return resolveSettingElementStrict(target.entry)
}

/**
 * The entity a select step with no `pick` settled on, per selection: frozen
 * when the step completes (the person chose it, or confirmed it with Next),
 * so every later step is about that same entity until the guide ends. Opening
 * another one is the selection changing, which sends the guide back to choose.
 */
const frozenPicks = new Map<string, string>()

/** Whether the entity open in *selection* is the one *want* names (by its shown name or its alias). */
function openIsNamed(selection: string, want: string): boolean {
  return isPickedName(selectedName(selection), want) || selectedAliases(selection).some(a => isPickedName(a, want))
}

/** Freeze the entity open now for a completed select step that named none. */
export function freezePick(step: GuideStepPlan): void {
  const c = step.complete
  if (c.kind !== 'select' || step.target.kind !== 'location' || step.target.pickName !== undefined) return
  const who = selectedIdentity(c.selection)
  if (who) frozenPicks.set(c.selection, who)
}

/** Forget a frozen choice: the select step is being made again, or the guide ended. */
export function clearFrozenPicks(selection?: string): void {
  if (selection === undefined) frozenPicks.clear()
  else frozenPicks.delete(selection)
}

/**
 * Whether the selection a step depends on still holds: an entity is open
 * and, when a pick named one or the person settled on one, it is that
 * entity. A step with no bound holds.
 */
export function stepBoundHolds(step: GuideStepPlan): boolean {
  const b = step.bound
  if (!b) return true
  if (selectionState(b.selection) !== 'selected') return false
  if (b.pickName !== undefined) return openIsNamed(b.selection, b.pickName)
  const frozen = frozenPicks.get(b.selection)
  return frozen === undefined || selectedIdentity(b.selection) === frozen
}

/** An element drawn for one entity outside its row (its ⋯ menu) is registered so (`guidePickOf`). */

const OPENER_FACTS: ReadonlySet<string> = new Set(UI_OPENER_FACT_PREDICATES)

/**
 * Selections whose pick is the list row marked current (`aria-current`): the
 * open session is the sidebar row the chat shows. A step bound to one, whose
 * control every row draws, points at the current row's copy.
 */
const CURRENT_ROW_SELECTIONS: ReadonlySet<string> = new Set(['session_open'])

/** Whether a predicate the step's opener owner reports (`UI_OPENER_FACT_PREDICATES`) is known unmet. */
function openerFactUnmet(step: GuideStepPlan): boolean {
  return unmetPredicates(step.requires).some(id => OPENER_FACTS.has(id))
}

/**
 * A step's own target, held to the selection it depends on: nothing while
 * that selection no longer holds, and never a copy drawn for another entity
 * than the one open (another app's menu item). Unbound steps resolve as
 * `resolveGuideTarget` does.
 */
export function resolveStepTarget(step: GuideStepPlan): HTMLElement | null {
  // An opener whose panel has nothing to act on (no loop running) is not
  // pointed at: the step reads as blocked and says why.
  if (openerFactUnmet(step)) return null
  if (!step.bound) return resolveGuideTarget(step.target)
  if (!stepBoundHolds(step)) return null
  const t = step.target
  const open = selectedName(step.bound.selection)
  const ownedRight = (el: HTMLElement) => {
    const owner = closestRegistered(el, 'pickOf')
    return !owner || (open !== undefined && isPickedName(owner.id, open))
  }
  if (t.kind === 'location') {
    const shown = (el: HTMLElement) => isGuideTargetVisible(el) && ownedRight(el)
    const copies = uiLocationCopies(t.id)
    if (t.repeated === true) return firstShown(copies, shown)
    const sole = soleShown(copies, shown).element
    if (sole || !CURRENT_ROW_SELECTIONS.has(step.bound.selection)) return sole
    // One copy per row (a session row's ⋯): the one on the row that is open.
    const current = Array.from(copies).filter(el => shown(el) && !!el.closest('[aria-current="true"]'))
    return current.length === 1 ? current[0] : null
  }
  const el = resolveGuideTarget(t)
  return el && ownedRight(el) ? el : null
}

/**
 * A `ui.find` step whose search has concluded the control cannot be shown:
 * nothing on the page or in its registered containers carries the name, or
 * the only match is part of the agent's own ceiling (`not_found` both: the
 * gateway hears no more than that), or several visible controls do and the
 * person has not picked one (`ambiguous`). Null while it may still be found
 * (searching, or found inside a container).
 */
function findVerdict(step: GuideStepPlan): 'not_found' | 'ambiguous' | null {
  const t = step.target
  const key = t.kind === 'find' || t.kind === 'find-container' ? t.key : null
  if (!key) return null
  if (isProbing()) return null
  const inner = t.kind === 'find' ? t : step.complete.kind === 'reach' ? step.complete.targets[0] : undefined
  if (inner?.kind === 'find') {
    const live = searchByName(inner.query, document, isGuideTargetVisible).result
    if (live === 'sensitive') return 'not_found'
    // Several and none picked, or the pick went stale (the list changed):
    // asked again, never a remaining single match taken for the picked one.
    if ((live === 'ambiguous' || live === 'found') && findNeedsPick(inner.query, key, isGuideTargetVisible)) return 'ambiguous'
  }
  const s = findState(key)
  if (s?.status === 'none') return 'not_found'
  if (s?.status === 'ambiguous' && findPick(key) === undefined) return 'ambiguous'
  return null
}

/**
 * An `open` step whose probe found several matches inside one container:
 * once the person has opened it and they are on screen, the step is done,
 * and the next one lets them pick.
 */
function hiddenChoiceShown(step: GuideStepPlan): boolean {
  const t = step.target
  if (t.kind !== 'find-container' || step.complete.kind !== 'reach') return false
  const s = findState(t.key)
  const inner = step.complete.targets[0]
  return s?.status === 'found' && (s.count ?? 0) > 1 && inner?.kind === 'find'
    && searchByName(inner.query, document, isGuideTargetVisible).result === 'ambiguous'
}

/** Why a step is missing, when the page can tell (see `GuideMissingDetail`). */
export type GuideStepMissingDetail = 'ambiguous' | 'predicate_unmet' | 'gate_off' | 'selection_empty' | 'not_found'

/**
 * A gate, select or arrive step's own verdict, read from the page's facts,
 * never from what is drawn: `done` (the gate is on, the entity is picked, the
 * person is on the page), `blocked` (the gate is off, the picker is empty: a
 * blocker, no pointing), `waiting` (unknown: no reporter yet), or `point` (a
 * select step pointing at its picker until the pick). An arrive step not yet
 * on its page has no verdict: it points at the entry that opens the page like
 * any other target, and arriving is only what completes it.
 */
export function factVerdict(step: GuideStepPlan): 'done' | 'blocked' | 'waiting' | 'point' | null {
  const c = step.complete
  if (c.kind === 'arrive') {
    return atPage(c.route, window.location) ? 'done' : null
  }
  if (c.kind === 'gate') {
    const s = predicateState(c.gate)
    return s === 'met' ? 'done' : s === 'unmet' ? 'blocked' : 'waiting'
  }
  if (c.kind === 'select') {
    const s = selectionState(c.selection)
    if (s === 'empty') return 'blocked'
    // A named pick binds the step: only that entity open completes it.
    // Another one open (or the pick not in the list) keeps pointing, so the
    // person picks; nothing stands in for the one they named.
    if (pickNameOf(step) !== undefined) return pickMade(step) ? 'done' : 'point'
    if (s === 'selected') return 'done'
    return soleChoiceShown(step) ? 'done' : 'point'
  }
  return null
}

/** The entity a select step was told the person means (`ui.show` `pick`), if any. */
function pickNameOf(step: GuideStepPlan): string | undefined {
  return step.target.kind === 'location' ? step.target.pickName : undefined
}

/**
 * Whether a select step's pick is made: an entity is open and, when the step
 * names one, it is that entity (`selectedName`, the name its row carries).
 */
export function pickMade(step: GuideStepPlan): boolean {
  const c = step.complete
  if (c.kind !== 'select' || selectionState(c.selection) !== 'selected') return false
  const want = pickNameOf(step)
  return want === undefined || openIsNamed(c.selection, want)
}

/**
 * A select step whose picker holds a single item while the next step's
 * control is already on screen: there is no choice left to make.
 */
function soleChoiceShown(step: GuideStepPlan): boolean {
  const c = step.complete
  if (c.kind !== 'select' || !c.then || step.target.kind !== 'location') return false
  const picker = findUiLocation(step.target.id, isGuideTargetVisible)
  if (!picker || pickItems(picker).length !== 1) return false
  return isGuideTargetVisible(resolveGuideTarget(c.then))
}

/**
 * Whether a `reach` step is done: its reveal scope's owner reports it open, or
 * a LATER target is on screen (the human moved on). The scope is the explicit
 * fact; the later target is the fallback for a container no owner reports.
 */
function laterTargetVisible(step: GuideStepPlan): boolean {
  if (step.complete.kind !== 'reach') return false
  // A later control shown for another entity than the one chosen is not this one's.
  if (!stepBoundHolds(step)) return false
  if (step.complete.scope && scopeOpen(step.complete.scope) === true) return true
  if (!isProbing() && hiddenChoiceShown(step)) return true
  return step.complete.targets.some(t => isGuideTargetVisible(resolveGuideTarget(t)))
}

/**
 * The part of *el* its clipping ancestors let show: a table wider than its
 * scrolling panel is outlined where it is visible, never past the panel's
 * edge over whatever the panel cuts off. Falls back to the whole box when the
 * visible part is empty (the step then reads as the control it is).
 */
export function visibleBox(el: Element): { top: number; left: number; width: number; height: number } {
  const r = el.getBoundingClientRect()
  let top = r.top
  let left = r.left
  let bottom = r.bottom
  let right = r.right
  for (let node = el.parentElement; node && node !== document.body; node = node.parentElement) {
    const style = getComputedStyle(node)
    const clipsX = style.overflowX !== 'visible'
    const clipsY = style.overflowY !== 'visible'
    if (!clipsX && !clipsY) continue
    const b = node.getBoundingClientRect()
    if (clipsX) { left = Math.max(left, b.left); right = Math.min(right, b.right) }
    if (clipsY) { top = Math.max(top, b.top); bottom = Math.min(bottom, b.bottom) }
  }
  if (right <= left || bottom <= top) return { top: r.top, left: r.left, width: r.width, height: r.height }
  return { top, left, width: right - left, height: bottom - top }
}

/**
 * An acknowledge step bound to a page (`ack.at`, a page guide's "you're
 * here") while the person is somewhere else: it says something untrue there,
 * so it is missing rather than waiting for Done.
 */
export function offStepRoute(step: GuideStepPlan): boolean {
  const c = step.complete
  return c.kind === 'ack' && c.at !== undefined && !atPage(c.at, window.location)
}

/** Rendered and painted: connected, non-empty box, not hidden or inert. */
export const isGuideTargetVisible = isDisplayed

/** Why a step's target is missing, when the page can tell. */
function missingDetail(step: GuideStepPlan): GuideStepMissingDetail | undefined {
  if (step.complete.kind === 'gate') return 'gate_off'
  if (step.complete.kind === 'select' && selectionState(step.complete.selection) === 'empty') return 'selection_empty'
  if (unmetPredicates(step.requires).length > 0) return 'predicate_unmet'
  const find = findVerdict(step)
  if (find) return find
  return step.target.kind === 'location' && step.target.repeated !== true && liveTarget(step.target.id).status === 'ambiguous' ? 'ambiguous' : undefined
}

const sameRect = (a: GuideRect | null, b: GuideRect | null) =>
  a === b || (!!a && !!b && a.top === b.top && a.left === b.left && a.width === b.width && a.height === b.height)

/**
 * Index of the LATEST earlier step whose own target is visible, or -1. A
 * select step whose choice was replaced counts too: the frozen entity is no
 * longer the one open (another artifact's page was opened after the pick),
 * so the guide goes back to choosing even where the picker is not drawn.
 */
export function latestVisibleEarlierStep(steps: readonly GuideStepPlan[] | undefined): number {
  if (!steps) return -1
  for (let i = steps.length - 1; i >= 0; i--) {
    if (isGuideTargetVisible(resolveStepTarget(steps[i])) || choiceReplaced(steps[i])) return i
  }
  return -1
}

/** A select step that named no entity whose frozen pick is not the entity open now. */
function choiceReplaced(step: GuideStepPlan): boolean {
  const c = step.complete
  if (c.kind !== 'select' || pickNameOf(step) !== undefined || selectionState(c.selection) !== 'selected') return false
  const frozen = frozenPicks.get(c.selection)
  return frozen !== undefined && selectedIdentity(c.selection) !== frozen
}

export function useGuideStepTracker({
  stepId,
  step,
  enabled,
  recover = false,
  suppressMissing,
  reduceMotion,
  onObserved,
  onMissing,
  onFound,
  earlierSteps,
  onPreselected,
}: {
  /** Changes whenever the tracked step changes (guide, action, step). */
  stepId: string
  step: GuideStepPlan | null
  enabled: boolean
  /** The step's target went missing: watch for it to come back instead of
   *  tracking it. Seeing the target (or, for a `reach` step, a later target)
   *  reports `onFound` once; nothing is outlined and nothing goes missing. */
  recover?: boolean
  /** The committed save was submitted: absence now means "waiting", not missing. */
  suppressMissing: boolean
  reduceMotion: boolean
  /** Each report callback returns false when nothing was sent (a report is
   *  already in flight or accepted); that offer does not count as an attempt. */
  onObserved: () => boolean | void
  /** `detail` 'ambiguous': several copies of the target were shown at once;
   *  'predicate_unmet': a runtime predicate the target needs is unmet;
   *  'gate_off': a gate step's gate is off; 'selection_empty': a select
   *  step's picker has nothing to choose. */
  onMissing: (detail?: GuideStepMissingDetail) => boolean | void
  /** `resumeStepIndex` is set when an EARLIER step's target came back instead. */
  onFound?: (resumeStepIndex?: number) => boolean | void
  /** The current action's steps before this one, in order. */
  earlierSteps?: readonly GuideStepPlan[]
  /**
   * Given for a select step a step that removes something follows: when its
   * pick was already made at its first report (nothing was ever seen
   * unpicked), the step is held instead of completing, its picker is pointed
   * at, and this is called once so the panel can ask the person to confirm
   * the pick (its Next reports `observed`). Without it the step completes.
   */
  onPreselected?: () => void
}): GuideRect | null {
  const [rect, setRect] = useState<GuideRect | null>(null)
  const cb = useRef({ onObserved, onMissing, onFound, suppressMissing, earlierSteps, onPreselected })
  cb.current = { onObserved, onMissing, onFound, suppressMissing, earlierSteps, onPreselected }

  useEffect(() => {
    setRect(null)
    if (!enabled || !step) return
    let done = false
    let lastReport: number | null = null
    let reportAttempts = 0
    let missingSince: number | null = null
    const startedAt = Date.now()
    let scrolled = false
    let frame = 0
    // A select step seen unpicked completes when the pick is made; one whose
    // pick was there from the first report completes too, unless the owner
    // asks for a confirm (`onPreselected`), when it is held for the person.
    let sawUnpicked = false
    let held = false
    // Choosing again: the earlier choice no longer binds the steps after it.
    if (!recover && step.complete.kind === 'select') clearFrozenPicks(step.complete.selection)
    const tick = () => {
      if (done) return
      let fact = factVerdict(step)
      if (!recover && step.complete.kind === 'select' && cb.current.onPreselected) {
        const picked = pickMade(step)
        if (!picked && fact !== 'done') sawUnpicked = true
        // Done with no pick seen made (already picked, or the only item's
        // control already showing): the person confirms before the removal.
        if (fact === 'done' && (!sawUnpicked || !picked) && !held) {
          held = true
          cb.current.onPreselected()
        }
        if (held) fact = 'point'
      }
      if (recover) {
        // Back once the step is done or its target is drawn; a step whose
        // predicate is still unmet stays missing until the page says it holds.
        // A gate is back only once it is on; a select step once the pick is
        // made, or its picker has entries again and is drawn.
        const back = fact === 'done'
          || (fact === null && laterTargetVisible(step))
          // Back on the page a "you're here" step names: it is true again.
          || (step.target.kind === 'none' && step.complete.kind === 'ack' && step.complete.at !== undefined && !offStepRoute(step))
          || ((fact === null || fact === 'point')
            && isGuideTargetVisible(resolveStepTarget(step)) && unmetPredicates(step.requires).length === 0)
        // Not this step, but an earlier one of the same action: the page came
        // back started over (a remounted form), so the guide follows it back.
        // A step the page's facts block (a gate off, an empty picker) stays
        // where it is: walking back to an earlier step would only block again.
        const earlier = back || fact === 'blocked' ? -1 : latestVisibleEarlierStep(cb.current.earlierSteps)
        if (!back && earlier < 0) return
        // The report can fail (network, a refused write): keep watching and
        // offer it again on a slower cadence, a bounded number of times. The
        // Provider de-duplicates while one is in flight or accepted.
        const now = Date.now()
        if (lastReport !== null && now - lastReport < GUIDE_FOUND_RETRY_MS) return
        // Only a report actually sent counts: one the Provider swallowed (the
        // previous is still in flight) must not use up an attempt, or the
        // tracker gives up while the Provider still thinks retries remain.
        if (cb.current.onFound?.(back ? undefined : earlier) === false) return
        lastReport = now
        reportAttempts += 1
        if (reportAttempts >= GUIDE_FOUND_MAX_ATTEMPTS) done = true
        return
      }
      // A gate or select step completes on the page's fact alone. A fact
      // that blocks (a gate off, an empty picker) is said after the short
      // settle; one no reporter knows yet waits the full bound.
      if (fact === 'done' || fact === 'blocked' || fact === 'waiting') {
        setRect(null)
        const now = Date.now()
        if (fact !== 'done') {
          if (missingSince === null) missingSince = now
          const bound = fact === 'blocked' ? GUIDE_EARLIER_STEP_WAIT_MS : GUIDE_TARGET_WAIT_MS
          if (now - missingSince < bound) return
        }
        if (lastReport !== null && now - lastReport < GUIDE_FOUND_RETRY_MS) return
        if (fact === 'done') freezePick(step)
        const sent = fact === 'done' ? cb.current.onObserved() : cb.current.onMissing(missingDetail(step))
        if (sent === false) return
        lastReport = now
        reportAttempts += 1
        if (reportAttempts >= GUIDE_FOUND_MAX_ATTEMPTS) done = true
        return
      }
      if (laterTargetVisible(step)) {
        setRect(null)
        // Offered again on the same bounded cadence as the other reports: a
        // failed write must not leave the guide a step behind the form.
        const now = Date.now()
        if (lastReport !== null && now - lastReport < GUIDE_FOUND_RETRY_MS) return
        if (cb.current.onObserved() === false) return
        lastReport = now
        reportAttempts += 1
        if (reportAttempts >= GUIDE_FOUND_MAX_ATTEMPTS) done = true
        return
      }
      // A step that points at nothing (a page guide's "you're here") is only
      // read: it is never missing, and Done ends it.
      if (step.target.kind === 'none' && !offStepRoute(step)) { setRect(null); missingSince = null; return }
      if (offStepRoute(step)) {
        // Left the page the step names: missing after the short settle, so the
        // guide walks back to the step that opens it instead of letting Done
        // end it here.
        setRect(null)
        const now = Date.now()
        if (missingSince === null) missingSince = now
        if (now - missingSince < GUIDE_EARLIER_STEP_WAIT_MS) return
        if (lastReport !== null && now - lastReport < GUIDE_FOUND_RETRY_MS) return
        if (cb.current.onMissing() === false) return
        lastReport = now
        reportAttempts += 1
        if (reportAttempts >= GUIDE_FOUND_MAX_ATTEMPTS) done = true
        return
      }
      const el = resolveStepTarget(step)
      if (isGuideTargetVisible(el)) {
        missingSince = null
        if (!scrolled) {
          scrolled = true
          el.scrollIntoView?.({ block: 'center', behavior: reduceMotion ? 'auto' : 'smooth' })
        }
        const next = visibleBox(el)
        setRect(prev => (sameRect(prev, next) ? prev : next))
        return
      }
      setRect(null)
      // A held pick needs no picker on screen: Next confirms it either way.
      if (held || cb.current.suppressMissing) { missingSince = null; return }
      const now = Date.now()
      // The control cannot be drawn: a predicate it needs is unmet. Nothing to
      // wait out beyond the short settle an earlier step gets.
      const blocked = unmetPredicates(step.requires).length > 0 || findVerdict(step) !== null
      // The page is still drawing placeholders: its controls are not there yet.
      if (pageStillLoading() && now - startedAt < GUIDE_PAGE_SETTLE_MAX_MS) { missingSince = null; return }
      if (missingSince === null) missingSince = now
      else if (
        (blocked && now - missingSince >= GUIDE_EARLIER_STEP_WAIT_MS)
        || now - missingSince >= GUIDE_TARGET_WAIT_MS
        // The page already shows an earlier step of this action: it started
        // over, so there is nothing to wait out before recovering at it.
        || (now - missingSince >= GUIDE_EARLIER_STEP_WAIT_MS && latestVisibleEarlierStep(cb.current.earlierSteps) >= 0)
      ) {
        // Re-offered like a recovery: a failed write must not strand the
        // guide on a step the page no longer shows.
        if (lastReport !== null && now - lastReport < GUIDE_FOUND_RETRY_MS) return
        if (cb.current.onMissing(missingDetail(step)) === false) return
        lastReport = now
        reportAttempts += 1
        if (reportAttempts >= GUIDE_FOUND_MAX_ATTEMPTS) done = true
      }
    }
    const onMove = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(tick)
    }
    tick()
    const id = setInterval(tick, GUIDE_TRACK_TICK_MS)
    window.addEventListener('scroll', onMove, true)
    window.addEventListener('resize', onMove)
    return () => {
      done = true
      clearInterval(id)
      cancelAnimationFrame(frame)
      window.removeEventListener('scroll', onMove, true)
      window.removeEventListener('resize', onMove)
    }
    // `stepId` names the step; `step` is derived from it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stepId, enabled, recover, reduceMotion])

  return rect
}
