/**
 * Settings ▸ Slack setup wizard (`pages/settings/SlackSetupWizard.tsx`).
 *
 * The wizard writes through the same mocked `api` methods as the panel. Tokens
 * here are obvious placeholders: they only ever reach a mocked writer.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import { api, type SlackConfigData, type SlackManifestStatus } from '../api/client'
import { SlackSetupWizard, firstOpenStep, manifestAppName, safeGuideUrl, WIZARD_STEPS } from '../pages/settings/SlackSetupWizard'

afterEach(() => vi.restoreAllMocks())

function config(over: Partial<SlackConfigData> = {}): SlackConfigData {
  return {
    connected: false,
    connect_error: '',
    configured: false,
    read_only: false,
    bot_token_set: false,
    app_token_set: false,
    bot_token_preview: '',
    app_token_preview: '',
    owner_id: '',
    command: 'kirocrew',
    allowed_enterprise_ids: [],
    reactions_enabled: true,
    show_thinking: false,
    ...over,
  } as SlackConfigData
}

const STATUS = {
  features: [], required_scopes: [], required_events: [], required_features: [], feature_changes: {},
  install: { status: 'up_to_date', missing_scopes: [], extra_scopes: [], disabled_features: [], app_id: 'A0123ABCD', team_id: 'T0456EFGH', reason: '' },
  app_manifest_url: 'https://api.slack.com/apps/A0123ABCD/app-manifest',
  app_basic_info_url: 'https://api.slack.com/apps/A0123ABCD/general',
  app_dm_url: 'https://slack.com/app_redirect?app=A0123ABCD&team=T0456EFGH',
} as SlackManifestStatus

const OK_SAVE = { ok: true, restart_required: false, reconnect_required: true, verify_warning: '' }

function mount(c: SlackConfigData, orgGuideUrl?: string) {
  vi.spyOn(api, 'getSlackManifestStatus').mockResolvedValue(STATUS)
  const onSaved = vi.fn()
  renderWithProviders(
    <SlackSetupWizard data={c} onSaved={onSaved} orgGuideUrl={orgGuideUrl} describeConnectError={code => `E:${code}`} />,
  )
  return { onSaved }
}

const step = () => screen.getByTestId('slack-wizard').getAttribute('data-step')

const MANIFEST = { alias: 'jdoe', manifest: 'display_information:\n  name: Acme-jdoe\n', create_url: 'https://api.slack.com/apps?new_app=1', command: 'kirocrew-jdoe' }

/** From the first step through Create to the bot-token step. */
async function toBotTokenStep() {
  fireEvent.click(screen.getByRole('button', { name: /I have a workspace/ }))
  fireEvent.change(screen.getByTestId('slack-wizard-alias'), { target: { value: 'jdoe' } })
  const done = screen.getByRole('button', { name: /I created the app/ })
  await waitFor(() => expect((done as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(done)
  await waitFor(() => expect(step()).toBe('bot_token'))
}

describe('firstOpenStep', () => {
  it('starts at the first thing the saved config lacks', () => {
    expect(WIZARD_STEPS[firstOpenStep(config())]).toBe('workspace')
    expect(WIZARD_STEPS[firstOpenStep(config({ bot_token_set: true }))]).toBe('app_token')
    expect(WIZARD_STEPS[firstOpenStep(config({ bot_token_set: true, app_token_set: true }))]).toBe('owner')
    expect(WIZARD_STEPS[firstOpenStep(config({ bot_token_set: true, app_token_set: true, owner_id: 'U1', connected: true }))]).toBe('test')
  })
})

describe('manifestAppName', () => {
  it('reads the display name the manifest renders', () => {
    expect(manifestAppName('display_information:\n  name: Acme-jdoe\n  description: x\n')).toBe('Acme-jdoe')
    expect(manifestAppName(undefined)).toBe('')
  })
})

describe('safeGuideUrl', () => {
  it.each([
    ['https://wiki.example.com/slack', 'https://wiki.example.com/slack'],
    ['http://wiki.example.com/slack', null],
    ['javascript:alert(1)', null],
    ['https://user:pw@example.com/', null],
    ['not a url', null],
    ['', null],
  ])('%s -> %s', (raw, want) => {
    expect(safeGuideUrl(raw)).toBe(want)
  })
})

describe('SlackSetupWizard', () => {
  it('shows an https organization guide with a safe rel, and no other scheme', () => {
    mount(config(), 'https://wiki.example.com/slack')
    const link = screen.getByTestId('slack-wizard-org-guide')
    expect(link.getAttribute('href')).toBe('https://wiki.example.com/slack')
    expect(link.getAttribute('rel')).toBe('noopener noreferrer')
  })

  it('renders no organization guide for a non-https value', () => {
    mount(config(), 'http://wiki.example.com/slack')
    expect(screen.queryByTestId('slack-wizard-org-guide')).toBeNull()
  })

  it('draws a picture on every step', () => {
    mount(config())
    expect(screen.getByTestId('slack-wizard-diagram-workspace')).toBeTruthy()
  })

  it('refuses an alias the manifest cannot render, and fetches none', () => {
    const manifest = vi.spyOn(api, 'getSlackManifest')
    mount(config())
    fireEvent.click(screen.getByRole('button', { name: /I have a workspace/ }))
    fireEvent.change(screen.getByTestId('slack-wizard-alias'), { target: { value: 'bad/alias' } })
    expect(screen.getByTestId('slack-wizard-create').getAttribute('aria-disabled')).toBe('true')
    expect(manifest).not.toHaveBeenCalled()
  })

  it('saves the alias command on "I created the app", not on the link click, then advances', async () => {
    vi.spyOn(api, 'getSlackManifest').mockResolvedValue(MANIFEST)
    const save = vi.spyOn(api, 'saveSlackConfig').mockResolvedValue({ ...OK_SAVE, reconnect_required: false })
    const { onSaved } = mount(config())
    fireEvent.click(screen.getByRole('button', { name: /I have a workspace/ }))
    fireEvent.change(screen.getByTestId('slack-wizard-alias'), { target: { value: 'jdoe' } })
    await waitFor(() => expect(api.getSlackManifest).toHaveBeenCalledWith('jdoe'))
    const link = await screen.findByTestId('slack-wizard-create')
    await waitFor(() => expect(link.getAttribute('href')).toBe(MANIFEST.create_url))
    fireEvent.click(link)
    expect(save).not.toHaveBeenCalled()
    expect(await screen.findByText(/Acme-jdoe/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /I created the app/ }))
    await waitFor(() => expect(save).toHaveBeenCalledWith({ command: 'kirocrew-jdoe' }))
    await waitFor(() => expect(step()).toBe('bot_token'))
    expect(onSaved).toHaveBeenCalledWith({ command: 'kirocrew-jdoe' })
  })

  it('stays on Create when saving the command fails', async () => {
    vi.spyOn(api, 'getSlackManifest').mockResolvedValue(MANIFEST)
    vi.spyOn(api, 'saveSlackConfig').mockRejectedValue(new Error(JSON.stringify({ error: 'nope' })))
    mount(config())
    fireEvent.click(screen.getByRole('button', { name: /I have a workspace/ }))
    fireEvent.change(screen.getByTestId('slack-wizard-alias'), { target: { value: 'jdoe' } })
    const done = screen.getByRole('button', { name: /I created the app/ })
    await waitFor(() => expect((done as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(done)
    expect(await screen.findByText(/nope/)).toBeTruthy()
    expect(step()).toBe('create')
  })

  it('saves only the bot token, then moves to the app-level token step', async () => {
    vi.spyOn(api, 'getSlackManifest').mockResolvedValue(MANIFEST)
    const save = vi.spyOn(api, 'saveSlackConfig').mockResolvedValue(OK_SAVE)
    mount(config())
    await toBotTokenStep()
    fireEvent.change(screen.getByTestId('slack-wizard-bot-token'), { target: { value: 'xoxb-placeholder' } })
    fireEvent.click(screen.getByRole('button', { name: /Verify and save/ }))
    await waitFor(() => expect(save).toHaveBeenCalledWith({ bot_token: 'xoxb-placeholder' }))
    await waitFor(() => expect(step()).toBe('app_token'))
    await waitFor(() => expect(screen.getByTestId('slack-wizard-basic-info').getAttribute('href')).toBe(STATUS.app_basic_info_url))
  })

  it('stays on the step when Slack rejects the token', async () => {
    vi.spyOn(api, 'getSlackManifest').mockResolvedValue(MANIFEST)
    vi.spyOn(api, 'saveSlackConfig')
      .mockResolvedValueOnce({ ...OK_SAVE, reconnect_required: false })
      .mockRejectedValue(new Error(JSON.stringify({ error: 'bot_token rejected by Slack (invalid_auth)' })))
    mount(config({ bot_token_set: false }))
    await toBotTokenStep()
    fireEvent.change(screen.getByTestId('slack-wizard-bot-token'), { target: { value: 'xoxb-placeholder' } })
    fireEvent.click(screen.getByRole('button', { name: /Verify and save/ }))
    expect(await screen.findByText(/invalid_auth/)).toBeTruthy()
    expect(step()).toBe('bot_token')
  })

  it('verifies the app-level token without reconnecting, then asks for the owner', async () => {
    vi.spyOn(api, 'saveSlackConfig').mockResolvedValue(OK_SAVE)
    const reconnect = vi.spyOn(api, 'reconnectSlack')
    mount(config({ bot_token_set: true }))
    fireEvent.change(screen.getByTestId('slack-wizard-app-token'), { target: { value: 'xapp-placeholder' } })
    fireEvent.click(screen.getByRole('button', { name: /Verify and save/ }))
    await waitFor(() => expect(step()).toBe('owner'))
    expect(reconnect).not.toHaveBeenCalled()
  })

  it('saves the owner, reconnects with no restart, and moves to the test step', async () => {
    const save = vi.spyOn(api, 'saveSlackConfig').mockResolvedValue(OK_SAVE)
    vi.spyOn(api, 'reconnectSlack').mockResolvedValue({ connected: true, connect_error: '' })
    mount(config({ bot_token_set: true, app_token_set: true }))
    fireEvent.change(screen.getByTestId('slack-wizard-owner'), { target: { value: 'U0123ABC456' } })
    fireEvent.click(screen.getByRole('button', { name: /Save and connect/ }))
    await waitFor(() => expect(save).toHaveBeenCalledWith({ owner_id: 'U0123ABC456' }))
    await waitFor(() => expect(step()).toBe('test'))
    await waitFor(() => expect(screen.getByTestId('slack-wizard-dm').getAttribute('href')).toBe(STATUS.app_dm_url))
  })

  it('reports a refused reconnect in the panel’s words and stays put', async () => {
    vi.spyOn(api, 'saveSlackConfig').mockResolvedValue(OK_SAVE)
    vi.spyOn(api, 'reconnectSlack').mockResolvedValue({ connected: false, connect_error: 'invalid_auth' })
    mount(config({ bot_token_set: true, app_token_set: true }))
    fireEvent.change(screen.getByTestId('slack-wizard-owner'), { target: { value: 'U0123ABC456' } })
    fireEvent.click(screen.getByRole('button', { name: /Save and connect/ }))
    expect(await screen.findByText(/E:invalid_auth/)).toBeTruthy()
    expect(step()).toBe('owner')
  })

  it('finishes only when the user says the bot replied', async () => {
    mount(config({ bot_token_set: true, app_token_set: true, owner_id: 'U1', connected: true }))
    expect(screen.queryByText(/Slack is set up/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /I got a reply/ }))
    expect(await screen.findByText(/Slack is set up/)).toBeTruthy()
  })

  it('shows a read-only viewer every step, with nothing to type or save', () => {
    mount(config({ read_only: true }))
    for (const name of WIZARD_STEPS.slice(1)) {
      fireEvent.click(screen.getByTestId('slack-wizard-next'))
      expect(step()).toBe(name)
    }
    expect(screen.queryByTestId('slack-wizard-next')).toBeNull()
    expect(screen.queryByRole('button', { name: /Verify and save/ })).toBeNull()
    expect(screen.queryByTestId('slack-wizard-test-reconnect')).toBeNull()
  })

  it('offers status and Reconnect on the test step', async () => {
    vi.spyOn(api, 'reconnectSlack').mockResolvedValue({ connected: true, connect_error: '' })
    mount(config({ bot_token_set: true, app_token_set: true, owner_id: 'U1', connected: true }))
    expect(screen.getByTestId('slack-wizard-test-status').textContent).toMatch(/Connected/)
    fireEvent.click(screen.getByTestId('slack-wizard-test-reconnect'))
    await waitFor(() => expect(api.reconnectSlack).toHaveBeenCalled())
  })

  it('says the install reading failed instead of a silent dead link', async () => {
    vi.spyOn(api, 'getSlackManifestStatus').mockRejectedValue(new Error('boom'))
    renderWithProviders(<SlackSetupWizard data={config({ bot_token_set: true })} onSaved={vi.fn()} describeConnectError={c => c} />)
    expect(await screen.findByText(/Could not check the installed Slack app/)).toBeTruthy()
  })

  it('says so too when the reading answers but its Slack call failed', async () => {
    const failed = { ...STATUS, install: { ...STATUS.install, status: 'unknown', app_id: '', team_id: '', reason: 'TimeoutError' }, app_basic_info_url: null, app_dm_url: null }
    vi.spyOn(api, 'getSlackManifestStatus').mockResolvedValue(failed)
    renderWithProviders(<SlackSetupWizard data={config({ bot_token_set: true })} onSaved={vi.fn()} describeConnectError={c => c} />)
    expect(await screen.findByText(/Could not check the installed Slack app/)).toBeTruthy()
  })

  it('reports no failure when Slack answered without the scope header', async () => {
    const unread = { ...STATUS, install: { ...STATUS.install, status: 'unknown', reason: 'scopes_unreadable' } }
    const spy = vi.spyOn(api, 'getSlackManifestStatus').mockResolvedValue(unread)
    renderWithProviders(<SlackSetupWizard data={config({ bot_token_set: true })} onSaved={vi.fn()} describeConnectError={c => c} />)
    await waitFor(() => expect(spy).toHaveBeenCalled())
    expect(await screen.findByTestId('slack-wizard-basic-info')).toBeTruthy()
    expect(screen.queryByText(/Could not check the installed Slack app/)).toBeNull()
  })
})

describe('SlackSetupWizard owner claim', () => {
  it('shows the claim control on the owner step and moves on once a claim connects', async () => {
    vi.spyOn(api, 'getSlackManifestStatus').mockResolvedValue(STATUS)
    let claimed: (id: string) => void = () => {}
    const props = {
      onSaved: vi.fn(),
      describeConnectError: (c: string) => c,
      renderOwnerClaim: (cb: (id: string) => void) => { claimed = cb; return <span>claim-here</span> },
    }
    const { rerender } = renderWithProviders(
      <SlackSetupWizard data={config({ bot_token_set: true, app_token_set: true })} {...props} />,
    )
    expect(screen.getByTestId('slack-wizard-owner-claim').textContent).toBe('claim-here')
    expect(screen.getByText(/Or paste your member ID by hand/)).toBeTruthy()
    expect(step()).toBe('owner')
    claimed('U0123ABC456')
    rerender(
      <SlackSetupWizard data={config({ bot_token_set: true, app_token_set: true, owner_id: 'U0123ABC456', connected: true })} {...props} />,
    )
    await waitFor(() => expect(step()).toBe('test'))
  })

  it('stays on the owner step when Back leads there on a connected install', () => {
    vi.spyOn(api, 'getSlackManifestStatus').mockResolvedValue(STATUS)
    renderWithProviders(
      <SlackSetupWizard data={config({ bot_token_set: true, app_token_set: true, owner_id: 'U1', connected: true })}
        onSaved={vi.fn()} describeConnectError={c => c} renderOwnerClaim={() => <span />} />,
    )
    expect(step()).toBe('test')
    fireEvent.click(screen.getByRole('button', { name: /Back/ }))
    expect(step()).toBe('owner')
  })
})
