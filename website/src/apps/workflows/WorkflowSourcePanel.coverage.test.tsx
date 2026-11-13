import { describe, it, expect, afterEach, vi } from 'vitest'
import { fireEvent, screen } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import WorkflowSourcePanel from './WorkflowSourcePanel'

const RUN = 'run 1'
const SOURCE = 'print(1)\nprint(2)'
const RERUN_URL = `/api/workflows/runs/${encodeURIComponent(RUN)}/rerun`

function stubRerun(respond: () => Promise<unknown>) {
  const fetchMock = vi.fn(() => respond())
  vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch)
  return fetchMock
}

function jsonResponse(status: number, body: unknown) {
  return Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) })
}

function openPanel() {
  fireEvent.click(screen.getByRole('button', { name: new RegExp(i18nT('apps.workflows.workflowSourcePanel.view_source')) }))
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('WorkflowSourcePanel', () => {
  it('keeps the source read-only and offers no edit while the source is still loading', () => {
    renderWithProviders(<WorkflowSourcePanel run_id={RUN} source={null} />)
    openPanel()
    expect(screen.queryByRole('textbox')).toBeNull()
    const edit = screen.getByRole('button', { name: new RegExp(i18nT('apps.workflows.workflowSourcePanel.edit')) })
    expect((edit as HTMLButtonElement).disabled).toBe(true)
  })

  it('says no source was captured when the snapshot has an empty source', () => {
    renderWithProviders(<WorkflowSourcePanel run_id={RUN} source="" />)
    openPanel()
    expect(screen.getByText(i18nT('apps.workflows.workflowSourcePanel.no_source_captured_for_this_run'))).toBeTruthy()
  })

  it('shows the source fetch error when the source could not be loaded', () => {
    renderWithProviders(<WorkflowSourcePanel run_id={RUN} source={null} sourceError="404 not found" />)
    openPanel()
    expect(screen.getByText('404 not found')).toBeTruthy()
  })

  it('edits a draft and reverts it on cancel', () => {
    renderWithProviders(<WorkflowSourcePanel run_id={RUN} source={SOURCE} />)
    openPanel()
    fireEvent.click(screen.getByRole('button', { name: new RegExp(i18nT('apps.workflows.workflowSourcePanel.edit')) }))
    const box = screen.getByRole('textbox') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'print(9)' } })
    expect(box.value).toBe('print(9)')

    fireEvent.click(screen.getByRole('button', { name: i18nT('apps.workflows.workflowSourcePanel.cancel') }))
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe(SOURCE)
    expect(screen.getByRole('textbox').hasAttribute('readonly')).toBe(true)

    // Edit again: the draft must have been dropped, so the editor opens on the saved source.
    fireEvent.click(screen.getByRole('button', { name: new RegExp(i18nT('apps.workflows.workflowSourcePanel.edit')) }))
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe(SOURCE)
  })

  it('posts the edited source and shows the new run id on success', async () => {
    const fetchMock = stubRerun(() => jsonResponse(200, { run_id: 'run-2' }))
    renderWithProviders(<WorkflowSourcePanel run_id={RUN} source={SOURCE} />)
    openPanel()
    fireEvent.click(screen.getByRole('button', { name: new RegExp(i18nT('apps.workflows.workflowSourcePanel.edit')) }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'print(3)' } })
    fireEvent.click(screen.getByRole('button', { name: new RegExp(i18nT('apps.workflows.workflowSourcePanel.rerun_with_edits')) }))

    expect(await screen.findByText(new RegExp(`run-2`))).toBeTruthy()
    expect(fetchMock).toHaveBeenCalledWith(RERUN_URL, expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ source: 'print(3)' }),
    }))
    expect(screen.getByRole('textbox').hasAttribute('readonly')).toBe(true)
  })

  it('lists the validation errors from a 400 rerun', async () => {
    stubRerun(() => jsonResponse(400, { error: 'bad', errors: ['line 2: syntax', 'line 5: unknown ctx'] }))
    renderWithProviders(<WorkflowSourcePanel run_id={RUN} source={SOURCE} />)
    openPanel()
    fireEvent.click(screen.getByRole('button', { name: new RegExp(i18nT('apps.workflows.workflowSourcePanel.edit')) }))
    fireEvent.click(screen.getByRole('button', { name: new RegExp(i18nT('apps.workflows.workflowSourcePanel.rerun_with_edits')) }))
    expect(await screen.findByText(/line 2: syntax/)).toBeTruthy()
    expect(screen.getByText(/line 5: unknown ctx/)).toBeTruthy()
  })

  it('reports the status when a rerun fails without validation errors', async () => {
    stubRerun(() => jsonResponse(500, {}))
    renderWithProviders(<WorkflowSourcePanel run_id={RUN} source={SOURCE} />)
    openPanel()
    fireEvent.click(screen.getByRole('button', { name: new RegExp(i18nT('apps.workflows.workflowSourcePanel.edit')) }))
    fireEvent.click(screen.getByRole('button', { name: new RegExp(i18nT('apps.workflows.workflowSourcePanel.rerun_with_edits')) }))
    expect(await screen.findByText(i18nT('apps.workflows.workflowSourcePanel.rerun_failed', { status: 500 }))).toBeTruthy()
  })

  it('shows the network error message when the request itself fails', async () => {
    stubRerun(() => Promise.reject(new Error('socket hang up')))
    renderWithProviders(<WorkflowSourcePanel run_id={RUN} source={SOURCE} />)
    openPanel()
    fireEvent.click(screen.getByRole('button', { name: new RegExp(i18nT('apps.workflows.workflowSourcePanel.edit')) }))
    fireEvent.click(screen.getByRole('button', { name: new RegExp(i18nT('apps.workflows.workflowSourcePanel.rerun_with_edits')) }))
    expect(await screen.findByText('socket hang up')).toBeTruthy()
  })
})
