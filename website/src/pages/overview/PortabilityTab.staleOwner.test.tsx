/**
 * Export and import are owner-gated direct fetches: `/api/portability/export`
 * and `/api/portability/import` run `require_owner_dashboard_request`, whose
 * denial for a session minted before `KIROCREW_OWNER_ID` was configured is
 * `401 stale_session_reauth`. Like the other direct-fetch owner-gated surfaces,
 * both must raise the installed re-auth prompt on that signal — and only that
 * signal — while keeping their own inline error.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import { STALE_OWNER_SESSION_CODE, __resetAuthRecoveryStateForTests } from '../../api/client'
import { i18nT } from '../../i18n/t'
import PortabilityTab from './PortabilityTab'

const STALE_ERROR = 'this session predates the configured owner; sign in again'

const jsonResponse = (status: number, body: unknown): Response =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

const bannerEl = (): HTMLElement | null => document.getElementById('mc-session-expired')

const exportButton = () =>
  screen.getByRole('button', { name: i18nT('pages.overview.portabilityTab.download_export_zip') })

const importButton = () =>
  screen.getByRole('button', { name: i18nT('pages.overview.portabilityTab.import') })

/** Pick an archive through the real input, let the (non-owner-gated) preview
 *  accept it, then press Import — the import answer is *importReply*. */
async function runImport(importReply: Response) {
  fetchMock.mockImplementation((url: string) => Promise.resolve(
    url === '/api/portability/preview'
      ? jsonResponse(200, { ok: true, manifest: { version: 1, created_at: 'now', hostname: 'h', user: 'u', contents: {} } })
      : importReply,
  ))
  const input = screen.getByLabelText(i18nT('pages.overview.portabilityTab.choose_import_file')) as HTMLInputElement
  const file = new File(['zip'], 'export.zip', { type: 'application/zip' })
  Object.defineProperty(input, 'files', { value: [file], configurable: true })
  fireEvent.change(input)
  await waitFor(() => expect(importButton()).not.toBeDisabled())
  fireEvent.click(importButton())
}

let fetchMock: ReturnType<typeof vi.fn>
let originalFetch: typeof fetch

beforeEach(() => {
  __resetAuthRecoveryStateForTests()
  fetchMock = vi.fn()
  originalFetch = globalThis.fetch
  globalThis.fetch = fetchMock as unknown as typeof fetch
})

afterEach(() => {
  globalThis.fetch = originalFetch
  __resetAuthRecoveryStateForTests()
})

describe('PortabilityTab export — stale pre-owner session', () => {
  it('raises the re-auth prompt on 401 stale_session_reauth and keeps the inline error', async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { error: STALE_ERROR, code: STALE_OWNER_SESSION_CODE }))
    render(<PortabilityTab />)

    fireEvent.click(exportButton())

    expect(await screen.findByText(STALE_ERROR)).toBeInTheDocument()
    expect(bannerEl()).not.toBeNull()
  })

  it('leaves a generic 401 to the inline error alone', async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { error: 'authentication required', code: 'auth_required' }))
    render(<PortabilityTab />)

    fireEvent.click(exportButton())

    expect(await screen.findByText('authentication required')).toBeInTheDocument()
    expect(bannerEl()).toBeNull()
  })

  it('leaves a 403 owner_only denial to the inline error alone', async () => {
    fetchMock.mockResolvedValue(jsonResponse(403, { error: 'owner authorization required', code: 'owner_only' }))
    render(<PortabilityTab />)

    fireEvent.click(exportButton())

    expect(await screen.findByText('owner authorization required')).toBeInTheDocument()
    expect(bannerEl()).toBeNull()
  })

  it('a successful export still downloads without prompting', async () => {
    fetchMock.mockResolvedValue(new Response(new Blob(['zip']), {
      status: 200,
      headers: { 'Content-Disposition': 'attachment; filename="kc.zip"' },
    }))
    const createUrl = vi.fn(() => 'blob:export')
    const revokeUrl = vi.fn()
    const origCreate = URL.createObjectURL
    const origRevoke = URL.revokeObjectURL
    URL.createObjectURL = createUrl
    URL.revokeObjectURL = revokeUrl
    try {
      render(<PortabilityTab />)
      fireEvent.click(exportButton())

      expect(await screen.findByText(i18nT('pages.overview.portabilityTab.download_started'))).toBeInTheDocument()
      expect(createUrl).toHaveBeenCalledTimes(1)
      expect(bannerEl()).toBeNull()
    } finally {
      URL.createObjectURL = origCreate
      URL.revokeObjectURL = origRevoke
    }
  })
})

describe('PortabilityTab import — stale pre-owner session', () => {
  it('raises the re-auth prompt on 401 stale_session_reauth and keeps the inline error', async () => {
    render(<PortabilityTab />)

    await runImport(jsonResponse(401, { error: STALE_ERROR, code: STALE_OWNER_SESSION_CODE }))

    expect(await screen.findByTestId('portability-import-error')).toHaveTextContent(STALE_ERROR)
    expect(bannerEl()).not.toBeNull()
  })

  it('leaves a generic 401 to the inline error alone', async () => {
    render(<PortabilityTab />)

    await runImport(jsonResponse(401, { error: 'authentication required', code: 'auth_required' }))

    expect(await screen.findByTestId('portability-import-error')).toHaveTextContent('authentication required')
    expect(bannerEl()).toBeNull()
  })

  it('leaves a 403 owner_only denial to the inline error alone', async () => {
    render(<PortabilityTab />)

    await runImport(jsonResponse(403, { error: 'owner authorization required', code: 'owner_only' }))

    expect(await screen.findByTestId('portability-import-error')).toHaveTextContent('owner authorization required')
    expect(bannerEl()).toBeNull()
  })

  it('a successful import still reports completion without prompting', async () => {
    render(<PortabilityTab />)

    await runImport(jsonResponse(200, { ok: true, summary: { items: ['a', 'b'] }, manifest: {} }))

    expect(await screen.findByText(/Import complete \(2 items\)/)).toBeInTheDocument()
    expect(bannerEl()).toBeNull()
  })
})
