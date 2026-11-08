/**
 * The sidebar app row's marks and menu items are decided by pure functions, so
 * the visibility rules are pinned here rather than through a rendered rail:
 * which row gets the update arrow and the attention mark, and which items its
 * menu offers in which order. The rendered menu is pinned at App level in
 * `test/App.appRailContextMenu.test.tsx`.
 */
import { describe, it, expect } from 'vitest'
import { appRailMenuItems, appRailStatuses, hasRailMark, menulessRailStatus, type AppRailStatusRow } from './appRailStatus'
import { stillTouchHoldPoint } from './appRail'

const row = (name: string, extra: Partial<AppRailStatusRow> = {}): AppRailStatusRow => ({ name, lifecycle: 'gateway', ...extra })

describe('appRailStatuses', () => {
  it('pairs each newer registry version with its own app, whatever the lifecycle', () => {
    const updates = new Map([['alpha', '2.0.0'], ['gamma', '3.1.0']])
    const statuses = appRailStatuses(updates, [row('alpha'), row('beta'), row('gamma', { lifecycle: 'app' })])

    expect(statuses.alpha.newVersion).toBe('2.0.0')
    expect(statuses.beta.newVersion).toBeUndefined()
    expect(statuses.gamma.newVersion).toBe('3.1.0')
  })

  it('marks an orphaned app for migration ahead of a failing backend, and a failing backend alone', () => {
    const exhausted = { restart_exhausted: { attempts: 5 } }
    const statuses = appRailStatuses(new Map(), [
      row('both', { orphaned: true, backend_status: exhausted }),
      row('backend', { backend_status: exhausted }),
      row('healthy', { backend_status: { running: true, restart_exhausted: null } }),
    ])

    expect(statuses.both.attention).toBe('migrate')
    expect(statuses.backend.attention).toBe('backend')
    expect(statuses.backend.restartExhausted).toEqual({ attempts: 5 })
    expect(statuses.healthy.attention).toBeUndefined()
  })

  it('keeps the lifecycle that decides Uninstall', () => {
    expect(appRailStatuses(new Map(), [row('pinned', { lifecycle: 'locked' })]).pinned.lifecycle).toBe('locked')
  })

  it('has no inherited keys, so an app named constructor reads only its own status', () => {
    const statuses = appRailStatuses(new Map(), [row('alpha')])
    expect(statuses.constructor).toBeUndefined()
    expect(appRailStatuses(new Map([['constructor', '1.1.0']]), [row('constructor')]).constructor.newVersion).toBe('1.1.0')
  })

  it('reads a cold apps cache as no statuses', () => {
    expect(Object.keys(appRailStatuses(new Map([['alpha', '2.0.0']]), undefined))).toEqual([])
  })
})

describe('hasRailMark', () => {
  it('is true for an update or an attention cause and false otherwise', () => {
    expect(hasRailMark({ newVersion: '2.0.0' })).toBe(true)
    expect(hasRailMark({ attention: 'backend' })).toBe(true)
    expect(hasRailMark({ lifecycle: 'gateway' })).toBe(false)
    expect(hasRailMark(undefined)).toBe(false)
  })
})

describe('appRailMenuItems', () => {
  it('offers the fixed items in menu order for a plain app', () => {
    expect(appRailMenuItems({ lifecycle: 'gateway' }, 0))
      .toEqual(['details', 'library', 'disable', 'unpin', 'uninstall'])
  })

  it('adds Update first and Mark as read after Open in Library, only under their conditions', () => {
    expect(appRailMenuItems({ newVersion: '2.0.0', lifecycle: 'gateway' }, 3))
      .toEqual(['update', 'details', 'library', 'mark-read', 'disable', 'unpin', 'uninstall'])
  })

  it('drops Uninstall for a locked app only, as the Library tile does', () => {
    expect(appRailMenuItems({ lifecycle: 'locked' }, 0)).not.toContain('uninstall')
    expect(appRailMenuItems({ lifecycle: 'app' }, 0)).toContain('uninstall')
    expect(appRailMenuItems({}, 0)).toContain('uninstall')
  })

  it('never offers Open, Enable or Settings', () => {
    const all = appRailMenuItems({ newVersion: '2.0.0', attention: 'migrate', lifecycle: 'gateway' }, 1) as string[]
    expect(all).not.toContain('open')
    expect(all).not.toContain('enable')
    expect(all).not.toContain('settings')
  })
})

describe('menulessRailStatus', () => {
  it('drops a backend failure, which needs the menu\'s notice, and keeps the rest', () => {
    expect(menulessRailStatus({ attention: 'backend', restartExhausted: { attempts: 5 }, newVersion: '2.0.0', lifecycle: 'gateway' }))
      .toEqual({ newVersion: '2.0.0', lifecycle: 'gateway' })
  })

  it('keeps an orphaned app\'s mark, since a tap on the row opens the migration page', () => {
    const status = { attention: 'migrate' as const, lifecycle: 'gateway' as const }
    expect(menulessRailStatus(status)).toBe(status)
    expect(menulessRailStatus(undefined)).toBeUndefined()
  })
})

describe('stillTouchHoldPoint', () => {
  const touchStart = { touches: [{ clientX: 12, clientY: 30 }] } as unknown as Event

  it('returns where a touch hold began when it lifts inside the sensor tolerance', () => {
    expect(stillTouchHoldPoint({ activatorEvent: touchStart, delta: { x: 3, y: -4 } })).toEqual({ x: 12, y: 30 })
  })

  it('treats a move past the tolerance as a reorder', () => {
    expect(stillTouchHoldPoint({ activatorEvent: touchStart, delta: { x: 0, y: 6 } })).toBeNull()
  })

  it('ignores a mouse or keyboard drag', () => {
    const mouse = { clientX: 12, clientY: 30 } as unknown as Event
    expect(stillTouchHoldPoint({ activatorEvent: mouse, delta: { x: 0, y: 0 } })).toBeNull()
  })
})
