/**
 * Runs a `ui.find` action's container probe once its page is showing.
 *
 * The owning tab walks the action's `open` step: when the control is already
 * visible the step passes by itself and nothing is opened; when several
 * visible controls carry the name the search is `ambiguous`; a match of the
 * agent's own ceiling is never probed for. When the index names the control
 * it is under (`opener`, a menu's trigger) and that control is showing, the
 * step points at it (`opener`) and nothing is probed. Otherwise the page is given a
 * moment to settle and `probeForName` looks inside its registered containers,
 * once per tracked step (a Go back re-runs it). Nothing is opened when the
 * person's focus is in a field they type into, or when they pressed, typed
 * or scrolled on the page since the step began (heard from the step's start,
 * not the probe's): the search is then `none` and the person is asked to
 * open the container. A container holding several
 * matches is kept as `found` with their `count`: the step points at it, and
 * the person picks among them once it is open. The probe is aborted when the
 * step changes, the page moves, the guide ends or `enabled` turns false (the
 * owner passes false from the moment the person presses Cancel); an aborted
 * probe's verdict is dropped. The verdict goes to `findState`, which the step tracker and the
 * panel read.
 */
import { useEffect } from 'react'
import { useLocation } from 'react-router-dom'
import { accessibleName, findState, isEditingFocus, probeForName, searchByName, setFindState, type FindQuery } from './findByName'
import { isPersonInput, PERSON_INPUTS } from './probeRegistry'
import { isGuideTargetVisible } from './useGuideStepTracker'
import { findOpenerLocation } from './guideActions'
import { isSafeOpenerTarget } from './findTargetPolicy'

/** How long a page that just opened gets before its containers are probed. */
export const GUIDE_FIND_SETTLE_MS = 400

export function useFindProbe({ runId, query, findKey, enabled, opener }: {
  /** Changes whenever the tracked step changes (a re-run probes again). */
  runId: string
  query: FindQuery | null
  findKey: string | null
  enabled: boolean
  /** The registered control the index places the target under (a menu's trigger): pointed at, never opened. */
  opener?: string
}): void {
  const { pathname, search } = useLocation()
  useEffect(() => {
    if (!enabled || !query || !findKey) return
    const controller = new AbortController()
    setFindState(findKey, undefined)
    // The person's input since the step began, not since the probe began:
    // a press or key during the settle wait is theirs too, and the probe
    // then leaves the page alone.
    let acted = false
    const onInput = (e: Event) => { if (isPersonInput(e)) acted = true }
    for (const type of PERSON_INPUTS) window.addEventListener(type, onInput, true)
    const id = window.setTimeout(() => {
      if (controller.signal.aborted) return
      const live = searchByName(query, document, isGuideTargetVisible)
      if (live.result === 'found' || live.result === 'sensitive') return
      if (live.result === 'ambiguous') { setFindState(findKey, { status: 'ambiguous', count: live.matches.length }); return }
      // The index says what it is under: the person opens that, and nothing
      // is probed. Pointing opens nothing, so it needs no quiet page. The
      // real element is judged too: a trigger of the agent's own ceiling or
      // one that removes something is never pointed at, and nothing else is
      // opened in its place: the person is asked to open the menu.
      const openerEl = opener ? findOpenerLocation(opener, isGuideTargetVisible) : null
      if (opener && openerEl) {
        if (!isSafeOpenerTarget(openerEl, accessibleName(openerEl))) { setFindState(findKey, { status: 'none' }); return }
        setFindState(findKey, { status: 'opener', location: opener })
        return
      }
      // Typing somewhere, or acted since the step began: nothing is opened,
      // and the person is asked to open the container themselves.
      if (acted || isEditingFocus()) { setFindState(findKey, { status: 'none' }); return }
      setFindState(findKey, { status: 'probing' })
      void probeForName(query, { shown: isGuideTargetVisible, signal: controller.signal, interrupted: () => acted }).then((r) => {
        if (controller.signal.aborted || findState(findKey)?.status !== 'probing') return
        if (r.result === 'found') setFindState(findKey, { status: 'found', path: r.path })
        else if (r.result === 'ambiguous') setFindState(findKey, { status: 'found', path: r.path, count: r.count })
        else setFindState(findKey, { status: 'none' })
      }, () => { if (!controller.signal.aborted) setFindState(findKey, { status: 'none' }) })
    }, GUIDE_FIND_SETTLE_MS)
    return () => {
      controller.abort()
      window.clearTimeout(id)
      for (const type of PERSON_INPUTS) window.removeEventListener(type, onInput, true)
    }
    // `query` is derived from `findKey`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, findKey, enabled, opener, pathname, search])
}
