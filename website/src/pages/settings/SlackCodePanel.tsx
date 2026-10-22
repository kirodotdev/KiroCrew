/**
 * Settings → Slack → Slack Code: the registered Slack Code switches (Agent
 * sessions and Code channels; see `slackCodeSwitches`), the code-channel defaults (folder /
 * agent / model / effort / approval mode), and the "Update your Slack app" panel.
 *
 * The draft lives in `SlackPanel` (one Save for the whole page); this module
 * owns the shape of the Slack Code slice of it, how it is read from the GET and
 * written to the PUT, and how it renders.
 */
import { useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Bot, CheckCircle2, Folder, FolderGit2, Info, ShieldCheck } from 'lucide-react'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { Badge } from '../../components/ui'
import SimpleSelect from '../../components/SimpleSelect'
import ErrorNotice from '../../components/ErrorNotice'
import FolderConfigModal from '../../components/FolderConfigModal'
import { invalidateFoldersWhenIdle } from '../../api/chatFoldersWrite'
import { useConfirm } from '../../components/ConfirmDialog'
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
  /** One of APPROVAL_MODES; 'ask' (the default) is stored as unset. */
  approval: string
}

/** The approval modes a code channel's session can start in, in picker order.
 *  The labels are the session approval picker's own, so the mode reads the same
 *  here and on the session where it can be changed later. */
export const APPROVAL_MODES = ['ask', 'trust_reads', 'trust'] as const
const APPROVAL_LABEL_KEY: Record<string, string> = {
  ask: 'components.approvalModePicker.normal_label',
  trust_reads: 'components.approvalModePicker.reads_label',
  trust: 'components.approvalModePicker.trust_label',
}

function approvalLabel(mode: string): string {
  return i18nT(APPROVAL_LABEL_KEY[mode] ?? APPROVAL_LABEL_KEY.ask)
}

function approvalFromWire(raw: string | null | undefined): string {
  return raw === 'trust_reads' || raw === 'trust' ? raw : 'ask'
}

