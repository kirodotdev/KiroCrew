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
import { markModelsDegraded } from '../providers/modelListHealth'

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true,
    json: async () => ({ entries: [
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
    code_channels: false,
    code_channel_defaults: { folder: null, agent: null, model: null, reasoning_effort: null },
    code_channel_effective: {
      folder: null, agent: 'kirocrew', agent_source: 'slack', model: null, reasoning_effort: null,
      project_dir: null, project_source: '', slack_agent: 'kirocrew',
    },
    code_channel_warnings: [],
    code_channel_auto_folder: { name: 'Code channels', agent: 'kirocrew-conductor', id: null },
    code_channel_agents: ['builder', 'reviewer'],
    ...over,
  }
}

function seed(cfg: Partial<SlackConfigData> = {}, st: SlackManifestStatus = status(), folders: unknown[] = FOLDERS) {
  vi.spyOn(api, 'getSlackConfig').mockResolvedValue(config(cfg))
  vi.spyOn(api, 'getSlackManifest').mockResolvedValue({ alias: 'kirocrew', manifest: 'display_information: {}\n', create_url: 'https://example.invalid/new' })
  const getStatus = vi.spyOn(api, 'getSlackManifestStatus').mockResolvedValue(st)
  vi.spyOn(api, 'chatFolders').mockResolvedValue(folders as never)
  vi.spyOn(api, 'chatTags').mockResolvedValue([] as never)
  const save = vi.spyOn(api, 'saveSlackConfig').mockResolvedValue({ ok: true, restart_required: true, verify_warning: '' })
  renderWithProviders(<SlackPanel />)
  return { save, getStatus }
}

const hydrated = () => screen.findByRole('heading', { name: 'Slack', level: 3 })
/** The page's own save at the bottom (the pending note offers the same save inline). */
const saveBtn = () => screen.getAllByRole('button', { name: 'Save Slack settings' }).at(-1)!
/** The manifest panel and the capability lines appear only after a chain of
 *  queries: the Slack config, the effect that seeds the draft from it, then the
 *  manifest-status query. A loaded coverage runner can take longer than the
 *  implicit one-second find deadline over that chain, so the waits on it name
 *  their own. */
const CHAINED = { timeout: 5000 }

describe('Slack Code payload', () => {
  it('sends only what changed, so an unrelated save never re-validates a default', () => {
    const saved = slackCodeDraftFrom(config({ code_channel_defaults: { folder: 'aaaaaaaaaaaa', agent: null, model: 'gone-model', reasoning_effort: null } }))
    expect(slackCodePayload(saved, saved)).toEqual({})
    expect(slackCodePayload({ ...saved, effort: 'high' }, saved)).toEqual({ code_channel_defaults: { reasoning_effort: 'high' } })
    expect(slackCodePayload({ ...saved, folder: '', switches: { code_channels: true } }, saved)).toEqual({
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
  it('labels the switch a preview and says the switch can be undone', async () => {
    seed()
    await hydrated()
    const section = await screen.findByTestId('slack-code-section', {}, CHAINED)
    const note = within(section).getByTestId('slack-code-preview-note')
    expect(note).toHaveTextContent('Preview')
    expect(note).toHaveTextContent('Code channels arrive in a later release')
    expect(section).toHaveTextContent('You can turn it off and remove them later.')
    expect(section).not.toHaveTextContent("changes your Slack app's manifest")
  })

  it('names every fallback Inherited in the pickers and the preview', async () => {
    seed({ code_channels: true }, status(), [])
    const card = await screen.findByTestId('slack-code-defaults', {}, CHAINED)
    expect(within(card).getByLabelText('Default agent')).toHaveTextContent('Inherited')
    const preview = screen.getByTestId('slack-code-preview')
    expect(preview).toHaveTextContent('Inherited')
    for (const old of ["Use the folder's agent", 'Folder default', "The agent's own model"]) {
      expect(card).not.toHaveTextContent(old)
    }
  })

  it('with two top-level Code channels folders, uses neither and creates none', async () => {
    seed({ code_channels: true }, status(), [
      ...FOLDERS,
      { id: 'cccccccccccc', name: 'Code channels', order: 2, default_agent: 'builder' },
      { id: 'dddddddddddd', name: 'code channels', order: 3 },
    ])
    const card = await screen.findByTestId('slack-code-defaults', {}, CHAINED)
    await waitFor(() => expect(within(card).getByLabelText('Default folder')).toHaveTextContent('two folders have this name'))
    expect(card).toHaveTextContent('Two top-level folders are named Code channels')
    const preview = screen.getByTestId('slack-code-preview')
    expect(preview).toHaveTextContent('Unfiled')
    expect(preview).not.toHaveTextContent('created on first use')
    expect(preview).not.toHaveTextContent('kirocrew-conductor')
  })
  it('with nothing configured, files into an auto-created Code channels folder run by the conductor', async () => {
    seed({ code_channels: true }, status(), [])
    await hydrated()
    const card = await screen.findByTestId('slack-code-defaults', {}, CHAINED)
    await waitFor(() => expect(within(card).getByLabelText('Default folder')).toHaveTextContent('Code channels (created automatically · kirocrew-conductor)'), CHAINED)
    expect(card).toHaveTextContent('No folders yet: create one with New folder…')
    const preview = screen.getByTestId('slack-code-preview')
    expect(preview).toHaveTextContent('Code channels (created on first use)')
    expect(preview).toHaveTextContent('kirocrew-conductor')
    expect(preview).toHaveTextContent('Inherited from folder')
  })

  it('uses an existing Code channels folder as the default row, with its own agent', async () => {
    const auto = { id: 'cccccccccccc', name: 'Code channels', order: 2, default_agent: 'reviewer', project_dir: '/repo/cc' }
    seed({ code_channels: true }, status(), [...FOLDERS, auto])
    await hydrated()
    const card = await screen.findByTestId('slack-code-defaults', {}, CHAINED)
    await waitFor(() => expect(within(card).getByLabelText('Default folder')).toHaveTextContent('Code channels (default · reviewer)'), CHAINED)
    const preview = screen.getByTestId('slack-code-preview')
    expect(preview).toHaveTextContent('/repo/cc')
    expect(preview).toHaveTextContent('reviewer')
    // Listed once, as the default row, not again among the folders.
    await userEvent.click(within(card).getByLabelText('Default folder'))
    expect(screen.getAllByRole('option', { name: /Code channels/ })).toHaveLength(1)
  })

  it('offers the shipped agents the server lists, conductor and team lead included', async () => {
    seed({ code_channels: true, code_channel_agents: ['kirocrew', 'kirocrew-conductor', 'kirocrew-team-lead'] })
    await hydrated()
    const card = await screen.findByTestId('slack-code-defaults', {}, CHAINED)
    await userEvent.click(within(card).getByLabelText('Default agent'))
    for (const name of ['kirocrew-conductor', 'kirocrew-team-lead']) {
      expect(await screen.findByRole('option', { name })).toBeInTheDocument()
    }
  })

  it('offers the agents of a folder picked but not saved, from that folder\u2019s project', async () => {
    seed({ code_channels: true, code_channel_agents: ['builder', 'reviewer'] })
    const scoped = vi.spyOn(api, 'getSlackCodeChannelAgents').mockResolvedValue({ agents: ['builder', 'dev-only'] })
    const card = await screen.findByTestId('slack-code-defaults', {}, CHAINED)
    // The default row (no folder saved) is asked for too, against today's folders.
    await waitFor(() => expect(scoped).toHaveBeenCalledWith(''))
    await userEvent.click(within(card).getByLabelText('Default folder'))
    await userEvent.click(await screen.findByRole('option', { name: 'Dev' }))
    await waitFor(() => expect(scoped).toHaveBeenCalledWith('aaaaaaaaaaaa'))
    await userEvent.click(within(card).getByLabelText('Default agent'))
    expect(await screen.findByRole('option', { name: 'dev-only' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'reviewer' })).toBeNull()
  })

  it('flags a picked agent the newly picked folder\u2019s project does not have', async () => {
    seed({
      code_channels: true,
      code_channel_defaults: { folder: null, agent: 'reviewer', model: null, reasoning_effort: null },
      code_channel_agents: ['builder', 'reviewer'],
    })
    vi.spyOn(api, 'getSlackCodeChannelAgents').mockImplementation(async folder => ({
      agents: folder === 'aaaaaaaaaaaa' ? ['builder'] : ['builder', 'reviewer'],
    }))
    const card = await screen.findByTestId('slack-code-defaults', {}, CHAINED)
    expect(card).not.toHaveTextContent(/not installed in this folder/)
    await userEvent.click(within(card).getByLabelText('Default folder'))
    await userEvent.click(await screen.findByRole('option', { name: 'Dev' }))
    const warning = await within(card).findByText(/“reviewer” is not installed in this folder’s project/)
    expect(warning).toBeInTheDocument()
    // Nothing failed: a configuration warning, not an error alert.
    expect(warning.closest('[role="alert"]')).toBeNull()
  })

  it('does not offer to copy a manifest from before the saved switches', async () => {
    seed({ code_channels: false })
    const manifest = vi.mocked(api.getSlackManifest)
    manifest.mockResolvedValue({ alias: 'kirocrew', manifest: 'old: yaml\n', create_url: 'https://example.invalid/new', features: [] })
    await hydrated()
    // The saved config now has the switch on, but the manifest refetch fails:
    // React Query keeps the old YAML, which must not be copyable.
    vi.mocked(api.getSlackConfig).mockResolvedValue(config({ code_channels: true }))
    manifest.mockRejectedValue(new Error('down'))
    fireEvent.click(screen.getByRole('switch', { name: 'Code channels' }))
    fireEvent.click(saveBtn())
    // Wait for the refetch to settle as failed, then the old YAML stays uncopyable.
    expect(await screen.findByText(/Could not load the Slack app manifest/, {}, CHAINED)).toBeInTheDocument()
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(within(panel).getByRole('button', { name: 'Copy manifest YAML' })).toBeDisabled()
    // The setup card's Copy and Create export the same YAML: both are off too.
    for (const btn of screen.getAllByRole('button', { name: 'Copy manifest YAML' })) expect(btn).toBeDisabled()
    const create = screen.getByRole('link', { name: /Create Slack app/ })
    expect(create).toHaveAttribute('aria-disabled', 'true')
    expect(create).toHaveAttribute('href', '#')
    expect(create).toHaveAttribute('tabindex', '-1')
  })

  it('does not offer to copy a manifest rendered for other switches than the saved ones', async () => {
    seed({ code_channels: true })
    const manifest = vi.mocked(api.getSlackManifest)
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    const copyBtn = () => within(panel).getByRole('button', { name: 'Copy manifest YAML' })
    manifest.mockResolvedValue({ alias: 'kirocrew', manifest: 'old: yaml\n', create_url: 'https://example.invalid/new', features: [] })
    fireEvent.click(within(panel).getByRole('button', { name: /Check again/ }))
    await waitFor(() => expect(copyBtn()).toBeDisabled())
    manifest.mockResolvedValue({ alias: 'kirocrew', manifest: 'new: yaml\n', create_url: 'https://example.invalid/new', features: ['code_channels'] })
    fireEvent.click(within(panel).getByRole('button', { name: /Check again/ }))
    await waitFor(() => expect(copyBtn()).toBeEnabled())
  })

  it('says when the agent list for the saved folder could not load', async () => {
    seed({ code_channels: true })
    vi.spyOn(api, 'getSlackCodeChannelAgents').mockRejectedValue(new Error('down'))
    const card = await screen.findByTestId('slack-code-defaults', {}, CHAINED)
    expect(await within(card).findByText(/Could not load folders or agents/)).toBeInTheDocument()
  })

  it('creates a folder from the picker through the sidebar create call and selects it', async () => {
    const { save } = seed({ code_channels: true, code_channel_agents: ['kirocrew-conductor', 'kirocrew-team-lead'] }, status(), [])
    const created = {
      id: 'eeeeeeeeeeee', name: 'Payments', order: 0, project_dir: '/repo/payments', default_agent: 'kirocrew-team-lead',
    }
    const create = vi.spyOn(api, 'createChatFolder').mockImplementation(async () => {
      // As on the server: the next folder read includes it.
      vi.mocked(api.chatFolders).mockResolvedValue([created] as never)
      return created as never
    })
    await hydrated()
    const card = await screen.findByTestId('slack-code-defaults', {}, CHAINED)
    await userEvent.click(within(card).getByLabelText('Default folder'))
    await userEvent.click(await screen.findByRole('option', { name: 'New folder…' }))

    const dialog = await screen.findByRole('dialog')
    await userEvent.type(within(dialog).getByLabelText('Name'), 'Payments')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create folder' }))

    await waitFor(() => expect(create).toHaveBeenCalledTimes(1))
    expect(create.mock.calls[0][0]).toBe('Payments')
    expect(create.mock.calls[0][1]).toBeUndefined()   // top level
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    // Selected, and the preview follows the new folder at once.
    expect(within(card).getByLabelText('Default folder')).toHaveTextContent('Payments')
    const preview = screen.getByTestId('slack-code-preview')
    expect(preview).toHaveTextContent('/repo/payments')
    expect(preview).toHaveTextContent('kirocrew-team-lead')

    fireEvent.click(saveBtn())
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({ code_channel_defaults: { folder: 'eeeeeeeeeeee' } })
  })

  it('keeps the create dialog open with the reason when the server refuses', async () => {
    seed({ code_channels: true }, status(), [])
    const create = vi.spyOn(api, 'createChatFolder').mockRejectedValue(new Error('project_dir is not an existing directory'))
    await hydrated()
    const card = await screen.findByTestId('slack-code-defaults', {}, CHAINED)
    await userEvent.click(within(card).getByLabelText('Default folder'))
    await userEvent.click(await screen.findByRole('option', { name: 'New folder…' }))
    const dialog = await screen.findByRole('dialog')
    await userEvent.type(within(dialog).getByLabelText('Name'), 'Broken')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create folder' }))
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1))
    expect(await within(dialog).findByText(/not an existing directory/)).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('reveals the defaults, previews the folder resolution and saves the picks', async () => {
    const { save } = seed()
    await hydrated()
    expect(screen.queryByTestId('slack-code-defaults')).toBeNull()

    fireEvent.click(screen.getByRole('switch', { name: 'Code channels' }))
    const card = await screen.findByTestId('slack-code-defaults', {}, CHAINED)
    await userEvent.click(within(card).getByLabelText('Default folder'))
    await userEvent.click(await screen.findByRole('option', { name: /Slack Code/ }))

    const preview = screen.getByTestId('slack-code-preview')
    expect(preview).toHaveTextContent('/repo/app')   // inherited from the parent folder
    expect(preview).toHaveTextContent('builder')     // the folder's default agent
    expect(preview).toHaveTextContent('Inherited from folder')

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

  it('says when the model list could not load', async () => {
    markModelsDegraded('acp', true)
    try {
      seed({ code_channels: true })
      expect(await screen.findByText(/Could not load the model list/, {}, CHAINED)).toBeInTheDocument()
    } finally {
      markModelsDegraded('acp', false)
    }
  })

  it('previews a folder the server reports missing as unfiled, even if the list still has it', async () => {
    seed({
      code_channels: true,
      code_channel_defaults: { folder: 'bbbbbbbbbbbb', agent: null, model: null, reasoning_effort: null },
      code_channel_warnings: ['folder_missing'],
    })
    const preview = await screen.findByTestId('slack-code-preview', {}, CHAINED)
    await waitFor(() => expect(preview).toHaveTextContent('Unfiled'))
    expect(preview).not.toHaveTextContent('Slack Code')
    expect(preview).not.toHaveTextContent('/repo/app')
    expect(preview).not.toHaveTextContent('builder')
    // The picker does not keep showing the folder the notice says is gone.
    await waitFor(() => expect(within(screen.getByTestId('slack-code-defaults')).getByLabelText('Default folder')).toHaveTextContent('No folder'), CHAINED)
  })

  it('shows a folder stored as a sidebar path as that folder, and an unrelated save keeps it', async () => {
    const { save } = seed({
      code_channels: true,
      code_channel_defaults: { folder: 'Dev/Slack Code', agent: null, model: null, reasoning_effort: null },
      code_channel_effective: {
        folder: { id: 'bbbbbbbbbbbb', path: 'Dev/Slack Code', project_dir: '/repo/app', default_agent: 'builder' },
        agent: 'builder', agent_source: 'folder', model: null, reasoning_effort: null,
        project_dir: '/repo/app', project_source: 'folder', slack_agent: 'kirocrew',
      },
    })
    const preview = await screen.findByTestId('slack-code-preview', {}, CHAINED)
    await waitFor(() => expect(preview).toHaveTextContent('/repo/app'))
    expect(preview).toHaveTextContent('builder')
    expect(preview).not.toHaveTextContent('Unfiled')
    expect(within(screen.getByTestId('slack-code-defaults')).getByLabelText('Default folder')).toHaveTextContent('Dev › Slack Code')

    fireEvent.click(saveBtn())
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).not.toHaveProperty('code_channel_defaults')
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

describe('Update your Slack app', () => {
  it('stays hidden while no switch is on and nothing is left over', async () => {
    seed({}, status({}, { features: [] }))
    await hydrated()
    await waitFor(() => expect(api.getSlackManifestStatus).toHaveBeenCalled())
    expect(screen.queryByTestId('slack-manifest-update')).toBeNull()
  })

  it('lists what code channels add, marks the missing scopes and gives the steps', async () => {
    seed({ code_channels: true }, status({ status: 'needs_update', missing_scopes: ['code_channels:manage'] }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(within(panel).getByTestId('slack-manifest-status')).toHaveAttribute('data-status', 'needs_update')
    expect(panel).toHaveTextContent('features.code_channels')
    expect(panel).toHaveTextContent('code_channels:manage· missing')
    expect(panel).toHaveTextContent('reinstall the app')
    expect(within(panel).getByTestId('slack-manifest-replace-warning')).toHaveTextContent('whole configuration')
    expect(within(panel).getByRole('link', { name: /Open App Manifest/ }))
      .toHaveAttribute('href', 'https://api.slack.com/apps/A0123ABCD/app-manifest')
  })

  it('reads Up to date after a reinstall, via Check again', async () => {
    const { getStatus } = seed({ code_channels: true }, status({ status: 'needs_update', missing_scopes: ['code_channels:manage'] }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    getStatus.mockResolvedValue(status())
    const cfgCalls = () => vi.mocked(api.getSlackConfig).mock.calls.length
    const cfgBefore = cfgCalls()
    fireEvent.click(within(panel).getByRole('button', { name: /Check again/ }))
    await waitFor(() => expect(getStatus).toHaveBeenLastCalledWith(true))
    await waitFor(() => expect(within(panel).getByTestId('slack-manifest-status')).toHaveAttribute('data-status', 'up_to_date'))
    expect(panel).toHaveTextContent('Nothing to update.')
    // Lines the install holds no longer read as additions.
    expect(panel).not.toHaveTextContent('+ code_channels:manage')
    expect(panel).toHaveTextContent('code_channels:manage')
    expect(screen.queryByTestId('slack-manifest-replace-warning')).toBeNull()
    // The capability lines (config query) are re-read after the check.
    await waitFor(() => expect(cfgCalls()).toBeGreaterThan(cfgBefore))
  })

  it('names a workspace that does not have code channels enabled', async () => {
    seed({ code_channels: true }, status({ status: 'feature_disabled', disabled_features: ['code_channels'] }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(within(panel).getByTestId('slack-manifest-status')).toHaveTextContent('Workspace not enabled')
    expect(panel).toHaveTextContent('does not have Code channels enabled')
    expect(panel).toHaveTextContent('No app update is needed')
    // Reinstalling does not enable a workspace feature, so no update steps.
    expect(panel).not.toHaveTextContent('reinstall the app')
  })

  it('shows a rejected bot token as an error, with no update steps', async () => {
    seed({ code_channels: true }, status({ status: 'token_rejected', reason: 'invalid_auth' }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(within(panel).getByTestId('slack-manifest-status')).toHaveTextContent('Bot token rejected')
    expect(within(panel).getByText(/Slack rejected the saved bot token/)).toBeInTheDocument()
    expect(panel).not.toHaveTextContent('reinstall the app')
  })

  it('says when Slack would not report the scopes', async () => {
    seed({ code_channels: true }, status({ status: 'unknown', reason: 'scopes_unreadable' }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(within(panel).getByTestId('slack-manifest-status')).toHaveTextContent('Cannot check')
    // A scope Slack did not report is neither held nor to be added.
    expect(panel).not.toHaveTextContent('+ code_channels:manage')
    expect(panel).toHaveTextContent('code_channels:manage')
    expect(within(panel).queryByText(/could not be reached/)).toBeNull()
  })

  it('shows a Slack call that failed as an error', async () => {
    seed({ code_channels: true }, status({ status: 'unknown', reason: 'ConnectionError' }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(within(panel).getByText(/Slack could not be reached to check the app/)).toBeInTheDocument()
    expect(panel).not.toHaveTextContent('did not report which permissions')
  })

  it('puts the hand-edited-app exception before the update steps', async () => {
    seed({ code_channels: true }, status({ status: 'needs_update', missing_scopes: ['code_channels:manage'] }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    const warning = within(panel).getByTestId('slack-manifest-replace-warning')
    const steps = within(panel).getByRole('list')
    expect(warning.compareDocumentPosition(steps) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('previews an unsaved switch as pending', async () => {
    seed({}, status({}, { features: [] }))
    await hydrated()
    fireEvent.click(screen.getByRole('switch', { name: 'Code channels' }))
    expect(await screen.findByTestId('slack-manifest-pending', {}, CHAINED)).toBeInTheDocument()
    expect(screen.getByTestId('slack-manifest-update')).toHaveTextContent('after you save')
    expect(screen.getByTestId('slack-manifest-pending')).toHaveTextContent('Save Slack settings')
    // The install reading is about the saved manifest: the pill does not
    // call the on-screen one up to date.
    expect(screen.getByTestId('slack-manifest-status')).toHaveAttribute('data-status', 'pending')
    // The update steps wait for the save: the reading is about the saved
    // manifest, so neither its detail line nor the steps show yet.
    expect(screen.getByTestId('slack-manifest-update')).not.toHaveTextContent('reinstall the app')
    expect(screen.getByTestId('slack-manifest-update')).not.toHaveTextContent('has every permission')
  })

  it('saves from the pending note, so the step after a flip is right there', async () => {
    const { save } = seed({}, status({}, { features: [] }))
    await hydrated()
    fireEvent.click(screen.getByRole('switch', { name: 'Code channels' }))
    const pending = await screen.findByTestId('slack-manifest-pending', {}, CHAINED)
    fireEvent.click(within(pending).getByRole('button', { name: 'Save Slack settings' }))
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({ code_channels: true })
  })

  it('keeps a default set elsewhere after a background refetch, on an unrelated save', async () => {
    const { save } = seed({ code_channels: true })
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    // The CLI sets the default agent while the page is open; a refetch (here
    // the one Check again triggers) picks it up without reseeding the draft.
    vi.mocked(api.getSlackConfig).mockResolvedValue(config({
      code_channels: true,
      code_channel_defaults: { folder: null, agent: 'builder', model: null, reasoning_effort: null },
    }))
    const cfgBefore = vi.mocked(api.getSlackConfig).mock.calls.length
    fireEvent.click(within(panel).getByRole('button', { name: /Check again/ }))
    await waitFor(() => expect(vi.mocked(api.getSlackConfig).mock.calls.length).toBeGreaterThan(cfgBefore), CHAINED)
    fireEvent.click(saveBtn())
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    // The draft never touched the defaults, so the save sends none of them.
    expect(save.mock.calls[0][0]).not.toHaveProperty('code_channel_defaults')
  })
})

describe('Slack Code section placement', () => {
  it('sits right after the tokens, above identity and behavior', async () => {
    seed({ code_channels: true })
    await hydrated()
    const section = await screen.findByTestId('slack-code-section', {}, CHAINED)
    const tokens = screen.getByText('Required')
    const behavior = screen.getByText('Behavior')
    expect(tokens.compareDocumentPosition(section) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(section.compareDocumentPosition(behavior) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // The switch, its defaults and the update panel live in this one card.
    expect(within(section).getByRole('switch', { name: 'Code channels' })).toBeInTheDocument()
    expect(within(section).getByTestId('slack-code-defaults')).toBeInTheDocument()
    expect(await within(section).findByTestId('slack-manifest-update', {}, CHAINED)).toBeInTheDocument()
  })
})

describe('Slack Code switch registry', () => {
  it('offers only the registered switches', async () => {
    seed()
    await hydrated()
    expect(screen.getByRole('switch', { name: 'Code channels' })).toBeInTheDocument()
    // Agent sessions lands with its runtime: this build offers no switch for it.
    expect(screen.queryByRole('switch', { name: 'Agent sessions' })).toBeNull()
  })

  it('names a feature the server reports that this build does not register by its raw name', async () => {
    seed(
      { code_channels: true },
      status({ status: 'feature_disabled', disabled_features: ['code_channels', 'future_switch'] }),
    )
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(panel).toHaveTextContent('Code channels, future_switch')
  })
})

describe('Capability line under a saved toggle', () => {
  // The update panel owns every status it can read (missing scope, workspace
  // not enabled, rejected token, unread install): the line under the toggle
  // never repeats it, so a status appears once on the page.
  it.each([
    ['missing_scope', status({ status: 'needs_update', missing_scopes: ['code_channels:manage'] })],
    ['feature_disabled', status({ status: 'feature_disabled', disabled_features: ['code_channels'] })],
    ['scopes_unknown', status({ status: 'token_rejected', reason: 'invalid_auth' })],
  ])('leaves %s to the update panel instead of repeating it', async (reason, install) => {
    seed({
      code_channels: true,
      code_channels_capability: { available: false, reason, scope: 'code_channels:manage' },
    }, install)
    await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(screen.queryByTestId('slack-capability')).toBeNull()
  })

  it('reports a token-type refusal, which no install reading reports, as an error', async () => {
    seed({
      code_channels: true,
      code_channels_capability: { available: false, reason: 'not_allowed_token_type', scope: 'code_channels:manage' },
    })
    const line = await screen.findByTestId('slack-capability', {}, CHAINED)
    // Slack rejected the request: the shared error surface, announced as an alert.
    expect(line).toHaveAttribute('role', 'alert')
    expect(line).toHaveTextContent('Needs a different token')
    // No agent hand-off beside the unsaved Slack settings draft.
    expect(within(line).queryByRole('button', { name: /ask the agent/i })).toBeNull()
  })

  it('says the app is prepared, not that the feature works, once it holds the permissions', async () => {
    seed({
      code_channels: true,
      code_channels_capability: { available: true, reason: '', scope: 'code_channels:manage' },
    })
    const line = await screen.findByTestId('slack-capability', {}, CHAINED)
    expect(line).toHaveTextContent('Slack app prepared')
    expect(line).not.toHaveTextContent('Ready')
  })

  it('shows nothing under a toggle that is off', async () => {
    seed({ code_channels_capability: { available: false, reason: 'scopes_unknown', scope: 'code_channels:manage' } })
    await hydrated()
    expect(screen.queryByTestId('slack-capability')).toBeNull()
  })
})
