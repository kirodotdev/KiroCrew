/**
 * Settings → Slack → Slack Code: the code-channel defaults card (folder / agent
 * / model / effort) and its live "a new code channel runs with" preview.
 *
 * Its own chunk, loaded by `SlackCodePanel` only while the Code channels switch
 * is on. The draft lives in `SlackPanel`; this card edits the defaults slice.
 */
import { useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Bot, Folder, FolderGit2, Info, ShieldCheck } from 'lucide-react'
import { Trans } from 'react-i18next'
import SimpleSelect from '../../components/SimpleSelect'
import ErrorNotice from '../../components/ErrorNotice'
import FolderConfigModal from '../../components/FolderConfigModal'
import { invalidateFoldersWhenIdle } from '../../api/chatFoldersWrite'
import { api, type SlackConfigData } from '../../api/client'
import type { ChatFolder, ChatTag } from '../../types'
import { orderFoldersWithPaths } from '../../utils/folderTree'
import { useFolderSortMode } from '../../hooks/useFolderSortMode'
import { useAvailableModelsQuery } from '../../hooks/useAvailableModels'
import { EFFORT_LEVELS, effortLabel } from '../../lib/effort'
import { Field, INHERIT_MODEL, ModelField } from '../../components/crew/crewFields'
import { SettingsToggle } from '../../components/settings'
import { useCrewMode } from '../../hooks/usePreviewFlag'
import { i18nT } from '../../i18n/t'
import { compareText } from '../../i18n/format'
import { APPROVAL_MODES, slackCodeDraftFrom, type SlackCodeDraft } from './slackCodeDraft'

const APPROVAL_LABEL_KEY: Record<string, string> = {
  ask: 'components.approvalModePicker.normal_label',
  trust_reads: 'components.approvalModePicker.reads_label',
  trust: 'components.approvalModePicker.trust_label',
}

function approvalLabel(mode: string): string {
  return i18nT(APPROVAL_LABEL_KEY[mode] ?? APPROVAL_LABEL_KEY.ask)
}

/** What the picked mode does. Normal reuses the dashboard picker's words; Reads
 *  and Trust have their own, because the picker's say "In this chat" and this
 *  page sets the defaults new code channels start with. */
const APPROVAL_DESC_KEY: Record<string, string> = {
  ask: 'components.approvalModePicker.normal_desc',
  trust_reads: 'pages.settings.slackPanel.slackCode.reads_desc',
  trust: 'pages.settings.slackPanel.slackCode.trust_desc',
}

function approvalDesc(mode: string): string {
  return i18nT(APPROVAL_DESC_KEY[mode] ?? APPROVAL_DESC_KEY.ask)
}

/** The folder picker's "New folder…" row: opens the create modal, never stored
 *  (a NUL cannot appear in a folder id). */
const newFolderValue = '\u0000'
/** The trigger's value while the server reports the saved folder gone: matches
 *  no option, so the trigger shows its fallback. */
const goneFolderValue = '\u0001'

const SOURCE_KEY: Record<string, string> = {
  default: 'pages.settings.slackPanel.slackCode.source_default',
  folder: 'pages.settings.slackPanel.slackCode.source_folder',
  slack: 'pages.settings.slackPanel.slackCode.source_slack',
  origin: 'pages.settings.slackPanel.slackCode.source_origin',
  auto: 'pages.settings.slackPanel.slackCode.source_auto',
  agent: 'pages.settings.slackPanel.slackCode.source_agent',
  builtin: 'pages.settings.slackPanel.slackCode.source_builtin',
  conductor: 'pages.settings.slackPanel.slackCode.source_conductor',
}

function sourceLabel(source: string): string {
  return Object.prototype.hasOwnProperty.call(SOURCE_KEY, source) ? i18nT(SOURCE_KEY[source]) : ''
}

/** Scrolls to "Let code channels start helper agents" in the Code channels block
 *  (SlackCodePanel) and focuses it; nothing when that block is not rendered. */
