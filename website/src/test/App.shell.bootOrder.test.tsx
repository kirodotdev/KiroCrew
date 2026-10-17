/**
 * The shell's boot reads, in the order the shell issues them.
 *
 * React runs a component's effects in declaration order, and a custom hook's
 * effects run where the hook is called. So the order below is the order of the
 * shell's own hook calls: the autolink rules, the global approvals, the
 * terminal restore probe, the phone-connection methods, the apps rail read,
 * the registry badge, the rail run state, the credit readout, the system
 * metrics, then the boot effect (slots before status) and only then the
 * WebSocket. Two of those pairs are load-bearing on their own: the boot effect
 * arms the notifications fallback before the socket's first connect can own the
 * boot fetch, and the apps mount read joins an ['apps'] request a child started
 * in the same commit. A new boot read belongs in this list at the position of
 * its hook call.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import App from '../App'

const H = vi.hoisted(() => ({ log: [] as string[] }))

vi.mock('../api/client', async (orig) => {
  const actual = await orig<typeof import('../api/client')>()
  const defaults: Record<string, unknown> = {
    listApps: [], approvals: [], chatSlots: [], crons: { jobs: [] },
    mobileConnectMethods: { methods: [] },
    sessionsUsage: { usage: null },
    kirocrewConfig: { agent: { acp_backend: 'claude' } },
    system: { mem_used_gb: 4, mem_total_gb: 16, cpu_pct: 25, disk_total_gb: 100, disk_free_gb: 60 },
    status: { version: '1.0.0' },
    changelog: { content: '' },
    notifications: { notifications: [] },
    listInstances: { instances: [], warm_set_cap: 5 },
    listRegistry: { apps: [], categoryOrder: [], editorialSections: [] },
    themes: { themes: [] },
    themeBoot: { mode: '', color: '', onboarded: true, import_onboarded: true },
  }
  const api = new Proxy({} as Record<string, unknown>, {
    get(_target, name: string) {
      if (name === 'then') return undefined
      return () => {
        H.log.push(`api.${name}`)
        return Promise.resolve(structuredClone(name in defaults ? defaults[name] : {}))
      }
    },
  })
  return { ...actual, api, isAuthBannerShown: () => false }
})
vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page" /> }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))
vi.mock('../hooks/useWebSocket', async () => {
  const { useEffect } = await import('react')
  const value = { subscribeLogs: () => {}, subscribeSubagents: () => {}, forceReconnect: () => {} }
  return {
    useWebSocket: () => {
      useEffect(() => { H.log.push('ws') }, [])
      return value
    },
  }
})
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' }) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))

/** The reads the shell itself owns at boot; everything else is a child's. */
const SHELL_READS = new Set([
  'api.dashboardConfig', 'api.approvals', 'fetch:/api/terminal/sessions', 'api.mobileConnectMethods',
  'api.listApps', 'api.listRegistry', 'api.crons', 'api.sessionsUsage', 'api.kirocrewConfig',
  'api.system', 'api.chatSlots', 'api.status', 'ws',
])

describe('shell boot order', () => {
  beforeEach(() => {
    H.log.length = 0
    localStorage.setItem('mc-onboarded', '1')
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      H.log.push(`fetch:${String(url)}`)
      return new Response(JSON.stringify({ enabled: true, sessions: [] }), { status: 200, headers: { 'content-type': 'application/json' } })
    }))
    return () => { vi.unstubAllGlobals() }
  })

  it('issues each shell-owned boot read once, in hook order, with the socket last', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    await screen.findByTestId('chat-page')
    const first = new Map<string, number>()
    H.log.forEach((entry, i) => { if (SHELL_READS.has(entry) && !first.has(entry)) first.set(entry, i) })
    const order = [...first.entries()].sort((a, b) => a[1] - b[1]).map(([entry]) => entry)
    expect(order).toEqual([
      'api.dashboardConfig',
      'api.approvals',
      'fetch:/api/terminal/sessions',
      'api.mobileConnectMethods',
      'api.listApps',
      'api.listRegistry',
      'api.crons',
      'api.sessionsUsage',
      'api.kirocrewConfig',
      'api.system',
      'api.chatSlots',
      'api.status',
      'ws',
    ])
    // No restored terminal tab is suspect here, so the confirm look never runs.
    expect(H.log.filter(e => e === 'fetch:/api/terminal/sessions')).toHaveLength(1)
    expect(H.log.filter(e => e === 'api.listApps')).toHaveLength(1)
  })
})
