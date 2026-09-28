import { screen, waitFor, within, fireEvent } from '@testing-library/react'
import EmbeddingModelCard from '../pages/overview/EmbeddingModelCard'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

// The Apply button's LABEL follows the path it will submit, its DISABLED state
// follows the guards, and the two are independent: a reapply of the configured
// file is a real action (rebuild the vectors) that stays enabled, while the same
// label is disabled under a path error. The confirm modal borrows the card's own
// neutral title for a reapply, because the configured path is unchanged (the
// file behind it may have been replaced; the rebuild is the point).

vi.mock('../api/client', () => ({
  api: {
    vectorEmbeddingStatus: vi.fn(),
    vectorValidateEmbedModel: vi.fn(),
    vectorApplyEmbedModel: vi.fn(),
  },
}))

const CUSTOM_OK = {
  provider: 'llama_cpp', setup_step: 'done', model_available: true,
  model_source: 'custom', model_path: '/models/model.gguf', model_id: 'custom-model', model_dim: 2, model_active: true,
  reembed: { step: 'idle' },
}

function setStatus(status: Record<string, unknown>) {
  vi.mocked(api.vectorEmbeddingStatus).mockResolvedValue(status as never)
  vi.mocked(api.vectorValidateEmbedModel).mockResolvedValue({ ok: true, size_bytes: 2 * 1024 * 1024 } as never)
  vi.mocked(api.vectorApplyEmbedModel).mockResolvedValue({ ok: true } as never)
}

function applyButton() {
  return within(screen.getByTestId('embed-model-card')).getByRole('button', { name: /^(Apply model|Rebuild memory vectors|Applying…)$/ })
}