export function slackCodeDraftFrom(c: SlackConfigData): SlackCodeDraft {
  const d = c.code_channel_defaults
  return {
    switches: Object.fromEntries(SLACK_CODE_SWITCHES.map(k => [k, !!c[k]])) as Record<SlackCodeSwitchKey, boolean>,
    folder: d?.folder ?? '',
    agent: d?.agent ?? '',
    model: d?.model ?? INHERIT_MODEL,
    effort: d?.reasoning_effort ?? '',
    approval: approvalFromWire(d?.approval_mode),
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
    approval_mode: x.approval === 'ask' ? null : x.approval,
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

/** One line of the "a new code channel runs" preview. */
function PreviewRow({ icon, label, value, source }: { icon: React.ReactNode; label: string; value: string; source?: string }) {
  return (
    <div className="flex items-start gap-2 min-w-0">
      <span className="mt-0.5 text-muted flex-none">{icon}</span>
      <span className="text-[12px] text-muted flex-none w-[84px]">{label}</span>
      <span className="text-[12px] text-text font-mono min-w-0 break-all">{value}</span>
      {source && <span className="ml-auto text-[11px] text-muted flex-none whitespace-nowrap">{source}</span>}
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
  return (
    <div className="flex items-start gap-1.5 text-[12px] text-warn mt-1" data-testid="slack-capability" data-available="false" data-reason={capability.reason}>
      <AlertTriangle size={12} className="flex-none mt-0.5" /> <span>{i18nT('pages.settings.slackPanel.slackCode.cap_token_type')}</span>
    </div>
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
  // The server's list (the chat picker's choices minus Kiro Crew's internal
  // agents), so the picker offers exactly what a save accepts. null = it could
  // not be listed.
  const agentsFailed = data.code_channel_agents === null
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
  const agentOptions = useMemo(() => {
    const names = [...(data.code_channel_agents ?? [])]
    // Keep a saved agent visible even if it is no longer offered, so the
    // picker shows what is stored and the warning below explains it.
    if (draft.agent && !names.includes(draft.agent)) names.push(draft.agent)
    return names.sort(compareText)
  }, [data.code_channel_agents, draft.agent])
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
  const { confirm, confirmDialog } = useConfirm()

  // Turning Agent sessions on is reversible here, but saving the manifest it
  // generates is not (Slack cannot take an app back from agent_view), so the
  // switch asks before it flips on. Off never asks.
  const setAgentSessions = async (on: boolean) => {
    if (!on) { onChange({ switches: { ...draft.switches, agent_sessions: false } }); return }
    const ok = await confirm({
      title: i18nT('pages.settings.slackPanel.slackCode.agent_sessions_confirm_title'),
      body: (
        <div className="flex flex-col gap-2 text-[13px]">
          <p className="m-0 text-warn">{i18nT('pages.settings.slackPanel.slackCode.agent_sessions_permanent')}</p>
          <p className="m-0">{i18nT('pages.settings.slackPanel.slackCode.agent_sessions_confirm_body')}</p>
        </div>
      ),
      confirmLabel: i18nT('pages.settings.slackPanel.slackCode.agent_sessions_confirm_ok'),
      danger: false,
      primary: true,
    })
    if (ok) onChange({ switches: { ...draft.switches, agent_sessions: true } })
  }

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
            {/* ── Agent sessions ── */}
            <div>
              <SettingsToggle
                label={i18nT('pages.settings.slackPanel.slackCode.agent_sessions')}
                description={i18nT('pages.settings.slackPanel.slackCode.agent_sessions_desc')}
                checked={draft.switches.agent_sessions}
                onChange={v => { void setAgentSessions(v) }}
                disabled={readOnly}
                configKey="slack.agent_sessions"
              />
              {/* Muted while off, so the one warn-toned line on the page is the
                  update notice that needs action; warn once the switch is on and
                  the permanent step is the user's next move. */}
              {draft.switches.agent_sessions ? (
                <div className="flex items-start gap-2 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 mt-1" data-testid="slack-agent-sessions-warning" data-tone="warn">
                  <AlertTriangle size={12} className="text-warn flex-none mt-0.5" />
                  <span className="text-[12px] text-warn">{i18nT('pages.settings.slackPanel.slackCode.agent_sessions_irreversible')}</span>
                </div>
              ) : (
                <div className="flex items-start gap-2 text-[12px] text-muted mt-1" data-testid="slack-agent-sessions-warning" data-tone="muted">
                  <Info size={12} className="flex-none mt-0.5" />
                  <span>{i18nT('pages.settings.slackPanel.slackCode.agent_sessions_irreversible')}</span>
                </div>
              )}
              {data.agent_sessions && <CapabilityLine capability={data.agent_sessions_capability} />}
            </div>
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
                <Field label={i18nT('components.approvalModePicker.approval_mode')} hint={i18nT('pages.settings.slackPanel.slackCode.approval_hint')}>
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
                <ErrorNotice variant="inline" message={i18nT('pages.settings.slackPanel.slackCode.folder_missing')} />
              )}
              {!draft.folder && autoAmbiguous && autoInfo && (
                <ErrorNotice variant="inline" message={i18nT('pages.settings.slackPanel.slackCode.auto_folder_ambiguous', { name: autoInfo.name })} />
              )}
              {savedAgentMissing && (
                <ErrorNotice variant="inline" message={i18nT('pages.settings.slackPanel.slackCode.agent_missing', { agent: draft.agent })} />
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
                <PreviewRow icon={<ShieldCheck size={12} />} label={i18nT('pages.settings.slackPanel.slackCode.preview_approval')}
                  value={approvalLabel(draft.approval)}
                  source={draft.approval !== 'ask' ? sourceLabel('default') : undefined} />
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
      {confirmDialog}
    </SettingsSection>
  )
}
