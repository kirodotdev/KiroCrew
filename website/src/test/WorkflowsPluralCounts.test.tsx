/**
 * The workflow run views' counts go through plural catalog keys.
 *
 * The run list used to print `{n} events` / `{n} agents` and the source panel
 * `${n} lines`, so a single item read "1 events", "1 lines", and every language
 * but English got the English noun. These pin count = 1 and count = 2 for each
 * count.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', () => ({ api: { promoteWorkflowRun: vi.fn() } }))

import WorkflowsRuns, { type RunSummary } from '../components/workflows/WorkflowsRuns'
import WorkflowSourcePanel from '../components/workflows/WorkflowSourcePanel'
import { i18next } from '../i18n/all'

const jsonResponse = (body: unknown) =>
  Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) }) as Promise<Response>

function run(id: string, count: number): RunSummary & { agent_count: number } {
  return {
    run_id: id,
    name: id,
    status: 'finished',
    result: null,
    error: null,
    author: null,
    event_count: count,
    agent_count: count,
  } as RunSummary & { agent_count: number }
}

function withClient(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/workflows/runs') return jsonResponse({ runs: [run('wf_one', 1), run('wf_two', 2)] })
      throw new Error(`Unexpected request: ${url}`)
    }),
  )
})

afterEach(async () => {
  vi.unstubAllGlobals()
  await i18next.changeLanguage('en')
})

describe('Workflows run list counts', () => {
  it('reads "1 agent · 1 event" for a count of 1', async () => {
    withClient(<WorkflowsRuns />)
    expect(await screen.findByText('1 agent · 1 event')).toBeInTheDocument()
  })

  it('reads "2 agents · 2 events" for a count of 2', async () => {
    withClient(<WorkflowsRuns />)
    expect(await screen.findByText('2 agents · 2 events')).toBeInTheDocument()
  })

  it('uses the language\'s own plural form, not the English noun', async () => {
    await i18next.changeLanguage('ru')
    withClient(<WorkflowsRuns />)
    expect(await screen.findByText('1 агент · 1 событие')).toBeInTheDocument()
    expect(await screen.findByText('2 агента · 2 события')).toBeInTheDocument()
  })
})

describe('Workflow source panel line count', () => {
  it('reads "1 line" for a one-line script', () => {
    withClient(<WorkflowSourcePanel run_id="wf_one" source="x = 1" />)
    expect(screen.getByText('1 line')).toBeInTheDocument()
  })

  it('reads "2 lines" for a two-line script', () => {
    withClient(<WorkflowSourcePanel run_id="wf_two" source={'x = 1\ny = 2'} />)
    expect(screen.getByText('2 lines')).toBeInTheDocument()
  })
})
