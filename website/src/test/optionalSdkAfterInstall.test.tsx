import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { TeamsPanel } from '../pages/settings/TeamsPanel'
import { WhatsAppPanel } from '../pages/settings/WhatsAppPanel'
import { FeishuPanel } from '../pages/settings/FeishuPanel'

// The three Settings panels around an optional channel SDK, in the states a
// user walks through: missing (the card owns the fix), and installed but not
// yet loaded (connect_error and jwt_available still describe the last start,
// so the panel must name the restart). No state shows a bare `pip install`.

const get = vi.hoisted(() => ({ teams: vi.fn(), whatsapp: vi.fn(), feishu: vi.fn() }))

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      getTeamsConfig: () => get.teams(),
      getWhatsAppConfig: () => get.whatsapp(),
      getFeishuConfig: () => get.feishu(),
    },
  }
})

// What the gateway writes into connect_error when the SDK is missing at start
// (extras.missing_at_start_status, via whatsapp/gateway.py and feishu/gateway.py).
const WA_STATUS = 'The gateway started without neonize. Restart it once neonize is installed.'
const FS_STATUS = 'The gateway started without lark-oapi. Restart it once lark-oapi is installed.'

const card = (cmd: string) => ({ sdk_installed: false, sdk_install_supported: true, sdk_install_command: cmd })
// After `pip install` into the gateway's Python, before a gateway restart:
// find_spec now finds the package, so the config GET reports it installed.
const installed = { sdk_installed: true, sdk_install_supported: true, sdk_install_command: '' }

const TEAMS = {
  connected: false, connect_error: '', configured: true, read_only: false,
  app_id_set: true, app_password_set: true, enabled: true, tenant_id: '',
  allowed_emails: [], soft_threshold_pct: 80, hard_threshold_pct: 95,
  jwt_available: false, // HAS_JWT is fixed when teams.client is first imported
}
const WHATSAPP = {
  configured: false, connected: false, connect_error: WA_STATUS, read_only: false,
  enabled: true, dm_policy: 'self', allowed_wa_ids: [], groups: [], session_folder: '',
  state: 'unpaired',
}
const FEISHU = {
  connected: false, connect_error: FS_STATUS, configured: true, read_only: false,
  bot_token_set: true, bot_token_preview: 'AbC', bot_id_set: true, bot_id_preview: 'cli',
  enabled: true, allowed_user_ids: ['ou_c99cbd8a1b2c3d4e5f6a7b8c9d0e1f2a'],
  allow_group: false, allowed_group_ids: [], soft_threshold_pct: 80,
}

const text = () => document.body.textContent ?? ''

beforeEach(() => {
  get.teams.mockReset()
  get.whatsapp.mockReset()
  get.feishu.mockReset()
})

describe('Teams', () => {
  it('before install: the card gives the command and a restart step', async () => {
    get.teams.mockResolvedValue({ ...TEAMS, ...card('/gw/bin/python -m pip install PyJWT') })
    renderWithProviders(<TeamsPanel />)
    expect(await screen.findByTestId('sdk-install-command')).toBeInTheDocument()
    expect(text()).toMatch(/restart the gateway/i)
  })

  it('after install, before restart: the alert says to restart the gateway', async () => {
    get.teams.mockResolvedValue({ ...TEAMS, ...installed })
    renderWithProviders(<TeamsPanel />)
    const notice = (await screen.findByText(/needs the PyJWT dependency/)).closest('[role="alert"]')
    expect(notice).toHaveTextContent(/restart the gateway/i)
    expect(screen.queryByTestId('sdk-install-command')).toBeNull()
    expect(text()).not.toContain('pip install')
  })
})

describe('WhatsApp', () => {
  it('before install: the gateway note is hidden behind the card', async () => {
    get.whatsapp.mockResolvedValue({ ...WHATSAPP, ...card('/gw/bin/python -m pip install neonize') })
    renderWithProviders(<WhatsAppPanel />)
    expect(await screen.findByTestId('sdk-install-command')).toBeInTheDocument()
    expect(text()).not.toContain('started without neonize')
    expect(text().replace('/gw/bin/python -m pip install neonize', '')).not.toContain('pip install')
  })

  it('after install, before restart: names the restart, no bare pip line', async () => {
    get.whatsapp.mockResolvedValue({ ...WHATSAPP, ...installed })
    renderWithProviders(<WhatsAppPanel />)
    await waitFor(() => expect(text()).toContain('did not connect'))
    expect(screen.getByTestId('whatsapp-connect-error')).toHaveTextContent(/Restart it once neonize is installed/)
    expect(text()).not.toContain('pip install')
  })
})

describe('Feishu', () => {
  it('no connect_error: the card adds no bare pip line', async () => {
    get.feishu.mockResolvedValue({ ...FEISHU, connect_error: '', ...card('/gw/bin/python -m pip install lark-oapi') })
    renderWithProviders(<FeishuPanel />)
    expect(await screen.findByTestId('sdk-install-command')).toBeInTheDocument()
    expect(text()).not.toMatch(/failed to start/)
  })

  it('before install: the startup failure stands aside for the card', async () => {
    get.feishu.mockResolvedValue({ ...FEISHU, ...card('/gw/bin/python -m pip install lark-oapi') })
    renderWithProviders(<FeishuPanel />)
    expect(await screen.findByTestId('sdk-install-command')).toBeInTheDocument()
    expect(text()).not.toMatch(/failed to start/)
    expect(text()).not.toContain('started without lark-oapi')
  })

  it('no install channel: only the unsupported notice, no startup failure', async () => {
    get.feishu.mockResolvedValue({ ...FEISHU, sdk_installed: false, sdk_install_supported: false, sdk_install_command: '' })
    renderWithProviders(<FeishuPanel />)
    await screen.findByText(/cannot install extra packages/i)
    expect(text()).not.toMatch(/failed to start/)
    expect(text()).not.toContain("pip install 'lark-oapi")
  })

  it('after install, before restart: names the restart, no bare pip line', async () => {
    get.feishu.mockResolvedValue({ ...FEISHU, ...installed })
    renderWithProviders(<FeishuPanel />)
    await waitFor(() => expect(text()).toContain('Restart it once lark-oapi is installed'))
    expect(screen.queryByTestId('sdk-install-command')).toBeNull()
    // The guide prose quotes the package with ", outside this report's scope;
    // the gateway's old hint quoted it with '.
    expect(text()).not.toContain("pip install 'lark-oapi")
  })
})
