/**
 * Settings → Slack → Slack Code: the registered Slack Code switches (today
 * Code channels; see `slackCodeSwitches`), the code-channel defaults (folder /
 * agent / model / effort), and the "Update your Slack app" panel.
 *
 * The draft lives in `SlackPanel` (one Save for the whole page); this module
 * owns the shape of the Slack Code slice of it, how it is read from the GET and
 * written to the PUT, and how it renders.
 */
import { useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Bot, CheckCircle2, Folder, FolderGit2, Info } from 'lucide-react'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { Badge } from '../../components/ui'
import SimpleSelect from '../../components/SimpleSelect'
import ErrorNotice from '../../components/ErrorNotice'
import FolderConfigModal from '../../components/FolderConfigModal'
import { invalidateFoldersWhenIdle } from '../../api/chatFoldersWrite'
import { api, type SlackCapability, type SlackCodeSwitchKey, type SlackConfigData, type SlackConfigSave } from '../../api/client'
import type { ChatFolder, ChatTag } from '../../types'
import { orderFoldersWithPaths } from '../../utils/folderTree'
import { useFolderSortMode } from '../../hooks/useFolderSortMode'
import { useAvailableModelsQuery } from '../../hooks/useAvailableModels'
import { EFFORT_LEVELS, effortLabel } from '../../lib/effort'
import { Field, INHERIT_MODEL, ModelField } from '../KiroCrewAgentsPage'
import { i18nT } from '../../i18n/t'
import { compareText } from '../../i18n/format'
import { SlackManifestUpdate } from './SlackManifestUpdate'
import { SLACK_CODE_SWITCHES } from './slackCodeSwitches'

/** The Slack Code slice of the Slack settings draft. '' = not set. */
export interface SlackCodeDraft {
  /** Every registered switch, as the draft has it. */
  switches: Record<SlackCodeSwitchKey, boolean>
  folder: string
  agent: string
  /** INHERIT_MODEL ('auto') = keep the agent's own model. */
  model: string
  effort: string
}

export function slackCodeDraftFrom(c: SlackConfigData): SlackCodeDraft {
  const d = c.code_channel_defaults
  // The stored folder may be a sidebar path (a hand-edited config) while the
  // picker matches ids only: seed it with the id the server resolved it to. A
  // reference that did not resolve stays as stored, for its warning.
  const resolvedId = d?.folder && !(c.code_channel_warnings ?? []).includes('folder_missing')
    ? c.code_channel_effective?.folder?.id
    : ''
  return {
    switches: Object.fromEntries(SLACK_CODE_SWITCHES.map(k => [k, !!c[k]])) as Record<SlackCodeSwitchKey, boolean>,
    folder: resolvedId || (d?.folder ?? ''),
    agent: d?.agent ?? '',
    model: d?.model ?? INHERIT_MODEL,
    effort: d?.reasoning_effort ?? '',
  }
}

/** The PUT fields for the Slack Code slice: only what differs from *saved*, so a
 *  save of an unrelated Slack field never re-validates a default (a stored model
 *  that has since become unavailable must not block a token change). A cleared
 *  picker sends null, which clears the stored value. */
export function slackCodePayload(d: SlackCodeDraft, saved: SlackCodeDraft): Partial<SlackConfigSave> {
  const out: Partial<SlackConfigSave> = {}
  for (const k of SLACK_CODE_SWITCHES) {
    if (d.switches[k] !== saved.switches[k]) out[k] = d.switches[k]
  }
  const wire = (x: SlackCodeDraft) => ({
    folder: x.folder || null,
    agent: x.agent || null,
    model: x.model && x.model !== INHERIT_MODEL ? x.model : null,
    reasoning_effort: x.effort || null,
  })
  const next = wire(d)
  const prev = wire(saved)
  const changed = (Object.keys(next) as (keyof typeof next)[]).filter(k => next[k] !== prev[k])
  if (changed.length) {
    out.code_channel_defaults = Object.fromEntries(changed.map(k => [k, next[k]]))
  }
  return out
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
}

function sourceLabel(source: string): string {
  return Object.prototype.hasOwnProperty.call(SOURCE_KEY, source) ? i18nT(SOURCE_KEY[source]) : ''
}

/** A configuration warning (a stale or unusable default): nothing failed, so it
 *  is guidance, not an `ErrorNotice`. Failed loads keep `ErrorNotice`. */
