/**
 * The live target registry, reveal scopes and the bounded observation reply.
 *
 * jsdom lays nothing out, so each target fakes its own box; everything else
 * (hidden/inert ancestors, disabled, the exactly-one rule) is the real DOM.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, cleanup, render } from '@testing-library/react'
import { useState } from 'react'
import { createPortal } from 'react-dom'
import { TAB_ID } from '../api/tabId'
import { GUIDE_BUILD_DIGEST } from '../uiLocations/guidePlans.gen'
import { GuideRevealScope } from './GuideRevealScope'
import { useGuideRevealScopes } from './guideRevealScopeContext'
import { resolveGuideAction, type GuideStepPlan } from './guideActions'
import { liveTarget, LIVE_TARGET_STATUSES, scopeOpen } from './liveRegistry'
import { buildObservationReply, DOCUMENT_EPOCH } from './liveObservation'
import { selectionState, useGuideGate, useGuidePredicate, useGuideSelection } from './guidePredicates'
import { useGuideStepTracker } from './useGuideStepTracker'
import { marks } from '../test/guideTargets'

type Box = { top: number; left: number; width: number; height: number }
const ON_SCREEN: Box = { top: 100, left: 100, width: 40, height: 20 }
const BELOW: Box = { top: 5000, left: 100, width: 40, height: 20 }

function Loc({ id, box = ON_SCREEN, ...rest }: { id: string; box?: Box } & Record<string, unknown>) {
  return (
    <button
      type="button"
      {...rest} {...marks({ location: id }, (el) => {
        if (!el) return
        el.getBoundingClientRect = () => ({ ...box, right: box.left + box.width, bottom: box.top + box.height, x: box.left, y: box.top, toJSON: () => ({}) }) as DOMRect
      })}
    >
      {id}
    </button>
  )
}

const ID = 'chat.older-sessions'

afterEach(() => cleanup())

describe('live target registry', () => {
  it('pointable: exactly one copy displayed, enabled and in the viewport', () => {
    render(<Loc id={ID} />)
    const t = liveTarget(ID)
    expect(t.status).toBe('pointable')
    expect(t.element?.getAttribute('data-ui-location')).toBe(ID)
  })

  it('offscreen: the one copy is scrolled out of the viewport', () => {
    render(<Loc id={ID} box={BELOW} />)
    expect(liveTarget(ID).status).toBe('offscreen')
  })

  it('unmounted: nothing carries the id', () => {
    expect(liveTarget(ID)).toEqual({ status: 'unmounted', element: null })
  })

  it('hidden: every copy is hidden, inert, undisplayed or has an empty box', () => {
    render(
      <>
        <div hidden><Loc id={ID} /></div>
        <div ref={(el) => el?.setAttribute('inert', '')}><Loc id={ID} /></div>
        <Loc id={ID} style={{ display: 'none' }} />
        <Loc id={ID} box={{ top: 0, left: 0, width: 0, height: 0 }} />
      </>,
    )
    expect(liveTarget(ID)).toEqual({ status: 'hidden', element: null })
  })

  it('disabled: the one displayed copy is disabled, by attribute or an aria-disabled ancestor', () => {
    const { unmount } = render(<Loc id={ID} disabled />)
    expect(liveTarget(ID).status).toBe('disabled')
    unmount()
    render(<div aria-disabled="true"><Loc id={ID} /></div>)
    expect(liveTarget(ID).status).toBe('disabled')
  })

  it('ambiguous: two displayed copies point at neither; a hidden copy is no rival', () => {
    const { unmount } = render(<><Loc id={ID} /><Loc id={ID} box={BELOW} /></>)
    expect(liveTarget(ID)).toEqual({ status: 'ambiguous', element: null })
    unmount()
    render(<><Loc id={ID} /><div hidden><Loc id={ID} /></div></>)
    expect(liveTarget(ID).status).toBe('pointable')
  })

  it('unknown: an id this build cannot observe, even when something carries it', () => {
    render(<Loc id="not.a-location" />)
    expect(liveTarget('not.a-location').status).toBe('unknown')
    expect(LIVE_TARGET_STATUSES).toContain('unknown')
  })
})

describe('<GuideRevealScope>', () => {
  it('reports open and closed, and nothing once unmounted', () => {
    function Owner() {
      const [open, setOpen] = useState(false)
      return (
        <GuideRevealScope id="chat.sessions-sidebar" open={open}>
          <button type="button" data-testid="flip" onClick={() => setOpen(o => !o)}>flip</button>
        </GuideRevealScope>
      )
    }
    const { getByTestId, unmount } = render(<Owner />)
    expect(scopeOpen('chat.sessions-sidebar')).toBe(false)
    act(() => getByTestId('flip').click())
    expect(scopeOpen('chat.sessions-sidebar')).toBe(true)
    unmount()
    expect(scopeOpen('chat.sessions-sidebar')).toBeNull()
  })

  it('carries the scope chain through a portal', () => {
    let seen: readonly { id: string; open: boolean }[] = []
    function Reader() {
      seen = useGuideRevealScopes()
      return null
    }
    render(
      <GuideRevealScope id="chat.sessions-drawer" open>
        <div>{createPortal(<Reader />, document.body)}</div>
      </GuideRevealScope>,
    )
    expect(seen).toEqual([{ id: 'chat.sessions-drawer', open: true }])
  })
})

describe('a reveal step completes when its scope reports open', () => {
  const plan = () => {
    const r = resolveGuideAction({ id: 'ui.show', params: { location_id: ID }, build_digest: GUIDE_BUILD_DIGEST })
    if (!r.ok) throw new Error(r.reason)
    return r.action.steps[0]
  }

  function Tracked({ step, onObserved }: { step: GuideStepPlan; onObserved: () => void }) {
    useGuideStepTracker({ stepId: 's', step, enabled: true, suppressMissing: true, reduceMotion: true, onObserved, onMissing: () => {} })
    return null
  }

  function Host({ step, onObserved }: { step: GuideStepPlan; onObserved: () => void }) {
    const [open, setOpen] = useState(false)
    // The sidebar opens, but its contents (the later target) never mount:
    // only the scope's report can complete the step.
    return (
      <>
        <Loc id="chat.sessions-sidebar-toggle" data-testid="open" onClick={() => setOpen(true)} />
        <GuideRevealScope id="chat.sessions-sidebar" open={open} />
        <Tracked step={step} onObserved={onObserved} />
      </>
    )
  }

  it('observes on scope open, without the later target ever showing', () => {
    vi.useFakeTimers()
    try {
      const step = plan()
      expect(step.complete).toMatchObject({ kind: 'reach', scope: 'chat.sessions-sidebar' })
      const onObserved = vi.fn(() => true)
      const { getByTestId } = render(<Host step={step} onObserved={onObserved} />)
      act(() => { vi.advanceTimersByTime(1000) })
      expect(onObserved).not.toHaveBeenCalled()
      act(() => getByTestId('open').click())
      act(() => { vi.advanceTimersByTime(300) })
      expect(onObserved).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('build digest', () => {
  it('refuses a ui.show guide accepted against another build, or carrying none', () => {
    const params = { location_id: ID }
    expect(resolveGuideAction({ id: 'ui.show', params, build_digest: 'sha256:' + '0'.repeat(64) })).toEqual({ ok: false, reason: 'build_mismatch' })
    expect(resolveGuideAction({ id: 'ui.show', params })).toEqual({ ok: false, reason: 'build_mismatch' })
    expect(resolveGuideAction({ id: 'ui.show', params, build_digest: GUIDE_BUILD_DIGEST }).ok).toBe(true)
  })
})

describe('observation reply', () => {
  const frame = (over: Record<string, unknown> = {}) => ({
    request_id: 'o_1', tab_id: TAB_ID, targets: [ID, 'chat.sessions-sidebar-toggle'], scopes: ['chat.sessions-sidebar'], predicates: ['has_open_sessions'], ...over,
  })

  it('answers only the frame naming this tab, with ids and enum states and nothing else', () => {
    function Facts() {
      useGuidePredicate('has_open_sessions', true)
      return null
    }
    render(
      <GuideRevealScope id="chat.sessions-sidebar" open>
        <Facts />
        <Loc id="chat.sessions-sidebar-toggle">Show sessions sidebar</Loc>
      </GuideRevealScope>,
    )
    expect(buildObservationReply(frame({ tab_id: 'another-tab' }))).toBeNull()
    const reply = buildObservationReply(frame())
    expect(reply).not.toBeNull()
    expect(Object.keys(reply!).sort()).toEqual(['build_digest', 'document_epoch', 'predicates', 'request_id', 'scopes', 'sequence', 'tab_id', 'targets'])
    expect(reply).toMatchObject({ tab_id: TAB_ID, request_id: 'o_1', build_digest: GUIDE_BUILD_DIGEST, document_epoch: DOCUMENT_EPOCH })
    expect(reply!.targets).toEqual([
      { id: ID, status: 'unmounted' },
      { id: 'chat.sessions-sidebar-toggle', status: 'pointable' },
    ])
    expect(reply!.scopes).toEqual([{ id: 'chat.sessions-sidebar', state: 'open' }])
    expect(reply!.predicates).toEqual([{ id: 'has_open_sessions', state: 'met' }])
    // No page text rides along, whatever the control says on screen.
    expect(JSON.stringify(reply)).not.toContain('Show sessions sidebar')
    const next = buildObservationReply(frame({ request_id: 'o_2' }))
    expect(next!.sequence).toBeGreaterThan(reply!.sequence)
  })

  it('answers selections and gates as met / unmet / unknown only, never which entity was picked', () => {
    function Facts() {
      // The page knows WHICH crewmate is open; the registry is told only that one is.
      useGuideSelection('crewmate_selected', { selected: true, available: true })
      useGuideSelection('job_open', { selected: false, available: false })
      useGuideGate('developer_mode', false)
      return null
    }
    render(<Facts />)
    const reply = buildObservationReply(frame({
      targets: [ID], scopes: [],
      predicates: ['crewmate_selected', 'job_open', 'session_open', 'developer_mode', 'preview_flag:mc-preview-webhooks'],
    }))
    expect(reply!.predicates).toEqual([
      { id: 'crewmate_selected', state: 'met' },
      // An empty picker is unmet to the gateway: the guide itself says it is empty.
      { id: 'job_open', state: 'unmet' },
      { id: 'session_open', state: 'unknown' },
      { id: 'developer_mode', state: 'unmet' },
      { id: 'preview_flag:mc-preview-webhooks', state: 'unmet' },
    ])
    for (const p of reply!.predicates) expect(Object.keys(p).sort()).toEqual(['id', 'state'])
    expect(selectionState('job_open')).toBe('empty')
  })

  it('drops a malformed or oversized request unanswered', () => {
    expect(buildObservationReply(frame({ targets: Array.from({ length: 17 }, (_, i) => `x.${i}`) }))).toBeNull()
    expect(buildObservationReply(frame({ targets: [{ id: ID }] }))).toBeNull()
    expect(buildObservationReply(frame({ scopes: ['a b'] }))).toBeNull()
    expect(buildObservationReply(frame({ predicates: Array.from({ length: 9 }, (_, i) => `p${i}`) }))).toBeNull()
    expect(buildObservationReply(null)).toBeNull()
  })

  it('reports an undeclared scope, or one with no owner mounted, as unknown, and a closed one as closed', () => {
    render(<GuideRevealScope id="menu:sessions.list-menu" open={false} />)
    const reply = buildObservationReply(frame({ scopes: ['chat.sessions-drawer', 'made.up', 'menu:sessions.list-menu'] }))
    expect(reply!.scopes).toEqual([
      { id: 'chat.sessions-drawer', state: 'unknown' },
      { id: 'made.up', state: 'unknown' },
      { id: 'menu:sessions.list-menu', state: 'closed' },
    ])
  })

  it('reports a predicate with no reporter as unknown, and one this build has no evaluator for as unmet', () => {
    const reply = buildObservationReply(frame({ predicates: ['full_dashboard', 'made_up'] }))
    expect(reply!.predicates).toEqual([
      { id: 'full_dashboard', state: 'unknown' },
      { id: 'made_up', state: 'unmet' },
    ])
  })
})

describe('auto targets in a bundle no build stamped', () => {
  const SITE = 'auto:page.schedule:SchedulePage:pages.schedulePage.retry'
  it('an auto site is not observable and an auto guide is refused', () => {
    render(<Loc id="x" {...marks({ auto: SITE })} />)
    expect(liveTarget(SITE).status).toBe('unknown')
    expect(resolveGuideAction({
      id: 'ui.show',
      params: { location_id: 'auto:page.schedule:pages.schedulePage.retry' },
      build_digest: '',
      auto_plan: { version: 2, label_key: 'k', placements: [{ id: 'any', route: '/schedule', steps: [{ id: 'any:s', location: SITE, label_key: 'k' }] }] },
    })).toEqual({ ok: false, reason: 'build_mismatch' })
  })
})
