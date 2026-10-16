/**
 * Settings ▸ Slack ▸ Slack Code: the agent-sessions and code-channels switches,
 * the code-channel defaults, and the "Update your Slack app" panel.
 *
 * Seams: the Slack config/manifest/status calls and the folder and agent lists
 * are spied on the real `api` object; the global fetch stub answers the schema
 * read the restart badges make.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders } from './helpers'
import { api, type SlackConfigData, type SlackManifestStatus } from '../api/client'
import { SlackPanel } from '../pages/settings/SlackPanel'
import { slackCodePayload, slackCodeDraftFrom } from '../pages/settings/SlackCodePanel'

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true,
    json: async () => ({ entries: [
      { path: 'slack.agent_sessions', type: 'boolean', requiresRestart: true },
      { path: 'slack.code_channels', type: 'boolean', requiresRestart: true },
    ] }),
  })) as unknown as typeof fetch)
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

const FOLDERS = [
  { id: 'aaaaaaaaaaaa', name: 'Dev', order: 0, project_dir: '/repo/app' },
  { id: 'bbbbbbbbbbbb', name: 'Slack Code', order: 1, parent_id: 'aaaaaaaaaaaa', default_agent: 'builder' },
]

const NO_CHANGE = { added_scopes: [], removed_scopes: [], added_events: [], removed_events: [], added_features: [], removed_features: [] }
const CHANGES = {
  agent_sessions: { ...NO_CHANGE, added_features: ['agent_view'], added_scopes: ['assistant:write'], added_events: ['agent_session_stopped'] },
  code_channels: { ...NO_CHANGE, added_features: ['code_channels'], added_scopes: ['canvases:write', 'chat:write.customize', 'code_channels:manage'], added_events: ['agent_session_stopped'] },
}

function status(install: Partial<SlackManifestStatus['install']> = {}, over: Partial<SlackManifestStatus> = {}): SlackManifestStatus {
  return {
    features: ['code_channels'],
    required_scopes: [],
    required_events: [],
    required_features: [],
    feature_changes: CHANGES,
    install: { status: 'up_to_date', missing_scopes: [], extra_scopes: [], disabled_features: [], app_id: 'A0123ABCD', team_id: 'T1', reason: '', ...install },
    app_manifest_url: 'https://api.slack.com/apps/A0123ABCD/app-manifest',
    ...over,
  }
}

function config(over: Partial<SlackConfigData> = {}): SlackConfigData {
  return {
    connected: true, connect_error: '', configured: true, read_only: false,
    bot_token_set: true, app_token_set: true, bot_token_preview: 'xoxb-••••', app_token_preview: 'xapp-••••',
    owner_id: 'U000OWNER', command: 'kirocrew', allowed_enterprise_ids: [],
    reactions_enabled: true, show_thinking: true,
    agent_sessions: false, code_channels: false,
    code_channel_defaults: { folder: null, agent: null, model: null, reasoning_effort: null },
    code_channel_effective: {
      folder: null, agent: 'kirocrew', agent_source: 'slack', model: null, reasoning_effort: null,
      project_dir: null, project_source: '', slack_agent: 'kirocrew',
    },
    code_channel_warnings: [],
    ...over,
  }
}

function seed(cfg: Partial<SlackConfigData> = {}, st: SlackManifestStatus = status()) {
  vi.spyOn(api, 'getSlackConfig').mockResolvedValue(config(cfg))
  vi.spyOn(api, 'getSlackManifest').mockResolvedValue({ alias: 'kirocrew', manifest: 'display_information: {}\n', create_url: 'https://example.invalid/new' })
  const getStatus = vi.spyOn(api, 'getSlackManifestStatus').mockResolvedValue(st)
  vi.spyOn(api, 'chatFolders').mockResolvedValue(FOLDERS as never)
  vi.spyOn(api, 'agentsInstalled').mockResolvedValue([{ name: 'builder' }, { name: 'reviewer' }])
  const save = vi.spyOn(api, 'saveSlackConfig').mockResolvedValue({ ok: true, restart_required: true, verify_warning: '' })
  renderWithProviders(<SlackPanel />)
  return { save, getStatus }
}

const hydrated = () => screen.findByRole('heading', { name: 'Slack', level: 3 })
const saveBtn = () => screen.getByRole('button', { name: 'Save Slack settings' })

describe('Slack Code payload', () => {
  it('sends only what changed, so an unrelated save never re-validates a default', () => {
    const saved = slackCodeDraftFrom(config({ code_channel_defaults: { folder: 'aaaaaaaaaaaa', agent: null, model: 'gone-model', reasoning_effort: null } }))
    expect(slackCodePayload(saved, saved)).toEqual({})
    expect(slackCodePayload({ ...saved, effort: 'high' }, saved)).toEqual({ code_channel_defaults: { reasoning_effort: 'high' } })
    expect(slackCodePayload({ ...saved, folder: '', code_channels: true }, saved)).toEqual({
      code_channels: true,
      code_channel_defaults: { folder: null },
    })
  })

  it('stores the inherit model as unset', () => {
    const saved = slackCodeDraftFrom(config({ code_channel_defaults: { folder: null, agent: null, model: 'claude-x', reasoning_effort: null } }))
    expect(slackCodePayload({ ...saved, model: 'auto' }, saved)).toEqual({ code_channel_defaults: { model: null } })
  })
})

describe('Code channels card', () => {
  it('reveals the defaults, previews the folder resolution and saves the picks', async () => {
    const { save } = seed()
    await hydrated()
    expect(screen.queryByTestId('slack-code-defaults')).toBeNull()

    fireEvent.click(screen.getByRole('switch', { name: 'Code channels' }))
    const card = await screen.findByTestId('slack-code-defaults')
    await userEvent.click(within(card).getByLabelText('Default folder'))
    await userEvent.click(await screen.findByRole('option', { name: /Slack Code/ }))

    const preview = screen.getByTestId('slack-code-preview')
    expect(preview).toHaveTextContent('/repo/app')   // inherited from the parent folder
    expect(preview).toHaveTextContent('builder')     // the folder's default agent
    expect(preview).toHaveTextContent('Folder default')

    fireEvent.click(saveBtn())
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({
      code_channels: true,
      code_channel_defaults: { folder: 'bbbbbbbbbbbb' },
    })
    expect(save.mock.calls[0][0]).not.toHaveProperty('agent_sessions')
  })

  it('explains a saved folder that was deleted since', async () => {
    seed({
      code_channels: true,
      code_channel_defaults: { folder: '999999999999', agent: null, model: null, reasoning_effort: null },
      code_channel_warnings: ['folder_missing'],
    })
    expect(await screen.findByText(/The saved folder no longer exists/)).toBeInTheDocument()
  })

  it('explains a saved agent that was removed since', async () => {
    seed({
      code_channels: true,
      code_channel_defaults: { folder: null, agent: 'ghost', model: null, reasoning_effort: null },
      code_channel_warnings: ['agent_missing'],
    })
    expect(await screen.findByText(/“ghost” is no longer installed/)).toBeInTheDocument()
  })
})

describe('Agent sessions switch', () => {
  it('asks before turning on and says it cannot be undone in Slack', async () => {
    const { save } = seed()
    await hydrated()
    fireEvent.click(screen.getByRole('switch', { name: 'Agent sessions' }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent(/cannot switch an app back/)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Agent sessions' })).not.toBeChecked())

    fireEvent.click(screen.getByRole('switch', { name: 'Agent sessions' }))
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Turn on agent sessions' }))
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Agent sessions' })).toBeChecked())
    fireEvent.click(saveBtn())
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({ agent_sessions: true })
  })

  it('turns off without a prompt', async () => {
    seed({ agent_sessions: true })
    await hydrated()
    fireEvent.click(screen.getByRole('switch', { name: 'Agent sessions' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByRole('switch', { name: 'Agent sessions' })).not.toBeChecked()
  })
})

describe('Update your Slack app', () => {
  it('stays hidden while no switch is on and nothing is left over', async () => {
    seed({}, status({}, { features: [] }))
    await hydrated()
    await waitFor(() => expect(api.getSlackManifestStatus).toHaveBeenCalled())
    expect(screen.queryByTestId('slack-manifest-update')).toBeNull()
  })

  it('lists what code channels add, marks the missing scopes and gives the steps', async () => {
    seed({ code_channels: true }, status({ status: 'needs_update', missing_scopes: ['code_channels:manage'] }))
    const panel = await screen.findByTestId('slack-manifest-update')
    expect(within(panel).getByTestId('slack-manifest-status')).toHaveAttribute('data-status', 'needs_update')
    expect(panel).toHaveTextContent('features.code_channels')
    expect(panel).toHaveTextContent('code_channels:manage· missing')
    expect(panel).toHaveTextContent('reinstall the app')
    expect(within(panel).getByRole('link', { name: /Open App Manifest/ }))
      .toHaveAttribute('href', 'https://api.slack.com/apps/A0123ABCD/app-manifest')
  })

  it('reads Up to date after a reinstall, via Check again', async () => {
    const { getStatus } = seed({ code_channels: true }, status({ status: 'needs_update', missing_scopes: ['code_channels:manage'] }))
    const panel = await screen.findByTestId('slack-manifest-update')
    getStatus.mockResolvedValue(status())
    fireEvent.click(within(panel).getByRole('button', { name: /Check again/ }))
    await waitFor(() => expect(getStatus).toHaveBeenLastCalledWith(true))
    await waitFor(() => expect(within(panel).getByTestId('slack-manifest-status')).toHaveAttribute('data-status', 'up_to_date'))
    expect(panel).toHaveTextContent('Nothing to update.')
  })

  it('names a workspace that does not have code channels enabled', async () => {
    seed({ code_channels: true }, status({ status: 'feature_disabled', disabled_features: ['code_channels'] }))
    const panel = await screen.findByTestId('slack-manifest-update')
    expect(within(panel).getByTestId('slack-manifest-status')).toHaveTextContent('Workspace not enabled')
    expect(panel).toHaveTextContent('does not have Code channels enabled')
    // Reinstalling does not enable a workspace feature, so no update steps.
    expect(panel).not.toHaveTextContent('reinstall the app')
  })

  it('shows a rejected bot token as an error, with no update steps', async () => {
    seed({ code_channels: true }, status({ status: 'token_rejected', reason: 'invalid_auth' }))
    const panel = await screen.findByTestId('slack-manifest-update')
    expect(within(panel).getByTestId('slack-manifest-status')).toHaveTextContent('Bot token rejected')
    expect(within(panel).getByText(/Slack rejected the saved bot token/)).toBeInTheDocument()
    expect(panel).not.toHaveTextContent('reinstall the app')
  })

  it('says when Slack would not report the scopes', async () => {
    seed({ code_channels: true }, status({ status: 'unknown', reason: 'scopes_unreadable' }))
    const panel = await screen.findByTestId('slack-manifest-update')
    expect(within(panel).getByTestId('slack-manifest-status')).toHaveTextContent('Cannot check')
  })

  it('previews an unsaved switch as pending', async () => {
    seed({}, status({}, { features: [] }))
    await hydrated()
    fireEvent.click(screen.getByRole('switch', { name: 'Code channels' }))
    expect(await screen.findByTestId('slack-manifest-pending')).toBeInTheDocument()
    expect(screen.getByTestId('slack-manifest-update')).toHaveTextContent('after you save')
  })
})

describe('Capability line under a saved toggle', () => {
  it('says the toggle is not running yet while the scope is missing', async () => {
    seed({
      code_channels: true,
      code_channels_capability: { available: false, reason: 'missing_scope', scope: 'code_channels:manage' },
    }, status({ status: 'needs_update', missing_scopes: ['code_channels:manage'] }))
    const line = await screen.findByTestId('slack-capability')
    expect(line).toHaveAttribute('data-available', 'false')
    expect(line).toHaveTextContent('code_channels:manage')
    expect(line).toHaveTextContent('Slack works as before')
  })

  it('says a workspace without the feature keeps working as before', async () => {
    seed({
      agent_sessions: true,
      agent_sessions_capability: { available: false, reason: 'feature_disabled', scope: 'assistant:write' },
    })
    const line = await screen.findByTestId('slack-capability')
    expect(line).toHaveAttribute('data-reason', 'feature_disabled')
    expect(line).toHaveTextContent('has not enabled this for your workspace')
  })

  it('reads Active once the app can use it', async () => {
    seed({
      agent_sessions: true,
      agent_sessions_capability: { available: true, reason: '', scope: 'assistant:write' },
    })
    expect(await screen.findByTestId('slack-capability')).toHaveTextContent('Active')
  })

  it('shows nothing under a toggle that is off', async () => {
    seed({ agent_sessions_capability: { available: false, reason: 'scopes_unknown', scope: 'assistant:write' } })
    await hydrated()
    expect(screen.queryByTestId('slack-capability')).toBeNull()
  })
})
