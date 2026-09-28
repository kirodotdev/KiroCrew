import { render, screen } from '@testing-library/react'
import { configureStore } from '@reduxjs/toolkit'
import dashboardReducer, { sseMcpReportUpdate, sseSlots } from '../store/dashboardSlice'
import McpToolsPanel, { DOT_CLASS, SESSION_DOT_CLASS } from '../pages/chat/McpToolsPanel'
import {
  mcpSessionExtraServers,
  mcpSessionFailureReason,
  mcpSessionHasReport,
  mcpSessionServerState,
} from '../lib/mcpSessionReport'
import type { McpSessionReport } from '../types'

const report = (over: Partial<McpSessionReport> = {}): McpSessionReport => ({
  configured: [],
  ready: [],
  failed: [],
  awaiting_auth: [],
  failures: {},
  ...over,
})

describe('mcpSessionServerState', () => {
  it('reads a name out of the bucket that reported it', () => {
    const r = report({ ready: ['a'], failed: ['b'], awaiting_auth: ['c'] })
    expect(mcpSessionServerState('a', r)).toBe('started')
    expect(mcpSessionServerState('b', r)).toBe('failed')
    expect(mcpSessionServerState('c', r)).toBe('awaiting_auth')
  })

  it('answers no_report for an unreported name, NOT absent', () => {
    // The backend's init drain is time bounded and a late frame still arrives,
    // so "we have not heard" must never render as "it is not there".
    expect(mcpSessionServerState('unheard', report({ ready: ['a'] }))).toBe('no_report')
  })

  it('answers no_report when there is no report at all', () => {
    expect(mcpSessionServerState('a', null)).toBe('no_report')
    expect(mcpSessionServerState('a', undefined)).toBe('no_report')
  })
})

describe('mcpSessionHasReport', () => {
  it('is false only for the absence of a report', () => {
    expect(mcpSessionHasReport(null)).toBe(false)
    expect(mcpSessionHasReport(undefined)).toBe(false)
  })

  it('is true for a roster-only report, so the panel does not fall back to green', () => {
    // A session that sent its roster but has not been reported on yet must read
    // as "no report yet" on every row. Requiring a populated bucket here sent it
    // back to the configured-flag green dots — the false all-clear this view
    // exists to remove.
    expect(mcpSessionHasReport(report({ configured: ['a'] }))).toBe(true)
  })

  it('is true once any bucket has a name', () => {
    expect(mcpSessionHasReport(report({ awaiting_auth: ['a'] }))).toBe(true)
  })
})

describe('mcpSessionExtraServers', () => {
  it('names servers the session STARTED that the shown list omits', () => {
    // Reports are a SUPERSET of any configured list: the backend starts the
    // agent spec's own servers too.
    const r = report({ ready: ['shown', 'hidden'] })
    expect(mcpSessionExtraServers(['shown'], r)).toEqual(['hidden'])
  })

  it('excludes failed and awaiting-auth servers, which did not start', () => {
    // The copy beside this list says these started; folding in a failure would
    // make that sentence false.
    const r = report({ ready: ['up'], failed: ['broken'], awaiting_auth: ['pending'] })
    expect(mcpSessionExtraServers([], r)).toEqual(['up'])
  })

  it('deduplicates and returns empty without a report', () => {
    expect(mcpSessionExtraServers([], report({ ready: ['a', 'a'] }))).toEqual(['a'])
    expect(mcpSessionExtraServers(['a'], null)).toEqual([])
  })
})

describe('mcpSessionFailureReason', () => {
  it('returns the reported reason, or empty', () => {
    expect(mcpSessionFailureReason('a', report({ failures: { a: 'ENOENT' } }))).toBe('ENOENT')
    expect(mcpSessionFailureReason('b', report({ failures: { a: 'ENOENT' } }))).toBe('')
    expect(mcpSessionFailureReason('a', null)).toBe('')
  })
})

const servers = [
  { name: 'kirocrew-core', enabled: true },
  { name: 'github-mcp', enabled: true },
  { name: 'slack-mcp', enabled: true },
]
const toolsByServer = {}

