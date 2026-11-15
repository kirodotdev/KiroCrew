/**
 * Task Runner on the rail after it stopped being the built-in app `projects`.
 *
 * Its row is a core surface now, and it must stand where the app's row stood: the
 * same id (so a saved order and an unpinned set still find it), the same place in
 * the Apps group until the user drags the group, and gone while its setting
 * (`taskrunner.enabled`) is off, as the row of a disabled app was.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import { naturalAppGroupOrder, useAppRailOrder, type AppNavRow } from './appRail'
import { APP_NAV_HIDDEN_KEY, APP_NAV_ORDER_KEY } from '../../lib/appNavHidden'
import { BUILTIN_SURFACE_NAV, surfaceSwitchedOn } from '../../surfaces/surfaceData'

const app = (name: string): AppNavRow => ({ path: `/${name}`, id: name, label: name, group: 'Apps', icon: <span />, appName: name })
const ids = (rows: { id: string }[]) => rows.map(r => r.id)
const OFF = { taskrunner: { enabled: false } }

describe('Task Runner keeps the place of the app row it replaced', () => {
  beforeEach(() => localStorage.clear())

  it('is the surface the app was, under the app row id', () => {
    expect(BUILTIN_SURFACE_NAV.projects).toMatchObject({
      navId: 'projects', route: '/projects', group: 'Apps',
      configSwitch: 'taskrunner.enabled', formerApp: 'projects',
    })
    expect('appOnly' in BUILTIN_SURFACE_NAV.projects).toBe(false)
  })

  it('stands among the name-sorted app rows where the app row stood', () => {
    const surface = { ...app('projects'), appName: undefined, formerApp: 'projects' }
    const other = { ...app('explore'), appName: undefined }
    expect(ids(naturalAppGroupOrder([other, surface], [app('file-explorer'), app('papyrus'), app('spec-builder')])))
      .toEqual(['explore', 'file-explorer', 'papyrus', 'projects', 'spec-builder'])
    expect(ids(naturalAppGroupOrder([surface], [app('aws-control')]))).toEqual(['aws-control', 'projects'])
    expect(ids(naturalAppGroupOrder([surface], []))).toEqual(['projects'])
  })

  it('the rail lists it among the installed apps, in the order they had', () => {
    const { result } = renderHook(() => useAppRailOrder([app('mochi'), app('spec-builder')]))
    expect(ids(result.current.sortedAppGroup)).toEqual(['mochi', 'projects', 'spec-builder'])
  })

  it('an order the user saved with the app row still places it', () => {
    localStorage.setItem(APP_NAV_ORDER_KEY, JSON.stringify(['spec-builder', 'projects', 'mochi']))
    const { result } = renderHook(() => useAppRailOrder([app('mochi'), app('spec-builder')]))
    expect(ids(result.current.sortedAppGroup)).toEqual(['spec-builder', 'projects', 'mochi'])
  })

  it('a row the user unpinned from the sidebar stays unpinned', () => {
    localStorage.setItem(APP_NAV_HIDDEN_KEY, JSON.stringify(['projects']))
    const { result } = renderHook(() => useAppRailOrder([app('mochi')]))
    expect(ids(result.current.sortedAppGroup)).toEqual(['mochi'])
  })

  it('leaves the rail while its setting is off, and only a literal false turns it off', () => {
    const off = renderHook(() => useAppRailOrder([app('mochi')], OFF))
    expect(ids(off.result.current.sortedAppGroup)).toEqual(['mochi'])
    expect(ids(off.result.current.advertisedNavItems)).not.toContain('projects')
    for (const config of [undefined, {}, { taskrunner: {} }, { taskrunner: { enabled: true } }]) {
      const on = renderHook(() => useAppRailOrder([app('mochi')], config))
      expect(ids(on.result.current.sortedAppGroup)).toEqual(['mochi', 'projects'])
    }
    expect(surfaceSwitchedOn(BUILTIN_SURFACE_NAV.projects, { taskrunner: { enabled: 0 } })).toBe(true)
    expect(surfaceSwitchedOn(BUILTIN_SURFACE_NAV.schedule, OFF)).toBe(true)
  })
})
