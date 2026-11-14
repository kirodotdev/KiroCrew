/**
 * Redaction engine selector (Settings -> Security -> Redaction).
 *
 * A view over the `engine` field of the credential-redaction record, separate from the
 * file-view switch above it (test/test_redaction_engine_selector.py pins the backend):
 *
 *  - structured is the default, including for a gateway that sends no `engine`;
 *  - flipping it sends exactly the engine to its own writer, never the switch's;
 *  - the two toggles sit under different headings and labels.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import type { CredentialRedactionState } from '../../api/client'
import { i18nT } from '../../i18n/t'

vi.mock('../../hooks/usePanelTabs', async () => ({
  ...(await vi.importActual<typeof import('../../hooks/usePanelTabs')>('../../hooks/usePanelTabs')),
  evictDocumentBodies: vi.fn(() => 0),
  purgeDocumentBodiesForRedactionChange: vi.fn(),
}))

vi.mock('../../api/client', async () => ({
  ApiError: (await vi.importActual<typeof import('../../api/apiError')>('../../api/apiError')).ApiError,
  api: {
    deniedCommands: vi.fn(),
    governancePolicy: vi.fn(),
    securityPosture: vi.fn(),
    kirocrewConfig: vi.fn(),
    patchConfig: vi.fn(),
    tailnetStatus: vi.fn(),
    credentialRedaction: vi.fn(),
    setCredentialRedaction: vi.fn(),
    setCredentialRedactionEngine: vi.fn(),
    redactionAllowedHosts: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { SecurityPanel } from './SecurityPanel'

const mock = (fn: unknown) => fn as ReturnType<typeof vi.fn>

async function renderEngine(state: CredentialRedactionState) {
  mock(api.credentialRedaction).mockResolvedValue(state)
  renderWithProviders(<SecurityPanel />, { route: '/?section=redaction', queryDefaults: { retryDelay: 0 } })
  return screen.findByTestId('credential-redaction-engine-row')
}

describe('SecurityPanel - redaction engine selector', () => {
  beforeEach(() => {
    cleanup()
    vi.clearAllMocks()
    mock(api.deniedCommands).mockResolvedValue({
      builtins: [], user_added: [], disable_all: false, effective_count: 0, governance_locked: false,
    })
    mock(api.securityPosture).mockResolvedValue({ controls: [], counts: {} })
    mock(api.governancePolicy).mockResolvedValue({
      version: null, has_policy: false, profile: null, unavailable: false, scopes: [],
    })
    mock(api.kirocrewConfig).mockResolvedValue({})
    mock(api.patchConfig).mockResolvedValue({ ok: true })
    mock(api.tailnetStatus).mockResolvedValue({
      enabled: false, governance_pinned: false, host: '', origin: '', resolved_at: 0, state: 'off',
    })
    mock(api.redactionAllowedHosts).mockResolvedValue({ workspaces: {} })
  })

  it('reads structured by default, also from a gateway that sends no engine', async () => {
    const row = await renderEngine({ enabled: true, changed_at: '' })
    expect(within(row).getByRole('switch')).toHaveAttribute('aria-checked', 'true')
    expect(screen.queryByTestId('credential-redaction-engine-legacy-notice')).toBeNull()
  })

  it('shows legacy when chosen, with its notice', async () => {
    const row = await renderEngine({ enabled: true, changed_at: '', engine: 'legacy' })
    expect(within(row).getByRole('switch')).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByTestId('credential-redaction-engine-legacy-notice').textContent).toBe(
      'IDs such as AWS key IDs are hidden too.',
    )
  })

  it('names the outcome, not the mechanism', async () => {
    const row = await renderEngine({ enabled: true, changed_at: '', engine: 'legacy' })
    const text = row.textContent ?? ''
    expect(text).toMatch(/Keep IDs readable/)
    expect(text).toMatch(/On: keeps IDs readable, such as an AWS key ID \(AKIA…\) or a 40-character commit hash\. Off: goes back to the earlier redaction, which hides them too\. This setting doesn't change whether secrets are hidden\./)
    for (const term of [/structured detectors/i, /pattern passes/i, /credential-shaped/i, /seams?\b/i]) {
      expect(text).not.toMatch(term)
    }
  })

  it('sends the engine to its own writer and never the switch', async () => {
    mock(api.setCredentialRedactionEngine).mockResolvedValue({ enabled: true, changed_at: 't', engine: 'legacy' })
    const row = await renderEngine({ enabled: true, changed_at: '', engine: 'structured' })
    fireEvent.click(within(row).getByRole('switch'))
    await waitFor(() => expect(api.setCredentialRedactionEngine).toHaveBeenCalledWith('legacy'))
    expect(api.setCredentialRedaction).not.toHaveBeenCalled()
  })

  it('is not confusable with the file-view or the prompt-and-logs switch', async () => {
    await renderEngine({ enabled: true, changed_at: '', engine: 'structured' })
    expect(screen.getAllByRole('switch')).toHaveLength(3)
    const section = i18nT('pages.settings.securityPanel.credential_redaction_engine_section')
    const label = i18nT('pages.settings.securityPanel.credential_redaction_engine_toggle')
    for (const other of ['credential_redaction_section', 'credential_redaction_seams_section']) {
      expect(section).not.toBe(i18nT(`pages.settings.securityPanel.${other}`))
    }
    for (const other of ['credential_redaction_toggle', 'credential_redaction_seams_toggle']) {
      expect(label).not.toBe(i18nT(`pages.settings.securityPanel.${other}`))
    }
    expect(screen.getByText(section)).toBeInTheDocument()
    expect(screen.getByText(label)).toBeInTheDocument()
  })
})
