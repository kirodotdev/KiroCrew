/**
 * Settings -> Security: settings files edited outside the dashboard.
 *
 * The cards are a VIEW over `/api/security/config-pending` (backend rules live in
 * test/test_config_pending.py). What a UI can get wrong here:
 *
 *  - the changed keys must be listed, and a value the gateway withheld must never
 *    be shown (only "value hidden");
 *  - Apply / Discard must echo the reviewed signature, and the card must go away
 *    once the gateway answers with no pending file;
 *  - Discard overwrites the file, so it asks first, and is not styled as the
 *    dangerous choice;
 *  - an edit that loosens confinement must not be one click away: Apply is not
 *    the primary button and asks first, restating what the edit does;
 *  - a 409 `pending_changed` must re-read, so the owner reviews the newer edit.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, cleanup, within, act } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import type { ConfigPendingData } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { i18nT } from '../../i18n/t'
import { useSettingHighlight } from '../../hooks/useSettingHighlight'
import { securityHints } from './SecurityPanelConfigPending'

vi.mock('../../api/client', async () => ({
  ApiError: (await vi.importActual<typeof import('../../api/apiError')>('../../api/apiError')).ApiError,
  api: {
    // The rail reads these on mount; they must resolve.
    deniedCommands: vi.fn(),
    governancePolicy: vi.fn(),
    securityPosture: vi.fn(),
    kirocrewConfig: vi.fn(),
    tailnetStatus: vi.fn(),
    configPending: vi.fn(),
    applyConfigPending: vi.fn(),
    discardConfigPending: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { SecurityPanel } from './SecurityPanel'
import { CONFIG_PENDING_QUERY_KEY } from './configPendingQuery'

const mocked = (f: unknown) => f as ReturnType<typeof vi.fn>

const NONE: ConfigPendingData = { enforcing: true, count: 0, pending: [] }
const ONE: ConfigPendingData = {
  enforcing: true,
  count: 1,
  pending: [{
    leaf: 'config.json',
    signature: 'sig-1',
    disk_valid: true,
    truncated: false,
    changes: [
      { path: ['agent', 'sandbox'], change: 'changed', before: 'strict', after: 'off' },
      { path: ['slack', 'bot_token'], change: 'added', redacted: true },
      { path: ['dashboard', 'port'], change: 'removed', before: 8765 },
    ],
  }],
}

// No key with a security line: Apply stays the primary, one-click action.
const PLAIN: ConfigPendingData = {
  ...ONE,
  pending: [{ ...ONE.pending[0], changes: [{ path: ['ui', 'theme'], change: 'changed', before: 'light', after: 'dark' }] }],
}

async function renderPanel(first: ConfigPendingData) {
  mocked(api.configPending).mockResolvedValue(first)
  renderWithProviders(<SecurityPanel />, { route: '/?section=posture', queryDefaults: { retryDelay: 0 } })
  if (first.count > 0) await screen.findByTestId('config-pending-card')
}

const applyBtn = () => screen.getByRole('button', { name: i18nT('pages.settings.securityPanel.configPending.apply_file', { file: 'config.json' }) })
/** Press Apply on a security-flagged edit and say yes in the confirm it opens. */
async function applyAndConfirm() {
  fireEvent.click(applyBtn())
  const dialog = await screen.findByRole('dialog')
  fireEvent.click(within(dialog).getByRole('button', { name: i18nT('pages.settings.securityPanel.configPending.apply_turn_off_sandbox') }))
}
const PRIMARY = 'bg-accent'
const DANGER = 'text-danger'
const discardBtn = () => screen.getByRole('button', { name: i18nT('pages.settings.securityPanel.configPending.discard_file', { file: 'config.json' }) })

