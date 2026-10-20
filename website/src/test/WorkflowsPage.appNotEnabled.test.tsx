/**
 * WorkflowsPage when the gateway refuses its backend because the app is off.
 *
 * The app reverse proxy answers every `/apps/workflows/api/*` call with a 403
 * whose body carries `code: "app_not_enabled"`. The page used to throw
 * `POST /validate → 403` from the status alone and print that. These cases pin
 * that the page reads the code instead, from whichever of its three calls meets
 * the refusal first, and that a 403 which is not that refusal still reads as a
 * failure rather than as an app to enable.
 *
 * What the page does with the refusal depends on what the author could lose. The
 * example fetch is asked as the page opens and is not retried, so until the author
 * has focused the editor or changed the starter script it replaces the Author view
 * with the disabled-app notice, whose refusal is an `ErrorNotice` that hands off to
 * the agent. The page keeps its header and its Author/Runs switch, so the Runs view
 * stays reachable while the app is off. Validate and Run answer over a script that
 * lives only in the editor, so their refusal is a notice beside it and the editor
 * keeps what was typed.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { i18nT } from '../i18n/t'
import { newQueryClient } from '../api/queryClient'
import {
  consumeChatHandoff, installSoftNavigate, __resetErrorJournalForTests, __resetNavSeamForTests,
} from '../utils/errorReport'

vi.mock('../apps/workflows/WorkflowsRuns', () => ({
  default: () => <div data-testid="zzq-runs-stub" />,
}))
vi.mock('../apps/workflows/WorkflowRunTree', () => ({
  default: () => <div data-testid="zzq-tree-stub" />,
}))

import WorkflowsPage from '../apps/workflows/WorkflowsPage'

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
const refusedAppOff = () =>
  json({ code: 'app_not_enabled', error: "app 'workflows' is not enabled" }, 403)
const oneExample = () => json([{ name: 'zzq-example', description: '', source: 'ZZQ_EXAMPLE' }])
/** A Run the backend answers: one event, so the page shows the run view (a stub here). */
const ranOk = () => json({
  ok: true,
  result: null,
  error: null,
  events: [{ run_id: 'zzq', seq: 1, ts: '2026-01-01T00:00:00Z', type: 'phase', data: { name: 'Work' } }],
})

let answers: Record<string, () => Response | Promise<Response>>
let fetchMock: ReturnType<typeof vi.fn>
const navigated: string[] = []

// Each refusal reaches the page through a fetch stub, a query or a mutation, and a
// re-render, in that order. The 1,000 ms default is a bet on runner speed; this only
// bounds a lost run.
const CHAIN = { timeout: 5000 }

const testClient = () =>
  new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

/** Where the router is, so a click that navigates can be read off the page. */
const Where = () => <div data-testid="zzq-where">{useLocation().pathname}</div>

const wrap = (qc: QueryClient = testClient()) =>
  render(
    <MemoryRouter>
      <Where />
      <QueryClientProvider client={qc}><WorkflowsPage /></QueryClientProvider>
    </MemoryRouter>,
  )

const disabledText = () => i18nT('components.appHost.this_app_is_disabled')
const enableText = () => i18nT('components.appHost.enable_this_app_from_apps_to_use_it')
const examplesFailedTitle = () => i18nT('apps.workflows.workflowsPage.couldn_t_load_the_example_workflows')
const validateBtn = () => screen.findByRole('button', { name: i18nT('apps.workflows.workflowsPage.validate') })
const runBtn = () => screen.findByRole('button', { name: new RegExp(i18nT('apps.workflows.workflowsPage.run')) })
const editor = () => screen.queryByLabelText(i18nT('apps.workflows.workflowsPage.workflow_source'))
const loadExample = () => screen.queryByLabelText(i18nT('apps.workflows.workflowsPage.load_example'))
const appsButton = () => screen.getByRole('button', { name: new RegExp(i18nT('components.appHost.apps')) })
const examplesCalls = () => fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/examples')).length
/** The view switch collapses to a dropdown where it has no width, as in happy-dom: open it, then pick. */
const chooseView = async (label: string) => {
  const trigger = screen.queryByRole('button', { expanded: false })
  if (trigger) fireEvent.click(trigger)
  const radios = await screen.findAllByRole('radio', { name: label }, CHAIN)
  fireEvent.click(radios[radios.length - 1])
}
/** Put a script in the editor, as an author who has started writing one. */
const typeDraft = async (text: string) =>
  fireEvent.change(await screen.findByLabelText(i18nT('apps.workflows.workflowsPage.workflow_source')), { target: { value: text } })