function focusHelpersToggle() {
  const toggle = document.querySelector<HTMLElement>('[data-testid="slack-code-dispatch"] [role="switch"]')
  // Optional call: jsdom has no scrollIntoView.
  toggle?.scrollIntoView?.({ block: 'center' })
  toggle?.focus({ preventScroll: true })
}

/** A configuration warning (a stale or unusable default): nothing failed, so it
 *  is guidance, not an `ErrorNotice`. Failed loads keep `ErrorNotice`. */
function InlineWarning({ text }: { text: React.ReactNode }) {
  return (
    <p role="status" className="text-[12px] text-warn m-0 flex items-start gap-1.5">
      <AlertTriangle size={13} className="flex-none mt-0.5" aria-hidden="true" />
      <span>{text}</span>
    </p>
  )
}

/** One line of the "a new code channel runs" preview. */
function PreviewRow({ icon, label, value, source }: { icon: React.ReactNode; label: string; value: string; source?: string }) {
  return (
    <div className="flex items-start gap-2 min-w-0">
      <span className="mt-0.5 text-muted flex-none">{icon}</span>
      <span className="text-[12px] text-muted flex-none w-[84px]">{label}</span>
      {/* Value and source share one wrapping box: side by side where they fit,
          the source on its own line under the value on a narrow screen. */}
      <div className="flex flex-wrap items-baseline gap-x-2 min-w-0 flex-1">
        <span className="text-[12px] text-text font-mono min-w-0 break-all">{value}</span>
        {source && <span className="ml-auto text-[11px] text-muted min-w-0">{source}</span>}
      </div>
    </div>
  )
}