describe('SecurityPanel - settings files changed outside the dashboard', () => {
  beforeEach(() => {
    cleanup()
    vi.clearAllMocks()
    mocked(api.deniedCommands).mockResolvedValue({ builtins: [], user_added: [], disable_all: false, effective_count: 0, governance_locked: false })
    mocked(api.securityPosture).mockResolvedValue({ controls: [], counts: {} })
    mocked(api.governancePolicy).mockResolvedValue({ version: null, has_policy: false, profile: null, unavailable: false, scopes: [] })
    mocked(api.kirocrewConfig).mockResolvedValue({})
    mocked(api.tailnetStatus).mockResolvedValue({ enabled: false, governance_pinned: false, host: '', origin: '', resolved_at: 0, state: 'off' })
  })

  it('shows nothing while no file is pending', async () => {
    await renderPanel(NONE)
    await waitFor(() => expect(api.configPending).toHaveBeenCalled())
    expect(screen.queryByTestId('config-pending-card')).toBeNull()
  })

  it('lists the changed keys, carries the highlight id, and never shows a withheld value', async () => {
    await renderPanel(ONE)
    const card = screen.getByTestId('config-pending-card')
    expect(card).toHaveAttribute('data-setting-id', 'security.config-pending')
    expect(card).toHaveTextContent(i18nT('pages.settings.securityPanel.configPending.title', { file: 'config.json' }))
    const items = within(card).getAllByRole('listitem').map(li => li.textContent)
    expect(items).toEqual([
      i18nT('pages.settings.securityPanel.configPending.changed_value', { key: 'agent.sandbox', before: '"strict"', after: '"off"' })
        + i18nT('pages.settings.securityPanel.configPending.hint_sandbox_off'),
      i18nT('pages.settings.securityPanel.configPending.added_hidden', { key: 'slack.bot_token' }),
      i18nT('pages.settings.securityPanel.configPending.removed_value', { key: 'dashboard.port', before: '8765' }),
    ])
  })

  it('with no security line, Apply is the primary button and posts the reviewed signature without asking', async () => {
    await renderPanel(PLAIN)
    expect(applyBtn()).toHaveClass(PRIMARY)
    mocked(api.applyConfigPending).mockResolvedValue(NONE)
    mocked(api.configPending).mockResolvedValue(NONE)
    fireEvent.click(applyBtn())
    await waitFor(() => expect(screen.queryByTestId('config-pending-card')).toBeNull())
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(api.applyConfigPending).toHaveBeenCalledWith('config.json', 'sig-1')
    expect(api.discardConfigPending).not.toHaveBeenCalled()
  })

  it('with a security line, Apply is not primary and asks first, restating what the edit does', async () => {
    await renderPanel(ONE)
    expect(applyBtn()).not.toHaveClass(PRIMARY)
    mocked(api.applyConfigPending).mockResolvedValue(NONE)
    mocked(api.configPending).mockResolvedValue(NONE)
    fireEvent.click(applyBtn())
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent(i18nT('pages.settings.securityPanel.configPending.apply_title', { file: 'config.json' }))
    expect(within(dialog).getByTestId('config-pending-apply-hints'))
      .toHaveTextContent(i18nT('pages.settings.securityPanel.configPending.hint_sandbox_off'))
    expect(api.applyConfigPending).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: i18nT('pages.settings.securityPanel.configPending.apply_turn_off_sandbox') }))
    await waitFor(() => expect(screen.queryByTestId('config-pending-card')).toBeNull())
    expect(api.applyConfigPending).toHaveBeenCalledWith('config.json', 'sig-1')
  })

  it('when the list is cut short, Apply asks first and says not every change is listed', async () => {
    await renderPanel({ ...PLAIN, pending: [{ ...PLAIN.pending[0], truncated: true }] })
    expect(applyBtn()).not.toHaveClass(PRIMARY)
    mocked(api.applyConfigPending).mockResolvedValue(NONE)
    mocked(api.configPending).mockResolvedValue(NONE)
    fireEvent.click(applyBtn())
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByTestId('config-pending-apply-truncated'))
      .toHaveTextContent(i18nT('pages.settings.securityPanel.configPending.truncated'))
    expect(within(dialog).queryByTestId('config-pending-apply-hints')).toBeNull()
    expect(api.applyConfigPending).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: i18nT('pages.settings.securityPanel.configPending.apply_edit') }))
    await waitFor(() => expect(api.applyConfigPending).toHaveBeenCalledWith('config.json', 'sig-1'))
  })

  it('cancelling the Apply confirm applies nothing', async () => {
    await renderPanel(ONE)
    fireEvent.click(applyBtn())
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: i18nT('components.confirmDialog.cancel') }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(api.applyConfigPending).not.toHaveBeenCalled()
    expect(screen.getByTestId('config-pending-card')).toBeInTheDocument()
  })

  it('an edit with several security lines lists each and confirms with the generic label', async () => {
    await renderPanel({
      ...ONE,
      pending: [{
        ...ONE.pending[0],
        changes: [
          { path: ['agent', 'sandbox'], change: 'changed', before: 'strict', after: 'off' },
          { path: ['agent', 'apps_allow_third_party'], change: 'changed', before: false, after: true },
        ],
      }],
    })
    mocked(api.applyConfigPending).mockResolvedValue(NONE)
    fireEvent.click(applyBtn())
    const dialog = await screen.findByRole('dialog')
    const lines = within(within(dialog).getByTestId('config-pending-apply-hints')).getAllByRole('listitem').map(li => li.textContent)
    expect(lines).toEqual([
      i18nT('pages.settings.securityPanel.configPending.hint_sandbox_off'),
      i18nT('pages.settings.securityPanel.configPending.hint_third_party_apps'),
    ])
    fireEvent.click(within(dialog).getByRole('button', { name: i18nT('pages.settings.securityPanel.configPending.apply_edit') }))
    await waitFor(() => expect(api.applyConfigPending).toHaveBeenCalledWith('config.json', 'sig-1'))
  })

  it('the confirmed Apply posts the version the owner saw, even if a newer one arrives while asking', async () => {
    mocked(api.configPending).mockResolvedValue(ONE)
    const { queryClient } = renderWithProviders(<SecurityPanel />, { route: '/?section=posture', queryDefaults: { retryDelay: 0 } })
    await screen.findByTestId('config-pending-card')
    fireEvent.click(applyBtn())
    const dialog = await screen.findByRole('dialog')
    const newer = { path: ['agent', 'model'], change: 'changed' as const, before: 'a', after: 'b' }
    act(() => {
      queryClient.setQueryData(CONFIG_PENDING_QUERY_KEY, {
        ...ONE, pending: [{ ...ONE.pending[0], signature: 'sig-2', changes: [...ONE.pending[0].changes, newer] }],
      })
    })
    // The card now draws the newer version behind the open confirm.
    await screen.findByText(i18nT('pages.settings.securityPanel.configPending.changed_value', { key: 'agent.model', before: '"a"', after: '"b"' }))
    mocked(api.applyConfigPending).mockResolvedValue(NONE)
    fireEvent.click(within(dialog).getByRole('button', { name: i18nT('pages.settings.securityPanel.configPending.apply_turn_off_sandbox') }))
    await waitFor(() => expect(api.applyConfigPending).toHaveBeenCalledWith('config.json', 'sig-1'))
  })

  it('Apply is disabled, with the reason, when the edited file is not valid JSON', async () => {
    await renderPanel({ ...ONE, pending: [{ ...ONE.pending[0], disk_valid: false, changes: [] }] })
    expect(applyBtn()).toBeDisabled()
    expect(applyBtn()).toHaveAccessibleDescription(i18nT('pages.settings.securityPanel.configPending.invalid_json'))
    expect(discardBtn()).toBeEnabled()
  })

  it('Discard is not styled as the dangerous choice, asks first, then posts the signature and clears', async () => {
    await renderPanel(ONE)
    expect(discardBtn()).not.toHaveClass(DANGER)
    mocked(api.discardConfigPending).mockResolvedValue(NONE)
    mocked(api.configPending).mockResolvedValue(NONE)
    fireEvent.click(discardBtn())
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent(i18nT('pages.settings.securityPanel.configPending.discard_body', { file: 'config.json' }))
    expect(api.discardConfigPending).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: i18nT('pages.settings.securityPanel.configPending.discard') }))
    await waitFor(() => expect(screen.queryByTestId('config-pending-card')).toBeNull())
    expect(api.discardConfigPending).toHaveBeenCalledWith('config.json', 'sig-1')
  })

  it('the bell note\'s highlight id scrolls to and rings the card once it mounts', async () => {
    mocked(api.configPending).mockResolvedValue(ONE)
    function Host() {
      useSettingHighlight()
      return <SecurityPanel />
    }
    // The gateway's note links to `/settings/security?highlight=security.config-pending`.
    renderWithProviders(<Host />, { route: '/?highlight=security.config-pending', queryDefaults: { retryDelay: 0 } })
    const card = await screen.findByTestId('config-pending-card')
    await waitFor(() => expect(card.style.outline).toContain('var(--accent)'))
  })

  it('cancelling the Discard confirm leaves the file and the card alone', async () => {
    await renderPanel(ONE)
    mocked(api.discardConfigPending).mockResolvedValue(NONE)
    fireEvent.click(discardBtn())
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: i18nT('components.confirmDialog.cancel') }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(api.discardConfigPending).not.toHaveBeenCalled()
    expect(screen.getByTestId('config-pending-card')).toBeInTheDocument()
  })

  it('a 409 write_refused says what to do first, shows the reason, and turns Apply off for that version', async () => {
    await renderPanel(ONE)
    const reason = 'allowed_apps must be a list of at most 64 strings, each at most 256 characters'
    mocked(api.applyConfigPending).mockRejectedValue(
      new ApiError(409, reason, JSON.stringify({ error: reason, code: 'write_refused' })),
    )
    await applyAndConfirm()
    const alert = await screen.findByRole('alert')
    const action = i18nT('pages.settings.securityPanel.configPending.apply_refused_action')
    expect(alert).toHaveTextContent(action)
    expect(alert).toHaveTextContent(reason)
    expect(alert.textContent!.indexOf(action)).toBeLessThan(alert.textContent!.indexOf(reason))
    expect(screen.getByTestId('config-pending-card')).toBeInTheDocument()
    await waitFor(() => expect(applyBtn()).toBeDisabled())
    expect(discardBtn()).toBeEnabled()
  })

  it('Apply comes back once the refused file is edited again', async () => {
    await renderPanel(ONE)
    const reason = 'agent.env refuses it'
    mocked(api.applyConfigPending).mockRejectedValue(
      new ApiError(409, reason, JSON.stringify({ error: reason, code: 'write_refused' })),
    )
    mocked(api.configPending).mockResolvedValue({ ...ONE, pending: [{ ...ONE.pending[0], signature: 'sig-2' }] })
    await applyAndConfirm()
    await waitFor(() => expect(api.configPending).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(applyBtn()).toBeEnabled())
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('marks a change that loosens confinement in words, and leaves an unknown key plain', async () => {
    await renderPanel({
      ...ONE,
      pending: [{
        ...ONE.pending[0],
        changes: [
          { path: ['agent', 'sandbox'], change: 'changed', before: 'auto', after: 'off' },
          { path: ['ui', 'theme'], change: 'changed', before: 'light', after: 'dark' },
        ],
      }],
    })
    const [sandbox, theme] = within(screen.getByTestId('config-pending-card')).getAllByRole('listitem')
    expect(within(sandbox).getByTestId('config-pending-security-hint'))
      .toHaveTextContent(i18nT('pages.settings.securityPanel.configPending.hint_sandbox_off'))
    expect(within(theme).queryByTestId('config-pending-security-hint')).toBeNull()
  })

  it('a deleted file says so, cannot be applied, and can be discarded', async () => {
    await renderPanel({
      ...ONE,
      pending: [{ ...ONE.pending[0], signature: 'removed', removed: true, disk_valid: false, changes: [] }],
    })
    expect(applyBtn()).toBeDisabled()
    expect(applyBtn()).toHaveAccessibleDescription(i18nT('pages.settings.securityPanel.configPending.removed_file'))
    expect(discardBtn()).toBeEnabled()
  })

  it('a failed read is shown, and the owner-only refusal is not', async () => {
    mocked(api.configPending).mockRejectedValue(new ApiError(500, 'boom', JSON.stringify({ error: 'boom', code: 'internal' })))
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture', queryDefaults: { retryDelay: 0 } })
    expect(await screen.findByTestId('config-pending-read-error'))
      .toHaveTextContent(i18nT('pages.settings.securityPanel.configPending.read_failed'))
    cleanup()
    mocked(api.configPending).mockClear()
    mocked(api.configPending).mockRejectedValue(new ApiError(403, 'owner only', JSON.stringify({ error: 'owner only', code: 'owner_only' })))
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture', queryDefaults: { retryDelay: 0 } })
    await waitFor(() => expect(api.configPending).toHaveBeenCalled())
    await screen.findAllByRole('option')
    expect(screen.queryByTestId('config-pending-read-error')).toBeNull()
  })

  it('a transport failure before any read succeeded is shown, with a retry', async () => {
    // jfetch rethrows the fetch's own TypeError; the socket can stay connected, so
    // no offline state explains the missing card.
    mocked(api.configPending).mockRejectedValue(new TypeError('Failed to fetch'))
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture', queryDefaults: { retryDelay: 0 } })
    expect(await screen.findByTestId('config-pending-read-error'))
      .toHaveTextContent(i18nT('pages.settings.securityPanel.configPending.read_failed'))
    mocked(api.configPending).mockResolvedValue(ONE)
    fireEvent.click(screen.getByTestId('config-pending-read-retry'))
    await screen.findByTestId('config-pending-card')
    expect(screen.queryByTestId('config-pending-read-error')).toBeNull()
  })

  it('a transport failure after a good read is shown above the cards it kept', async () => {
    mocked(api.configPending).mockResolvedValue(ONE)
    const { queryClient } = renderWithProviders(<SecurityPanel />, { route: '/?section=posture', queryDefaults: { retryDelay: 0 } })
    await screen.findByTestId('config-pending-card')
    mocked(api.configPending).mockRejectedValue(new TypeError('Failed to fetch'))
    await act(async () => { await queryClient.invalidateQueries({ queryKey: CONFIG_PENDING_QUERY_KEY }) })
    expect(await screen.findByTestId('config-pending-read-error'))
      .toHaveTextContent(i18nT('pages.settings.securityPanel.configPending.read_failed'))
    expect(screen.getByTestId('config-pending-card')).toBeInTheDocument()
  })

  it('the Japanese Apply button reads 反映', async () => {
    const { default: ja } = await import('../../i18n/locales/ja.json')
    expect(ja.pages.settings.securityPanel.configPending.apply).toBe('反映')
  })

  it('a 409 pending_changed re-reads and shows the newer edit', async () => {
    await renderPanel(ONE)
    const NEWER: ConfigPendingData = {
      ...ONE,
      pending: [{ ...ONE.pending[0], signature: 'sig-2', changes: [{ path: ['agent', 'model'], change: 'changed', before: 'a', after: 'b' }] }],
    }
    mocked(api.applyConfigPending).mockRejectedValue(
      new ApiError(409, 'the file changed since it was reviewed', JSON.stringify({ error: 'the file changed since it was reviewed', code: 'pending_changed' })),
    )
    mocked(api.configPending).mockResolvedValue(NEWER)
    const readsBefore = mocked(api.configPending).mock.calls.length
    await applyAndConfirm()
    await screen.findByText(i18nT('pages.settings.securityPanel.configPending.pending_changed'))
    await screen.findByText(i18nT('pages.settings.securityPanel.configPending.changed_value', { key: 'agent.model', before: '"a"', after: '"b"' }))
    expect(mocked(api.configPending).mock.calls.length).toBeGreaterThan(readsBefore)
  })

  it('removing a sandbox override, or the section holding it, asks first', () => {
    expect(securityHints('config.local.json', { path: ['agent', 'sandbox'], change: 'removed', before: 'strict' }))
      .toEqual([i18nT('pages.settings.securityPanel.configPending.hint_sandbox_changed')])
    expect(securityHints('config.local.json', { path: ['agent'], change: 'removed' }))
      .toContain(i18nT('pages.settings.securityPanel.configPending.hint_section'))
  })

  it.each(['dangerously_skip_permissions', 'dangerouslySkipPermissions', 'yolo'])(
    'any change to the approve-every-tool grant agent.%s asks first',
    name => {
      for (const change of [
        { path: ['agent', name], change: 'added' as const, after: true },
        { path: ['agent', name], change: 'removed' as const, before: false },
      ]) {
        expect(securityHints('config.json', change)).toEqual([
          i18nT('pages.settings.securityPanel.configPending.hint_auto_approve'),
        ])
      }
    },
  )
})