/** An example fetch held open until the test settles it. */
const heldExamples = () => {
  let settle!: (answer: Response) => void
  const pending = new Promise<Response>(resolve => { settle = resolve })
  answers['/examples'] = () => pending
  return (answer: Response) => settle(answer)
}

beforeEach(() => {
  answers = {
    '/examples': () => json([]),
    '/validate': () => json({ ok: true, errors: [], meta: null }),
    '/run': () => json({ ok: true, result: null, error: null, events: [] }),
  }
  fetchMock = vi.fn((url: string) => {
    const key = Object.keys(answers).find(k => url.endsWith(k))!
    return Promise.resolve(answers[key]())
  })
  vi.stubGlobal('fetch', fetchMock)
  __resetErrorJournalForTests()
  __resetNavSeamForTests()
  navigated.length = 0
  sessionStorage.clear()
  installSoftNavigate((to) => { navigated.push(to) })
})
afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks() })

describe('WorkflowsPage while the app is off', () => {
  it('replaces the Author view with the disabled-app notice when the example fetch on mount is refused', async () => {
    answers['/examples'] = refusedAppOff
    wrap()

    expect(await screen.findByText(disabledText())).toBeInTheDocument()
    expect(screen.getByText(enableText())).toBeInTheDocument()
    expect(editor()).not.toBeInTheDocument()
    // The page keeps its own header, rather than the disabled-app page's title and subtitle.
    expect(screen.getByTestId('page-title')).toHaveTextContent(i18nT('apps.workflows.workflowsPage.workflows'))
    expect(screen.getByTestId('page-subtitle')).toHaveTextContent(
      i18nT('apps.workflows.workflowsPage.author_run_and_watch_dynamic_workflows'),
    )
  })

  it('keeps the view switch, so the Runs view is reachable while the Author view shows the notice', async () => {
    answers['/examples'] = refusedAppOff
    wrap()
    expect(await screen.findByText(disabledText())).toBeInTheDocument()

    // The switch is collapsed to its one toggle where the page has no width, as in happy-dom.
    expect(screen.getByRole('button', { expanded: false })).toBeInTheDocument()
    await chooseView('Runs')

    expect(await screen.findByTestId('zzq-runs-stub', {}, CHAIN)).toBeInTheDocument()
    expect(screen.queryByText(disabledText())).not.toBeInTheDocument()

    await chooseView('Author')

    // The notice is read from the page's state on every render, so the Author view shows it again.
    expect(await screen.findByText(disabledText(), {}, CHAIN)).toBeInTheDocument()
    expect(screen.queryByTestId('zzq-runs-stub')).not.toBeInTheDocument()
    expect(editor()).not.toBeInTheDocument()
  })

  it('shows that refusal through ErrorNotice, whose hand-off carries the refused call', async () => {
    answers['/examples'] = refusedAppOff
    wrap()

    const notice = await screen.findByRole('alert', {}, CHAIN)
    expect(notice).toHaveTextContent(enableText())
    fireEvent.click(within(notice).getByRole('button', { name: new RegExp(i18nT('components.askAgent.ask_the_agent'), 'i') }))

    expect(navigated).toEqual(['/chat'])
    const staged = consumeChatHandoff() ?? ''
    expect(staged).toContain('/apps/workflows/api/examples')
    expect(staged).toContain('HTTP 403')
    expect(staged).toContain('app_not_enabled')
  })

  it('does not retry the refusal, so the Author view gives way on the first answer', async () => {
    answers['/examples'] = refusedAppOff
    // The dashboard's own client: its retry policy retries a failed read once, a second later.
    wrap(newQueryClient())

    expect(await screen.findByText(disabledText(), {}, CHAIN)).toBeInTheDocument()
    expect(examplesCalls()).toBe(1)
  })

  it('keeps the editor once the author has clicked into it, even before typing', async () => {
    const settle = heldExamples()
    wrap()
    fireEvent.focus(await screen.findByLabelText(i18nT('apps.workflows.workflowsPage.workflow_source')))
    settle(refusedAppOff())

    // Chain: the refused example fetch -> the query's error state -> the notice.
    expect(await screen.findByText(disabledText(), {}, CHAIN)).toBeInTheDocument()
    expect(appsButton()).toBeInTheDocument()
    expect(editor()).toBeInTheDocument()
  })

  it('keeps the editor and what was typed when Validate is the first call refused', async () => {
    answers['/validate'] = refusedAppOff
    wrap()
    await typeDraft('ZZQ_DRAFT')
    fireEvent.click(await validateBtn())

    // Chain: the Validate click -> the validate mutation -> the refused fetch -> the notice.
    expect(await screen.findByText(disabledText(), {}, CHAIN)).toBeInTheDocument()
    expect(screen.getByText(enableText())).toBeInTheDocument()
    expect(appsButton()).toBeInTheDocument()
    expect(editor()).toHaveValue('ZZQ_DRAFT')
    expect(screen.queryByText(/403/)).not.toBeInTheDocument()
    expect(screen.queryByText(/POST \/validate/)).not.toBeInTheDocument()
    expect(screen.queryByText(i18nT('apps.workflows.workflowsPage.couldn_t_validate_the_script'))).not.toBeInTheDocument()
  })

  it('keeps the editor and what was typed when Run is refused after a clean validation', async () => {
    answers['/run'] = refusedAppOff
    wrap()
    await typeDraft('ZZQ_DRAFT')
    fireEvent.click(await runBtn())

    // Chain: the Run click -> the validate mutation -> the run mutation -> the refused fetch -> the notice.
    expect(await screen.findByText(disabledText(), {}, CHAIN)).toBeInTheDocument()
    expect(appsButton()).toBeInTheDocument()
    expect(editor()).toHaveValue('ZZQ_DRAFT')
    expect(screen.queryByText(/POST \/run/)).not.toBeInTheDocument()
    expect(screen.queryByText(i18nT('apps.workflows.workflowsPage.couldn_t_start_the_run'))).not.toBeInTheDocument()
  })

  it('keeps the editor when the example fetch is refused after the author has started typing', async () => {
    const settle = heldExamples()
    wrap()
    await typeDraft('ZZQ_DRAFT')
    settle(refusedAppOff())

    // Chain: the refused example fetch -> the query's error state -> the notice.
    expect(await screen.findByText(disabledText(), {}, CHAIN)).toBeInTheDocument()
    expect(appsButton()).toBeInTheDocument()
    expect(editor()).toHaveValue('ZZQ_DRAFT')
    // The refusal is the disabled-app notice alone, not also an example-load failure.
    expect(screen.queryByText(examplesFailedTitle())).not.toBeInTheDocument()
  })

  it('lets the author go again once the app is switched back on', async () => {
    answers['/validate'] = refusedAppOff
    wrap()
    fireEvent.click(await validateBtn())
    // Chain: the Validate click -> the validate mutation -> the refused fetch -> the notice.
    expect(await screen.findByText(disabledText(), {}, CHAIN)).toBeInTheDocument()

    answers['/validate'] = () => json({ ok: true, errors: [], meta: null })
    fireEvent.click(await validateBtn())

    // Chain: the second Validate click -> the validate mutation starts again and drops its error.
    await waitFor(() => expect(screen.queryByText(disabledText())).not.toBeInTheDocument(), CHAIN)
    expect(editor()).toBeInTheDocument()
  })

  it('takes a held Run refusal down once Validate is answered', async () => {
    answers['/run'] = refusedAppOff
    wrap()
    await typeDraft('ZZQ_DRAFT')
    fireEvent.click(await runBtn())
    // Chain: the Run click -> the validate mutation -> the run mutation -> the refused fetch -> the notice.
    expect(await screen.findByText(disabledText(), {}, CHAIN)).toBeInTheDocument()

    // The app is back on, and the author checks the script rather than running it.
    fireEvent.click(await validateBtn())

    // Chain: the answered Validate -> the held Run refusal is reset -> no notice.
    await waitFor(() => expect(screen.queryByText(disabledText())).not.toBeInTheDocument(), CHAIN)
    expect(editor()).toHaveValue('ZZQ_DRAFT')
  })

  it('shows the run when Run is clicked again after a refusal', async () => {
    answers['/run'] = refusedAppOff
    wrap()
    fireEvent.click(await runBtn())
    // Chain: the Run click -> the validate mutation -> the run mutation -> the refused fetch -> the notice.
    expect(await screen.findByText(disabledText(), {}, CHAIN)).toBeInTheDocument()

    // The app is back on. This Run answers, and what it answers must stay on screen: the Run
    // that is answered is not a held refusal to take down.
    answers['/run'] = ranOk
    fireEvent.click(await runBtn())

    // Chain: the second Run click -> the validate mutation -> the run mutation -> its events.
    expect(await screen.findByTestId('zzq-tree-stub', {}, CHAIN)).toBeInTheDocument()
    expect(screen.queryByText(disabledText())).not.toBeInTheDocument()
  })

  it('leads to Apps through the same control beside the editor as in place of the Author view', async () => {
    answers['/examples'] = refusedAppOff
    const inPlace = wrap()
    expect(await screen.findByText(disabledText(), {}, CHAIN)).toBeInTheDocument()
    const replacing = appsButton().outerHTML
    inPlace.unmount()

    answers['/examples'] = () => json([])
    answers['/validate'] = refusedAppOff
    wrap()
    fireEvent.click(await validateBtn())
    expect(await screen.findByText(disabledText(), {}, CHAIN)).toBeInTheDocument()

    // One shape and one label wherever the notice shows, and it goes to the Apps page.
    expect(appsButton().outerHTML).toBe(replacing)
    expect(screen.getByTestId('zzq-where')).toHaveTextContent(/^\/$/)
    fireEvent.click(appsButton())
    expect(screen.getByTestId('zzq-where')).toHaveTextContent(/^\/apps$/)
  })

  it('takes a held example refusal down once Validate is answered, and loads the examples', async () => {
    const settle = heldExamples()
    wrap()
    await typeDraft('ZZQ_DRAFT')
    settle(refusedAppOff())
    expect(await screen.findByText(disabledText(), {}, CHAIN)).toBeInTheDocument()

    answers['/examples'] = oneExample
    fireEvent.click(await validateBtn())

    // Chain: the answered Validate -> the example fetch asked again -> its data -> no notice.
    await waitFor(() => expect(loadExample()).toBeInTheDocument(), CHAIN)
    expect(screen.queryByText(disabledText())).not.toBeInTheDocument()
    expect(editor()).toHaveValue('ZZQ_DRAFT')
  })

  it('asks for the examples again once per answer, so a refused re-ask is not repeated', async () => {
    const settle = heldExamples()
    wrap()
    await typeDraft('ZZQ_DRAFT')
    settle(refusedAppOff())
    expect(await screen.findByText(disabledText(), {}, CHAIN)).toBeInTheDocument()

    // Validate is answered, and the app is off again by the time the examples are asked for.
    // The re-ask is held open, as a network round trip is, so the page renders it in flight.
    const settleReask = heldExamples()
    fireEvent.click(await validateBtn())

    // Chain: the answered Validate -> one more example fetch -> refused -> the notice again.
    await waitFor(() => expect(examplesCalls()).toBe(2), CHAIN)
    settleReask(refusedAppOff())
    expect(await screen.findByText(disabledText(), {}, CHAIN)).toBeInTheDocument()
    await act(async () => {})
    expect(examplesCalls()).toBe(2)
  })

  it('does not show the disabled-app notice again on a return visit after the app is back on', async () => {
    answers['/examples'] = refusedAppOff
    const qc = newQueryClient()
    const first = wrap(qc)
    expect(await screen.findByText(disabledText(), {}, CHAIN)).toBeInTheDocument()
    first.unmount()

    answers['/examples'] = oneExample
    wrap(qc)

    // The cached refusal is not what the return visit renders: the query asks again as it mounts.
    expect(screen.queryByText(disabledText())).not.toBeInTheDocument()
    expect(editor()).toBeInTheDocument()
    await waitFor(() => expect(loadExample()).toBeInTheDocument(), CHAIN)
    expect(screen.queryByText(disabledText())).not.toBeInTheDocument()
  })

  it('keeps the page and prints the server sentence for a 403 that is not the refusal', async () => {
    answers['/validate'] = () => json({ code: 'dashboard_owner_required', error: 'only the owner may do this' }, 403)
    wrap()
    fireEvent.click(await validateBtn())

    expect(await screen.findByText('only the owner may do this')).toBeInTheDocument()
    expect(screen.getByText(i18nT('apps.workflows.workflowsPage.couldn_t_validate_the_script'))).toBeInTheDocument()
    expect(editor()).toBeInTheDocument()
    expect(screen.queryByText(disabledText())).not.toBeInTheDocument()
  })

  it('reads the code and not the sentence: the same words without it keep the page', async () => {
    answers['/validate'] = () => json({ error: "app 'workflows' is not enabled" }, 403)
    wrap()
    fireEvent.click(await validateBtn())

    expect(await screen.findByText("app 'workflows' is not enabled")).toBeInTheDocument()
    expect(editor()).toBeInTheDocument()
    expect(screen.queryByText(disabledText())).not.toBeInTheDocument()
  })

  it('says which load failed when the example fetch fails for another reason, and keeps the page', async () => {
    answers['/examples'] = () => json({ error: "app 'workflows' has no reachable backend" }, 502)
    wrap()

    // Chain: the failed example fetch, asked once more after the retry delay -> the query's
    // error state -> the notice.
    expect(await screen.findByText("app 'workflows' has no reachable backend", {}, CHAIN)).toBeInTheDocument()
    expect(screen.getByText(examplesFailedTitle())).toBeInTheDocument()
    expect(editor()).toBeInTheDocument()
    expect(screen.queryByText(disabledText())).not.toBeInTheDocument()
  })
})