export default function SlackCodeChannelDefaults({ data, draft, onChange, readOnly }: {
  data: SlackConfigData
  draft: SlackCodeDraft
  onChange: (patch: Partial<SlackCodeDraft>) => void
  readOnly: boolean
}) {
  const { data: foldersData, isError: foldersFailed } = useQuery<ChatFolder[]>({
    queryKey: ['chat-folders'],
    queryFn: () => api.chatFolders(),
  })
  // A shape other than a list (an older gateway, an error body) reads as "no
  // folders" rather than crashing the whole Slack settings page.
  const folders = useMemo(() => (Array.isArray(foldersData) ? foldersData : []), [foldersData])
  const modelsQ = useAvailableModelsQuery()
  const models = modelsQ.data
  const modelsFailed = modelsQ.isError || modelsQ.isDegraded
  const { mode: sortMode } = useFolderSortMode()
  const queryClient = useQueryClient()
  const [creatingFolder, setCreatingFolder] = useState(false)
  // The sidebar's tag vocabulary, read only while the create modal is open.
  const tagsQ = useQuery<ChatTag[]>({ queryKey: ['chat-tags'], queryFn: () => api.chatTags(), enabled: creatingFolder })

  // What an unset folder means (the server's rule): the top-level auto folder,
  // created with no default agent on first use. Matched here too so a folder
  // made a moment ago through "New folder…" counts before the next GET.
  const autoInfo = data.code_channel_auto_folder
  // Top-level folders with the auto name: one is the auto folder, none means it
  // is created on first use, two or more are ambiguous (the server's rule:
  // neither is used and none is created, so the session goes unfiled).
  const autoHits = useMemo(() => {
    if (!autoInfo) return []
    const wanted = autoInfo.name.toLowerCase()
    return folders.filter(f => !f.parent_id && (f.name ?? '').trim().toLowerCase() === wanted)
  }, [folders, autoInfo])
  const autoFolder = autoHits.length === 1 ? autoHits[0] : null
  const autoAmbiguous = autoHits.length > 1
  // The auto folder's agent as the server reads it: the value earlier releases
  // created it with (never edited since) counts as none.
  const autoFolderAgent = autoFolder?.default_agent && autoFolder.default_agent !== autoInfo?.legacy_agent
    ? autoFolder.default_agent
    : ''
  // "Always use conductor mode" decides the agent only where conductor mode is
  // offered (every build but LTS; the server reads the same edition lock);
  // hidden and inert otherwise.
  const conductorModeOffered = useCrewMode()
  // `config.local.json` sets the switch, or may: a save here could not change it.
  const conductorOverlay = data.code_channel_conductor_mode_overlay_state ?? 'absent'
  // The switch's value in force. Pinned by config.local.json: the server's
  // (overlay-applied) value, never a draft kept from before the pin; unreadable:
  // off, as the bind and the save read it (fail-closed, as for the helper-agents
  // switch); otherwise the draft. The toggle, its note, the preview and the
  // picker all read this one value.
  const conductorValue = conductorOverlay === 'pinned'
    ? data.code_channel_defaults?.conductor_mode === true
    : conductorOverlay === 'unreadable' ? false : draft.conductorMode
  const conductorOn = conductorModeOffered && conductorValue
  const conductorAgent = data.code_channel_conductor_agent ?? ''
  // Where config.local.json lives, as the server names it (home shown as ~).
  const overlayPath = data.code_channel_dispatch_overlay_path || '~/.kiro/crew/config.local.json'

  const folderOptions = useMemo(() => {
    // The auto folder is the default row; it is listed again only when it is
    // the saved choice, so the picker never shows it twice.
    const ordered = orderFoldersWithPaths(folders, sortMode)
      .filter(f => f.folder.id !== autoFolder?.id || f.folder.id === draft.folder)
    const values = ordered.map(f => f.folder.id)
    const labels = ordered.map(f => f.path)
    if (!readOnly) {
      values.unshift(newFolderValue)
      labels.unshift(i18nT('pages.settings.slackPanel.slackCode.folder_new'))
    }
    return { values, labels }
  }, [folders, sortMode, autoFolder, draft.folder, readOnly])
  const autoLabel = !autoInfo
    ? i18nT('pages.settings.slackPanel.slackCode.folder_none')
    : autoAmbiguous
      ? i18nT('pages.settings.slackPanel.slackCode.folder_auto_ambiguous', { name: autoInfo.name })
    : autoFolder
      ? autoFolderAgent
        ? i18nT('pages.settings.slackPanel.slackCode.folder_auto_existing_agent', { name: autoFolder.name, agent: autoFolderAgent })
        : i18nT('pages.settings.slackPanel.slackCode.folder_auto_existing', { name: autoFolder.name })
      : i18nT('pages.settings.slackPanel.slackCode.preview_folder_auto', { name: autoInfo.name })
  // The GET lists agents for the SAVED folder's project as the folders stood
  // when the page loaded. A folder picked but not saved yet, or a folder made
  // since (New folder…), can mean another project, so the list is asked for
  // the draft folder and the current folder tree: the picker then offers what
  // a save with that folder accepts.
  const savedFolder = slackCodeDraftFrom(data).folder
  const folderChanged = draft.folder !== savedFolder
  const folderTreeKey = useMemo(
    () => folders.map(f => [f.id, f.name, f.parent_id ?? '', f.project_dir ?? ''].join('\u0000')).join('\u0001'),
    [folders],
  )
  const scopedAgentsQ = useQuery({
    queryKey: ['slack-code-channel-agents', draft.folder, folderTreeKey],
    queryFn: () => api.getSlackCodeChannelAgents(draft.folder),
    staleTime: 0,
  })
  const scopedAgents = scopedAgentsQ.data?.agents
  // Until the draft folder's answer arrives, the saved folder's list stands in
  // for it; a changed folder offers nothing it cannot vouch for.
  const offeredAgents = scopedAgents !== undefined ? scopedAgents : folderChanged ? undefined : data.code_channel_agents
  // The server's list (the chat picker's choices minus Kiro Crew's internal
  // agents), so the picker offers exactly what a save accepts. null = it could
  // not be listed.
  const agentsFailed = offeredAgents === null || scopedAgentsQ.isError
  const agentOptions = useMemo(() => {
    const names = [...(offeredAgents ?? [])]
    // Keep a saved agent visible even if it is no longer offered, so the
    // picker shows what is stored and the warning below explains it.
    if (draft.agent && !names.includes(draft.agent)) names.push(draft.agent)
    return names.sort(compareText)
  }, [offeredAgents, draft.agent])
  // A picked agent the draft folder's project does not have: a save would be
  // refused, so say so before it is tried.
  const agentNotInFolder = !!draft.agent && Array.isArray(scopedAgents) && !scopedAgents.includes(draft.agent)
  const modelOptions = [
    INHERIT_MODEL,
    ...(Array.isArray(models) ? models : []).map((m: { name: string }) => m.name).filter((n: string) => n && n !== INHERIT_MODEL),
  ]

  // The live resolution for the DRAFT: the server's answer for saved values,
  // recomputed here for an unsaved folder/agent pick so the preview follows
  // the pickers before Save.
  const eff = data.code_channel_effective
  // A saved folder the server no longer finds resolves as no folder, so the
  // preview never shows a folder, project or agent that will not apply.
  const savedFolderMissing = (data.code_channel_warnings ?? []).includes('folder_missing')
    && draft.folder === (data.code_channel_defaults?.folder ?? '')
  const pickedFolder = savedFolderMissing
    ? null
    : draft.folder ? folders.find(f => f.id === draft.folder) ?? null : autoFolder
  const folderPath = draft.folder
    ? folderOptions.labels[folderOptions.values.indexOf(draft.folder)] ?? ''
    : autoFolder
      ? autoFolder.name
      : autoInfo && !autoAmbiguous ? i18nT('pages.settings.slackPanel.slackCode.preview_folder_auto', { name: autoInfo.name }) : ''
  const folderProject = useMemo(() => {
    const byId = new Map(folders.map(f => [f.id, f]))
    const seen = new Set<string>()
    let cur: ChatFolder | undefined = pickedFolder ?? undefined
    while (cur && !seen.has(cur.id)) {
      seen.add(cur.id)
      if (cur.project_dir) return cur.project_dir
      cur = cur.parent_id ? byId.get(cur.parent_id) : undefined
    }
    return ''
  }, [folders, pickedFolder])
  const slackAgent = eff?.slack_agent ?? ''
  // A saved agent that is gone, or one the draft folder's project does not
  // have, does not run: the preview resolves past it, as the warning says.
  const savedAgentMissing = (data.code_channel_warnings ?? []).includes('agent_missing')
    && draft.agent === (data.code_channel_defaults?.agent ?? '')
  const agentRuns = !!draft.agent && !savedAgentMissing && !agentNotInFolder
  // The folder's agent as the server reads it: on a top-level auto-named folder,
  // however it is picked, the value earlier releases created it with counts as none.
  const folderAgent = !pickedFolder?.default_agent
    ? ''
    : autoHits.some(f => f.id === pickedFolder.id) && pickedFolder.default_agent === autoInfo?.legacy_agent
      ? ''
      : pickedFolder.default_agent
  const [agent, agentSource] = conductorOn
    ? [conductorAgent, 'conductor']
    : agentRuns
      ? [draft.agent, 'default']
      : folderAgent
        ? [folderAgent, 'folder']
        : slackAgent ? [slackAgent, 'slack'] : ['', '']
  // A model picked here, or the agent's own (Inherited).
  const modelSet = !!draft.model && draft.model !== INHERIT_MODEL

  return (
    <>
            <div className="mt-4 rounded-lg border border-border bg-bg-elevated p-4 flex flex-col gap-4" data-testid="slack-code-defaults">
              <div>
                <div className="text-[13px] font-semibold text-text">{i18nT('pages.settings.slackPanel.slackCode.defaults_title')}</div>
                <div className="text-[12px] text-muted mt-0.5">{i18nT('pages.settings.slackPanel.slackCode.defaults_desc')}</div>
              </div>
              <div className="grid gap-4 sm:grid-cols-2">
                {/* Full width: its default row names the folder AND the agent it
                    is created with, which half a row cuts off. */}
                <div className="sm:col-span-2">
                <Field label={i18nT('pages.settings.slackPanel.slackCode.folder')}
                  hint={i18nT(folders.length === 0 && !foldersFailed
                    ? 'pages.settings.slackPanel.slackCode.folder_empty_hint'
                    : 'pages.settings.slackPanel.slackCode.folder_hint')}>
                  <SimpleSelect
                    options={folderOptions.values}
                    optionLabels={folderOptions.labels}
                    value={savedFolderMissing ? goneFolderValue : draft.folder}
                    onChange={v => (v === newFolderValue ? setCreatingFolder(true) : onChange({ folder: v }))}
                    clearLabel={autoLabel}
                    // A saved folder that is gone resolves as no folder (not the
                    // auto folder), so the trigger says that, like the preview.
                    triggerFallback={i18nT('pages.settings.slackPanel.slackCode.folder_none')}
                    disabled={readOnly}
                    aria-label={i18nT('pages.settings.slackPanel.slackCode.folder')}
                  />
                </Field>
                </div>
                {conductorModeOffered && (
                  // Full width, above the field it overrides: while on, conductor
                  // mode decides every new code channel's agent.
                  <div className="sm:col-span-2" data-testid="slack-code-conductor-mode">
                    <SettingsToggle
                      label={i18nT('pages.settings.slackPanel.slackCode.conductor_mode')}
                      description={i18nT('pages.settings.slackPanel.slackCode.conductor_mode_desc', { agent: conductorAgent })}
                      hint={i18nT('pages.settings.slackPanel.slackCode.conductor_mode_hint')}
                      checked={conductorValue}
                      onChange={v => onChange({ conductorMode: v })}
                      disabled={readOnly || conductorOverlay !== 'absent'}
                      configKey="slack.code_channel_defaults.conductor_mode"
                    />
                    {/* No hand-off: the card sits beside the unsaved Slack draft. */}
                    {conductorOverlay === 'unreadable' && (
                      <div className="mt-1" data-testid="slack-code-conductor-mode-overlay" data-state="unreadable">
                        <ErrorNotice variant="inline" message={i18nT('pages.settings.slackPanel.slackCode.conductor_mode_overlay_unreadable', { path: overlayPath })} />
                      </div>
                    )}
                    {conductorOverlay === 'pinned' && (
                      <div className="flex items-start gap-2 rounded-md border border-border bg-bg-elevated px-3 py-2 mt-1" data-testid="slack-code-conductor-mode-overlay" data-state="pinned">
                        <Info size={12} className="text-muted flex-none mt-0.5" />
                        <span className="text-[12px] text-muted">{conductorValue
                          ? i18nT('pages.settings.slackPanel.slackCode.code_channel_dispatch_overlay_on', { path: overlayPath })
                          : i18nT('pages.settings.slackPanel.slackCode.code_channel_dispatch_overlay_off', { path: overlayPath })}</span>
                      </div>
                    )}
                    {/* Display only: a conductor cannot work without helpers, and
                        the switch never turns them on for the owner. The setting's
                        name is a link to its toggle in the Code channels block above. */}
                    {conductorOn && !draft.dispatch && (
                      <div data-testid="slack-code-conductor-helpers-off">
                        <InlineWarning text={
                          <Trans
                            i18nKey="pages.settings.slackPanel.slackCode.conductor_mode_helpers_off"
                            // eslint-disable-next-line jsx-a11y/control-has-associated-label -- the control's label is the setting name `Trans` renders inside it
                            components={{ toggle: <button type="button" className="text-accent hover:underline bg-transparent border-none p-0 cursor-pointer text-left" onClick={focusHelpersToggle} /> }}
                          />
                        } />
                      </div>
                    )}
                  </div>
                )}
                <Field label={i18nT('pages.settings.slackPanel.slackCode.agent')}
                  hint={i18nT(conductorOn ? 'pages.settings.slackPanel.slackCode.agent_conductor_note' : 'pages.settings.slackPanel.slackCode.agent_hint')}>
                  <SimpleSelect
                    options={agentOptions}
                    value={draft.agent}
                    onChange={v => onChange({ agent: v })}
                    clearLabel={i18nT('pages.settings.slackPanel.slackCode.agent_none')}
                    // Kept as stored while conductor mode decides, and used again once it is off.
                    disabled={readOnly || conductorOn}
                    aria-label={i18nT('pages.settings.slackPanel.slackCode.agent')}
                  />
                </Field>
                <ModelField options={modelOptions} value={draft.model} onChange={v => onChange({ model: v })} disabled={readOnly} hint={i18nT('pages.settings.slackPanel.slackCode.model_hint')} />
                <Field label={i18nT('pages.kiroCrewAgentsPage.reasoning_effort')} hint={i18nT('pages.settings.slackPanel.slackCode.effort_hint')}>
                  <SimpleSelect
                    options={[...EFFORT_LEVELS]}
                    optionLabels={EFFORT_LEVELS.map(l => (l === '' ? i18nT('pages.kiroCrewAgentsPage.inherited') : effortLabel(l)))}
                    value={draft.effort}
                    onChange={v => onChange({ effort: v })}
                    disabled={readOnly}
                    aria-label={i18nT('pages.kiroCrewAgentsPage.edit_reasoning_effort')}
                  />
                </Field>
                <Field
                  label={i18nT('components.approvalModePicker.approval_mode')}
                  // Trust's effect is the warning below, worded for a channel.
                  hint={approvalDesc(draft.approval)}
                  info={i18nT('pages.settings.slackPanel.slackCode.approval_hint')}
                >
                  <SimpleSelect
                    options={[...APPROVAL_MODES]}
                    optionLabels={APPROVAL_MODES.map(approvalLabel)}
                    value={draft.approval}
                    onChange={v => onChange({ approval: v || 'ask' })}
                    disabled={readOnly}
                    aria-label={i18nT('components.approvalModePicker.approval_mode')}
                  />
                </Field>
              </div>
              {draft.approval === 'trust' && (
                <div className="flex items-start gap-2 rounded-md border border-border bg-bg px-3 py-2" role="note" data-testid="slack-code-approval-warning">
                  <AlertTriangle size={13} className="text-warn flex-none mt-0.5" />
                  <span className="text-[12px] text-warn">{i18nT('pages.settings.slackPanel.slackCode.approval_trust_warning')}</span>
                </div>
              )}

              {/* No hand-off for any of these: they sit beside the unsaved
                  Slack draft (the pickers above, the token fields and owner id
                  further up), and the hand-off navigates away, which would
                  discard it. The fix for each is on this card: reload the lists,
                  or pick another folder or agent. */}
              {modelsFailed && (
                <ErrorNotice variant="inline" message={i18nT('pages.settings.slackPanel.slackCode.models_unavailable')} />
              )}
              {(foldersFailed || agentsFailed) && (
                <ErrorNotice variant="inline" message={i18nT('pages.settings.slackPanel.slackCode.options_unavailable')} />
              )}
              {savedFolderMissing && (
                <InlineWarning text={i18nT('pages.settings.slackPanel.slackCode.folder_missing')} />
              )}
              {!draft.folder && autoAmbiguous && autoInfo && (
                <InlineWarning text={i18nT('pages.settings.slackPanel.slackCode.auto_folder_ambiguous', { name: autoInfo.name })} />
              )}
              {/* Not while conductor mode decides the agent: the stored pick is
                  dormant, the picker is disabled, and a folder change saves. */}
              {savedAgentMissing && !conductorOn && (
                <InlineWarning text={i18nT('pages.settings.slackPanel.slackCode.agent_missing', { agent: draft.agent })} />
              )}
              {agentNotInFolder && !savedAgentMissing && !conductorOn && (
                <InlineWarning text={i18nT('pages.settings.slackPanel.slackCode.agent_not_in_folder', { agent: draft.agent })} />
              )}

              {/* What a new code channel will actually run with. */}
              <div className="rounded-md border border-border bg-bg px-3 py-2.5 flex flex-col gap-1.5" data-testid="slack-code-preview">
                <div className="text-[11px] uppercase tracking-wider text-muted font-medium">{i18nT('pages.settings.slackPanel.slackCode.preview_title')}</div>
                <PreviewRow icon={<Folder size={12} />} label={i18nT('pages.settings.slackPanel.slackCode.preview_folder')}
                  value={(!savedFolderMissing && folderPath) || i18nT('pages.settings.slackPanel.slackCode.preview_unfiled')}
                  source={!draft.folder && autoInfo && !autoAmbiguous && !savedFolderMissing ? sourceLabel('auto') : undefined} />
                <PreviewRow icon={<FolderGit2 size={12} />} label={i18nT('pages.settings.slackPanel.slackCode.preview_project')}
                  value={folderProject || i18nT('pages.settings.slackPanel.slackCode.preview_project_origin')}
                  source={folderProject ? sourceLabel('folder') : sourceLabel('origin')} />
                <PreviewRow icon={<Bot size={12} />} label={i18nT('pages.settings.slackPanel.slackCode.preview_agent')}
                  value={agent || i18nT('pages.settings.slackPanel.slackCode.preview_agent_builtin')}
                  source={sourceLabel(agentSource)} />
                <PreviewRow icon={<Bot size={12} />} label={i18nT('pages.settings.slackPanel.slackCode.preview_model')}
                  value={[
                    modelSet ? draft.model : i18nT('pages.settings.slackPanel.slackCode.preview_agent_own'),
                    draft.effort ? effortLabel(draft.effort) : '',
                  ].filter(Boolean).join(' · ')}
                  source={sourceLabel(modelSet ? 'default' : 'agent')} />
                <PreviewRow icon={<ShieldCheck size={12} />} label={i18nT('components.approvalModePicker.approval_mode')}
                  value={approvalLabel(draft.approval)}
                  source={sourceLabel(draft.approval !== 'ask' ? 'default' : 'builtin')} />
              </div>
              <div className="flex items-start gap-2 text-[12px] text-muted">
                <Info size={12} className="flex-none mt-0.5" />
                <ul className="m-0 p-0 list-none flex flex-col gap-0.5">
                  <li>{i18nT('pages.settings.slackPanel.slackCode.resolution_agent')}</li>
                  <li>{i18nT('pages.settings.slackPanel.slackCode.resolution_project')}</li>
                </ul>
              </div>
            </div>
          {creatingFolder && (
            // The sidebar's own create modal and create call (POST
            // /api/chat/folders, SEL-audited there): no second folder store. It
            // stays open on a refusal (a bad project directory) and shows why.
            <FolderConfigModal
              open={true}
              mode="create"
              parentId=""
              folders={folders}
              installedAgents={agentOptions.map(name => ({ name }))}
              globalDefaultAgent={slackAgent || undefined}
              availableTags={tagsQ.data}
              availableTagsFailed={tagsQ.isError}
              onRetryTags={() => { void tagsQ.refetch() }}
              onClose={() => setCreatingFolder(false)}
              onSubmit={async d => {
                const created = await api.createChatFolder(d.name, undefined, {
                  project_dir: d.projectDir || undefined,
                  default_agent: d.defaultAgent || undefined,
                  color: d.color || undefined,
                  icon: d.icon || undefined,
                  tags: d.tags.length > 0 ? d.tags : undefined,
                  steering_dirs: d.steeringDirs.length > 0 ? d.steeringDirs : undefined,
                }) as ChatFolder
                // Seen at once by the picker and the preview; the idle-gated
                // refetch then reconciles with the server's list.
                queryClient.setQueryData<ChatFolder[]>(['chat-folders'], old => [...(old ?? []).filter(f => f.id !== created.id), created])
                invalidateFoldersWhenIdle(queryClient)
                onChange({ folder: created.id })
                setCreatingFolder(false)
              }}
            />
          )}
    </>
  )
}
