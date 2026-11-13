// @vitest-environment jsdom
/**
 * THE WHEEL RELAY IS OPT-IN PER HOST.
 *
 * The relay cancels the browser's back swipe over the frame for a rightward run,
 * which is right only where a host turns that run into something (the Mates side
 * panel's trackpad close). The same component is drawn by the chat page's preview
 * panel and the Command Center, where nothing listens, so the document carries the
 * relay only when the host asks for it.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { waitFor } from '@testing-library/react'
import { api, type DashboardManifest } from '../../api/client'
import { renderWithProviders } from '../../test/helpers'
import CrewDynamicDashboard from './CrewDynamicDashboard'

vi.mock('../../hooks/websocket/dashboardFields', () => ({
  subscribeDashboardFields: () => () => {},
}))

const docs: string[] = []
vi.mock('../../hooks/useSandboxDoc', () => ({
  useSandboxDoc: (srcdoc: string | null) => {
    if (srcdoc) docs.push(srcdoc)
    return { url: srcdoc ? '/sandbox-doc/x' : null, pending: false, failed: false, stalled: false, retry: () => {} }
  },
}))

const MANIFEST = { id: 'atlas-tree', version: 1, title: 'Tree', fields: [] } as unknown as DashboardManifest
const TARGET = { kind: 'member', slug: 'atlas', member: 'Atlas' } as const
const PAGE = {
  instance_version: 1,
  template: { id: 'atlas-tree', version: 1 },
  html: '<!doctype html><p>stored</p>',
  rendered_html: '<!doctype html><p>page</p>',
  manifest: MANIFEST,
  state: 'live' as const,
}

describe('relayWheel on the crewmate dashboard frame', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    docs.length = 0
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(PAGE as never)
  })

  it('is off by default, for hosts that do not listen', async () => {
    renderWithProviders(<CrewDynamicDashboard target={TARGET} displayName="Atlas" />)
    await waitFor(() => expect(docs.length).toBeGreaterThan(0))
    expect(docs.some((d) => d.includes('mc-frame-wheel'))).toBe(false)
  })

  it('is in the document when the host asks for it', async () => {
    renderWithProviders(<CrewDynamicDashboard target={TARGET} displayName="Atlas" relayWheel />)
    await waitFor(() => expect(docs.length).toBeGreaterThan(0))
    expect(docs.every((d) => d.includes('mc-frame-wheel'))).toBe(true)
  })
})
