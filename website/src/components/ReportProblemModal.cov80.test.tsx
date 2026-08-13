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

  it('shows the bundle path and the three deliveries on success', async () => {
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

    // The reveal delivery names the GATEWAY host's file manager. This render
    // seeds no platform, so it is the generic arm; the per-platform arms live in
    // test/ReportProblemModal.test.tsx.
    fireEvent.click(
      screen.getByRole('button', {
        name: i18nT('components.reportProblemModal.show_in_file_manager'),
      }),
    )
    expect(revealPath).toHaveBeenCalledWith('/zzq/tmp/zzq-bundle.zip')
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
