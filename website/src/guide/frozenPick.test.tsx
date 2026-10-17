/**
 * A select step with no `pick`: the entity the person settled on binds every
 * later step until the guide ends; another one opened sends the guide back.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, waitFor } from '@testing-library/react'
import { resolveGuideAction, type GuideStepPlan } from './guideActions'
import { GUIDE_BUILD_DIGEST } from '../uiLocations/guidePlans.gen'
import { useGuideSelection } from './guidePredicates'
import { clearFrozenPicks, freezePick, latestVisibleEarlierStep, stepBoundHolds, useGuideStepTracker } from './useGuideStepTracker'

function plan(): GuideStepPlan[] {
  const r = resolveGuideAction({ id: 'ui.show', params: { location_id: 'agents.delete' }, build_digest: GUIDE_BUILD_DIGEST })
  if (!r.ok) throw new Error(r.reason)
  return r.action.steps
}

function Open({ name }: { name: string }) {
  useGuideSelection('crewmate_editor_open', { selected: true, available: true, name })
  return null
}

afterEach(() => {
  cleanup()
  clearFrozenPicks()
})

describe('a choice made without a pick', () => {
  it('binds the later steps to the entity confirmed, and a switch breaks the bound', () => {
    const steps = plan()
    const [choose, ...later] = steps
    expect(choose.complete.kind).toBe('select')
    const { rerender } = render(<Open name="Helper" />)
    // Before anything is frozen any open crewmate holds (the old behaviour).
    expect(later.every(stepBoundHolds)).toBe(true)
    freezePick(choose)
    expect(later.every(stepBoundHolds)).toBe(true)
    rerender(<Open name="Ops" />)
    expect(later.some(st => st.bound) && later.filter(st => st.bound).every(st => !stepBoundHolds(st))).toBe(true)
    // Back to the choice: it is made again, and the new one binds.
    clearFrozenPicks('crewmate_editor_open')
    expect(later.every(stepBoundHolds)).toBe(true)
  })

  it('the tracker freezes the choice when the select step completes', async () => {
    const steps = plan()
    const choose = steps[0]
    const onObserved = vi.fn()
    function Track() {
      useGuideStepTracker({ stepId: 's', step: choose, enabled: true, suppressMissing: false, reduceMotion: true, onObserved, onMissing: () => true })
      return null
    }
    const { rerender } = render(<><Open name="Helper" /><Track /></>)
    await waitFor(() => expect(onObserved).toHaveBeenCalled())
    const bound = steps.slice(1).filter(st => st.bound)
    expect(bound.every(stepBoundHolds)).toBe(true)
    rerender(<><Open name="Ops" /></>)
    expect(bound.every(st => !stepBoundHolds(st))).toBe(true)
  })
})

function OnArtifactPage({ name }: { name: string }) {
  useGuideSelection('artifact_open', { selected: true, available: true, name })
  return null
}

describe('a choice made on one route and used on the next', () => {
  it('opening an artifact in the library is the pick, and its own page keeps the move bound to it', () => {
    const r = resolveGuideAction({ id: 'ui.show', params: { location_id: 'artifacts.detail.move-to-folder' }, build_digest: GUIDE_BUILD_DIGEST })
    if (!r.ok) throw new Error(r.reason)
    const [choose, move] = r.action.steps
    expect(choose.complete.kind === 'select' && choose.complete.selection).toBe('artifact_open')
    expect(move.bound?.selection).toBe('artifact_open')
    const { rerender } = render(<OnArtifactPage name="Q3 plan" />)
    freezePick(choose)
    expect(stepBoundHolds(move)).toBe(true)
    expect(latestVisibleEarlierStep([choose])).toBe(-1)
    // Another artifact's page: the move is not about it, and the guide goes
    // back to choosing even though the library's cards are not drawn here.
    rerender(<OnArtifactPage name="Draft notes" />)
    expect(stepBoundHolds(move)).toBe(false)
    expect(latestVisibleEarlierStep([choose])).toBe(0)
  })
})

function OpenSession({ id }: { id: string }) {
  // As ChatPage reports it: open, listed, and which session by its key; no name.
  useGuideSelection('session_open', { selected: true, available: true, identity: id })
  return null
}

describe('a session chosen without a pick', () => {
  it('binds the later steps to that session; switching to another sends the guide back to choose', () => {
    const r = resolveGuideAction({ id: 'ui.show', params: { location_id: 'sessions.row-menu.rename' }, build_digest: GUIDE_BUILD_DIGEST })
    if (!r.ok) throw new Error(r.reason)
    const i = r.action.steps.findIndex(st => st.complete.kind === 'select')
    const choose = r.action.steps[i]
    const later = r.action.steps.slice(i + 1).filter(st => st.bound)
    expect(later.length).toBeGreaterThan(0)
    const { rerender } = render(<OpenSession id="dashboard:a" />)
    freezePick(choose)
    expect(later.every(stepBoundHolds)).toBe(true)
    rerender(<OpenSession id="dashboard:b" />)
    expect(later.every(st => !stepBoundHolds(st))).toBe(true)
    expect(latestVisibleEarlierStep([choose])).toBe(0)
  })
})
