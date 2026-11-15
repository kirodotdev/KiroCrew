/**
 * Channels on the rail after it stopped being the built-in app `channels`.
 *
 * The app was hidden and off by default, so its row showed only once someone
 * turned it on. Its row is a core surface now, switched by
 * `dashboard.channels_enabled` (default off): the same id (so a saved order and
 * an unpinned set still find it), the same place in the Apps group, and shown only
 * while the setting says `true`, never before the config has been read.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useAppRailOrder, type AppNavRow } from './appRail'
import { APP_NAV_HIDDEN_KEY, APP_NAV_ORDER_KEY } from '../../lib/appNavHidden'
import { BUILTIN_SURFACE_NAV, SURFACE_FORMER_APPS, surfaceSwitchedOn } from '../../surfaces/surfaceData'

const app = (name: string): AppNavRow => ({ path: `/${name}`, id: name, label: name, group: 'Apps', icon: <span />, appName: name })
const ids = (rows: { id: string }[]) => rows.map(r => r.id)
const ON = { dashboard: { channels_enabled: true } }

describe('Channels keeps the row of the app it replaced', () => {
  beforeEach(() => localStorage.clear())

  it('is the surface the app was, under the app row id, off by default', () => {
    expect(BUILTIN_SURFACE_NAV.channels).toMatchObject({
      navId: 'channels', route: '/channels', group: 'Apps',
      configSwitch: 'dashboard.channels_enabled', configSwitchDefault: false, formerApp: 'channels',
    })
    expect(SURFACE_FORMER_APPS.get('channels')).toBe('/channels')
  })

  it('shows only while the setting is a literal true', () => {
    for (const config of [undefined, {}, { dashboard: {} }, { dashboard: { channels_enabled: false } }, { dashboard: { channels_enabled: 1 } }]) {
      const off = renderHook(() => useAppRailOrder([app('mochi')], config))
      expect(ids(off.result.current.sortedAppGroup)).toEqual(['mochi', 'projects'])
      expect(ids(off.result.current.advertisedNavItems)).not.toContain('channels')
    }
    const on = renderHook(() => useAppRailOrder([app('mochi')], ON))
    expect(ids(on.result.current.sortedAppGroup)).toEqual(['channels', 'mochi', 'projects'])
  })

  it('stands among the name-sorted app rows where the app row stood', () => {
    const { result } = renderHook(() => useAppRailOrder([app('aws-control'), app('code-review-sage')], ON))
    expect(ids(result.current.sortedAppGroup)).toEqual(['aws-control', 'channels', 'code-review-sage', 'projects'])
  })

  it('an order the user saved with the app row still places it', () => {
    localStorage.setItem(APP_NAV_ORDER_KEY, JSON.stringify(['mochi', 'channels', 'projects']))
    const { result } = renderHook(() => useAppRailOrder([app('mochi')], ON))
    expect(ids(result.current.sortedAppGroup)).toEqual(['mochi', 'channels', 'projects'])
  })

  it('a row the user unpinned from the sidebar stays unpinned', () => {
    localStorage.setItem(APP_NAV_HIDDEN_KEY, JSON.stringify(['channels']))
    const { result } = renderHook(() => useAppRailOrder([app('mochi')], ON))
    expect(ids(result.current.sortedAppGroup)).toEqual(['mochi', 'projects'])
  })

  it('a default-on switch still reads an unread config as on', () => {
    expect(surfaceSwitchedOn(BUILTIN_SURFACE_NAV.projects, undefined)).toBe(true)
    expect(surfaceSwitchedOn(BUILTIN_SURFACE_NAV.channels, undefined)).toBe(false)
    expect(surfaceSwitchedOn(BUILTIN_SURFACE_NAV.channels, ON)).toBe(true)
  })
})
