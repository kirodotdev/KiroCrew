/**
 * Settings ▸ Slack ▸ Slack Code: the Agent sessions switch and the "Update your
 * Slack app" panel.
 *
 * Seams: the Slack config/manifest/status calls are spied on the real `api`
 * object; the global fetch stub answers the schema read the restart badges make.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

import { renderWithProviders } from './helpers'
import { api, type SlackConfigData, type SlackManifestStatus } from '../api/client'
import { SlackPanel } from '../pages/settings/SlackPanel'
import { slackCodePayload, slackCodeDraftFrom } from '../pages/settings/SlackCodePanel'
import { forgetSlackInstallReading, useInstallRecheckFailed } from '../pages/settings/SlackManifestUpdate'

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true,
    json: async () => ({ entries: [
      { path: 'slack.agent_sessions', type: 'boolean', requiresRestart: true },
    ] }),
  })) as unknown as typeof fetch)
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

const NO_CHANGE = { added_scopes: [], removed_scopes: [], added_events: [], removed_events: [], added_features: [], removed_features: [] }
const CHANGES = {
  agent_sessions: { ...NO_CHANGE, added_features: ['agent_view'], added_scopes: ['assistant:write'], added_events: ['agent_session_stopped', 'agent_session_title_changed'] },
}

function status(install: Partial<SlackManifestStatus['install']> = {}, over: Partial<SlackManifestStatus> = {}): SlackManifestStatus {
  return {
    features: ['agent_sessions'],
    required_scopes: [],
    required_events: [],
    required_features: [],
    feature_changes: CHANGES,
    install: { status: 'up_to_date', missing_scopes: [], extra_scopes: [], disabled_features: [], app_id: 'A0123ABCD', team_id: 'T1', reason: '', ...install },
    app_manifest_url: 'https://api.slack.com/apps/A0123ABCD/app-manifest',
    ...over,
  }
}

/** Every switch saved off, read under that set, and the install still holds a
 *  scope only Agent sessions needed. */
const LEFTOVER_ONLY = status({ extra_scopes: ['assistant:write'] }, { features: [] })

function config(over: Partial<SlackConfigData> = {}): SlackConfigData {
  return {
    connected: true, connect_error: '', configured: true, read_only: false,
    bot_token_set: true, app_token_set: true, bot_token_preview: 'xoxb-••••', app_token_preview: 'xapp-••••',
    owner_id: 'U000OWNER', command: 'kirocrew', allowed_enterprise_ids: [],
    reactions_enabled: true, show_thinking: true,
    agent_sessions: false,
    ...over,
  }
}

function seed(cfg: Partial<SlackConfigData> = {}, st: SlackManifestStatus = status()) {
  vi.spyOn(api, 'getSlackConfig').mockResolvedValue(config(cfg))
  vi.spyOn(api, 'getSlackManifest').mockResolvedValue({ alias: 'kirocrew', manifest: 'display_information: {}\n', create_url: 'https://example.invalid/new' })
  const getStatus = vi.spyOn(api, 'getSlackManifestStatus').mockResolvedValue(st)
  vi.spyOn(api, 'chatFolders').mockResolvedValue([] as never)
  vi.spyOn(api, 'chatTags').mockResolvedValue([] as never)
  const save = vi.spyOn(api, 'saveSlackConfig').mockResolvedValue({ ok: true, restart_required: true, verify_warning: '' })
  const { rerender, queryClient } = renderWithProviders(<SlackPanel />)
  return { save, getStatus, rerender, queryClient }
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
  it('sends only the switches that changed, so an unrelated save writes none of them', () => {
    const saved = slackCodeDraftFrom(config({ agent_sessions: true }))
    expect(slackCodePayload(saved, saved)).toEqual({})
    expect(slackCodePayload({ switches: { agent_sessions: false } }, saved)).toEqual({ agent_sessions: false })
  })
})