describe('EmbeddingModelCard Apply label follows the path it will submit', () => {
  it('reads "Rebuild memory vectors" for the configured path and stays enabled', async () => {
    setStatus(CUSTOM_OK)
    renderWithProviders(<EmbeddingModelCard />)
    await screen.findByDisplayValue('/models/model.gguf')
    const button = applyButton()
    expect(button).toHaveTextContent('Rebuild memory vectors')
    expect(button).toBeEnabled()
    expect(button).not.toHaveClass('bg-accent')
    expect(within(screen.getByTestId('embed-model-card')).queryByRole('button', { name: 'Apply model' })).not.toBeInTheDocument()
  })

  it('reads "Rebuild memory vectors" for an empty field while the bundled model is configured', async () => {
    setStatus({ ...CUSTOM_OK, model_source: 'default', model_path: '', model_id: 'bundled', model_dim: 384 })
    renderWithProviders(<EmbeddingModelCard />)
    await waitFor(() => expect(applyButton()).toBeEnabled())
    expect(applyButton()).toHaveTextContent('Rebuild memory vectors')
  })

  it('reads "Apply model" once the path differs, and flips back when the edit is restored', async () => {
    setStatus(CUSTOM_OK)
    renderWithProviders(<EmbeddingModelCard />)
    const input = await screen.findByDisplayValue('/models/model.gguf')
    fireEvent.change(input, { target: { value: '/models/other.gguf' } })
    expect(applyButton()).toHaveTextContent('Apply model')
    expect(applyButton()).toHaveClass('bg-accent')
    // Edit-then-restore: the field is still "touched", but it submits the
    // configured path again, so it is a reapply of that path, not a path change.
    fireEvent.change(input, { target: { value: '/models/model.gguf' } })
    expect(applyButton()).toHaveTextContent('Rebuild memory vectors')
    // Surrounding whitespace does not make it a different path.
    fireEvent.change(input, { target: { value: '  /models/model.gguf ' } })
    expect(applyButton()).toHaveTextContent('Rebuild memory vectors')
    // An emptied field on a custom install reverts to the bundled model: a change.
    fireEvent.change(input, { target: { value: '' } })
    expect(applyButton()).toHaveTextContent('Apply model')
  })

  it('keeps the disabled guards regardless of the label', async () => {
    const error = 'The model path points at a file that does not exist'
    setStatus({
      ...CUSTOM_OK, model_active: false, setup_error: error, setup_error_code: 'model_path_not_found',
      setup_error_params: { path: '/models/model.gguf', error },
    })
    renderWithProviders(<EmbeddingModelCard />)
    await screen.findByTestId('embed-model-path-status-error')
    // Same path, so the label is the reapply one, and the path error still disables it.
    expect(applyButton()).toHaveTextContent('Rebuild memory vectors')
    expect(applyButton()).toBeDisabled()
    // A failed live check on a different path disables "Apply model" the same
    // way. The validate call is held open so the two phases are observed
    // separately: disabled while "Checking the file…" is up, then still
    // disabled once the check has settled on the failure message.
    const apiError = Object.assign(new Error('bad'), { name: 'ApiError', status: 400, body: JSON.stringify({ ok: false, error, code: 'model_path_not_found' }) })
    let reject: (e: unknown) => void = () => {}
    vi.mocked(api.vectorValidateEmbedModel).mockImplementationOnce(() => new Promise((_, rej) => { reject = rej }))
    const input = screen.getByDisplayValue('/models/model.gguf')
    fireEvent.change(input, { target: { value: '/models/other.gguf' } })
    // Editing does NOT lift the status-derived gate: nothing is known about the
    // new path yet, so the last verdict (the status error) stays on screen and
    // Apply stays off until the live check replaces it. The label still follows
    // the path it would submit.
    expect(applyButton()).toHaveTextContent('Apply model')
    expect(applyButton()).toBeDisabled()
    expect(screen.getByTestId('embed-model-path-status-error')).toHaveTextContent('No file at that path.')
    fireEvent.blur(input)
    await screen.findByText('Checking the file…')
    expect(screen.queryByTestId('embed-model-path-status-error')).not.toBeInTheDocument()
    expect(applyButton()).toBeDisabled()
    reject(apiError)
    const failed = await screen.findByText('No file at that path.')
    expect(screen.queryByText('Checking the file…')).not.toBeInTheDocument()
    expect(failed.closest('.text-danger')).not.toBeNull()
    expect(api.vectorValidateEmbedModel).toHaveBeenCalledWith('/models/other.gguf')
    expect(applyButton()).toBeDisabled()
    expect(applyButton()).toHaveTextContent('Apply model')
  })

  it('stays disabled while a change is being applied or vectors are rebuilding', async () => {
    setStatus({ ...CUSTOM_OK, reembed: { step: 'running', done: 1, total: 4 } })
    renderWithProviders(<EmbeddingModelCard />)
    await screen.findByRole('progressbar')
    expect(applyButton()).toBeDisabled()
    expect(applyButton()).toHaveTextContent('Rebuild memory vectors')
  })

  it('confirms a reapply under the neutral card title and the reapply action, a change under the change title', async () => {
    setStatus(CUSTOM_OK)
    renderWithProviders(<EmbeddingModelCard />)
    const input = await screen.findByDisplayValue('/models/model.gguf')
    fireEvent.click(applyButton())
    let dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent('Embedding Model')
    expect(dialog).not.toHaveTextContent('Change the embedding model?')
    expect(within(dialog).getByRole('button', { name: 'Rebuild memory vectors' })).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: 'Change model' })).not.toBeInTheDocument()
    expect(dialog).toHaveTextContent('This reloads the configured model and clears and rebuilds the stored memory vectors.')
    expect(dialog).not.toHaveTextContent('new one')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    fireEvent.change(input, { target: { value: '/models/other.gguf' } })
    fireEvent.click(applyButton())
    dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent('Change the embedding model?')
    expect(dialog).toHaveTextContent('Stored memory vectors were produced by the current model, so they cannot be compared with the new one. They will be cleared and rebuilt.')
    expect(dialog).not.toHaveTextContent('This reloads the configured model')
    expect(within(dialog).getByRole('button', { name: 'Change model' })).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: 'Rebuild memory vectors' })).not.toBeInTheDocument()
  })

  it('submits the configured path on a confirmed reapply and shows the applying label meanwhile', async () => {
    setStatus(CUSTOM_OK)
    let release: () => void = () => {}
    vi.mocked(api.vectorApplyEmbedModel).mockImplementation(() => new Promise(resolve => { release = () => resolve({ ok: true } as never) }))
    renderWithProviders(<EmbeddingModelCard />)
    await screen.findByDisplayValue('/models/model.gguf')
    fireEvent.click(applyButton())
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Rebuild memory vectors' }))
    await waitFor(() => expect(api.vectorApplyEmbedModel).toHaveBeenCalledWith('/models/model.gguf'))
    expect(applyButton()).toHaveTextContent('Applying…')
    expect(applyButton()).toBeDisabled()
    release()
    await waitFor(() => expect(applyButton()).toHaveTextContent('Rebuild memory vectors'))
  })
})

