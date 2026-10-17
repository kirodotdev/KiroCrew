/**
 * Auto guide targets in the browser: a stamped `data-ui-auto` site resolves
 * under the same exactly-one rule as a curated `data-ui-location`, and an auto
 * `ui.show` plan is walked only from the guide record AND only when its digest
 * is this bundle's own auto digest (the build that stamped the markers).
 *
 * The digest is defined by the ui-auto-stamp Vite plugin at build time and is
 * absent under Vitest, so it is mocked here; the unmocked case (no stamped
 * build: every auto guide refused) is in liveRegistry.test.tsx.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { Btn, IconButton, SendBtn, Toggle } from '../components/ui'
import { Tabs, TabsList, TabsTrigger } from '../components/ui/tabs'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../components/ui/dropdown-menu'

vi.mock('../uiLocations/autoBuild', async (orig) => ({
  ...(await orig<typeof import('../uiLocations/autoBuild')>()),
  UI_AUTO_BUILD_DIGEST: 'sha256:auto-of-this-build',
}))

import { findUiLocation, guideClaimPlacements, pickGuidePlacement, replanPlacementFor, resolveGuideAction, type GuideAction } from './guideActions'
import { GUIDE_CONFIRM_ATTR, GUIDE_CONFIRM_OPEN_MS, openDialogsNow, watchConfirmDialog } from './guideConfirmWatch'
import { isDisplayed, liveTarget } from './liveRegistry'
import { marks } from '../test/guideTargets'
import { registeredCopies, registerRef } from '../uiLocations/targetRegistry'

const LID = 'auto:page.schedule:pages.schedulePage.retry'
const SITE = 'auto:page.schedule:SchedulePage:pages.schedulePage.retry'
const box = (el: HTMLElement | null) => {
  if (!el) return
  el.getBoundingClientRect = () => ({ top: 10, left: 10, width: 40, height: 20, right: 50, bottom: 30, x: 10, y: 10, toJSON: () => ({}) }) as DOMRect
}

function action(over: Partial<GuideAction> = {}): GuideAction {
  return {
    id: 'ui.show',
    params: { location_id: LID },
    build_digest: 'sha256:auto-of-this-build',
    plan_version: 2,
    placements: { any: [`any:${LID}`] },
    auto_plan: {
      version: 2,
      label_key: 'pages.schedulePage.retry',
      placements: [{ id: 'any', route: '/schedule', steps: [{ id: `any:${LID}`, location: SITE, label_key: 'pages.schedulePage.retry' }] }],
    },
    ...over,
  }
}

afterEach(() => {
  cleanup()
  window.history.replaceState({}, '', '/')
})

describe('auto ui.show', () => {
  it('walks the record\'s single-step plan at the site id', () => {
    const r = resolveGuideAction(action())
    expect(r.ok).toBe(true)
    if (!r.ok) return
    expect(r.action.steps).toHaveLength(1)
    // An auto site may be drawn once per list row: any visible copy shows it.
    expect(r.action.steps[0].target).toEqual({ kind: 'location', id: SITE, repeated: true })
    expect(r.action.steps[0].complete).toEqual({ kind: 'ack' })
  })

  it('refuses a plan of another build (the curated digest included) as build_mismatch', () => {
    expect(resolveGuideAction(action({ build_digest: 'sha256:other' }))).toEqual({ ok: false, reason: 'build_mismatch' })
    expect(resolveGuideAction(action({ build_digest: undefined }))).toEqual({ ok: false, reason: 'build_mismatch' })
  })

  it('refuses a record with no plan, several steps, or a step that is not a site', () => {
    expect(resolveGuideAction(action({ auto_plan: undefined }))).toEqual({ ok: false, reason: 'unknown_location' })
    const two = action()
    two.auto_plan!.placements[0].steps.push({ id: 'any:x', location: SITE, label_key: 'k' })
    expect(resolveGuideAction(two)).toEqual({ ok: false, reason: 'unknown_location' })
    const curatedTarget = action()
    curatedTarget.auto_plan!.placements[0].steps[0].location = 'chat.older-sessions'
    expect(resolveGuideAction(curatedTarget)).toEqual({ ok: false, reason: 'unknown_location' })
  })
})

describe('auto targets under the exactly-one rule', () => {
  it('a stamped site is found and pointable', () => {
    render(<button type="button" {...marks({ auto: SITE }, box)}>Retry</button>)
    expect(findUiLocation(SITE, isDisplayed)).not.toBeNull()
    expect(liveTarget(SITE).status).toBe('pointable')
  })

  it('two displayed copies point at neither, whichever attribute each carries', () => {
    render(<><button type="button" {...marks({ auto: SITE }, box)}>a</button><button type="button" {...marks({ location: SITE }, box)}>b</button></>)
    expect(findUiLocation(SITE, isDisplayed)).toBeNull()
    expect(liveTarget(SITE).status).toBe('ambiguous')
  })

  it('a hidden copy is no rival', () => {
    render(<><button type="button" {...marks({ auto: SITE }, box)}>a</button><div hidden><button type="button" {...marks({ auto: SITE })}>b</button></div></>)
    expect(liveTarget(SITE).status).toBe('pointable')
  })

  it('a curated id never matches a data-ui-auto copy', () => {
    render(<button type="button" {...marks({ auto: "chat.older-sessions" }, box)}>x</button>)
    expect(liveTarget('chat.older-sessions').status).toBe('unmounted')
  })
})

const SHARED_LID = 'auto:shared:pages.common.retry'
const SHARED_SITE = 'auto:shared:RetryRow:pages.common.retry'

function sharedAction(over: Partial<GuideAction> = {}): GuideAction {
  const routes = ['/members', '/schedule', null]
  return {
    id: 'ui.show',
    params: { location_id: SHARED_LID },
    build_digest: 'sha256:auto-of-this-build',
    plan_version: 2,
    placements: { pa: [`pa:${SHARED_LID}`], pb: [`pb:${SHARED_LID}`], pc: [`pc:${SHARED_LID}`] },
    auto_plan: {
      version: 2,
      label_key: 'pages.schedulePage.retry',
      placements: routes.map((route, i) => {
        const id = `p${'abc'[i]}`
        return { id, route, steps: [{ id: `${id}:${SHARED_LID}`, location: SHARED_SITE, label_key: 'pages.schedulePage.retry' }] }
      }),
    },
    ...over,
  }
}

describe('a control several pages share', () => {
  it('walks the placement of the page the person is on', () => {
    window.history.replaceState({}, '', '/schedule?view=list')
    expect(guideClaimPlacements([sharedAction()])).toEqual(['pb'])
    const r = resolveGuideAction(sharedAction())
    expect(r.ok).toBe(true)
    if (!r.ok) return
    expect(r.action.steps[0].target).toEqual({ kind: 'location', id: SHARED_SITE, repeated: true })
    expect(r.action.enter.to({ pathname: '/schedule', search: '?view=list' })).toBe('/schedule?view=list')
  })

  it('stays on any page when the shell draws it too, and opens the first page otherwise', () => {
    window.history.replaceState({}, '', '/chat')
    expect(guideClaimPlacements([sharedAction()])).toEqual(['pc'])
    const noShell = sharedAction()
    noShell.auto_plan = { ...noShell.auto_plan!, placements: noShell.auto_plan!.placements.slice(0, 2) }
    expect(guideClaimPlacements([noShell])).toEqual(['pa'])
    const plan = noShell.auto_plan!
    expect(pickGuidePlacement(plan, 'mobile', { pathname: '/chat', search: '' })?.route).toBe('/members')
  })

  it('never re-plans a claimed per-page placement on a viewport change', () => {
    window.history.replaceState({}, '', '/members')
    expect(replanPlacementFor(sharedAction({ placement: 'pb' }), 'mobile')).toBeNull()
  })

  it('refuses several placements for an id that is not shared, or placements naming two sites', () => {
    const notShared = sharedAction({ params: { location_id: LID } })
    expect(resolveGuideAction(notShared)).toEqual({ ok: false, reason: 'unknown_location' })
    const twoSites = sharedAction()
    twoSites.auto_plan!.placements[1].steps[0].location = `${SHARED_SITE}:2`
    expect(resolveGuideAction(twoSites)).toEqual({ ok: false, reason: 'unknown_location' })
  })

  it('points only when exactly one copy is visible on the page', () => {
    render(<button type="button" {...marks({ auto: SHARED_SITE }, box)}>Retry</button>)
    expect(liveTarget(SHARED_SITE).status).toBe('pointable')
    cleanup()
    render(<><button type="button" {...marks({ auto: SHARED_SITE }, box)}>a</button><button type="button" {...marks({ auto: SHARED_SITE }, box)}>b</button></>)
    expect(liveTarget(SHARED_SITE).status).toBe('ambiguous')
    expect(findUiLocation(SHARED_SITE, isDisplayed)).toBeNull()
  })

  it('points a repeated list-row site at its first visible copy, and never a destructive one', () => {
    render(
      <>
        <button type="button" hidden {...marks({ auto: SHARED_SITE })}>hidden</button>
        <button type="button" data-testid="first" {...marks({ auto: SHARED_SITE }, box)}>a</button>
        <button type="button" {...marks({ auto: SHARED_SITE }, box)}>b</button>
      </>,
    )
    expect(findUiLocation(SHARED_SITE, isDisplayed, true)?.dataset.testid).toBe('first')
    const r = resolveGuideAction(action())
    expect(r.ok && r.action.steps[0].target).toMatchObject({ repeated: true })
    const destructive = action()
    destructive.auto_plan!.placements[0].steps[0].caution = true
    const d = resolveGuideAction(destructive)
    expect(d.ok && d.action.steps[0].target).toEqual({ kind: 'location', id: SITE })
  })
})

describe('a destructive auto control', () => {
  it('carries the caution onto its step', () => {
    const a = action()
    a.auto_plan!.placements[0].steps[0].caution = true
    const r = resolveGuideAction(a)
    expect(r.ok && r.action.steps[0].caution).toBe(true)
    const plain = resolveGuideAction(action())
    expect(plain.ok && plain.action.steps[0].caution).toBeFalsy()
  })
})

describe('reviewed primitives forward the stamped marker to the element pointed at', () => {
  it('Btn, SendBtn, IconButton and Toggle', () => {
    render(<>
      <Btn data-ui-auto="auto:x:A:k">b</Btn>
      <SendBtn data-ui-auto="auto:x:B:k">s</SendBtn>
      <IconButton aria-label="i" data-ui-auto="auto:x:C:k"><span /></IconButton>
      <Toggle checked={false} onChange={() => undefined} label="t" data-ui-auto="auto:x:D:k" />
    </>)
    expect(screen.getByText('b').closest('button')?.getAttribute('data-ui-auto')).toBe('auto:x:A:k')
    expect(screen.getByText('s').closest('button')?.getAttribute('data-ui-auto')).toBe('auto:x:B:k')
    expect(screen.getByRole('button', { name: 'i' }).getAttribute('data-ui-auto')).toBe('auto:x:C:k')
    expect(screen.getByRole('switch', { name: 't' }).getAttribute('data-ui-auto')).toBe('auto:x:D:k')
    // The primitive registers the element it draws under the site it was handed.
    expect(registeredCopies('auto', 'auto:x:A:k')).toEqual([screen.getByText('b').closest('button')])
    expect(registeredCopies('auto', 'auto:x:B:k')).toEqual([screen.getByText('s').closest('button')])
    expect(registeredCopies('auto', 'auto:x:C:k')).toEqual([screen.getByRole('button', { name: 'i' })])
    expect(registeredCopies('auto', 'auto:x:D:k')).toEqual([screen.getByRole('switch', { name: 't' })])
  })

  it('TabsTrigger', () => {
    render(<Tabs value="a"><TabsList><TabsTrigger value="a" data-ui-auto="auto:x:E:k">Tab A</TabsTrigger></TabsList></Tabs>)
    expect(screen.getByRole('tab', { name: 'Tab A' }).getAttribute('data-ui-auto')).toBe('auto:x:E:k')
    expect(registeredCopies('auto', 'auto:x:E:k')).toEqual([screen.getByRole('tab', { name: 'Tab A' })])
  })

  it('DropdownMenuItem', () => {
    render(<DropdownMenu open><DropdownMenuTrigger>menu</DropdownMenuTrigger><DropdownMenuContent><DropdownMenuItem data-ui-auto="auto:x:F:k">Item F</DropdownMenuItem></DropdownMenuContent></DropdownMenu>)
    expect(screen.getByRole('menuitem', { name: 'Item F' }).getAttribute('data-ui-auto')).toBe('auto:x:F:k')
    expect(registeredCopies('auto', 'auto:x:F:k')).toEqual([screen.getByRole('menuitem', { name: 'Item F' })])
  })
})

describe('watchConfirmDialog', () => {
  // MutationObserver callbacks run as microtasks: one resolved await delivers them.
  const flush = () => Promise.resolve()
  const dialog = (marked: boolean) => {
    const d = document.createElement('div')
    d.setAttribute('role', 'alertdialog')
    const yes = document.createElement('button')
    if (marked) { yes.setAttribute(GUIDE_CONFIRM_ATTR, ''); registerRef('confirm', '')(yes) }
    const no = document.createElement('button')
    d.append(yes, no)
    return { d, yes, no }
  }

  it('answers confirmed once a dialog opened after the start closes on its confirm control, never on the open alone', async () => {
    const answer = vi.fn()
    const stop = watchConfirmDialog(openDialogsNow(), answer)
    const { d, yes } = dialog(true)
    document.body.appendChild(d)
    await flush()
    expect(answer).not.toHaveBeenCalled()
    yes.addEventListener('click', () => d.remove())
    yes.click()
    await flush()
    expect(answer).toHaveBeenCalledExactlyOnceWith('confirmed')
    stop()
  })

  it('answers cancelled when a marked dialog closes any other way', async () => {
    const answer = vi.fn()
    const stop = watchConfirmDialog(openDialogsNow(), answer)
    const { d, no } = dialog(true)
    document.body.appendChild(d)
    await flush()
    no.addEventListener('click', () => d.remove())
    no.click()
    await flush()
    expect(answer).toHaveBeenCalledExactlyOnceWith('cancelled')
    stop()
  })

  it('a dialog that marks no confirm control is unknown on any close: Escape, backdrop or Cancel never confirm', async () => {
    const answer = vi.fn()
    const stop = watchConfirmDialog(openDialogsNow(), answer)
    const { d, no } = dialog(false)
    document.body.appendChild(d)
    await flush()
    no.addEventListener('click', () => d.remove())
    no.click()
    await flush()
    expect(answer).toHaveBeenCalledExactlyOnceWith('unknown')
    stop()
  })

  it('inside a dialog already open, only its registered final control confirms: a swap or a close never does', async () => {
    for (const how of ['close', 'swap'] as const) {
      const host = document.createElement('div')
      host.setAttribute('role', 'dialog')
      const submit = document.createElement('button')
      host.append(submit)
      document.body.appendChild(host)
      const answer = vi.fn()
      const stop = watchConfirmDialog(openDialogsNow(), answer, submit)
      await flush()
      if (how === 'close') host.remove()
      else submit.remove()
      await flush()
      expect(answer).not.toHaveBeenCalledWith('confirmed')
      if (how === 'close') expect(answer).toHaveBeenCalledExactlyOnceWith('unknown')
      stop()
      host.remove()
    }
  })

  it('a pressed control that is itself the final one is confirmed', async () => {
    const host = document.createElement('div')
    host.setAttribute('role', 'dialog')
    const submit = document.createElement('button')
    registerRef('confirm', '')(submit)
    host.append(submit)
    document.body.appendChild(host)
    const answer = vi.fn()
    const stop = watchConfirmDialog(openDialogsNow(), answer, submit)
    await flush()
    expect(answer).toHaveBeenCalledExactlyOnceWith('confirmed')
    stop()
    host.remove()
  })

  it('a copied data-guide-confirm attribute that React never registered confirms nothing', async () => {
    const answer = vi.fn()
    const stop = watchConfirmDialog(openDialogsNow(), answer)
    const { d, no } = dialog(false)
    no.setAttribute(GUIDE_CONFIRM_ATTR, '')
    document.body.appendChild(d)
    await flush()
    no.addEventListener('click', () => d.remove())
    no.click()
    await flush()
    expect(answer).toHaveBeenCalledExactlyOnceWith('unknown')
    stop()
  })

  it('ignores a dialog already open at the start; with none opening in time it answers unknown, never cancelled', async () => {
    vi.useFakeTimers()
    try {
      const open = document.createElement('div')
      open.setAttribute('role', 'dialog')
      document.body.appendChild(open)
      const answer = vi.fn()
      const stop = watchConfirmDialog(openDialogsNow(), answer)
      open.remove()
      await flush()
      expect(answer).not.toHaveBeenCalled()
      vi.advanceTimersByTime(GUIDE_CONFIRM_OPEN_MS - 1)
      expect(answer).not.toHaveBeenCalled()
      vi.advanceTimersByTime(1)
      expect(answer).toHaveBeenCalledExactlyOnceWith('unknown')
      stop()
    } finally {
      vi.useRealTimers()
    }
  })

  it('stopped, it answers nothing', async () => {
    const answer = vi.fn()
    const stop = watchConfirmDialog(openDialogsNow(), answer)
    const { d } = dialog(false)
    document.body.appendChild(d)
    await flush()
    stop()
    d.remove()
    await flush()
    expect(answer).not.toHaveBeenCalled()
  })
})
