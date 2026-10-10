/**
 * Phase 2 of the live UI map: the shared primitives own reveal scopes (a menu,
 * a popover, a tab panel report open/closed, through portals, even while their
 * contents are unmounted), a `ui.show` menu step completes the moment its menu
 * reports open, and a step's runtime predicates gate pointing and recovery.
 * Nothing here ever opens a container: the tests flip `open` themselves.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, cleanup, render } from '@testing-library/react'
import { useState } from 'react'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../components/ui/dropdown-menu'
import { Popover, PopoverContent, PopoverTrigger } from '../components/ui/popover'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../components/ui/tabs'
import { GUIDE_BUILD_DIGEST, GUIDE_PLANS, GUIDE_REVEAL_SCOPES } from '../uiLocations/guidePlans.gen'
import { UI_RUNTIME_PREDICATES } from '../uiLocations/conditions'
import { guideClaimPlacements, resolveGuideAction, type GuideStepPlan } from './guideActions'
import { GUIDE_PREDICATE_EVALUATORS, predicateState, unmetPredicates, useGuidePredicate } from './guidePredicates'
import { useGuideRevealScopes } from './guideRevealScopeContext'
import { disclosureScopeId, useGuideDisclosureScope, useGuideRevealScope } from './GuideRevealScope'
import { scopeOpen } from './liveRegistry'
import { GUIDE_EARLIER_STEP_WAIT_MS, GUIDE_FOUND_RETRY_MS, useGuideStepTracker } from './useGuideStepTracker'
import { marks } from '../test/guideTargets'

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

type Box = { top: number; left: number; width: number; height: number }
const ON_SCREEN: Box = { top: 100, left: 100, width: 40, height: 20 }
function Loc({ id, box = ON_SCREEN }: { id: string; box?: Box }) {
  return (
    <button
      type="button" {...marks({ location: id }, (el) => {
        if (!el) return
        el.getBoundingClientRect = () => ({ ...box, right: box.left + box.width, bottom: box.top + box.height, x: box.left, y: box.top, toJSON: () => ({}) }) as DOMRect
      })}
    >
      {id}
    </button>
  )
}

const stepsOf = (id: string) => {
  const r = resolveGuideAction({ id: 'ui.show', params: { location_id: id }, build_digest: GUIDE_BUILD_DIGEST })
  if (!r.ok) throw new Error(r.reason)
  return r.action.steps
}

describe('the shared primitives own reveal scopes', () => {
  it('a dropdown menu reports closed while its content is unmounted, open once open, and carries the scope into its portal', () => {
    let seen: readonly { id: string; open: boolean }[] = []
    function Reader() {
      seen = useGuideRevealScopes()
      return null
    }
    function Host() {
      const [open, setOpen] = useState(false)
      return (
        <>
          <button type="button" data-testid="flip" onClick={() => setOpen(o => !o)}>flip</button>
          <DropdownMenu guideScope="menu:sessions.list-menu" open={open} onOpenChange={setOpen} modal={false}>
            <DropdownMenuTrigger asChild><button type="button">menu</button></DropdownMenuTrigger>
            <DropdownMenuContent><DropdownMenuItem><Reader />item</DropdownMenuItem></DropdownMenuContent>
          </DropdownMenu>
        </>
      )
    }
    const { getByTestId, unmount } = render(<Host />)
    expect(scopeOpen('menu:sessions.list-menu')).toBe(false)
    expect(seen).toEqual([])
    act(() => getByTestId('flip').click())
    expect(scopeOpen('menu:sessions.list-menu')).toBe(true)
    expect(seen).toEqual([{ id: 'menu:sessions.list-menu', open: true }])
    unmount()
    expect(scopeOpen('menu:sessions.list-menu')).toBeNull()
  })

  it('a menu without guideScope reports nothing', () => {
    render(
      <DropdownMenu open modal={false}>
        <DropdownMenuTrigger asChild><button type="button">menu</button></DropdownMenuTrigger>
        <DropdownMenuContent><DropdownMenuItem>item</DropdownMenuItem></DropdownMenuContent>
      </DropdownMenu>,
    )
    expect(scopeOpen('menu:sessions.list-menu')).toBeNull()
  })

  it('a popover, controlled or not, reports its own open state', () => {
    function Controlled() {
      const [open, setOpen] = useState(false)
      return (
        <>
          <button type="button" data-testid="pop" onClick={() => setOpen(o => !o)}>flip</button>
          <Popover guideScope="menu:apps.sources" open={open} onOpenChange={setOpen}>
            <PopoverTrigger asChild><button type="button">t</button></PopoverTrigger>
            <PopoverContent>body</PopoverContent>
          </Popover>
        </>
      )
    }
    const { getByTestId } = render(<Controlled />)
    expect(scopeOpen('menu:apps.sources')).toBe(false)
    act(() => getByTestId('pop').click())
    expect(scopeOpen('menu:apps.sources')).toBe(true)
    cleanup()
    render(
      <Popover guideScope="menu:members.switcher" defaultOpen>
        <PopoverTrigger asChild><button type="button">t</button></PopoverTrigger>
        <PopoverContent>body</PopoverContent>
      </Popover>,
    )
    expect(scopeOpen('menu:members.switcher')).toBe(true)
  })

  it('a tab panel reports selected even while the inactive panel is unmounted', () => {
    function Host() {
      const [tab, setTab] = useState('a')
      return (
        <Tabs value={tab} onValueChange={setTab}>
          <TabsList>
            <TabsTrigger value="a">A</TabsTrigger>
            <TabsTrigger value="b" data-testid="tab-b" onMouseDown={() => setTab('b')}>B</TabsTrigger>
          </TabsList>
          <TabsContent value="a">a</TabsContent>
          <TabsContent value="b" guideScope="open:connections.mcp-servers-tab">b</TabsContent>
        </Tabs>
      )
    }
    const { getByTestId } = render(<Host />)
    expect(scopeOpen('open:connections.mcp-servers-tab')).toBe(false)
    act(() => { getByTestId('tab-b').dispatchEvent(new MouseEvent('mousedown', { bubbles: true })) })
    expect(scopeOpen('open:connections.mcp-servers-tab')).toBe(true)
  })

  it('every scope a generated plan step names is one this build compiled', () => {
    for (const plan of Object.values(GUIDE_PLANS)) {
      for (const p of plan.placements) {
        p.steps.forEach((st, i) => {
          // The location and a select or gate step open nothing; a reveal step names its scope.
          if (i === p.steps.length - 1 || st.kind !== undefined) expect(st.scope).toBeUndefined()
          else expect(Object.hasOwn(GUIDE_REVEAL_SCOPES, st.scope!)).toBe(true)
        })
      }
    }
  })
})

function Tracked({ step, recover = false, onObserved, onMissing, onFound }: {
  step: GuideStepPlan
  recover?: boolean
  onObserved?: () => boolean
  onMissing?: (detail?: string) => boolean
  onFound?: () => boolean
}) {
  useGuideStepTracker({
    stepId: 's', step, enabled: true, recover, suppressMissing: false, reduceMotion: true,
    onObserved: onObserved ?? (() => true), onMissing: onMissing ?? (() => true), onFound,
  })
  return null
}

describe('a menu step completes when its menu reports open', () => {
  it('observes on the menu opening, without any later target mounting', () => {
    vi.useFakeTimers()
    const steps = stepsOf('sessions.list-menu.view')
    const menuStep = steps.find(s => s.target.kind === 'location' && s.target.id === 'sessions.list-menu')!
    expect(menuStep.complete).toMatchObject({ kind: 'reach', scope: 'menu:sessions.list-menu' })
    const onObserved = vi.fn(() => true)
    function Host() {
      const [open, setOpen] = useState(false)
      return (
        <>
          <button type="button" data-testid="flip" onClick={() => setOpen(true)}>flip</button>
          <Loc id="sessions.list-menu" />
          {/* The menu reports open, but its items never mount here. */}
          <DropdownMenu guideScope="menu:sessions.list-menu" open={open} onOpenChange={setOpen} modal={false}>
            <DropdownMenuTrigger asChild><span /></DropdownMenuTrigger>
          </DropdownMenu>
          <Tracked step={menuStep} onObserved={onObserved} />
        </>
      )
    }
    const { getByTestId } = render(<Host />)
    act(() => { vi.advanceTimersByTime(1000) })
    expect(onObserved).not.toHaveBeenCalled()
    act(() => getByTestId('flip').click())
    act(() => { vi.advanceTimersByTime(300) })
    expect(onObserved).toHaveBeenCalledTimes(1)
  })
})