describe('EmbeddingModelCard keeps a known-bad path gated until the live check has a verdict', () => {
  const error = 'The model path points at a file that does not exist'
  const PATH_MISSING = {
    ...CUSTOM_OK, model_active: false, setup_error: error, setup_error_code: 'model_path_not_found',
    setup_error_params: { path: '/models/model.gguf', error },
  }
  beforeEach(() => {
    vi.mocked(api.vectorEmbeddingStatus).mockReset()
    vi.mocked(api.vectorValidateEmbedModel).mockReset()
    vi.mocked(api.vectorApplyEmbedModel).mockReset()
  })

  it('a keystroke keeps the status error, its field accessibility, and the disabled Apply until the check passes', async () => {
    setStatus(PATH_MISSING)
    renderWithProviders(<EmbeddingModelCard />)
    const input = await screen.findByDisplayValue('/models/model.gguf')
    const notice = await screen.findByTestId('embed-model-path-status-error')
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input).toHaveAttribute('aria-describedby', notice.id)
    // One keystroke: the field now shows an unverified path. No verdict exists
    // for it yet, so the last known one stays, and so does the gate.
    fireEvent.change(input, { target: { value: '/models/model.ggu' } })
    expect(screen.getByTestId('embed-model-path-status-error')).toHaveTextContent('No file at that path.')
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input).toHaveAttribute('aria-describedby', notice.id)
    expect(applyButton()).toBeDisabled()
    expect(api.vectorValidateEmbedModel).not.toHaveBeenCalled()
    // Blur runs the live check; while it is in flight the spinner owns the
    // line and the button is still off.
    let release: () => void = () => {}
    vi.mocked(api.vectorValidateEmbedModel).mockImplementationOnce(() => new Promise(resolve => { release = () => resolve({ ok: true, size_bytes: 2 * 1024 * 1024 } as never) }))
    fireEvent.change(input, { target: { value: '/models/other.gguf' } })
    fireEvent.blur(input)
    await screen.findByText('Checking the file…')
    expect(screen.queryByTestId('embed-model-path-status-error')).not.toBeInTheDocument()
    expect(applyButton()).toBeDisabled()
    // The live verdict replaces the status one: a readable file enables Apply
    // and the field is no longer marked invalid.
    release()
    await screen.findByText(/Readable GGUF/)
    expect(screen.queryByTestId('embed-model-path-status-error')).not.toBeInTheDocument()
    expect(input).not.toHaveAttribute('aria-invalid')
    expect(input).not.toHaveAttribute('aria-describedby')
    expect(applyButton()).toBeEnabled()
    expect(applyButton()).toHaveTextContent('Apply model')
  })

  it('a failed live check replaces the status error with its own verdict and keeps Apply off; an emptied field reverts and is enabled', async () => {
    setStatus(PATH_MISSING)
    const apiError = Object.assign(new Error('bad'), { name: 'ApiError', status: 400, body: JSON.stringify({ ok: false, error: 'not a file', code: 'model_path_not_a_file' }) })
    vi.mocked(api.vectorValidateEmbedModel).mockRejectedValueOnce(apiError)
    renderWithProviders(<EmbeddingModelCard />)
    const input = await screen.findByDisplayValue('/models/model.gguf')
    await screen.findByTestId('embed-model-path-status-error')
    fireEvent.change(input, { target: { value: '/models' } })
    expect(applyButton()).toBeDisabled()
    fireEvent.blur(input)
    const failed = await screen.findByText('That path is a directory, not a file.')
    // One fault stated once: the live verdict, not the stale status notice.
    expect(screen.queryByTestId('embed-model-path-status-error')).not.toBeInTheDocument()
    expect(screen.queryByText('No file at that path.')).not.toBeInTheDocument()
    expect(failed.closest('.text-danger')).not.toBeNull()
    expect(applyButton()).toBeDisabled()
    // Emptying the field is a change back to the bundled model; the check
    // needs no request for that and its verdict enables Apply.
    fireEvent.change(input, { target: { value: '' } })
    expect(applyButton()).toBeDisabled()
    expect(screen.getByTestId('embed-model-path-status-error')).toBeInTheDocument()
    fireEvent.blur(input)
    await screen.findByText('Empty — this will revert to the bundled model.')
    expect(screen.queryByTestId('embed-model-path-status-error')).not.toBeInTheDocument()
    expect(applyButton()).toBeEnabled()
    expect(applyButton()).toHaveTextContent('Apply model')
    expect(api.vectorValidateEmbedModel).toHaveBeenCalledTimes(1)
  })

  it('a healthy configured path keeps the normal edit semantics: editing enables Apply without a check', async () => {
    setStatus(CUSTOM_OK)
    renderWithProviders(<EmbeddingModelCard />)
    const input = await screen.findByDisplayValue('/models/model.gguf')
    expect(screen.queryByTestId('embed-model-path-status-error')).not.toBeInTheDocument()
    fireEvent.change(input, { target: { value: '/models/other.gguf' } })
    expect(applyButton()).toBeEnabled()
    expect(applyButton()).toHaveTextContent('Apply model')
    expect(input).not.toHaveAttribute('aria-invalid')
    expect(api.vectorValidateEmbedModel).not.toHaveBeenCalled()
  })
})

