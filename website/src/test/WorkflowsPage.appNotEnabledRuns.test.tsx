/**
 * WorkflowsPage when the example fetch is refused while the Runs view is open.
 *
 * The example fetch is asked as the page opens, and an answer can land after the author has
 * moved on. While the app is off its refusal replaces the Author view with the disabled-app
 * notice, but only while the editor holds nothing the author wrote, and the page keeps its
 * header and view switch. The Runs view reads the gateway's own run registry rather than the
 * app's proxied API, and keeps the name, slug and description typed into its "Save to library"
 * form in its own state, which a refusal that lands while it is open must not unmount. These
 * cases use the real Runs view; the other app-off cases stub it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { i18nT } from '../i18n/t'
import { isAppNotEnabledError } from '../api/apiError'
import { __resetErrorJournalForTests } from '../utils/errorReport'
import WorkflowsPage from '../apps/workflows/WorkflowsPage'

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
const refusedAppOff = () =>
  json({ code: 'app_not_enabled', error: "app 'workflows' is not enabled" }, 403)

// A finished run that can be saved to the library.
const RUN = {
  run_id: 'wf_zzq',
  name: 'Zzq run',
  status: 'finished',
  result: null,
  error: null,
  author: 'zzq',
  session_key: null,
  event_count: 1,
  source: 'ZZQ_SOURCE',
  source_format: 'python',
  driver: 'workflow',
  capabilities: ['save'],
  events: [{ run_id: 'wf_zzq', seq: 1, ts: '2026-10-12T00:00:00Z', type: 'run_started', data: { name: 'Zzq run' } }],
}
const TYPED = { name: 'ZZQ typed name', slug: 'zzq-typed-slug', description: 'ZZQ typed description' }

// Each refusal reaches the page through a fetch stub, a query and a re-render, in that order.
// The 1,000 ms default is a bet on runner speed; this only bounds a lost run.
const CHAIN = { timeout: 5000 }

let settleExamples: (answer: Response) => void

beforeEach(() => {
  // The example fetch stays open until a case settles it, as a slow answer does.
  const examples = new Promise<Response>(resolve => { settleExamples = resolve })
  vi.stubGlobal('fetch', vi.fn((url: string) => {
    if (url.endsWith('/examples')) return examples
    if (url === '/api/workflows/runs') return Promise.resolve(json({ runs: [RUN] }))
    if (url === `/api/workflows/runs/${RUN.run_id}`) return Promise.resolve(json(RUN))
    throw new Error(`Unexpected request: ${url}`)
  }))
  __resetErrorJournalForTests()
  sessionStorage.clear()
})
afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks() })

const mount = () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <MemoryRouter>
      <QueryClientProvider client={qc}><WorkflowsPage /></QueryClientProvider>
    </MemoryRouter>,
  )
  return qc
}

const disabledText = () => i18nT('components.appHost.this_app_is_disabled')
const editor = () => screen.queryByLabelText(i18nT('apps.workflows.workflowsPage.workflow_source'))
const nameField = () => screen.getByLabelText(i18nT('pages.overview.workflowLibrary.name'))
const slugField = () => screen.getByLabelText(i18nT('pages.overview.workflowLibrary.slug'))
const descriptionField = () => screen.getByLabelText(i18nT('pages.overview.workflowLibrary.workflow_description'))

/** The view switch collapses to a dropdown where it has no width, as in happy-dom: open it, then pick. */
const chooseView = async (label: string) => {
  const trigger = screen.queryByRole('button', { expanded: false })
  if (trigger) fireEvent.click(trigger)
  const radios = await screen.findAllByRole('radio', { name: label }, CHAIN)
  fireEvent.click(radios[radios.length - 1])
}

/** Open the finished run's Save form on the Runs view and type into all three of its fields. */
const typeIntoSaveForm = async () => {
  fireEvent.click(await screen.findByText(RUN.name, {}, CHAIN))
  fireEvent.click(await screen.findByRole('button', { name: i18nT('pages.chat.workflowRunCard.save_workflow') }, CHAIN))
  fireEvent.change(nameField(), { target: { value: TYPED.name } })
  fireEvent.change(slugField(), { target: { value: TYPED.slug } })
  fireEvent.change(descriptionField(), { target: { value: TYPED.description } })
}

/** Answer the held example fetch with the refusal, and let the page render what it now knows. */
const refuseExamples = async (qc: QueryClient) => {
  settleExamples(refusedAppOff())
  // Chain: the refused fetch -> the query's error state.
  await waitFor(
    () => expect(qc.getQueryCache().getAll().some(q => isAppNotEnabledError(q.state.error))).toBe(true),
    CHAIN,
  )
  // The query library tells its observers on the next task; a render that is going to replace the
  // page has happened by the time this one has run.
  await act(async () => { await new Promise<void>(resolve => setTimeout(resolve, 0)) })
}

describe('WorkflowsPage while the Runs view is open and the app is off', () => {
  it('keeps the Runs view and what was typed into its Save form when the example fetch is refused', async () => {
    const qc = mount()
    await chooseView('Runs')
    await typeIntoSaveForm()

    await refuseExamples(qc)

    expect(nameField()).toHaveValue(TYPED.name)
    expect(slugField()).toHaveValue(TYPED.slug)
    expect(descriptionField()).toHaveValue(TYPED.description)
    expect(screen.queryByText(disabledText())).not.toBeInTheDocument()
  })

  it('shows the disabled-app notice in the Author slot when the author comes back to an untouched Author view', async () => {
    const qc = mount()
    await chooseView('Runs')
    expect(await screen.findByText(RUN.name, {}, CHAIN)).toBeInTheDocument()

    await refuseExamples(qc)

    expect(screen.getByText(RUN.name)).toBeInTheDocument()
    expect(screen.queryByText(disabledText())).not.toBeInTheDocument()

    await chooseView('Author')

    // The notice is read from the page's state on every render, so returning to the view it guards shows it.
    expect(await screen.findByText(disabledText(), {}, CHAIN)).toBeInTheDocument()
    expect(editor()).not.toBeInTheDocument()
    // It sits under the page's own header, and the switch is still on the page: Runs can be chosen again.
    expect(screen.getByTestId('page-title')).toHaveTextContent(i18nT('apps.workflows.workflowsPage.workflows'))
    await chooseView('Runs')
    expect(await screen.findByText(RUN.name, {}, CHAIN)).toBeInTheDocument()
    expect(screen.queryByText(disabledText())).not.toBeInTheDocument()
  })
})
