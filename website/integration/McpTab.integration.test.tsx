import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import McpTab from '../src/pages/overview/McpTab'
import { server } from './mocks/server'
import { http, HttpResponse } from 'msw'

describe('McpTab Integration Tests', () => {
  beforeEach(() => {
    vi.clearAllMocks()

    server.use(
      http.get('/api/mcp/probe', () => HttpResponse.json([])),
    )
  })

  it('loads and displays MCP servers on mount', async () => {
    renderWithProviders(<McpTab />)

    await waitFor(() => {
      // Each server name appears in two places: the filter-chip Badge at the
      // top and the <code> element inside the table row.  Both are expected
      // in the new UI, so use getAllByText to assert presence.
      expect(screen.getAllByText('builder-mcp').length).toBeGreaterThan(0)
      expect(screen.getAllByText('ai-community-slack-mcp').length).toBeGreaterThan(0)
    })
  })

  it('displays server status badges correctly', async () => {
    server.use(
      http.get('/api/mcp', () => {
        return HttpResponse.json([
          {
            name: 'builder-mcp',
            status: 'ok',
            enabled: true,
            tools: ['ReadInternalWebsites', 'TaskeiGetTask'],
            presence: { kirocrew: true, kiroGlobal: true, ccGlobal: false },
          },
          {
            name: 'ai-community-slack-mcp',
            status: 'error',
            error: 'Connection failed',
            enabled: false,
            tools: [],
            presence: { kirocrew: false, kiroGlobal: false, ccGlobal: false },
          },
        ])
      })
    )

    renderWithProviders(<McpTab />)

    await waitFor(() => {
      expect(screen.getByText('Online')).toBeInTheDocument()
      expect(screen.getByText('Error')).toBeInTheDocument()
    })
  })

  it('displays tools for each server', async () => {
    const user = userEvent.setup()
    server.use(
      http.get('/api/mcp', () => {
        return HttpResponse.json([
          {
            name: 'test-server',
            status: 'ok',
            enabled: true,
            tools: ['ToolAlpha', 'ToolBeta', 'ToolGamma'],
            disabledTools: [],
            command: 'node test',
            presence: { kirocrew: true, kiroGlobal: false, ccGlobal: false },
          },
        ])
      })
    )
    renderWithProviders(<McpTab />)

    await waitFor(() => {
      expect(screen.getByText(/3 tools/)).toBeInTheDocument()
    })

    await user.click(screen.getByText(/3 tools/))

    await waitFor(() => {
      expect(screen.getByText('ToolAlpha')).toBeInTheDocument()
      expect(screen.getByText('ToolBeta')).toBeInTheDocument()
      expect(screen.getByText('ToolGamma')).toBeInTheDocument()
    }, { timeout: 3000 })
  })

  it('stages tool disable and commits it via Apply', async () => {
    const user = userEvent.setup()

    let applyPayload: any = null
    server.use(
      http.get('/api/mcp', () => {
        return HttpResponse.json([
          {
            name: 'toggle-server',
            status: 'ok',
            enabled: true,
            tools: ['ToggleTool'],
            disabledTools: [],
            command: 'node toggle',
            presence: { kirocrew: true, kiroGlobal: false, ccGlobal: false },
          },
        ])
      }),
      http.post('/api/mcp/apply', async ({ request }) => {
        applyPayload = await request.json()
        return HttpResponse.json({ ok: true, applied: 1, results: [] })
      })
    )

    renderWithProviders(<McpTab />)

    // Expand tool list
    await waitFor(() => expect(screen.getByText(/1 tools/)).toBeInTheDocument())
    await user.click(screen.getByText(/1 tools/))
    await waitFor(() => expect(screen.getByText('ToggleTool')).toBeInTheDocument())

    // Click the tool button — should stage a pending change, NOT fire an API call
    await user.click(screen.getByText('ToggleTool'))

    // Pending banner should appear with Apply button
    await waitFor(() => {
      expect(screen.getByText(/pending change/i)).toBeInTheDocument()
    })

    // Click Apply
    await user.click(screen.getByRole('button', { name: /^apply$/i }))

    // Verify batched Apply was called with the tool override
    await waitFor(() => {
      expect(applyPayload).toBeTruthy()
      expect(applyPayload.changes).toHaveLength(1)
      expect(applyPayload.changes[0].name).toBe('toggle-server')
      expect(applyPayload.changes[0].toolOverrides).toEqual({ ToggleTool: false })
    })
  })

  it('stages KiroCrew scope off and commits it via Apply', async () => {
    const user = userEvent.setup()

    let applyPayload: any = null
    server.use(
      http.post('/api/mcp/apply', async ({ request }) => {
        applyPayload = await request.json()
        return HttpResponse.json({ ok: true, applied: 1, results: [] })
      })
    )

    renderWithProviders(<McpTab />)

    await waitFor(() => {
      expect(screen.getAllByText('builder-mcp').length).toBeGreaterThan(0)
    })

    // Find the builder-mcp row and click its KiroCrew scope badge
    const rows = screen.getAllByRole('row')
    const builderRow = rows.find(row =>
      within(row as HTMLElement).queryByText('builder-mcp')
    )
    expect(builderRow).toBeDefined()

    const mcBadge = within(builderRow as HTMLElement).getByRole('button', {
      name: /Kiro Crew.*click to disable/i,
    })
    await user.click(mcBadge)

    // Pending banner shows
    await waitFor(() => {
      expect(screen.getByText(/pending change/i)).toBeInTheDocument()
    })

    await user.click(screen.getByRole('button', { name: /^apply$/i }))

    await waitFor(() => {
      expect(applyPayload).toBeTruthy()
      expect(applyPayload.changes).toHaveLength(1)
      expect(applyPayload.changes[0].name).toBe('builder-mcp')
      expect(applyPayload.changes[0].kirocrew).toBe(false)
    })
  })

  it('filters servers by search term', async () => {
    const user = userEvent.setup()
    renderWithProviders(<McpTab />)

    await waitFor(() => {
      expect(screen.getAllByText('builder-mcp').length).toBeGreaterThan(0)
      expect(screen.getAllByText('ai-community-slack-mcp').length).toBeGreaterThan(0)
    })

    const filterInput = screen.getByPlaceholderText(/filter servers or tools/i)
    await user.type(filterInput, 'builder')

    // builder-mcp still visible
    expect(screen.getAllByText('builder-mcp')[0]).toBeVisible()
    // slack should be filtered out
    expect(screen.queryAllByText('ai-community-slack-mcp')).toHaveLength(0)
  })

  it('stages an uninstall with Undo and commits it via Apply', async () => {
    const user = userEvent.setup()

    let applyPayload: any = null
    server.use(
      http.post('/api/mcp/apply', async ({ request }) => {
        applyPayload = await request.json()
        return HttpResponse.json({ ok: true, applied: 1, results: [] })
      })
    )

    renderWithProviders(<McpTab />)

    await waitFor(() => {
      expect(screen.getAllByText('ai-community-slack-mcp').length).toBeGreaterThan(0)
    })

    const rows = screen.getAllByRole('row')
    const slackRow = rows.find(row =>
      within(row as HTMLElement).queryByText('ai-community-slack-mcp')
    )
    expect(slackRow).toBeDefined()

    // Click Uninstall — stages, shows Undo
    await user.click(
      within(slackRow as HTMLElement).getByRole('button', { name: /uninstall/i })
    )
    await waitFor(() => {
      expect(within(slackRow as HTMLElement).getByRole('button', { name: /undo/i }))
        .toBeInTheDocument()
    })

    // Apply commits
    await user.click(screen.getByRole('button', { name: /^apply$/i }))

    await waitFor(() => {
      expect(applyPayload).toBeTruthy()
      expect(applyPayload.changes).toHaveLength(1)
      expect(applyPayload.changes[0].name).toBe('ai-community-slack-mcp')
      expect(applyPayload.changes[0].uninstall).toBe(true)
    })
  })

  it('shows error message for server with error status', async () => {
    server.use(
      http.get('/api/mcp', () => {
        return HttpResponse.json([
          {
            name: 'broken-server',
            status: 'error',
            error: 'Failed to connect',
            enabled: true,
            tools: [],
            presence: { kirocrew: true, kiroGlobal: false, ccGlobal: false },
          },
        ])
      })
    )

    renderWithProviders(<McpTab />)

    await waitFor(() => {
      expect(screen.getAllByText('broken-server').length).toBeGreaterThan(0)
      expect(screen.getByText(/failed to connect/i)).toBeInTheDocument()
    })
  })

  it('shows a server row for every installed server', async () => {
    renderWithProviders(<McpTab />)

    // Each server appears both in the top filter-chip Badge row and in the
    // table row — verify both known servers render without asserting a
    // specific enabled/disabled suffix (the old UI appended "(off)" to the
    // chip text; the new UI shows presence via scope badges instead).
    await waitFor(() => {
      expect(screen.getAllByText('ai-community-slack-mcp').length).toBeGreaterThan(0)
      expect(screen.getAllByText('builder-mcp').length).toBeGreaterThan(0)
    })
  })

  // Regression: the Apply success banner schedules a setTimeout that clears
  // the banner 5s later. If the component unmounts before it fires, the timer
  // must be cancelled — otherwise setApplyMsg('') runs after teardown and
  // throws "window is not defined", failing the whole vitest run (and the
  // build) even though every test technically passed.
  //
  // Uses REAL timers + a clearTimeout spy (not fake timers): fake timers
  // deadlock against MSW/react-query promise resolution under userEvent.
  // We capture the 5s dismiss timer's id when it is scheduled, then assert
  // unmount calls clearTimeout with that exact id — proving the cleanup
  // cancels the pending banner timer.
  it('cancels the pending Apply-banner timer on unmount (no post-teardown state update)', async () => {
    const user = userEvent.setup()
    server.use(
      http.get('/api/mcp', () =>
        HttpResponse.json([
          {
            name: 'timer-server',
            status: 'ok',
            enabled: true,
            tools: ['T'],
            disabledTools: [],
            command: 'node t',
            presence: { kirocrew: true, kiroGlobal: false, ccGlobal: false },
          },
        ])
      ),
      http.post('/api/mcp/apply', () => HttpResponse.json({ ok: true, applied: 1, results: [] }))
    )

    // Capture the id of the 5000ms banner-dismiss timer as it is scheduled.
    // Call through to the real setTimeout so MSW/react-query keep working.
    const realSetTimeout = globalThis.setTimeout
    let bannerTimerId: ReturnType<typeof setTimeout> | undefined
    const setSpy = vi.spyOn(globalThis, 'setTimeout')
    setSpy.mockImplementation(((fn: any, ms?: number, ...rest: any[]) => {
      const tid = realSetTimeout(fn, ms as any, ...rest)
      if (ms === 5000) bannerTimerId = tid
      return tid
    }) as any)
    const clearSpy = vi.spyOn(globalThis, 'clearTimeout')

    const { unmount } = renderWithProviders(<McpTab />)

    await waitFor(() => expect(screen.getByText(/1 tools/)).toBeInTheDocument())
    await user.click(screen.getByText(/1 tools/))
    await waitFor(() => expect(screen.getByText('T')).toBeInTheDocument())
    await user.click(screen.getByText('T'))
    await waitFor(() => expect(screen.getByText(/pending change/i)).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /^apply$/i }))

    // Success banner scheduled the 5s dismiss timer.
    await waitFor(() => expect(screen.getByText(/Applied 1 change/)).toBeInTheDocument())
    await waitFor(() => expect(bannerTimerId).toBeDefined())

    // Unmount must cancel the still-pending banner timer.
    unmount()
    expect(clearSpy).toHaveBeenCalledWith(bannerTimerId)

    setSpy.mockRestore()
    clearSpy.mockRestore()
  })
})