describe('EmbeddingModelCard re-reads the status on return focus while the path error is showing', () => {
  const error = 'The model path points at a file that does not exist'
  const PATH_MISSING = {
    ...CUSTOM_OK, model_active: false, setup_error: error, setup_error_code: 'model_path_not_found',
    setup_error_params: { path: '/models/model.gguf', error },
  }
  const focusWindow = () => fireEvent(window, new Event('focus'))
  // Call counts are the assertion here, and nothing resets these mocks
  // between files' cases: reset (not clear — a queued mockImplementationOnce
  // must not leak either) before each one.
  beforeEach(() => {
    vi.mocked(api.vectorEmbeddingStatus).mockReset()
    vi.mocked(api.vectorValidateEmbedModel).mockReset()
    vi.mocked(api.vectorApplyEmbedModel).mockReset()
  })

  it('a file restored in place clears the notice and re-enables Apply with no blur and no live check', async () => {
    setStatus(PATH_MISSING)
    renderWithProviders(<EmbeddingModelCard />)
    await screen.findByTestId('embed-model-path-status-error')
    expect(applyButton()).toBeDisabled()
    expect(api.vectorEmbeddingStatus).toHaveBeenCalledTimes(1)
    // The operator puts the file back and comes back to the browser: the
    // status endpoint re-validates the configured path on every call, so the
    // next read carries no path error.
    vi.mocked(api.vectorEmbeddingStatus).mockResolvedValue(CUSTOM_OK as never)
    focusWindow()
    await waitFor(() => expect(api.vectorEmbeddingStatus).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByTestId('embed-model-path-status-error')).not.toBeInTheDocument())
    expect(applyButton()).toBeEnabled()
    expect(applyButton()).toHaveTextContent('Rebuild memory vectors')
    // The status read IS the re-check: no validate call, and the field still
    // holds the configured path.
    expect(api.vectorValidateEmbedModel).not.toHaveBeenCalled()
    expect(screen.getByDisplayValue('/models/model.gguf')).toBeInTheDocument()
  })

  it('a file still missing keeps the same notice and the disabled button, and keeps listening', async () => {
    setStatus(PATH_MISSING)
    renderWithProviders(<EmbeddingModelCard />)
    await screen.findByTestId('embed-model-path-status-error')
    focusWindow()
    await waitFor(() => expect(api.vectorEmbeddingStatus).toHaveBeenCalledTimes(2))
    expect(screen.getByTestId('embed-model-path-status-error')).toHaveTextContent('No file at that path.')
    expect(applyButton()).toBeDisabled()
    expect(api.vectorValidateEmbedModel).not.toHaveBeenCalled()
    // Not a one-shot: the next return re-reads again.
    focusWindow()
    await waitFor(() => expect(api.vectorEmbeddingStatus).toHaveBeenCalledTimes(3))
  })

  it('does not listen for focus when no path error is showing, so an idle card stays quiet', async () => {
    setStatus(CUSTOM_OK)
    renderWithProviders(<EmbeddingModelCard />)
    await screen.findByDisplayValue('/models/model.gguf')
    expect(api.vectorEmbeddingStatus).toHaveBeenCalledTimes(1)
    focusWindow()
    // Nothing is in flight to wait for; give a pending call the chance to land.
    await new Promise(r => setTimeout(r, 20))
    expect(api.vectorEmbeddingStatus).toHaveBeenCalledTimes(1)
  })

  it('stops listening once the user edits the field, so a draft is never refetched over', async () => {
    setStatus(PATH_MISSING)
    renderWithProviders(<EmbeddingModelCard />)
    const input = await screen.findByDisplayValue('/models/model.gguf')
    await screen.findByTestId('embed-model-path-status-error')
    fireEvent.change(input, { target: { value: '/models/dra' } })
    // Editing keeps the last verdict on screen (the gate stays until the live
    // check replaces it) but drops the listener: the notice now sits beside a
    // draft, and a refetch is for the untouched configured path only.
    expect(screen.getByTestId('embed-model-path-status-error')).toBeInTheDocument()
    expect(applyButton()).toBeDisabled()
    focusWindow()
    await new Promise(r => setTimeout(r, 20))
    expect(api.vectorEmbeddingStatus).toHaveBeenCalledTimes(1)
    expect(input).toHaveValue('/models/dra')
  })

  it('a status read still in flight when typing starts does not overwrite the draft when it lands', async () => {
    setStatus(PATH_MISSING)
    renderWithProviders(<EmbeddingModelCard />)
    const input = await screen.findByDisplayValue('/models/model.gguf')
    await screen.findByTestId('embed-model-path-status-error')
    // Hold the focus-triggered read open, then type before it resolves.
    let release: () => void = () => {}
    vi.mocked(api.vectorEmbeddingStatus).mockImplementationOnce(() => new Promise(resolve => { release = () => resolve(CUSTOM_OK as never) }))
    focusWindow()
    await waitFor(() => expect(api.vectorEmbeddingStatus).toHaveBeenCalledTimes(2))
    fireEvent.change(input, { target: { value: '/models/other.gguf' } })
    release()
    // The late response updates the status (the header no longer reports a
    // path error) but leaves the field exactly as typed.
    await waitFor(() => expect(screen.getByTestId('embed-model-active')).toHaveAttribute('data-state', 'active'))
    expect(input).toHaveValue('/models/other.gguf')
    expect(applyButton()).toHaveTextContent('Apply model')
  })

  it('a stale error status landing after a passed live check does not re-disable Apply', async () => {
    setStatus(PATH_MISSING)
    renderWithProviders(<EmbeddingModelCard />)
    const input = await screen.findByDisplayValue('/models/model.gguf')
    await screen.findByTestId('embed-model-path-status-error')
    // The focus read is slow and will come back with the OLD error; meanwhile
    // the user edits to a real file and blurs, and the live check passes.
    let release: () => void = () => {}
    vi.mocked(api.vectorEmbeddingStatus).mockImplementationOnce(() => new Promise(resolve => { release = () => resolve(PATH_MISSING as never) }))
    focusWindow()
    await waitFor(() => expect(api.vectorEmbeddingStatus).toHaveBeenCalledTimes(2))
    fireEvent.change(input, { target: { value: '/models/other.gguf' } })
    fireEvent.blur(input)
    await screen.findByText(/Readable GGUF/)
    expect(applyButton()).toBeEnabled()
    release()
    await new Promise(r => setTimeout(r, 20))
    // The live verdict owns the gate: the stale status neither disables the
    // button nor brings the status notice back nor touches the draft.
    expect(applyButton()).toBeEnabled()
    expect(screen.queryByTestId('embed-model-path-status-error')).not.toBeInTheDocument()
    expect(input).toHaveValue('/models/other.gguf')
  })
})