describe('runtime predicates on a step', () => {
  const toggleStep = () => stepsOf('chat.older-sessions')[0]

  function Facts({ open = true, full = true }: { open?: boolean; full?: boolean }) {
    useGuidePredicate('has_open_sessions', open)
    useGuidePredicate('full_dashboard', full)
    return null
  }

  it('the plan carries the reveal control’s own conditions as predicates', () => {
    expect(toggleStep().requires).toEqual(['has_open_sessions', 'full_dashboard'])
  })

  it('one evaluator per predicate; none reported is unknown, an unknown id fails closed', () => {
    expect(Object.keys(GUIDE_PREDICATE_EVALUATORS).sort()).toEqual([...UI_RUNTIME_PREDICATES].sort())
    expect(predicateState('has_open_sessions')).toBe('unknown')
    expect(predicateState('not_a_predicate')).toBe('unmet')
    expect(unmetPredicates(['has_open_sessions'])).toEqual([])
  })

  it('reports predicate_unmet after the short settle when the control cannot be drawn', () => {
    vi.useFakeTimers()
    const onMissing = vi.fn(() => true)
    render(<><Facts open={false} /><Tracked step={toggleStep()} onMissing={onMissing} /></>)
    act(() => { vi.advanceTimersByTime(GUIDE_EARLIER_STEP_WAIT_MS - 300) })
    expect(onMissing).not.toHaveBeenCalled()
    act(() => { vi.advanceTimersByTime(600) })
    expect(onMissing).toHaveBeenCalledWith('predicate_unmet')
  })

  it('with every predicate met, an absent control waits the full time instead', () => {
    vi.useFakeTimers()
    const onMissing = vi.fn(() => true)
    render(<><Facts /><Tracked step={toggleStep()} onMissing={onMissing} /></>)
    act(() => { vi.advanceTimersByTime(GUIDE_EARLIER_STEP_WAIT_MS + 500) })
    expect(onMissing).not.toHaveBeenCalled()
  })

  it('recovers only once the predicates hold and the control is drawn', () => {
    vi.useFakeTimers()
    const onFound = vi.fn(() => true)
    function Host() {
      const [ok, setOk] = useState(false)
      return (
        <>
          <button type="button" data-testid="ok" onClick={() => setOk(true)}>ok</button>
          <Facts open={ok} />
          <Loc id="chat.sessions-sidebar-toggle" />
          <Tracked step={toggleStep()} recover onFound={onFound} />
        </>
      )
    }
    const { getByTestId } = render(<Host />)
    act(() => { vi.advanceTimersByTime(GUIDE_FOUND_RETRY_MS) })
    expect(onFound).not.toHaveBeenCalled()
    act(() => getByTestId('ok').click())
    act(() => { vi.advanceTimersByTime(300) })
    expect(onFound).toHaveBeenCalledTimes(1)
  })
})

