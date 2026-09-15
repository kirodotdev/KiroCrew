/**
 * WorkflowsPage — the author/run shell. The pure event-stream helpers live in
 * runModel (covered by WorkflowsPage.test.ts); what belongs to the PAGE is the
 * two-phase Run gesture (validate, then run only if validation passed), the
 * request-failure banner, the example loader writing into the editor, the
 * budget badge, and the author/runs view switch.
 *
 * The two heavy children each own their own test file and are stubbed, so these
 * assertions are about the props the page hands down.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { i18nT } from '../i18n/t'

vi.mock('../apps/workflows/WorkflowsRuns', () => ({
  default: () => <div data-testid="zzq-runs-stub" />,
}))
vi.mock('../apps/workflows/WorkflowRunTree', () => ({
  default: ({ events, status, error }: { events: unknown[]; status: string; error?: string | null }) => (
    <div data-testid="zzq-tree-stub">
      <span>events:{events.length}</span>
      <span>status:{status}</span>
      <span>error:{error ?? 'none'}</span>
    </div>
  ),
}))
// Radix Select needs pointer geometry happy-dom does not provide; a native
// select keeps the assertion on the page's onChange contract.
vi.mock('../components/SimpleSelect', () => ({
  default: ({ options, onChange, 'aria-label': ariaLabel }: {
    options: string[]
    onChange: (v: string) => void
    'aria-label'?: string
  }) => (
    <select aria-label={ariaLabel} onChange={e => onChange(e.target.value)}>
      <option value="">--</option>
      {options.map(o => <option key={o} value={o}>{o}</option>)}
    </select>
  ),
}))

import WorkflowsPage from '../apps/workflows/WorkflowsPage'

const EXAMPLE = { name: 'zzq-example', description: 'zzq desc', source: 'ZZQ_EXAMPLE_SOURCE' }

interface Route { ok?: boolean; status?: number; body?: unknown }
let routes: Record<string, Route>
let fetchMock: ReturnType<typeof vi.fn>

const wrap = (ui: ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

const editor = () => screen.getByLabelText(i18nT('apps.workflows.workflowsPage.workflow_source')) as HTMLTextAreaElement
const runBtn = () => screen.getByRole('button', { name: new RegExp(i18nT('apps.workflows.workflowsPage.run')) })

beforeEach(() => {
  routes = {
    '/examples': { body: [EXAMPLE] },
    '/validate': { body: { ok: true, errors: [], meta: null } },
    '/run': { body: { ok: true, result: { said: 'zzq' }, error: null, events: [{ t: 'phase', name: 'Work' }] } },
  }
  fetchMock = vi.fn((url: string) => {
    const key = Object.keys(routes).find(k => url.endsWith(k))
    const r = key ? routes[key] : { body: {} }
    return Promise.resolve({
      ok: r.ok ?? true,
      status: r.status ?? 200,
      json: () => Promise.resolve(r.body ?? {}),
    })
  })
  vi.stubGlobal('fetch', fetchMock)
})
afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks() })

describe('WorkflowsPage', () => {
  it('opens on the author view with the starter script and the empty run panel', async () => {
    wrap(<WorkflowsPage />)
    expect(editor().value).toContain('async def workflow(ctx):')
    expect(screen.getByText(i18nT('apps.workflows.workflowsPage.run_a_workflow_to_see_its_phases_agents_and_resu'))).toBeInTheDocument()
    expect(screen.queryByTestId('zzq-tree-stub')).not.toBeInTheDocument()
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/apps/workflows/api/examples', { credentials: 'same-origin' }))
  })

  it('Validate posts the current editor source', async () => {
    wrap(<WorkflowsPage />)
    fireEvent.change(editor(), { target: { value: 'ZZQ_EDITED' } })
    fireEvent.click(screen.getByRole('button', { name: i18nT('apps.workflows.workflowsPage.validate') }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/apps/workflows/api/validate', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ source: 'ZZQ_EDITED' }),
    })))
  })

  it('lists validation errors and does NOT run an invalid script', async () => {
    routes['/validate'] = { body: { ok: false, errors: ['zzq missing META'], meta: null } }
    wrap(<WorkflowsPage />)
    fireEvent.click(runBtn())

    await screen.findByText('zzq missing META')
    expect(screen.getByText(i18nT('apps.workflows.workflowsPage.invalid_fix_before_running'))).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([u]) => String(u).endsWith('/run'))).toBe(false)
  })

  it('runs a valid script and hands the event stream to the run tree', async () => {
    wrap(<WorkflowsPage />)
    fireEvent.click(runBtn())

    await screen.findByTestId('zzq-tree-stub')
    expect(screen.getByText('events:1')).toBeInTheDocument()
    expect(screen.getByText('status:finished')).toBeInTheDocument()
    const runCall = fetchMock.mock.calls.find(([u]) => String(u).endsWith('/run'))!
    expect(JSON.parse((runCall[1] as RequestInit).body as string)).toMatchObject({ source: expect.stringContaining('async def workflow') })
  })

  it('marks the run failed when the backend reports ok:false', async () => {
    routes['/run'] = { body: { ok: false, result: null, error: 'zzq agent blew up', events: [{ t: 'phase' }] } }
    wrap(<WorkflowsPage />)
    fireEvent.click(runBtn())

    await screen.findByText('status:failed')
    expect(screen.getByText('error:zzq agent blew up')).toBeInTheDocument()
  })

  it('surfaces a transport failure as the request-failed banner', async () => {
    routes['/run'] = { ok: false, status: 500, body: {} }
    wrap(<WorkflowsPage />)
    fireEvent.click(runBtn())

    await screen.findByText(new RegExp(`POST /run`))
    expect(screen.getByText(new RegExp(i18nT('apps.workflows.workflowsPage.request_failed')))).toBeInTheDocument()
    expect(screen.queryByTestId('zzq-tree-stub')).not.toBeInTheDocument()
  })

  it('shows the budget badge once the stream carries one', async () => {
    routes['/run'] = {
      body: {
        ok: true, result: null, error: null,
        events: [
          { run_id: 'zzq', seq: 1, ts: 't', type: 'run_started', data: { budget_total: 10 } },
          { run_id: 'zzq', seq: 2, ts: 't', type: 'budget_update', data: { spent: 3 } },
        ],
      },
    }
    wrap(<WorkflowsPage />)
    fireEvent.click(runBtn())
    await screen.findByText(/3 \/ 10/)
  })

  it('loading an example copies its source into the editor', async () => {
    wrap(<WorkflowsPage />)
    const picker = await screen.findByLabelText(i18nT('apps.workflows.workflowsPage.load_example'))

    fireEvent.change(picker, { target: { value: 'zzq-example' } })
    expect(editor().value).toBe('ZZQ_EXAMPLE_SOURCE')

    // An unknown name is ignored rather than blanking the editor.
    fireEvent.change(picker, { target: { value: '' } })
    expect(editor().value).toBe('ZZQ_EXAMPLE_SOURCE')
  })

  it('switches to the Runs view', () => {
    wrap(<WorkflowsPage />)
    // SegmentedControl measures itself and collapses to a dropdown when it has
    // no width — which is what happy-dom reports — so the Runs segment lives
    // behind the trigger showing the active segment's label.
    if (!screen.queryByText('Runs')) fireEvent.click(screen.getByText('Author'))
    fireEvent.click(screen.getByText('Runs'))
    expect(screen.getByTestId('zzq-runs-stub')).toBeInTheDocument()
    expect(screen.queryByLabelText(i18nT('apps.workflows.workflowsPage.workflow_source'))).not.toBeInTheDocument()
  })
})
