import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '../../api/client'
import { renderWithProviders } from '../../test/helpers'
import { FirstRunChapters, useFirstRunChapters } from './firstRun'

// Spread the real module: the import flow branches on `error instanceof ApiError`.
vi.mock('../../api/client', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../../api/client')>()
  return {
    ...mod,
    api: {
      onboardingImportScan: vi.fn(),
      onboardingImportApply: vi.fn(),
      onboardingImportState: vi.fn(),
      kirocrewConfig: vi.fn(),
      patchConfig: vi.fn(),
    },
  }
})

const markOnboarded = vi.fn()
const markImportOnboarded = vi.fn()

/** The real first-run host, with the given completion flags. */
function Host({ importOnboarded = false }: { importOnboarded?: boolean }) {
  const firstRun = useFirstRunChapters({
    onboarded: false,
    importOnboarded,
    themeBootReady: true,
    markOnboarded,
  })
  return (
    <FirstRunChapters
      firstRun={firstRun}
      onboarded={false}
      markOnboarded={markOnboarded}
      markImportOnboarded={markImportOnboarded}
    />
  )
}

const scanFailed = () =>
  new ApiError(500, 'request failed', '{"error":"request failed","code":"scan_failed"}')

describe('first run: Ask the agent from the importer', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    vi.mocked(api.onboardingImportScan).mockRejectedValue(scanFailed())
    vi.mocked(api.onboardingImportState).mockResolvedValue({ ok: true })
  })

  it('closes the importer without finishing the chapter, and can open it again', async () => {
    renderWithProviders(<Host />)
    await screen.findByRole('heading', { name: 'We could not scan agent setup' })

    await userEvent.click(within(screen.getByTestId('agent-import-scan-error')).getByText('Ask the agent'))

    await waitFor(() => {
      expect(screen.queryByRole('heading', { name: 'We could not scan agent setup' })).not.toBeInTheDocument()
    })
    // Nothing was imported or skipped, so no chapter is marked done and the tour,
    // which follows a finished import, does not open over the chat.
    expect(markImportOnboarded).not.toHaveBeenCalled()
    expect(markOnboarded).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

    // Import setup, from Settings, opens it again instead of doing nothing.
    act(() => { window.dispatchEvent(new CustomEvent('mc-start-import')) })

    expect(await screen.findByRole('heading', { name: 'We could not scan agent setup' })).toBeInTheDocument()
  })
})

describe('first run: Import setup leads straight to Customize', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ dashboard: { user_role: '', user_technical_level: '' } } as never)
    vi.mocked(api.patchConfig).mockResolvedValue({} as never)
  })

  it('opens the tour once import is done, with no Privacy chapter before it', async () => {
    renderWithProviders(<Host importOnboarded />)
    expect(await screen.findByText('Pick your look')).toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: 'Privacy' })).not.toBeInTheDocument()
    expect(markOnboarded).not.toHaveBeenCalled()
  })
})