describe('EmbeddingModelCard keyword-search reassurance', () => {
  const DEFERRED = {
    ...CUSTOM_OK, model_active: false, reembed: { step: 'deferred' },
    repair: { generation: 'r', pending_vectors: 5, pending_invalidation: 0, deferred_stores: 1 },
  }

  it('renders the reassurance once under a deferred rebuild, with no progress bar and no zero clause', async () => {
    setStatus(DEFERRED)
    renderWithProviders(<EmbeddingModelCard />)
    const line = await screen.findByTestId('embed-model-repair-status')
    expect(line).toHaveTextContent('5 memories still need a new vector and 1 closed or unavailable store will be rebuilt when it next opens.')
    expect(line).not.toHaveTextContent('0 open stores')
    expect(screen.getAllByText('Keyword search still works. Safe to leave this page.')).toHaveLength(1)
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    // Not dressed up as an error: the reassurance is muted, and no alert is raised.
    expect(screen.getByText('Keyword search still works. Safe to leave this page.')).toHaveClass('text-muted')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('renders it while applying and running, and replaces it with the failure hint on failure', async () => {
    setStatus({ ...CUSTOM_OK, reembed: { step: 'applying' } })
    const { unmount } = renderWithProviders(<EmbeddingModelCard />)
    expect(await screen.findByText('Keyword search still works. Safe to leave this page.')).toBeInTheDocument()
    unmount()

    setStatus({ ...CUSTOM_OK, reembed: { step: 'running', done: 2, total: 5 } })
    const second = renderWithProviders(<EmbeddingModelCard />)
    expect(await screen.findByText('Keyword search still works. Safe to leave this page.')).toBeInTheDocument()
    second.unmount()

    setStatus({ ...CUSTOM_OK, reembed: { step: 'failed', done: 2, total: 5, error: 'raw backend prose' } })
    renderWithProviders(<EmbeddingModelCard />)
    const hint = await screen.findByText(/The model could not be applied/)
    expect(screen.queryByText('Keyword search still works. Safe to leave this page.')).not.toBeInTheDocument()
    // Raw prose stays in the tooltip, never in the visible text.
    expect(hint).toHaveAttribute('title', 'raw backend prose')
    expect(screen.getByTestId('embed-model-card')).not.toHaveTextContent('raw backend prose')
  })

  it('renders nothing of it when no rebuild is in flight', async () => {
    setStatus(CUSTOM_OK)
    renderWithProviders(<EmbeddingModelCard />)
    await screen.findByDisplayValue('/models/model.gguf')
    expect(screen.queryByText('Keyword search still works. Safe to leave this page.')).not.toBeInTheDocument()
    expect(screen.queryByTestId('embed-model-repair-status')).not.toBeInTheDocument()
  })
})


describe('EmbeddingModelCard shares the embedding-status snapshot', () => {
  beforeEach(() => {
    vi.mocked(api.vectorEmbeddingStatus).mockReset()
    vi.mocked(api.vectorValidateEmbedModel).mockReset()
    vi.mocked(api.vectorApplyEmbedModel).mockReset()
  })

  it('observes a newer shared snapshot without clobbering a path draft', async () => {
    const repair = { generation: 'request-1', pending_vectors: 7, pending_invalidation: 1, deferred_stores: 1, unknown_scope: false }
    setStatus({ ...CUSTOM_OK, reembed: { step: 'deferred' }, repair })
    const { queryClient } = renderWithProviders(<EmbeddingModelCard />)
    const input = await screen.findByDisplayValue('/models/model.gguf')
    expect(screen.getByTestId('embed-model-repair-status')).toHaveTextContent('1 closed or unavailable store')
    fireEvent.change(input, { target: { value: '/models/draft.gguf' } })
    // The other Memory-tab observer publishes a newer backend response.
    queryClient.setQueryData(['member-memory', 'default', 'embedding-status'], {
      ...CUSTOM_OK, reembed: { step: 'deferred' }, repair: { ...repair, deferred_stores: 9 },
    })
    await waitFor(() => expect(screen.getByTestId('embed-model-repair-status')).toHaveTextContent('9 closed or unavailable stores'))
    expect(input).toHaveValue('/models/draft.gguf')
    expect(api.vectorEmbeddingStatus).toHaveBeenCalledTimes(1)
  })

  it('deduplicates overlapping reads of the same mounted status', async () => {
    let release: () => void = () => {}
    vi.mocked(api.vectorEmbeddingStatus).mockImplementation(() => new Promise(resolve => { release = () => resolve(CUSTOM_OK as never) }))
    renderWithProviders(<><EmbeddingModelCard /><EmbeddingModelCard /></>)
    await waitFor(() => expect(api.vectorEmbeddingStatus).toHaveBeenCalledTimes(1))
    release()
    await waitFor(() => expect(screen.getAllByDisplayValue('/models/model.gguf')).toHaveLength(2))
  })
})


it.each([true, false])('offers a separate Logs page only for unknown repair scope: %s', async unknown => {
  setStatus({
    ...CUSTOM_OK,
    reembed: { step: 'deferred' },
    repair: { generation: 'request', unknown_scope: unknown, pending_vectors: 1, pending_invalidation: 0, deferred_stores: 0 },
  })
  renderWithProviders(<EmbeddingModelCard />)
  const input = await screen.findByDisplayValue('/models/model.gguf')
  fireEvent.change(input, { target: { value: '/models/unsaved.gguf' } })
  const notice = screen.getByTestId('embed-model-repair-status')
  if (unknown) {
    const link = within(notice).getByRole('link', { name: 'Logs' })
    expect(link).toHaveAttribute('href', '/logs')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  } else {
    expect(within(notice).queryByRole('link')).not.toBeInTheDocument()
  }
  expect(input).toHaveValue('/models/unsaved.gguf')
})
