import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import McpTab from '../src/pages/overview/McpTab'
import MemoryTab from '../src/pages/overview/MemoryTab'
import HooksPage from '../src/pages/HooksPage'
import SchedulePage from '../src/pages/SchedulePage'
import { server } from './mocks/server'
import { http, HttpResponse } from 'msw'

describe('SortableTable Integration Tests', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    server.use(
      http.get('/api/agents/installed', () =>
        HttpResponse.json([{ name: 'default', source: 'builtin' }])
      )
    )
  })

  describe('McpTab sorting', () => {
    it('default sort orders servers by name ascending', async () => {
      renderWithProviders(<McpTab />)
      await waitFor(() => expect(screen.getAllByText('builder-mcp').length).toBeGreaterThan(0))

      const nameBtn = screen.getByRole('button', { name: /name/i })

      const rows = screen.getAllByRole('row').slice(1)
      expect(rows[0]).toHaveTextContent('ai-community-slack-mcp')
      expect(rows[1]).toHaveTextContent('builder-mcp')
    })

    it('clicking Name toggles to descending and reverses row order', async () => {
      const user = userEvent.setup()
      renderWithProviders(<McpTab />)
      await waitFor(() => expect(screen.getAllByText('builder-mcp').length).toBeGreaterThan(0))

      const nameBtn = screen.getByRole('button', { name: /name/i })
      await user.click(nameBtn) // asc -> desc

      const rows = screen.getAllByRole('row').slice(1)
      expect(rows[0]).toHaveTextContent('builder-mcp')
      expect(rows[1]).toHaveTextContent('ai-community-slack-mcp')
    })

    it('clicking Tools sorts by tool count ascending', async () => {
      server.use(
        http.get('/api/mcp', () =>
          HttpResponse.json([
            { name: 'few-tools', status: 'ok', enabled: true, tools: ['t1'], disabledTools: [], command: 'node few' },
            { name: 'many-tools', status: 'ok', enabled: true, tools: ['t1', 't2', 't3'], disabledTools: [], command: 'node many' },
          ])
        )
      )
      const user = userEvent.setup()
      renderWithProviders(<McpTab />)
      await waitFor(() => expect(screen.getAllByText('few-tools').length).toBeGreaterThan(0))

      const toolsBtn = within(screen.getAllByRole('row')[0]).getByRole('button', { name: /tools/i })
      await user.click(toolsBtn)

      const rows = screen.getAllByRole('row').slice(1)
      expect(rows[0]).toHaveTextContent('few-tools')
      expect(rows[1]).toHaveTextContent('many-tools')
    })
  })

  describe('MemoryTab Lessons sorting', () => {
    beforeEach(() => {
      server.use(
        http.get('/api/lessons', () =>
          HttpResponse.json({
            lessons: [
              { rule: 'Zebra rule', category: 'knowledge', ts: '2026-01-01T00:00:00Z' },
              { rule: 'Alpha rule', category: 'tool', ts: '2026-03-01T00:00:00Z' },
            ],
          })
        )
      )
    })

    it('default sort orders lessons by newest first', async () => {
      renderWithProviders(<MemoryTab refreshTrigger={0} />)
      await waitFor(() => expect(screen.getByText('Alpha rule')).toBeInTheDocument())

      const rows = screen.getAllByRole('row').slice(1)
      expect(rows[0]).toHaveTextContent('Alpha rule')   // 2026-03 (newest)
      expect(rows[1]).toHaveTextContent('Zebra rule')   // 2026-01 (oldest)
    })

    it('clicking Rule twice sorts rules descending', async () => {
      const user = userEvent.setup()
      renderWithProviders(<MemoryTab refreshTrigger={0} />)
      await waitFor(() => expect(screen.getByText('Alpha rule')).toBeInTheDocument())

      const ruleBtn = screen.getByRole('button', { name: /rule/i })
      await user.click(ruleBtn) // asc (same as default order, not useful alone)
      await user.click(ruleBtn) // desc

      const rows = screen.getAllByRole('row').slice(1)
      expect(rows[0]).toHaveTextContent('Zebra rule')
      expect(rows[1]).toHaveTextContent('Alpha rule')
    })

    it('clicking Category sorts by category ascending', async () => {
      const user = userEvent.setup()
      renderWithProviders(<MemoryTab refreshTrigger={0} />)
      await waitFor(() => expect(screen.getByText('Alpha rule')).toBeInTheDocument())

      const catBtn = screen.getByRole('button', { name: /category/i })
      await user.click(catBtn)

      const rows = screen.getAllByRole('row').slice(1)
      // knowledge < tool alphabetically
      expect(rows[0]).toHaveTextContent('Zebra rule')  // knowledge
      expect(rows[1]).toHaveTextContent('Alpha rule')   // tool
    })
  })

  describe('HooksPage sorting', () => {
    beforeEach(() => {
      server.use(
        http.get('/api/hooks', () =>
          HttpResponse.json({ hooks: [
            { id: 'h1', name: 'Beta hook', event: 'UserPromptSubmit', matcher: '*', command: 'echo b', timeout: 30, enabled: true, last_run: 100, last_status: 'ok', run_count: 5 },
            { id: 'h2', name: 'Alpha hook', event: 'AgentReply', matcher: '*', command: 'echo a', timeout: 30, enabled: true, last_run: 200, last_status: 'error', run_count: 12 },
          ]})
        )
      )
    })

    it('default sort orders hooks by name ascending', async () => {
      renderWithProviders(<HooksPage />)
      await waitFor(() => expect(screen.getByText('Alpha hook')).toBeInTheDocument())

      const rows = screen.getAllByRole('row').slice(1)
      expect(rows[0]).toHaveTextContent('Alpha hook')
      expect(rows[1]).toHaveTextContent('Beta hook')
    })

    it('clicking Runs sorts by run count ascending', async () => {
      const user = userEvent.setup()
      renderWithProviders(<HooksPage />)
      await waitFor(() => expect(screen.getByText('Alpha hook')).toBeInTheDocument())

      const runsBtn = screen.getByRole('button', { name: /runs/i })
      await user.click(runsBtn)

      const rows = screen.getAllByRole('row').slice(1)
      expect(rows[0]).toHaveTextContent('Beta hook')   // 5 runs
      expect(rows[1]).toHaveTextContent('Alpha hook')   // 12 runs
    })
  })

  describe('SchedulePage sorting', () => {
    beforeEach(() => {
      globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as any
    })

    /** Fixture shared by the sorting cases below. */
    function serveJobs() {
      server.use(
        http.get('/api/crons', () =>
          HttpResponse.json({ jobs: [
            { id: 'c1', name: 'ok-job', message: 'm', schedule: '* * * * *', enabled: true, last_status: 'ok', last_run_ts: 1, next_run_ts: 100 },
            { id: 'c2', name: 'paused-job', message: 'm', schedule: '* * * * *', enabled: false, last_status: '', last_run_ts: 0, next_run_ts: 0 },
            { id: 'c3', name: 'error-job', message: 'm', schedule: '* * * * *', enabled: true, last_status: 'error', last_run_ts: 2, next_run_ts: 200 },
            { id: 'c4', name: 'ready-job', message: 'm', schedule: '* * * * *', enabled: true, last_status: '', last_run_ts: 0, next_run_ts: 300 },
          ]})
        )
      )
    }

    it('clicking Status sorts by rendered state: Paused < Error < OK < Ready', async () => {
      serveJobs()
      const user = userEvent.setup()
      renderWithProviders(<SchedulePage />)
      await waitFor(() => expect(screen.getByText('ok-job')).toBeInTheDocument())

      const statusBtn = screen.getByRole('button', { name: /status/i })
      await user.click(statusBtn) // asc: Paused(0) < Error(1) < OK(2) < Ready(3)

      const rows = screen.getAllByRole('row').slice(1)
      expect(rows[0]).toHaveTextContent('paused-job')
      expect(rows[1]).toHaveTextContent('error-job')
      expect(rows[2]).toHaveTextContent('ok-job')
      expect(rows[3]).toHaveTextContent('ready-job')
    })

    // Name is not the default column here (the table opens on nextRun), so the
    // first click selects it ascending and the second flips it — covering both
    // directions of the name comparator on the live cron surface.
    it('clicking Name sorts ascending, clicking again reverses it', async () => {
      serveJobs()
      const user = userEvent.setup()
      renderWithProviders(<SchedulePage />)
      await waitFor(() => expect(screen.getByText('ok-job')).toBeInTheDocument())

      const nameBtn = within(screen.getAllByRole('row')[0]).getByRole('button', { name: /name/i })

      await user.click(nameBtn) // unsorted-by-name -> asc
      let rows = screen.getAllByRole('row').slice(1)
      expect(rows[0]).toHaveTextContent('error-job')
      expect(rows[3]).toHaveTextContent('ready-job')

      await user.click(nameBtn) // asc -> desc
      rows = screen.getAllByRole('row').slice(1)
      expect(rows[0]).toHaveTextContent('ready-job')
      expect(rows[3]).toHaveTextContent('error-job')
    })
  })
})
