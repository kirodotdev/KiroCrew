/**
 * Search Everywhere leaves out a page its setting turned off (`configSwitch`), the
 * way it left out Task Runner while it was a disabled app.
 */
import { describe, it, expect, vi } from 'vitest'
import type { NavigateFunction } from 'react-router-dom'

const { surfaces } = vi.hoisted(() => ({
  surfaces: [
    { navId: 'projects', label: 'Task Runner', route: '/projects', icon: null, configSwitch: 'taskrunner.enabled' },
    { navId: 'channels', label: 'Channels', route: '/channels', icon: null, configSwitch: 'dashboard.channels_enabled', configSwitchDefault: false },
    { navId: 'schedule', label: 'Schedule', route: '/schedule', icon: null },
  ],
}))
vi.mock('../../../surfaces/registry', () => ({
  getAdvertisedSurfaces: () => surfaces,
  surfaceLabel: (s: { label: string }) => s.label,
}))

import { createPagesProvider } from './pagesProvider'

const routes = (config: unknown, query: string) =>
  (createPagesProvider(vi.fn() as unknown as NavigateFunction, () => config).search(query) as { subtitle?: string }[])
    .map(r => r.subtitle)

describe('pages provider and a page switch', () => {
  it('offers the page while its setting is on or not read yet', () => {
    expect(routes(undefined, 'Task Runner')).toContain('/projects')
    expect(routes({ taskrunner: { enabled: true } }, 'Task Runner')).toContain('/projects')
  })

  it('leaves it out while its setting is off, and nothing else', () => {
    const off = { taskrunner: { enabled: false } }
    expect(routes(off, 'Task Runner')).not.toContain('/projects')
    expect(routes(off, 'Schedule')).toContain('/schedule')
  })

  it('offers a default-off page only once its setting is read as on', () => {
    expect(routes(undefined, 'Channels')).not.toContain('/channels')
    expect(routes({ dashboard: { channels_enabled: false } }, 'Channels')).not.toContain('/channels')
    expect(routes({ dashboard: { channels_enabled: true } }, 'Channels')).toContain('/channels')
  })

  it('takes the /tasks alias, which redirects to Task Runner, out with it', () => {
    expect(routes({ taskrunner: { enabled: true } }, 'Tasks')).toContain('/tasks')
    expect(routes({ taskrunner: { enabled: false } }, 'Tasks')).not.toContain('/tasks')
  })
})