describe('claim placements', () => {
  it('names the viewport placement for each ui.show action and nothing for the others', () => {
    expect(guideClaimPlacements([
      { id: 'ui.show', params: { location_id: 'chat.older-sessions' } },
      { id: 'settings.show', params: { setting_id: 'x' } },
      { id: 'ui.show', params: { location_id: 'nope.nope' } },
    ])).toEqual(['desktop', null, null])
  })
})

describe('the remaining scope owners (phase 3)', () => {
  // Each owner reports a scope the generator compiled, from the component that
  // holds the container's open state. `menu:` ids are the drawer menus.
  const OWNERS: Array<[string, string]> = [
    ['composer.box', '../components/ChatInput.tsx'],
    ['chat.side-panel', '../pages/ChatPage.tsx'],
    ['shell.nav-rail', '../shell/nav/railChrome.tsx'],
    ['shell.terminal-panel', '../components/BottomTerminalPanel.tsx'],
    ['members.roster', '../pages/members/MembersPage.tsx'],
    ['members.roster-phone', '../pages/members/MembersPage.tsx'],
    ['menu:shell.mobile-menu', '../App.tsx'],
  ]
  const sources = import.meta.glob(
    ['../components/ChatInput.tsx', '../pages/ChatPage.tsx', '../shell/nav/railChrome.tsx', '../components/BottomTerminalPanel.tsx', '../pages/members/MembersPage.tsx', '../App.tsx', '../pages/ChatSidebar.tsx'],
    { query: '?raw', import: 'default', eager: true },
  ) as Record<string, string>

  it.each(OWNERS)('%s is a compiled scope and its owner reports it', (id, file) => {
    expect(Object.hasOwn(GUIDE_REVEAL_SCOPES, id)).toBe(true)
    expect(sources[file]).toContain(`'${id}'`)
    expect(sources[file]).toMatch(/useGuideRevealScope\(/)
  })

  it('the hook owner reports open and closed, and drops its report on unmount', () => {
    function Owner({ open }: { open: boolean }) {
      useGuideRevealScope('shell.terminal-panel', open)
      return null
    }
    const { rerender, unmount } = render(<Owner open={false} />)
    expect(scopeOpen('shell.terminal-panel')).toBe(false)
    rerender(<Owner open />)
    expect(scopeOpen('shell.terminal-panel')).toBe(true)
    unmount()
    expect(scopeOpen('shell.terminal-panel')).toBeNull()
  })

  it('an owner with no id for this layout reports nothing', () => {
    function Owner() {
      useGuideRevealScope(undefined, true)
      return null
    }
    render(<Owner />)
    expect(scopeOpen('members.roster')).toBeNull()
  })

  it('the disclosure helper reports the compiled open:<id> scope, and nothing for an uncompiled disclosure', () => {
    expect(disclosureScopeId('chat.older-sessions')).toBe('open:chat.older-sessions')
    expect(disclosureScopeId('nope.nope')).toBeUndefined()
    function Owner({ open }: { open: boolean }) {
      useGuideDisclosureScope('chat.older-sessions', open)
      useGuideDisclosureScope('nope.nope', open)
      return null
    }
    const { rerender } = render(<Owner open={false} />)
    expect(scopeOpen('open:chat.older-sessions')).toBe(false)
    rerender(<Owner open />)
    expect(scopeOpen('open:chat.older-sessions')).toBe(true)
    expect(scopeOpen('open:nope.nope')).toBeNull()
    expect(sources['../pages/ChatSidebar.tsx']).toMatch(/useGuideDisclosureScope\('chat\.older-sessions',/)
  })
})