describe('Agent sessions switch', () => {
  it('flips on without a confirmation and its note says only what the switch changes', async () => {
    const { save } = seed()
    await hydrated()
    const note = screen.getByTestId('slack-agent-sessions-note')
    // The one name the steps and buttons use, "app manifest", glossed in plain words.
    expect(note).toHaveTextContent('Turning this on changes nothing in Slack yet. It only adds to the app manifest, the setup text you copy into Slack in the steps below.')
    // The permanence warning lives at the update steps alone: neither the switch
    // nor its note says "permanent", and the intro never calls this undoable.
    expect(note).not.toHaveTextContent(/permanent|safe/i)
    expect(screen.getByTestId('slack-code-switches')).not.toHaveTextContent(/permanent/i)
    expect(screen.getByTestId('slack-code-section')).not.toHaveTextContent('You can turn it off')
    expect(note.className).toContain('text-muted')

    fireEvent.click(screen.getByRole('switch', { name: 'Agent sessions' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByRole('switch', { name: 'Agent sessions' })).toBeChecked()
    // The switch does not repeat the permanence warning in a box of its own.
    expect(screen.queryAllByText(/Chat and History tabs/)).toHaveLength(0)

    fireEvent.click(saveBtn())
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({ agent_sessions: true })
  })

  it('turns off without asking', async () => {
    const { save } = seed({ agent_sessions: true })
    await hydrated()
    const toggle = screen.getByRole('switch', { name: 'Agent sessions' })
    await waitFor(() => expect(toggle).toBeChecked())
    fireEvent.click(toggle)
    expect(screen.queryByRole('dialog')).toBeNull()
    fireEvent.click(saveBtn())
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({ agent_sessions: false })
  })

  it('puts the permanent-step warning at the manifest update steps', async () => {
    seed({ agent_sessions: true }, status({ status: 'needs_update', missing_scopes: ['assistant:write'] }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    const warning = within(panel).getByTestId('slack-manifest-agent-sessions-warning')
    expect(warning).toHaveTextContent("Agent sessions: saving this manifest in Slack can't be undone.")
    // What moves, what keeps working, and what the user gets for it.
    expect(warning).toHaveTextContent("from its separate Chat and History tabs into the app's Messages tab")
    expect(warning).toHaveTextContent('a live status and a Stop button')
    expect(warning).toHaveTextContent('DMs and channel threads keep working')
    expect(warning).toHaveTextContent('turning Agent sessions off here does not switch the app back')
    expect(warning.className).toContain('border-warn/40')
    const steps = within(panel).getByRole('list')
    expect(warning.compareDocumentPosition(steps) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    const keeps = within(panel).getAllByTestId('slack-manifest-keeps-working')
    expect(keeps).toHaveLength(1)
    expect(keeps[0]).toHaveTextContent('Slack keeps working as before until you update.')
    expect(warning).toContainElement(keeps[0])
  })

})

describe('Update your Slack app', () => {
  it('does not offer to copy a manifest from before the saved switches', async () => {
    seed({ agent_sessions: false }, status({ status: 'needs_update', missing_scopes: ['assistant:write'] }))
    const manifest = vi.mocked(api.getSlackManifest)
    manifest.mockResolvedValue({ alias: 'kirocrew', manifest: 'old: yaml\n', create_url: 'https://example.invalid/new', features: [] })
    await hydrated()
    // The saved config now has the switch on, but the manifest refetch fails:
    // React Query keeps the old YAML, which must not be copyable.
    vi.mocked(api.getSlackConfig).mockResolvedValue(config({ agent_sessions: true }))
    manifest.mockRejectedValue(new Error('down'))
    fireEvent.click(screen.getByRole('switch', { name: 'Agent sessions' }))
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
    seed({ agent_sessions: true }, status({ status: 'needs_update', missing_scopes: ['assistant:write'] }))
    const manifest = vi.mocked(api.getSlackManifest)
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    const copyBtn = () => within(panel).getByRole('button', { name: 'Copy manifest YAML' })
    manifest.mockResolvedValue({ alias: 'kirocrew', manifest: 'old: yaml\n', create_url: 'https://example.invalid/new', features: [] })
    fireEvent.click(within(panel).getByRole('button', { name: /Check again/ }))
    await waitFor(() => expect(copyBtn()).toBeDisabled())
    manifest.mockResolvedValue({ alias: 'kirocrew', manifest: 'new: yaml\n', create_url: 'https://example.invalid/new', features: ['agent_sessions'] })
    fireEvent.click(within(panel).getByRole('button', { name: /Check again/ }))
    await waitFor(() => expect(copyBtn()).toBeEnabled())
  })

  it('stays hidden while no switch is on and nothing is left over', async () => {
    seed({}, status({}, { features: [] }))
    await hydrated()
    await waitFor(() => expect(api.getSlackManifestStatus).toHaveBeenCalled())
    expect(screen.queryByTestId('slack-manifest-update')).toBeNull()
  })

  it('lists what agent sessions add, marks the missing scopes and gives the steps', async () => {
    seed({ agent_sessions: true }, status({ status: 'needs_update', missing_scopes: ['assistant:write'] }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(within(panel).getByTestId('slack-manifest-status')).toHaveAttribute('data-status', 'needs_update')
    expect(panel).toHaveTextContent('features.agent_view')
    expect(panel).toHaveTextContent('assistant:write· missing')
    expect(panel).toHaveTextContent('reinstall the app')
    expect(within(panel).getByTestId('slack-manifest-replace-warning')).toHaveTextContent('whole configuration')
    expect(within(panel).getByRole('link', { name: /Open App Manifest/ }))
      .toHaveAttribute('href', 'https://api.slack.com/apps/A0123ABCD/app-manifest')
  })

  it('reads Up to date after a reinstall, via Check again', async () => {
    const { getStatus } = seed({ agent_sessions: true }, status({ status: 'needs_update', missing_scopes: ['assistant:write'] }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    getStatus.mockResolvedValue(status())
    const cfgCalls = () => vi.mocked(api.getSlackConfig).mock.calls.length
    const cfgBefore = cfgCalls()
    fireEvent.click(within(panel).getByRole('button', { name: /Check again/ }))
    await waitFor(() => expect(getStatus).toHaveBeenLastCalledWith(true))
    await waitFor(() => expect(within(panel).getByTestId('slack-manifest-status')).toHaveAttribute('data-status', 'up_to_date'))
    expect(panel).toHaveTextContent('Permissions are up to date.')
    // Lines the install holds no longer read as additions.
    expect(panel).not.toHaveTextContent('+ assistant:write')
    expect(panel).toHaveTextContent('assistant:write')
    expect(screen.queryByTestId('slack-manifest-replace-warning')).toBeNull()
    // The capability lines (config query) are re-read after the check.
    await waitFor(() => expect(cfgCalls()).toBeGreaterThan(cfgBefore))
  })

  it('names a workspace that does not have agent sessions enabled', async () => {
    seed({ agent_sessions: true }, status({ status: 'feature_disabled', disabled_features: ['agent_sessions'] }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(within(panel).getByTestId('slack-manifest-status')).toHaveTextContent('Workspace not enabled')
    expect(panel).toHaveTextContent('does not have Agent sessions enabled')
    expect(panel).toHaveTextContent('No app update is needed')
    // Reinstalling does not enable a workspace feature, so no update steps.
    expect(panel).not.toHaveTextContent('reinstall the app')
  })

  it('shows a rejected bot token as an error, with no update steps', async () => {
    seed({ agent_sessions: true }, status({ status: 'token_rejected', reason: 'invalid_auth' }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(within(panel).getByTestId('slack-manifest-status')).toHaveTextContent('Bot token rejected')
    expect(within(panel).getByText(/Slack rejected the saved bot token/)).toBeInTheDocument()
    expect(panel).not.toHaveTextContent('reinstall the app')
  })

  it('says when Slack would not report the scopes', async () => {
    seed({ agent_sessions: true }, status({ status: 'unknown', reason: 'scopes_unreadable' }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(within(panel).getByTestId('slack-manifest-status')).toHaveTextContent('Cannot check')
    // A scope Slack did not report is neither held nor to be added.
    expect(panel).not.toHaveTextContent('+ assistant:write')
    expect(panel).toHaveTextContent('assistant:write')
    expect(within(panel).queryByText(/could not be reached/)).toBeNull()
  })

  it('asks for no one-way step while it cannot tell whether one is needed', async () => {
    seed({ agent_sessions: true }, status({ status: 'unknown', reason: 'scopes_unreadable' }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(panel).toHaveTextContent('cannot tell whether it is current')
    // Check again first, and a test the user can run before any update.
    expect(within(panel).getByRole('button', { name: /Check again/ })).toBeInTheDocument()
    const next = within(panel).getByTestId('slack-manifest-unknown-next')
    expect(next).toHaveTextContent('Select Check again first. If it still cannot check and threads show no Stop button, your app needs the update.')
    // Nothing that performs or warns about the one-way step yet.
    expect(within(panel).queryByTestId('slack-manifest-agent-sessions-warning')).toBeNull()
    expect(within(panel).queryByRole('list')).toBeNull()
    expect(within(panel).queryByTestId('slack-manifest-replace-warning')).toBeNull()
    expect(within(panel).queryByRole('button', { name: 'Copy manifest YAML' })).toBeNull()
    expect(within(panel).queryByRole('link', { name: /Open App Manifest/ })).toBeNull()

    // Asked for, the steps arrive with their warning, as for a reading that needs the update.
    fireEvent.click(within(next).getByRole('button', { name: 'Show update steps' }))
    expect(within(panel).queryByTestId('slack-manifest-unknown-next')).toBeNull()
    const warning = within(panel).getByTestId('slack-manifest-agent-sessions-warning')
    expect(warning).toHaveTextContent("can't be undone")
    expect(within(panel).getByRole('list')).toHaveTextContent('reinstall the app')
    expect(within(panel).getByRole('button', { name: 'Copy manifest YAML' })).toBeInTheDocument()
    expect(within(panel).getByRole('link', { name: /Open App Manifest/ })).toBeInTheDocument()
  })

  it('shows a Slack call that failed as an error', async () => {
    seed({ agent_sessions: true }, status({ status: 'unknown', reason: 'ConnectionError' }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(within(panel).getByText(/Slack could not be reached to check the app/)).toBeInTheDocument()
    expect(panel).not.toHaveTextContent('did not report which permissions')
    // Nothing to do but retry: no update steps or permanent-step warning yet.
    expect(within(panel).queryByRole('list')).toBeNull()
    expect(within(panel).queryByTestId('slack-manifest-agent-sessions-warning')).toBeNull()
    expect(within(panel).queryByTestId('slack-manifest-replace-warning')).toBeNull()
  })

  it('keeps the panel and its error when a re-check of leftover scopes fails', async () => {
    // Switched off, but the install still holds the scopes: the panel shows
    // only because of them. A failed re-check reports no leftovers, which is
    // "not known", so the error and the retry must stay on screen.
    const { getStatus } = seed({ agent_sessions: false }, LEFTOVER_ONLY)
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(panel).toHaveTextContent('assistant:write')
    getStatus.mockResolvedValue(status({ status: 'unknown', reason: 'ConnectionError' }, { features: [] }))
    fireEvent.click(within(panel).getByRole('button', { name: /Check again/ }))
    await waitFor(() => expect(getStatus).toHaveBeenLastCalledWith(true))
    const after = await screen.findByTestId('slack-manifest-update')
    expect(await within(after).findByText(/Slack could not be reached to check the app/)).toBeInTheDocument()
    expect(within(after).getByRole('button', { name: /Check again/ })).toBeInTheDocument()
    // The last known leftovers stay listed, so the panel says why it is there.
    expect(after).toHaveTextContent('assistant:write')
    // A successful reading with nothing left is what hides it.
    getStatus.mockResolvedValue(status())
    fireEvent.click(within(after).getByRole('button', { name: /Check again/ }))
    await waitFor(() => expect(screen.queryByTestId('slack-manifest-update')).toBeNull())
  })

  it.each([
    ['a rejected token', { status: 'token_rejected', reason: 'invalid_auth' }, /Slack rejected the saved bot token/],
    ['unreadable scopes', { status: 'unknown', reason: 'scopes_unreadable' }, /Cannot check/],
  ] as const)('keeps the leftover-scopes panel when a re-check hits %s', async (_label, install, shown) => {
    const { getStatus } = seed({ agent_sessions: false }, LEFTOVER_ONLY)
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    getStatus.mockResolvedValue(status(install, { features: [] }))
    fireEvent.click(within(panel).getByRole('button', { name: /Check again/ }))
    await waitFor(() => expect(getStatus).toHaveBeenLastCalledWith(true))
    const after = await screen.findByTestId('slack-manifest-update')
    expect(await within(after).findByText(shown)).toBeInTheDocument()
    expect(after).toHaveTextContent('assistant:write')
  })

  it('keeps the leftover-scopes panel when the re-check request itself fails', async () => {
    const { getStatus } = seed({ agent_sessions: false }, LEFTOVER_ONLY)
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    getStatus.mockRejectedValue(new Error('down'))
    fireEvent.click(within(panel).getByRole('button', { name: /Check again/ }))
    await waitFor(() => expect(getStatus).toHaveBeenLastCalledWith(true))
    const after = await screen.findByTestId('slack-manifest-update')
    expect(await within(after).findByText(/Could not check the installed Slack app/)).toBeInTheDocument()
    expect(after).toHaveTextContent('assistant:write')
  })

  it('explains an unreadable-scopes reading that leftovers alone keep on screen', async () => {
    const { getStatus } = seed({ agent_sessions: false }, LEFTOVER_ONLY)
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    getStatus.mockResolvedValue(status({ status: 'unknown', reason: 'scopes_unreadable' }, { features: [] }))
    fireEvent.click(within(panel).getByRole('button', { name: /Check again/ }))
    expect(await screen.findByText(/did not report which permissions/)).toBeInTheDocument()
  })

  it('does not list a scope as leftover once the switch that needs it is saved on', async () => {
    const { getStatus } = seed({ agent_sessions: false }, LEFTOVER_ONLY)
    await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    // Saved on, and the status refetch under the new switch set fails: the
    // leftovers were read under "all off" and are not true any more.
    vi.mocked(api.getSlackConfig).mockResolvedValue(config({ agent_sessions: true }))
    getStatus.mockResolvedValue(status({ status: 'unknown', reason: 'ConnectionError' }))
    fireEvent.click(screen.getByRole('switch', { name: 'Agent sessions' }))
    fireEvent.click(saveBtn())
    const after = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(await within(after).findByText(/Slack could not be reached to check the app/, {}, CHAINED)).toBeInTheDocument()
    expect(within(after).queryByText(/still holds permissions these switches no longer need/)).toBeNull()
  })

  it('does not call the old switch set\'s reading up to date after a save whose status refetch fails', async () => {
    // Read with every switch off: up to date, nothing left over.
    const { getStatus } = seed({ agent_sessions: false }, status({}, { features: [] }))
    await hydrated()
    vi.mocked(api.getSlackConfig).mockResolvedValue(config({ agent_sessions: true }))
    getStatus.mockRejectedValue(new Error('down'))
    fireEvent.click(screen.getByRole('switch', { name: 'Agent sessions' }))
    fireEvent.click(saveBtn())
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(await within(panel).findByText(/Could not check the installed Slack app/, {}, CHAINED)).toBeInTheDocument()
    // The cached reading was for "all off": it says nothing about the scopes
    // Agent sessions needs, so none reads as held and the steps stay offered.
    await waitFor(() => expect(within(panel).getByTestId('slack-manifest-status')).toHaveAttribute('data-status', 'unknown'))
    expect(panel).not.toHaveTextContent('Permissions are up to date.')
    expect(within(panel).getByRole('list')).toBeInTheDocument()
  })

  it('claims nothing about the install while the reading for a just-saved switch is still loading', async () => {
    // The install already holds the Agent sessions scope from an earlier time.
    const { getStatus } = seed({ agent_sessions: false }, LEFTOVER_ONLY)
    await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    vi.mocked(api.getSlackConfig).mockResolvedValue(config({ agent_sessions: true }))
    getStatus.mockReturnValue(new Promise(() => {}))
    fireEvent.click(screen.getByRole('switch', { name: 'Agent sessions' }))
    fireEvent.click(saveBtn())
    // The saved config lands; the status refetch for the new set does not.
    await waitFor(() => expect(screen.queryByTestId('slack-manifest-pending')).toBeNull(), CHAINED)
    const panel = screen.getByTestId('slack-manifest-update')
    expect(within(panel).queryByTestId('slack-manifest-status')).toBeNull()
    expect(panel).not.toHaveTextContent('Permissions are up to date.')
    // The old set's leftover is a scope the new set needs: not "no longer needed".
    expect(panel).not.toHaveTextContent('still holds permissions these switches no longer need')
    expect(within(panel).queryByRole('list')).toBeNull()
  })

  it('shows the real status again once a refetch lands after a failed Check again', async () => {
    const { getStatus, queryClient } = seed({ agent_sessions: true })
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    getStatus.mockRejectedValue(new Error('down'))
    fireEvent.click(within(panel).getByRole('button', { name: /Check again/ }))
    expect(await within(panel).findByText(/Could not check the installed Slack app/)).toBeInTheDocument()
    getStatus.mockResolvedValue(status())
    await queryClient.invalidateQueries({ queryKey: ['slack-manifest-status'] })
    await waitFor(() => expect(within(panel).getByTestId('slack-manifest-status')).toHaveAttribute('data-status', 'up_to_date'))
    expect(within(panel).queryByText(/Could not check the installed Slack app/)).toBeNull()
  })

  it('still shows a failed re-check of leftover scopes after leaving and coming back', async () => {
    const { getStatus, rerender } = seed({ agent_sessions: false }, LEFTOVER_ONLY)
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    getStatus.mockResolvedValue(status({ status: 'unknown', reason: 'ConnectionError' }, { features: [] }))
    fireEvent.click(within(panel).getByRole('button', { name: /Check again/ }))
    await screen.findByText(/Slack could not be reached to check the app/)
    // Same query client, fresh component: the cached failure is all the
    // install reading has, and the leftovers must not be forgotten.
    rerender(<div />)
    expect(screen.queryByTestId('slack-manifest-update')).toBeNull()
    rerender(<SlackPanel />)
    const back = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(within(back).getByText(/Slack could not be reached to check the app/)).toBeInTheDocument()
    expect(back).toHaveTextContent('assistant:write')
  })

  it('puts the hand-edited-app exception after the update steps, scoped in its first words', async () => {
    seed({ agent_sessions: true }, status({ status: 'needs_update', missing_scopes: ['assistant:write'] }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    const warning = within(panel).getByTestId('slack-manifest-replace-warning')
    const steps = within(panel).getByRole('list')
    expect(steps.compareDocumentPosition(warning) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(warning.textContent).toMatch(/^Only if you edited this app in Slack by hand:/)
  })

  it('still offers Copy and Open on an up-to-date app, since event subscriptions are unverified', async () => {
    // Up to date covers permissions only. An app missing agent_session_stopped
    // shows no Stop button, and the fix is this same manifest, so the panel
    // keeps it on offer and says why.
    seed({ agent_sessions: true }, status({ status: 'up_to_date' }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    await waitFor(() => expect(within(panel).getByTestId('slack-manifest-status')).toHaveAttribute('data-status', 'up_to_date'))
    expect(panel).toHaveTextContent('if threads show no Stop button, copy the manifest')
    expect(within(panel).getByRole('button', { name: 'Copy manifest YAML' })).toBeInTheDocument()
    expect(within(panel).getByRole('link', { name: /Open App Manifest/ })).toBeInTheDocument()
  })

  it.each([
    ['the workspace lacks the feature', { status: 'feature_disabled', disabled_features: ['agent_sessions'] }],
  ] as const)('offers nothing to copy or open when %s', async (_label, install) => {
    seed({ agent_sessions: true }, status(install))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    await waitFor(() => expect(within(panel).getByTestId('slack-manifest-status')).toHaveAttribute('data-status', install.status))
    expect(within(panel).queryByRole('button', { name: 'Copy manifest YAML' })).toBeNull()
    expect(within(panel).queryByRole('link', { name: /Open App Manifest/ })).toBeNull()
  })

  it('offers nothing to copy or open while a switch change is unsaved', async () => {
    // The manifest on offer is the saved switches'. Copying it before the save
    // would hand over the old one, so Copy and Open wait for the save, as the
    // steps do.
    seed({ agent_sessions: true }, status({ status: 'needs_update', missing_scopes: ['assistant:write'] }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(await within(panel).findByRole('button', { name: 'Copy manifest YAML' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('switch', { name: 'Agent sessions' }))
    await within(panel).findByTestId('slack-manifest-pending')
    expect(within(panel).queryByRole('button', { name: 'Copy manifest YAML' })).toBeNull()
    expect(within(panel).queryByRole('link', { name: /Open App Manifest|Open your Slack apps/ })).toBeNull()
    // Flipped back: nothing unsaved, so the update is offered again.
    fireEvent.click(screen.getByRole('switch', { name: 'Agent sessions' }))
    expect(await within(panel).findByRole('button', { name: 'Copy manifest YAML' })).toBeInTheDocument()
  })

  it('offers nothing to copy on a first flip that is not saved yet', async () => {
    seed({}, status({}, { features: [] }))
    await hydrated()
    fireEvent.click(screen.getByRole('switch', { name: 'Agent sessions' }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    await within(panel).findByTestId('slack-manifest-pending')
    expect(within(panel).queryByRole('button', { name: 'Copy manifest YAML' })).toBeNull()
  })

  it('offers Copy and Open again when a re-check of an up-to-date app fails', async () => {
    const { getStatus } = seed({ agent_sessions: true })
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    await waitFor(() => expect(within(panel).getByTestId('slack-manifest-status')).toHaveAttribute('data-status', 'up_to_date'))
    getStatus.mockRejectedValue(new Error('down'))
    fireEvent.click(within(panel).getByRole('button', { name: /Check again/ }))
    expect(await within(panel).findByRole('button', { name: 'Copy manifest YAML' })).toBeInTheDocument()
    // A failed check names no app, so the link is the Slack apps list.
    expect(within(panel).getByRole('link', { name: /Open your Slack apps/ })).toBeInTheDocument()
  })

  it('does not point at the previous app after a new bot token whose status read fails', async () => {
    // No stored token, so the field takes a paste directly (no Replace step).
    const { getStatus } = seed({ agent_sessions: true, bot_token_set: false }, status({ status: 'needs_update', missing_scopes: ['assistant:write'] }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(within(panel).getByRole('link', { name: /Open App Manifest/ }))
      .toHaveAttribute('href', 'https://api.slack.com/apps/A0123ABCD/app-manifest')
    getStatus.mockRejectedValue(new Error('down'))
    fireEvent.change(screen.getByLabelText('Slack bot token'), { target: { value: 'xoxb-not-a-real-value' } })
    fireEvent.click(saveBtn())
    const after = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(await within(after).findByText(/Could not check the installed Slack app/, {}, CHAINED)).toBeInTheDocument()
    expect(within(after).queryByRole('link', { name: /Open App Manifest/ })).toBeNull()
    expect(within(after).getByRole('link', { name: /Open your Slack apps/ })).toHaveAttribute('href', 'https://api.slack.com/apps')
  })

  it('shows nothing of the previous app while the new bot token\'s reading loads', async () => {
    const { getStatus } = seed({ agent_sessions: true, bot_token_set: false }, status({ status: 'needs_update', missing_scopes: ['assistant:write'] }))
    await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    getStatus.mockReturnValue(new Promise(() => {}))
    fireEvent.change(screen.getByLabelText('Slack bot token'), { target: { value: 'xoxb-not-a-real-value' } })
    fireEvent.click(saveBtn())
    await waitFor(() => expect(screen.queryByTestId('slack-manifest-update')).toBeNull(), CHAINED)
    expect(screen.queryByRole('link', { name: /Open App Manifest/ })).toBeNull()
  })

  it('forgets the previous app\'s leftover scopes when the bot token changes', async () => {
    const { getStatus } = seed({ agent_sessions: true, bot_token_set: false }, status({ status: 'up_to_date', extra_scopes: ['some:leftover'] }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    await waitFor(() => expect(panel).toHaveTextContent('some:leftover'))
    getStatus.mockRejectedValue(new Error('down'))
    fireEvent.change(screen.getByLabelText('Slack bot token'), { target: { value: 'xoxb-not-a-real-value' } })
    fireEvent.click(saveBtn())
    const after = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(await within(after).findByText(/Could not check the installed Slack app/, {}, CHAINED)).toBeInTheDocument()
    expect(after).not.toHaveTextContent('some:leftover')
  })

  it('forgets the previous app when the bot token is removed', async () => {
    const { getStatus } = seed({ agent_sessions: true }, status({ status: 'up_to_date', extra_scopes: ['some:leftover'] }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    await waitFor(() => expect(panel).toHaveTextContent('some:leftover'))
    getStatus.mockRejectedValue(new Error('down'))
    // Bot token first in DOM order, app token second.
    fireEvent.click(screen.getAllByRole('button', { name: 'Remove' })[0])
    fireEvent.click(saveBtn())
    const after = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(await within(after).findByText(/Could not check the installed Slack app/, {}, CHAINED)).toBeInTheDocument()
    expect(after).not.toHaveTextContent('some:leftover')
    expect(within(after).queryByRole('link', { name: /Open App Manifest/ })).toBeNull()
  })

  it('drops a Check again of the previous app that lands after the bot token changed', async () => {
    const { getStatus } = seed({ agent_sessions: true, bot_token_set: false }, status({ status: 'needs_update', missing_scopes: ['assistant:write'] }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    let landOld: (v: SlackManifestStatus) => void = () => {}
    getStatus.mockReturnValueOnce(new Promise<SlackManifestStatus>(r => { landOld = r }))
    fireEvent.click(within(panel).getByRole('button', { name: /Check again/ }))
    await waitFor(() => expect(getStatus).toHaveBeenLastCalledWith(true))
    // The new app's reading: Slack cannot be reached.
    getStatus.mockResolvedValue(status({ status: 'unknown', reason: 'ConnectionError' }))
    fireEvent.change(screen.getByLabelText('Slack bot token'), { target: { value: 'xoxb-not-a-real-value' } })
    fireEvent.click(saveBtn())
    const after = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(await within(after).findByText(/Slack could not be reached to check the app/, {}, CHAINED)).toBeInTheDocument()
    // The old app's Check again lands now, claiming it is up to date.
    landOld(status())
    // The dropped check has ended once its button is enabled again.
    await waitFor(() => expect(within(screen.getByTestId('slack-manifest-update')).getByRole('button', { name: /Check again/ })).not.toBeDisabled(), CHAINED)
    const pill = within(screen.getByTestId('slack-manifest-update')).getByTestId('slack-manifest-status')
    expect(pill).not.toHaveAttribute('data-status', 'up_to_date')
    expect(screen.getByTestId('slack-manifest-update')).not.toHaveTextContent('Permissions are up to date.')
  })

  it('takes the app manifest link only from a check that did not fail', async () => {
    const { getStatus } = seed({ agent_sessions: true }, status({ status: 'needs_update', missing_scopes: ['assistant:write'] }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    getStatus.mockRejectedValue(new Error('down'))
    fireEvent.click(within(panel).getByRole('button', { name: /Check again/ }))
    expect(await within(panel).findByRole('link', { name: /Open your Slack apps/ })).toHaveAttribute('href', 'https://api.slack.com/apps')
  })

  it('still offers Copy and Open on an up-to-date app that holds leftover scopes', async () => {
    seed({ agent_sessions: true }, status({ status: 'up_to_date', extra_scopes: ['some:leftover'] }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    await waitFor(() => expect(within(panel).getByTestId('slack-manifest-status')).toHaveAttribute('data-status', 'up_to_date'))
    expect(within(panel).getByRole('button', { name: 'Copy manifest YAML' })).toBeInTheDocument()
    expect(within(panel).getByRole('link', { name: /Open App Manifest/ })).toBeInTheDocument()
  })

  it('still offers Copy and Open when leftover scopes are the only reason for the panel', async () => {
    seed({ agent_sessions: false }, LEFTOVER_ONLY)
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(within(panel).getByRole('button', { name: 'Copy manifest YAML' })).toBeInTheDocument()
    expect(within(panel).getByRole('link', { name: /Open App Manifest/ })).toBeInTheDocument()
  })

  it('previews an unsaved switch as pending', async () => {
    seed({}, status({}, { features: [] }))
    await hydrated()
    fireEvent.click(screen.getByRole('switch', { name: 'Agent sessions' }))
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
    fireEvent.click(screen.getByRole('switch', { name: 'Agent sessions' }))
    const pending = await screen.findByTestId('slack-manifest-pending', {}, CHAINED)
    fireEvent.click(within(pending).getByRole('button', { name: 'Save Slack settings' }))
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({ agent_sessions: true })
  })

})

describe('Slack Code section placement', () => {
  it('sits right after the tokens, above identity and behavior', async () => {
    seed({ agent_sessions: true })
    await hydrated()
    const section = await screen.findByTestId('slack-code-section', {}, CHAINED)
    const tokens = screen.getByText('Required')
    const behavior = screen.getByText('Behavior')
    expect(tokens.compareDocumentPosition(section) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(section.compareDocumentPosition(behavior) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // The switch and the update panel live in this one card.
    expect(within(section).getByRole('switch', { name: 'Agent sessions' })).toBeInTheDocument()
    expect(await within(section).findByTestId('slack-manifest-update', {}, CHAINED)).toBeInTheDocument()
  })
})

describe('Slack Code switch registry', () => {
  it('offers only the registered switches', async () => {
    seed()
    await hydrated()
    const section = screen.getByTestId('slack-code-switches')
    expect(within(section).getAllByRole('switch')).toHaveLength(1)
    expect(within(section).getByRole('switch', { name: 'Agent sessions' })).toBeInTheDocument()
  })

  it('names a feature the server reports that this build does not register by its raw name', async () => {
    seed(
      { agent_sessions: true },
      status({ status: 'feature_disabled', disabled_features: ['agent_sessions', 'future_switch'] }),
    )
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(panel).toHaveTextContent('Agent sessions, future_switch')
  })
})

describe('Capability line under a saved toggle', () => {
  // The update panel owns every status it can read (missing scope, workspace
  // not enabled, rejected token, unread install): the line under the toggle
  // never repeats it, so a status appears once on the page.
  it.each([
    ['missing_scope', status({ status: 'needs_update', missing_scopes: ['assistant:write'] })],
    ['feature_disabled', status({ status: 'feature_disabled', disabled_features: ['agent_sessions'] })],
    ['scopes_unknown', status({ status: 'token_rejected', reason: 'invalid_auth' })],
  ])('leaves %s to the update panel instead of repeating it', async (reason, install) => {
    seed({
      agent_sessions: true,
      agent_sessions_capability: { available: false, reason, scope: 'assistant:write' },
    }, install)
    await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(screen.queryByTestId('slack-capability')).toBeNull()
  })

  it('drops the "nothing in Slack yet" note once Slack reports Agent sessions live', async () => {
    seed({
      agent_sessions: true,
      agent_sessions_capability: { available: true, reason: '', scope: 'assistant:write' },
    })
    await screen.findByTestId('slack-capability', {}, CHAINED)
    expect(screen.queryByTestId('slack-agent-sessions-note')).toBeNull()
  })

  it('drops the note once Slack has the setup, before the gateway re-reads the scopes', async () => {
    seed(
      {
        agent_sessions: true,
        agent_sessions_capability: { available: false, reason: 'scopes_unknown', scope: 'assistant:write' },
      },
      status({ status: 'up_to_date' }, { features: ['agent_sessions'] }),
    )
    await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    await waitFor(() => expect(screen.queryByTestId('slack-agent-sessions-note')).toBeNull())
  })

  it('keeps the note when an update is still due, even with assistant:write granted', async () => {
    // The older assistant view holds that scope too: it does not prove the agent
    // view was saved, so the steps (and the note pointing at them) stay.
    seed(
      {
        agent_sessions: true,
        agent_sessions_capability: { available: false, reason: 'scopes_unknown', scope: 'assistant:write' },
      },
      status({ status: 'needs_update', missing_scopes: ['some:other'] }, { features: ['agent_sessions'] }),
    )
    await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(screen.getByTestId('slack-agent-sessions-note')).toBeInTheDocument()
  })

  it('keeps the note on a reading taken for another switch set', async () => {
    seed(
      {
        agent_sessions: true,
        agent_sessions_capability: { available: false, reason: 'scopes_unknown', scope: 'assistant:write' },
      },
      status({ status: 'up_to_date' }, { features: [] }),
    )
    await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(screen.getByTestId('slack-agent-sessions-note')).toBeInTheDocument()
  })

  it('brings the note back when a refetch of the install reading fails', async () => {
    const { getStatus, queryClient } = seed(
      {
        agent_sessions: true,
        agent_sessions_capability: { available: false, reason: 'scopes_unknown', scope: 'assistant:write' },
      },
      status({ status: 'up_to_date' }, { features: ['agent_sessions'] }),
    )
    await waitFor(() => expect(screen.queryByTestId('slack-agent-sessions-note')).toBeNull(), CHAINED)
    getStatus.mockRejectedValue(new Error('slack down'))
    await queryClient.refetchQueries({ queryKey: ['slack-manifest-status'] })
    expect(await screen.findByTestId('slack-agent-sessions-note')).toBeInTheDocument()
  })

  it('brings the note back when Check again fails, as the steps come back below it', async () => {
    const { getStatus } = seed(
      {
        agent_sessions: true,
        agent_sessions_capability: { available: false, reason: 'scopes_unknown', scope: 'assistant:write' },
      },
      status({ status: 'up_to_date' }, { features: ['agent_sessions'] }),
    )
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    await waitFor(() => expect(screen.queryByTestId('slack-agent-sessions-note')).toBeNull())
    getStatus.mockRejectedValue(new Error('down'))
    fireEvent.click(within(panel).getByRole('button', { name: /Check again/ }))
    await waitFor(() => expect(getStatus).toHaveBeenLastCalledWith(true))
    expect(await screen.findByTestId('slack-agent-sessions-note')).toBeInTheDocument()
    expect(within(panel).getByRole('list')).toBeInTheDocument()
  })

  it('keeps the permanent-step warning when assistant:write is already granted', async () => {
    // An app on the older assistant view holds assistant:write but not the agent
    // view: the update would still migrate it, which cannot be undone.
    seed(
      { agent_sessions: true },
      status({ status: 'needs_update', missing_scopes: ['some:other'] }, { features: ['agent_sessions'] }),
    )
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    await within(panel).findByRole('list')
    expect(within(panel).getByTestId('slack-manifest-agent-sessions-warning')).toBeInTheDocument()
    expect(within(panel).getAllByTestId('slack-manifest-keeps-working')).toHaveLength(1)
  })

  it('keeps the note while Agent sessions is saved on but not live yet', async () => {
    seed({
      agent_sessions: true,
      agent_sessions_capability: { available: false, reason: 'missing_scope', scope: 'assistant:write' },
    }, status({ status: 'needs_update', missing_scopes: ['assistant:write'] }))
    await hydrated()
    expect(await screen.findByTestId('slack-agent-sessions-note')).toBeInTheDocument()
  })

  it('does not read Up to date beside a token-type refusal, and names the fix', async () => {
    // The install reading alone says up to date; the runtime says the token's
    // type is refused. The panel gives one verdict, with the next step.
    seed({
      agent_sessions: true,
      agent_sessions_capability: { available: false, reason: 'not_allowed_token_type', scope: 'assistant:write' },
    }, status({ status: 'up_to_date' }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    await waitFor(() => expect(within(panel).getByTestId('slack-manifest-status')).toHaveAttribute('data-status', 'token_type'))
    expect(within(panel).getByTestId('slack-manifest-status')).toHaveTextContent('Needs a different token')
    expect(panel).not.toHaveTextContent('Permissions are up to date.')
    // Which token, and where it comes from: not "paste a current one", which
    // would send the user to re-paste the same token.
    expect(panel).toHaveTextContent('starts with xoxb-')
    expect(panel).not.toHaveTextContent('Paste a current one')
    // One message: the line under the switch does not repeat it.
    expect(screen.queryByTestId('slack-capability')).toBeNull()
    expect(within(panel).queryByRole('list')).toBeNull()
  })

  it('says the app is prepared, not that the feature works, once it holds the permissions', async () => {
    seed({
      agent_sessions: true,
      agent_sessions_capability: { available: true, reason: '', scope: 'assistant:write' },
    })
    const line = await screen.findByTestId('slack-capability', {}, CHAINED)
    expect(line).toHaveTextContent('Slack app prepared')
    expect(line).not.toHaveTextContent('Ready')
  })

  it('shows nothing under a toggle that is off', async () => {
    seed({ agent_sessions_capability: { available: false, reason: 'scopes_unknown', scope: 'assistant:write' } })
    await hydrated()
    expect(screen.queryByTestId('slack-capability')).toBeNull()
  })
})

describe('a failed Check again', () => {
  function setup() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    const hook = renderHook(({ at }: { at: number }) => useInstallRecheckFailed(at), { wrapper, initialProps: { at: 0 } })
    return { qc, hook }
  }

  it('speaks until a newer reading lands', async () => {
    const { hook } = setup()
    act(() => hook.result.current[1](true))
    await waitFor(() => expect(hook.result.current[0]).toBe(true))
    hook.rerender({ at: 5 })
    expect(hook.result.current[0]).toBe(false)
  })

  it('is not carried over to the app a new token points at', async () => {
    const { qc, hook } = setup()
    act(() => hook.result.current[1](true)) // failed before any reading landed
    await waitFor(() => expect(hook.result.current[0]).toBe(true))
    hook.rerender({ at: 5 }) // a good reading
    vi.spyOn(api, 'getSlackManifestStatus').mockReturnValue(new Promise(() => {}))
    await act(async () => { void forgetSlackInstallReading(qc) })
    // The new app's first reading is still in flight (dataUpdatedAt back at 0):
    // the old failure must not match it.
    expect(qc.getQueryData(['slack-manifest-recheck-failed'])).toBeNull()
    hook.rerender({ at: 0 })
    await waitFor(() => expect(hook.result.current[0]).toBe(false))
  })
})
