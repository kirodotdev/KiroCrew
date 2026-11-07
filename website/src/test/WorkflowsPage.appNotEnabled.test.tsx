/**
 * WorkflowsPage while the gateway refuses its backend because the app is off.
 *
 * The app reverse proxy answers every `/apps/workflows/api/*` call with a 403
 * whose body carries `code: "app_not_enabled"`. The page used to print
 * `POST /validate → 403`; it now shows the disabled-app notice with a way to
 * Apps, and keeps the editor and what was typed in it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { i18nT } from '../i18n/t'

vi.mock('../apps/workflows/WorkflowsRuns', () => ({
  default: () => <div data-testid="zzq-runs-stub" />,
}))
vi.mock('../apps/workflows/WorkflowRunTree', () => ({
  default: () => <div data-testid="zzq-tree-stub" />,
}))

import WorkflowsPage from '../apps/workflows/WorkflowsPage'

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
const refusedAppOff = () => json({ code: 'app_not_enabled', error: "app 'workflows' is not enabled" }, 403)

let answers: Record<string, () => Response>

/** Where the router is, so the Apps button's navigation can be read off the page. */
const Where = () => <div data-testid="zzq-where">{useLocation().pathname}</div>

const wrap = () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <MemoryRouter initialEntries={['/workflows']}>
      <Where />
      <QueryClientProvider client={qc}><WorkflowsPage /></QueryClientProvider>
    </MemoryRouter>,
  )
}

const disabledText = () => i18nT('components.appHost.this_app_is_disabled')
const editor = () => screen.getByLabelText(i18nT('apps.workflows.workflowsPage.workflow_source'))
const validateBtn = () => screen.getByRole('button', { name: i18nT('apps.workflows.workflowsPage.validate') })

beforeEach(() => {
  answers = {
    '/examples': () => json([]),
    '/validate': () => json({ ok: true, errors: [], meta: null }),
    '/run': () => json({ ok: true, result: null, error: null, events: [] }),
  }
  vi.stubGlobal('fetch', vi.fn((url: string) => {
    const key = Object.keys(answers).find(k => url.endsWith(k))!
    return Promise.resolve(answers[key]())
  }))
})
afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks() })

describe('WorkflowsPage while the app is off', () => {
  it('shows the disabled-app notice instead of the refused status, and keeps the draft', async () => {
    answers['/validate'] = refusedAppOff
    wrap()
    fireEvent.change(editor(), { target: { value: 'ZZQ_DRAFT' } })
    fireEvent.click(validateBtn())

    // Chain: the Validate click -> the refused fetch -> the mutation's error -> the notice.
    expect(await screen.findByText(disabledText())).toBeInTheDocument()
    expect(screen.getByText(i18nT('components.appHost.enable_this_app_from_apps_to_use_it'))).toBeInTheDocument()
    expect(screen.queryByText(/403/)).not.toBeInTheDocument()
    expect(screen.queryByText(i18nT('apps.workflows.workflowsPage.couldn_t_validate_the_script'))).not.toBeInTheDocument()
    expect(editor()).toHaveValue('ZZQ_DRAFT')

    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.appHost.apps') }))
    // The app's own detail page (Workflows is a hidden builtin, so the Apps grid and
    // Library do not list it), in a new tab: this page and its unsaved draft stay.
    expect(open).toHaveBeenCalledWith(expect.stringMatching(/\/apps\/detail\/workflows$/), '_blank', 'noopener')
    expect(screen.getByTestId('zzq-where')).toHaveTextContent(/^\/workflows$/)
    expect(editor()).toHaveValue('ZZQ_DRAFT')
    open.mockRestore()
  })

  it('shows the notice from the refused examples load, and drops it once Validate is answered', async () => {
    answers['/examples'] = refusedAppOff
    wrap()

    // Chain: the examples fetch on mount -> refused -> the query's error -> the notice.
    expect(await screen.findByText(disabledText())).toBeInTheDocument()

    // The app was switched on elsewhere: Validate is answered, so the page-load refusal is stale.
    fireEvent.click(validateBtn())
    await waitFor(() => expect(screen.queryByText(disabledText())).not.toBeInTheDocument())
    expect(editor()).toBeInTheDocument()
  })

  it('drops a refused Run notice once a later Validate is answered', async () => {
    answers['/run'] = refusedAppOff
    wrap()
    fireEvent.click(screen.getByRole('button', { name: i18nT('apps.workflows.workflowsPage.run') }))

    // Chain: Run validates (answered), then /run is refused -> the run mutation's error -> the notice.
    expect(await screen.findByText(disabledText())).toBeInTheDocument()

    // The app was switched back on: a standalone Validate is answered, so the Run refusal is stale.
    fireEvent.click(validateBtn())
    await waitFor(() => expect(screen.queryByText(disabledText())).not.toBeInTheDocument())
  })

  it('drops a refused Run notice when a later Validate fails another way', async () => {
    answers['/run'] = refusedAppOff
    wrap()
    fireEvent.click(screen.getByRole('button', { name: i18nT('apps.workflows.workflowsPage.run') }))
    expect(await screen.findByText(disabledText())).toBeInTheDocument()

    // The app is on again but its backend is down: the newer answer is the 502, not "disabled".
    answers['/validate'] = () => json({ error: "app 'workflows' has no reachable backend" }, 502)
    fireEvent.click(validateBtn())
    expect(await screen.findByText("app 'workflows' has no reachable backend")).toBeInTheDocument()
    expect(screen.queryByText(disabledText())).not.toBeInTheDocument()
  })

  it('shows an examples load failure of another kind as its own notice', async () => {
    answers['/examples'] = () => json({ error: "app 'workflows' has no reachable backend" }, 502)
    wrap()

    // Chain: the examples fetch on mount -> the 502 -> the query's error -> its own notice.
    expect(await screen.findByText("app 'workflows' has no reachable backend")).toBeInTheDocument()
    expect(screen.getByText(i18nT('apps.workflows.workflowsPage.couldn_t_load_examples'))).toBeInTheDocument()
    expect(screen.queryByText(disabledText())).not.toBeInTheDocument()
  })

  it('shows a later Validate failure of another kind in place of the page-load notice', async () => {
    answers['/examples'] = refusedAppOff
    answers['/validate'] = () => json({ error: "app 'workflows' has no reachable backend" }, 502)
    wrap()
    expect(await screen.findByText(disabledText())).toBeInTheDocument()

    fireEvent.click(validateBtn())

    // Chain: the Validate click -> the 502 -> the mutation's error -> its own notice.
    expect(await screen.findByText("app 'workflows' has no reachable backend")).toBeInTheDocument()
    expect(screen.queryByText(disabledText())).not.toBeInTheDocument()
  })
})
