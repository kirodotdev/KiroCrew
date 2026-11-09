// "Install from Kiro Space": paste a reference, review what the gateway staged,
// then Install (confirmed: true) or leave (confirmed: false, so staging is
// discarded). `i18nT` is mocked to `key {params}` so assertions pin the key,
// not any locale's copy.
import { useRef, useState } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

const { prepare, confirm } = vi.hoisted(() => ({ prepare: vi.fn(), confirm: vi.fn() }))

vi.mock('../../api/client', () => ({
  api: {
    marketInstallPrepare: (...a: unknown[]) => prepare(...a),
    marketInstallConfirm: (...a: unknown[]) => confirm(...a),
  },
}))

vi.mock('../../i18n/t', async importOriginal => {
  const orig = await importOriginal<typeof import('../../i18n/t')>()
  return {
    ...orig,
    i18nT: (key: string, params?: Record<string, unknown>) =>
      params ? `${key} ${JSON.stringify(params)}` : key,
  }
})

import { ApiError } from '../../api/apiError'
import InstallFromSpaceDialog from './InstallFromSpaceDialog'

const K = 'components.appstore.installFromSpace.'
const STAGING_ID = 'abcdefghijklmnopqrstuv'

const CONFIRM = {
  app: { ref: 'acme/notes', version: 3, appVersion: '1.2.0', publisher: 'acme-dev', ownerGroup: 'acme' },
  components: [{ kind: 'skill', name: 'note-taker' }, { kind: 'agent', name: 'scribe' }],
  dependencies: [],
  permissions: {
    notice: 'declared by the publisher, not enforced',
    commands: ['git status'],
    network: ['api.example.com'],
    files: ['~/notes'],
    background: ['cron: nightly'],
    mcpServers: ['notes-mcp'],
    agents: ['scribe'],
  },
  size: 2048,
  trustLabel: 'Community, not reviewed by Kiro Space',
}

const refused = (status: number, code: string) =>
  new ApiError(status, code, JSON.stringify({ ok: false, error: 'x', code }))

/** The page's role, reduced: a trigger that opens the dialog and gets focus back. */
function Harness({ onInstalled }: { onInstalled: (name: string) => void }) {
  const [open, setOpen] = useState(false)
  const opener = useRef<HTMLElement | null>(null)
  return (
    <>
      <button type="button" onClick={(e) => { opener.current = e.currentTarget; setOpen(true) }}>
        open-dialog
      </button>
      {open && (
        <InstallFromSpaceDialog
          returnFocusTo={opener}
          onClose={() => setOpen(false)}
          onInstalled={onInstalled}
        />
      )}
    </>
  )
}

async function openAndReview(user: ReturnType<typeof userEvent.setup>, ref = 'acme/notes@3') {
  await user.click(screen.getByText('open-dialog'))
  await user.type(screen.getByRole('textbox'), ref)
  await user.click(screen.getByRole('button', { name: K + 'review' }))
}

beforeEach(() => {
  prepare.mockReset()
  confirm.mockReset()
  prepare.mockResolvedValue({ ok: true, stagingId: STAGING_ID, confirm: CONFIRM })
  confirm.mockResolvedValue({ ok: true, name: 'notes' })
})
afterEach(cleanup)