function InlineWarning({ text }: { text: string }) {
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

/** Whether a SAVED toggle can run: it also needs the installed app to hold the
 *  feature's scope, and Slack not to have refused it. Only what the "Update
 *  your Slack app" panel below does not already say: that the app is prepared,
 *  or a token-type refusal (which no install reading reports). A missing
 *  scope, a workspace without the feature, a rejected token or an unread
 *  install is the panel's status, with its detail and steps, so it is not
 *  repeated here. */
function CapabilityLine({ capability }: { capability?: SlackCapability }) {
  if (!capability) return null
  if (capability.available) {
    return (
      <div className="flex items-center gap-1.5 text-[12px] text-ok mt-1" data-testid="slack-capability" data-available="true">
        <CheckCircle2 size={12} className="flex-none" /> {i18nT('pages.settings.slackPanel.slackCode.cap_active')}
      </div>
    )
  }
  if (capability.reason !== 'not_allowed_token_type') return null
  // Slack rejected the request, so this is an error, not guidance.
  // No hand-off: the Slack settings draft on this page (toggles, code-channel
  // defaults) is unsaved, and opening an agent chat would navigate away from it.
  return (
    <ErrorNotice
      variant="inline"
      className="mt-1"
      testId="slack-capability"
      message={i18nT('pages.settings.slackPanel.slackCode.cap_token_type')}
    />
  )
}

export function SlackCodeSection({ data, draft, onChange, readOnly, manifestText, onSave, saving }: {
  data: SlackConfigData
  draft: SlackCodeDraft
  onChange: (patch: Partial<SlackCodeDraft>) => void
  readOnly: boolean
  /** The manifest the SAVED switches render (GET /api/slack/manifest). */
  manifestText: string | undefined
  /** The page's own save (one save for the whole Slack page), offered in the
   *  update panel while a switch change is unsaved. */
  onSave: () => void
  saving: boolean
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
  // created with the conductor on first use. Matched here too so a folder made a
  // moment ago through "New folder…" counts before the next GET.
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
      ? autoFolder.default_agent
        ? i18nT('pages.settings.slackPanel.slackCode.folder_auto_existing_agent', { name: autoFolder.name, agent: autoFolder.default_agent })
        : i18nT('pages.settings.slackPanel.slackCode.folder_auto_existing', { name: autoFolder.name })
      : i18nT('pages.settings.slackPanel.slackCode.folder_auto_create', { name: autoInfo.name, agent: autoInfo.agent })
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
  // No folder set and the auto folder not made yet: it will be, with its agent.
  // Two top-level folders of that name: neither is used and none is created.
  const autoPending = !draft.folder && !autoFolder && !autoAmbiguous && !!autoInfo
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
  const [agent, agentSource] = draft.agent
    ? [draft.agent, 'default']
    : pickedFolder?.default_agent
      ? [pickedFolder.default_agent, 'folder']
      : autoPending && autoInfo
        ? [autoInfo.agent, 'folder']
        : slackAgent ? [slackAgent, 'slack'] : ['', '']
  const savedAgentMissing = (data.code_channel_warnings ?? []).includes('agent_missing')
    && draft.agent === (data.code_channel_defaults?.agent ?? '')


  return (
    <SettingsSection title={i18nT('pages.settings.slackPanel.slackCode.section')}>
      <SettingsCard index={2}>
        {/* Framed, unlike the page's other borderless cards: it is the one
            section whose switches change the Slack app itself. */}
        <div className="rounded-lg border border-border p-4" data-testid="slack-code-section">
          <p className="text-[12px] text-muted m-0 mb-2">{i18nT('pages.settings.slackPanel.slackCode.intro')}</p>

          {/* ── Switches, one row each, in registry order. The Agent sessions
              row (#21422) is the first, above Code channels. ── */}
          <div className="flex flex-col gap-4" data-testid="slack-code-switches">
            {/* ── Code channels ── */}
            <div>
              <SettingsToggle
                label={i18nT('pages.settings.slackPanel.slackCode.code_channels')}
                description={i18nT('pages.settings.slackPanel.slackCode.code_channels_desc')}
                checked={draft.switches.code_channels}
                onChange={v => onChange({ switches: { ...draft.switches, code_channels: v } })}
                disabled={readOnly}
                configKey="slack.code_channels"
              />
              {/* A preview, not a promise: the switch is a normal Slack
                  setting, but the code-channel runtime ships in a later
                  release, so the row says so instead of reading as "works now". */}
              <div className="flex items-start gap-2 mt-1" data-testid="slack-code-preview-note">
                <Badge variant="aim" className="text-[11px] font-sans px-1.5 py-0 flex-none">{i18nT('pages.settings.slackPanel.slackCode.stage_tag')}</Badge>
                <span className="text-[12px] text-muted">{i18nT('pages.settings.slackPanel.slackCode.stage_note')}</span>
              </div>
              {data.code_channels && <CapabilityLine capability={data.code_channels_capability} />}
            </div>
          </div>

          {draft.switches.code_channels && (
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
                <Field label={i18nT('pages.settings.slackPanel.slackCode.agent')} hint={i18nT('pages.settings.slackPanel.slackCode.agent_hint')}>
                  <SimpleSelect
                    options={agentOptions}
                    value={draft.agent}
                    onChange={v => onChange({ agent: v })}
                    clearLabel={i18nT('pages.settings.slackPanel.slackCode.agent_none')}
                    disabled={readOnly}
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
              </div>

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
              {savedAgentMissing && (
                <InlineWarning text={i18nT('pages.settings.slackPanel.slackCode.agent_missing', { agent: draft.agent })} />
              )}
              {agentNotInFolder && !savedAgentMissing && (
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
                    draft.model && draft.model !== INHERIT_MODEL ? draft.model : i18nT('pages.settings.slackPanel.slackCode.preview_agent_own'),
                    draft.effort ? effortLabel(draft.effort) : '',
                  ].filter(Boolean).join(' · ')} />
              </div>
              <div className="flex items-start gap-2 text-[12px] text-muted">
                <Info size={12} className="flex-none mt-0.5" />
                <ul className="m-0 p-0 list-none flex flex-col gap-0.5">
                  <li>{i18nT('pages.settings.slackPanel.slackCode.resolution_agent')}</li>
                  <li>{i18nT('pages.settings.slackPanel.slackCode.resolution_project')}</li>
                </ul>
              </div>
            </div>
          )}

          <SlackManifestUpdate data={data} draft={draft} manifestText={manifestText} onSave={readOnly ? undefined : onSave} saving={saving} />
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
        </div>
      </SettingsCard>
    </SettingsSection>
  )
}
