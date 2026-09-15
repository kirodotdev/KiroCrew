import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import ReportProblemModal from './ReportProblemModal'
import { api, ApiError } from '../api/client'
import { i18nT } from '../i18n/t'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: { ...mod.api, collectDiagnostics: vi.fn(), revealPath: vi.fn() },
  }
})

// The modal no longer offers a reveal delivery, so its output does not depend on
// directLocal; the branding hook is stubbed only so the component renders under
// test. Pinned local for a representative session.
const brandingEnv = vi.hoisted(() => ({ directLocal: true }))
vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Test', avatar: '', directLocal: brandingEnv.directLocal }),
}))

const collectDiagnostics = vi.mocked(api.collectDiagnostics)
const revealPath = vi.mocked(api.revealPath)

type Collected = Awaited<ReturnType<typeof api.collectDiagnostics>>

function bundle(over: Partial<Collected> = {}): Collected {
  return {
    zip_path: '/zzq/tmp/zzq-bundle.zip',
    download_url: '/api/zzq-download',
    github_issue_url: 'https://example.invalid/zzq-issue',
    total_redactions: 7,
    included: ['zzq-a.log', 'zzq-b.log'],
    ...over,
  } as Collected
}

const createBtn = () => screen.getByRole('button', {
  name: i18nT('components.reportProblemModal.create_report'),
})

describe('ReportProblemModal', () => {
  beforeEach(() => {
    collectDiagnostics.mockReset()
    revealPath.mockReset()
    revealPath.mockResolvedValue(undefined as never)
    brandingEnv.directLocal = true
  })

  it('sends the typed note and the logs toggle to the collector', async () => {
    collectDiagnostics.mockResolvedValue(bundle())
    renderWithProviders(<ReportProblemModal open onClose={vi.fn()} />)

    fireEvent.change(
      screen.getByLabelText(i18nT('components.reportProblemModal.what_happened')),
      { target: { value: 'zzq note' } },
    )
    fireEvent.click(screen.getByRole('switch'))
    fireEvent.click(createBtn())

    await waitFor(() =>
      expect(collectDiagnostics).toHaveBeenCalledWith({ note: 'zzq note', include_logs: false }),
    )
  })

  it('shows the bundle path and the two deliveries on success', async () => {
    collectDiagnostics.mockResolvedValue(bundle())
    renderWithProviders(<ReportProblemModal open onClose={vi.fn()} />)
    fireEvent.click(createBtn())

    await waitFor(() => expect(screen.getByText('/zzq/tmp/zzq-bundle.zip')).toBeInTheDocument())

    const download = screen.getByRole('link', {
      name: i18nT('components.reportProblemModal.download_zip'),
    })
    expect(download).toHaveAttribute('href', '/api/zzq-download')
    expect(
      screen.getByRole('link', { name: i18nT('components.reportProblemModal.open_github_issue') }),
    ).toHaveAttribute('href', 'https://example.invalid/zzq-issue')
  })

  it('carries no reveal delivery — the saved path stands in for it', async () => {
    // The success row keeps two buttons (download + GitHub issue); a reveal is
    // not offered because /api/reveal shells out on the GATEWAY, so it cannot
    // usefully drive a file manager for a browser that is not on that host. The
    // saved path is shown as text so a local operator can still locate the zip.
    collectDiagnostics.mockResolvedValue(bundle())
    renderWithProviders(<ReportProblemModal open onClose={vi.fn()} />)
    fireEvent.click(createBtn())

    await waitFor(() => expect(screen.getByText('/zzq/tmp/zzq-bundle.zip')).toBeInTheDocument())

    expect(
      screen.queryByRole('button', {
        name: i18nT('components.markdownPanel.show_in_file_manager'),
      }),
    ).not.toBeInTheDocument()
    expect(
      screen.getByRole('link', { name: i18nT('components.reportProblemModal.download_zip') }),
    ).toBeInTheDocument()
    expect(revealPath).not.toHaveBeenCalled()
  })

  it('surfaces an ApiError message verbatim', async () => {
    collectDiagnostics.mockRejectedValue(new ApiError(500, 'zzq collector exploded'))
    renderWithProviders(<ReportProblemModal open onClose={vi.fn()} />)
    fireEvent.click(createBtn())

    await waitFor(() => expect(screen.getByText('zzq collector exploded')).toBeInTheDocument())
  })

  it('falls back to the generic message for a non-ApiError rejection', async () => {
    collectDiagnostics.mockRejectedValue(new Error('zzq raw'))
    renderWithProviders(<ReportProblemModal open onClose={vi.fn()} />)
    fireEvent.click(createBtn())

    await waitFor(() =>
      expect(
        screen.getByText(i18nT('components.reportProblemModal.collect_failed')),
      ).toBeInTheDocument(),
    )
    expect(screen.queryByText('zzq raw')).not.toBeInTheDocument()
  })

  it('refuses to close while the collect call is in flight', async () => {
    let release: (v: Collected) => void = () => {}
    collectDiagnostics.mockReturnValue(new Promise<Collected>(r => { release = r }))
    const onClose = vi.fn()
    renderWithProviders(<ReportProblemModal open onClose={onClose} />)

    fireEvent.click(createBtn())
    await waitFor(() => expect(collectDiagnostics).toHaveBeenCalled())

    fireEvent.click(
      screen.getByRole('button', { name: i18nT('components.reportProblemModal.cancel') }),
    )
    expect(onClose).not.toHaveBeenCalled()

    await act(async () => { release(bundle()) })
    await waitFor(() => expect(screen.getByText('/zzq/tmp/zzq-bundle.zip')).toBeInTheDocument())
  })

  it('clears the form on the deferred reset after closing', () => {
    vi.useFakeTimers()
    try {
      const onClose = vi.fn()
      renderWithProviders(<ReportProblemModal open onClose={onClose} />)
      const note = screen.getByLabelText(
        i18nT('components.reportProblemModal.what_happened'),
      ) as HTMLTextAreaElement
      fireEvent.change(note, { target: { value: 'zzq draft' } })

      fireEvent.click(
        screen.getByRole('button', { name: i18nT('components.reportProblemModal.cancel') }),
      )
      expect(onClose).toHaveBeenCalledTimes(1)
      expect(note.value).toBe('zzq draft')

      act(() => { vi.advanceTimersByTime(250) })
      expect(note.value).toBe('')
    } finally {
      vi.useRealTimers()
    }
  })
})