describe('InstallFromSpaceDialog', () => {
  it('refuses a malformed reference without calling the gateway', async () => {
    const user = userEvent.setup()
    render(<Harness onInstalled={() => {}} />)
    await user.click(screen.getByText('open-dialog'))
    await user.type(screen.getByRole('textbox'), 'Not A Ref')
    await user.click(screen.getByRole('button', { name: K + 'review' }))
    expect(screen.getByText(K + 'ref_invalid')).toBeTruthy()
    expect(prepare).not.toHaveBeenCalled()
  })

  it('sends the ref and the version it read from <name>@<version>', async () => {
    const user = userEvent.setup()
    render(<Harness onInstalled={() => {}} />)
    await openAndReview(user)
    await waitFor(() => expect(prepare).toHaveBeenCalledWith('acme/notes', 3))
  })

  it('shows the confirm data with every permission group and the notice', async () => {
    const user = userEvent.setup()
    render(<Harness onInstalled={() => {}} />)
    await openAndReview(user)
    await screen.findByText('acme-dev')
    for (const text of [
      'acme/notes', 'acme-dev', CONFIRM.trustLabel, CONFIRM.permissions.notice,
      'note-taker', 'scribe', 'git status', 'api.example.com', '~/notes',
      'cron: nightly', 'notes-mcp',
    ]) {
      expect(screen.getAllByText(new RegExp(text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))).length).toBeGreaterThan(0)
    }
    for (const group of ['commands', 'network', 'files', 'background', 'mcpServers', 'agents']) {
      expect(screen.getByText(K + 'perm_' + group)).toBeTruthy()
    }
    expect(screen.getByText(/\b2(\.0)?\s*kB/i)).toBeTruthy()
  })

  it('says None for an empty permission group and for no dependencies', async () => {
    prepare.mockResolvedValue({
      ok: true,
      stagingId: STAGING_ID,
      confirm: {
        ...CONFIRM,
        permissions: { notice: CONFIRM.permissions.notice, commands: [], network: [], files: [], background: [], mcpServers: [], agents: [] },
      },
    })
    const user = userEvent.setup()
    render(<Harness onInstalled={() => {}} />)
    await openAndReview(user)
    await screen.findByText('acme-dev')
    // six permission groups + dependencies
    expect(screen.getAllByText(K + 'none')).toHaveLength(7)
  })

  it('Install posts confirmed:true, reports the app, and closes without discarding', async () => {
    const onInstalled = vi.fn()
    const user = userEvent.setup()
    render(<Harness onInstalled={onInstalled} />)
    await openAndReview(user)
    await user.click(await screen.findByRole('button', { name: K + 'install' }))
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1))
    expect(confirm).toHaveBeenCalledWith(STAGING_ID, true)
    await waitFor(() => expect(onInstalled).toHaveBeenCalledWith('notes'))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('Cancel posts confirmed:false', async () => {
    const user = userEvent.setup()
    render(<Harness onInstalled={() => {}} />)
    await openAndReview(user)
    await user.click(await screen.findByRole('button', { name: K + 'cancel' }))
    await waitFor(() => expect(confirm).toHaveBeenCalledWith(STAGING_ID, false))
    expect(confirm).not.toHaveBeenCalledWith(STAGING_ID, true)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('Escape on the review step posts confirmed:false', async () => {
    const user = userEvent.setup()
    render(<Harness onInstalled={() => {}} />)
    await openAndReview(user)
    await screen.findByRole('button', { name: K + 'install' })
    await user.keyboard('{Escape}')
    await waitFor(() => expect(confirm).toHaveBeenCalledWith(STAGING_ID, false))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('Escape on the first step closes without any request', async () => {
    const user = userEvent.setup()
    render(<Harness onInstalled={() => {}} />)
    await user.click(screen.getByText('open-dialog'))
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(prepare).not.toHaveBeenCalled()
    expect(confirm).not.toHaveBeenCalled()
  })

  it.each([
    [400, 'invalid_json'],
    [400, 'market_resolve_invalid'],
    [400, 'market_staging_id_invalid'],
    [400, 'market_confirm_invalid'],
    [404, 'market_staging_unknown'],
    [502, 'market_unavailable'],
    [503, 'market_client_unavailable'],
  ])('renders one sentence for %i %s', async (status, code) => {
    prepare.mockRejectedValue(refused(status, code))
    const user = userEvent.setup()
    render(<Harness onInstalled={() => {}} />)
    await openAndReview(user)
    expect(await screen.findByText(K + 'error_' + code)).toBeTruthy()
  })

  it('renders the generic sentence for an install refusal it has no line for', async () => {
    prepare.mockRejectedValue(refused(400, 'market_component_mismatch'))
    const user = userEvent.setup()
    render(<Harness onInstalled={() => {}} />)
    await openAndReview(user)
    expect(await screen.findByText(K + 'error_generic')).toBeTruthy()
  })

  it('says why an install was refused and goes back to the reference step', async () => {
    confirm.mockRejectedValueOnce(refused(404, 'market_staging_unknown'))
    const user = userEvent.setup()
    render(<Harness onInstalled={() => {}} />)
    await openAndReview(user)
    await user.click(await screen.findByRole('button', { name: K + 'install' }))
    expect(await screen.findByText(K + 'error_market_staging_unknown')).toBeTruthy()
  })

  it('returns focus to the trigger when the dialog closes', async () => {
    const user = userEvent.setup()
    render(<Harness onInstalled={() => {}} />)
    const trigger = screen.getByText('open-dialog')
    await user.click(trigger)
    expect(screen.getByRole('dialog')).toBeTruthy()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(document.activeElement).toBe(trigger))
  })
})