describe('McpToolsPanel session report', () => {
  const base = {
    servers,
    toolsByServer,
    loaded: new Set<string>(),
    toolSearchOn: true,
    loading: false,
  }

  it('says nothing about the session when there is no report', () => {
    render(<McpToolsPanel {...base} />)
    expect(screen.queryByTitle('Started in this session')).not.toBeInTheDocument()
    expect(
      screen.queryByText(/this agent's configuration/),
    ).not.toBeInTheDocument()
  })

  it('marks each configured server with what this session reported', () => {
    // The reported case: the spec declares github-mcp but this session never
    // reported it, which is exactly the divergence the panel could not show.
    render(
      <McpToolsPanel
        {...base}
        sessionReport={report({
          configured: ['kirocrew-core'],
          ready: ['kirocrew-core'],
          failed: ['slack-mcp'],
          failures: { 'slack-mcp': 'spawn ENOENT' },
        })}
      />,
    )
    expect(screen.getByTitle('Started in this session')).toBeInTheDocument()
    expect(screen.getByTitle('Failed to start in this session: spawn ENOENT')).toBeInTheDocument()
    expect(screen.getByTitle('No report from this session yet')).toBeInTheDocument()
  })

  it('marks each server with a ring, never the tool dots’ fill', () => {
    // The mark's SHAPE is what tells the two vocabularies apart, because colour
    // is already spent on state. Asserting the ring (not just a colour) is what
    // catches a session mark drifting back onto a tool-dot treatment.
    render(
      <McpToolsPanel
        {...base}
        sessionReport={report({
          ready: ['kirocrew-core'],
          failed: ['slack-mcp'],
          awaiting_auth: ['github-mcp'],
        })}
      />,
    )
    const started = screen.getByTitle('Started in this session')
    expect(started.className).toContain('border-ok')
    expect(started.className).not.toContain('bg-ok')
    expect(screen.getByTitle('Failed to start in this session').className).toContain(
      'border-danger',
    )
    expect(screen.getByTitle('Waiting for authorization').className).toContain('border-warn')
  })

  it('renders an unreported server as a dashed ring, not a solid hollow dot', () => {
    render(<McpToolsPanel {...base} sessionReport={report({ ready: ['kirocrew-core'] })} />)
    const unreported = screen.getAllByTitle('No report from this session yet')
    expect(unreported).toHaveLength(2)
    for (const mark of unreported) {
      expect(mark.className).not.toContain('bg-ok')
      // Dashed says "not known", where the tool dot's solid hollow ring says
      // "known to be unsent".
      expect(mark.className).toContain('border-dashed')
    }
  })

  it('shares no mark with the tool-status vocabulary', () => {
    // The rule, not the current values: both vocabularies live in this one panel
    // with their legends adjacent, so a shared mark makes four labels read as
    // two. This is the guard for the defect where started/loaded were both
    // `bg-ok` and no_report/deferred were both a solid hollow dot.
    const norm = (c: string) => c.split(/\s+/).filter(Boolean).sort().join(' ')
    const toolMarks = new Set(Object.values(DOT_CLASS).map(norm))
    for (const [state, cls] of Object.entries(SESSION_DOT_CLASS)) {
      expect(toolMarks.has(norm(cls))).toBe(false)
      expect(cls, `${state} must be a ring`).toMatch(/\bborder(-2)?\b/)
    }
  })

  it('names where each half of the view comes from', () => {
    render(<McpToolsPanel {...base} sessionReport={report({ ready: ['kirocrew-core'] })} />)
    expect(screen.getByText(/this agent's configuration/)).toBeInTheDocument()
  })

  it('surfaces a server the session started that the list does not show', () => {
    render(<McpToolsPanel {...base} sessionReport={report({ ready: ['pooled-broker'] })} />)
    expect(screen.getByText(/Also started in this session: pooled-broker/)).toBeInTheDocument()
  })

  it('still surfaces them when the host has NO configured servers', () => {
    // The caller passes `loading={servers.length === 0}`, which is also true when
    // the host simply configures nothing — so gating the extras on `loading` hid
    // the session's real servers permanently in the one case where they are the
    // only thing there is to show.
    render(
      <McpToolsPanel
        {...base}
        servers={[]}
        toolsByServer={{}}
        loading
        sessionReport={report({ ready: ['pooled-broker'] })}
      />,
    )
    expect(screen.getByText(/Also started in this session: pooled-broker/)).toBeInTheDocument()
  })

  it('shows a server awaiting authorization as neither started nor failed', () => {
    render(<McpToolsPanel {...base} sessionReport={report({ awaiting_auth: ['github-mcp'] })} />)
    expect(screen.getByTitle('Waiting for authorization')).toBeInTheDocument()
    expect(screen.queryByTitle('Started in this session')).not.toBeInTheDocument()
  })
})

describe('sseMcpReportUpdate', () => {
  const store = () => {
    const s = configureStore({ reducer: { dashboard: dashboardReducer } })
    s.dispatch(sseSlots([{ key: 's1', title: 't' }] as never))
    return s
  }

  it('merges a report into the slot it names', () => {
    const s = store()
    s.dispatch(sseMcpReportUpdate({ slot: 's1', mcp_report: report({ ready: ['a'] }) }))
    expect(s.getState().dashboard.slots?.[0].mcp_report?.ready).toEqual(['a'])
  })

  it('stores a null report rather than ignoring it', () => {
    // Null is what the gateway pushes when a session reset makes the previous
    // report describe a session that no longer exists. Treating it as "no
    // update" would leave a dead session's server list on screen as the live
    // one's — the exact stale evidence this view exists to remove.
    const s = store()
    s.dispatch(sseMcpReportUpdate({ slot: 's1', mcp_report: report({ ready: ['a'] }) }))
    s.dispatch(sseMcpReportUpdate({ slot: 's1', mcp_report: null }))
    expect(s.getState().dashboard.slots?.[0].mcp_report).toBeNull()
  })

  it('drops a delta for an unknown slot', () => {
    const s = store()
    s.dispatch(sseMcpReportUpdate({ slot: 'nope', mcp_report: report({ ready: ['a'] }) }))
    expect(s.getState().dashboard.slots?.[0].mcp_report).toBeUndefined()
  })
})
