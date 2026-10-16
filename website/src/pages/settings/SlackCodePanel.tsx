/**
 * Settings → Slack → Slack Code: the agent-sessions and code-channels switches,
 * the code-channel defaults (folder / agent / model / effort), and the
 * "Update your Slack app" panel.
 *
 * The draft lives in `SlackPanel` (one Save for the whole page); this module
 * owns the shape of the Slack Code slice of it, how it is read from the GET and
 * written to the PUT, and how it renders.
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Bot, CheckCircle2, Folder, FolderGit2, Info } from 'lucide-react'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import SimpleSelect from '../../components/SimpleSelect'
import ErrorNotice from '../../components/ErrorNotice'
import { useConfirm } from '../../components/ConfirmDialog'
import { api, type SlackCapability, type SlackConfigData, type SlackConfigSave } from '../../api/client'
import type { ChatFolder } from '../../types'
import { orderFoldersWithPaths } from '../../utils/folderTree'
import { useFolderSortMode } from '../../hooks/useFolderSortMode'
import { useAvailableModels } from '../../hooks/useAvailableModels'
import { EFFORT_LEVELS, effortLabel } from '../../lib/effort'
import { Field, INHERIT_MODEL, ModelField } from '../KiroCrewAgentsPageFields'
import { i18nT } from '../../i18n/t'
import { compareText } from '../../i18n/format'
import { SlackManifestUpdate } from './SlackManifestUpdate'

/** The Slack Code slice of the Slack settings draft. '' = not set. */
export interface SlackCodeDraft {
  agent_sessions: boolean
  code_channels: boolean
  folder: string
  agent: string
  /** INHERIT_MODEL ('auto') = keep the agent's own model. */
  model: string
  effort: string
}

export function slackCodeDraftFrom(c: SlackConfigData): SlackCodeDraft {
  const d = c.code_channel_defaults
  return {
    agent_sessions: !!c.agent_sessions,
    code_channels: !!c.code_channels,
    folder: d?.folder ?? '',
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
  if (d.agent_sessions !== saved.agent_sessions) out.agent_sessions = d.agent_sessions
  if (d.code_channels !== saved.code_channels) out.code_channels = d.code_channels
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

const SOURCE_KEY: Record<string, string> = {
  default: 'pages.settings.slackPanel.slackCode.source_default',
  folder: 'pages.settings.slackPanel.slackCode.source_folder',
  slack: 'pages.settings.slackPanel.slackCode.source_slack',
  origin: 'pages.settings.slackPanel.slackCode.source_origin',
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

const CAPABILITY_KEY: Record<string, string> = {
  missing_scope: 'pages.settings.slackPanel.slackCode.cap_missing_scope',
  feature_disabled: 'pages.settings.slackPanel.slackCode.cap_feature_disabled',
  not_allowed_token_type: 'pages.settings.slackPanel.slackCode.cap_token_type',
  scopes_unknown: 'pages.settings.slackPanel.slackCode.cap_scopes_unknown',
}

/** Whether a SAVED toggle is actually running: it also needs the installed app
 *  to hold the feature's scope, and Slack not to have refused it. Until then
 *  Slack behaves exactly as with the toggle off, and this line says why. */
function CapabilityLine({ capability }: { capability?: SlackCapability }) {
  if (!capability) return null
  if (capability.available) {
    return (
      <div className="flex items-center gap-1.5 text-[12px] text-ok mt-1" data-testid="slack-capability" data-available="true">
        <CheckCircle2 size={12} className="flex-none" /> {i18nT('pages.settings.slackPanel.slackCode.cap_active')}
      </div>
    )
  }
  const key = CAPABILITY_KEY[capability.reason] ?? CAPABILITY_KEY.scopes_unknown
  return (
    <div className="flex items-start gap-1.5 text-[12px] text-warn mt-1" data-testid="slack-capability" data-available="false" data-reason={capability.reason}>
      <AlertTriangle size={12} className="flex-none mt-0.5" /> <span>{i18nT(key, { scope: capability.scope })}</span>
    </div>
  )
}

export function SlackCodeSection({ data, draft, onChange, readOnly, manifestText }: {
  data: SlackConfigData
  draft: SlackCodeDraft
  onChange: (patch: Partial<SlackCodeDraft>) => void
  readOnly: boolean
  /** The manifest the SAVED switches render (GET /api/slack/manifest). */
  manifestText: string | undefined
}) {
  const { confirm, confirmDialog } = useConfirm()
  const { data: foldersData, isError: foldersFailed } = useQuery<ChatFolder[]>({
    queryKey: ['chat-folders'],
    queryFn: () => api.chatFolders(),
  })
  // A shape other than a list (an older gateway, an error body) reads as "no
  // folders" rather than crashing the whole Slack settings page.
  const folders = useMemo(() => (Array.isArray(foldersData) ? foldersData : []), [foldersData])
  const { data: installed, isError: agentsFailed } = useQuery({
    queryKey: ['agents-installed'],
    queryFn: () => api.agentsInstalled(),
  })
  const models = useAvailableModels()
  const { mode: sortMode } = useFolderSortMode()

  const folderOptions = useMemo(() => {
    const ordered = orderFoldersWithPaths(folders, sortMode)
    return { values: ordered.map(f => f.folder.id), labels: ordered.map(f => f.path) }
  }, [folders, sortMode])
  const agentOptions = useMemo(() => {
    const names = Array.isArray(installed)
      ? installed
        .filter((x: { name?: string; private_to?: string }) => !!x.name && !x.private_to)
        .map((x: { name: string }) => x.name)
      : []
    // Keep a saved agent visible even if it is no longer installed, so the
    // picker shows what is stored and the warning below explains it.
    if (draft.agent && !names.includes(draft.agent)) names.push(draft.agent)
    return names.sort(compareText)
  }, [installed, draft.agent])
  const modelOptions = [
    INHERIT_MODEL,
    ...(Array.isArray(models) ? models : []).map((m: { name: string }) => m.name).filter((n: string) => n && n !== INHERIT_MODEL),
  ]

  // The live resolution for the DRAFT: the server's answer for saved values,
  // recomputed here for an unsaved folder/agent pick so the preview follows
  // the pickers before Save.
  const eff = data.code_channel_effective
  const pickedFolder = folders.find(f => f.id === draft.folder) ?? null
  const folderPath = folderOptions.labels[folderOptions.values.indexOf(draft.folder)] ?? ''
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
      : slackAgent ? [slackAgent, 'slack'] : ['', '']
  const savedFolderMissing = (data.code_channel_warnings ?? []).includes('folder_missing')
    && draft.folder === (data.code_channel_defaults?.folder ?? '')
  const savedAgentMissing = (data.code_channel_warnings ?? []).includes('agent_missing')
    && draft.agent === (data.code_channel_defaults?.agent ?? '')

  const enableAgentSessions = async (on: boolean) => {
    if (!on) { onChange({ agent_sessions: false }); return }
    const ok = await confirm({
      title: i18nT('pages.settings.slackPanel.slackCode.agent_sessions_confirm_title'),
      body: (
        <div className="flex flex-col gap-2 text-[13px]">
          <p className="m-0">{i18nT('pages.settings.slackPanel.slackCode.agent_sessions_confirm_body')}</p>
          <p className="m-0 text-warn">{i18nT('pages.settings.slackPanel.slackCode.agent_sessions_irreversible')}</p>
        </div>
      ),
      confirmLabel: i18nT('pages.settings.slackPanel.slackCode.agent_sessions_confirm_ok'),
      danger: false,
      primary: true,
    })
    if (ok) onChange({ agent_sessions: true })
  }

  return (
    <SettingsSection title={i18nT('pages.settings.slackPanel.slackCode.section')}>
      <SettingsCard index={4}>
        <p className="text-[12px] text-muted m-0 mb-2">{i18nT('pages.settings.slackPanel.slackCode.intro')}</p>

        {/* ── Agent sessions ── */}
        <SettingsToggle
          label={i18nT('pages.settings.slackPanel.slackCode.agent_sessions')}
          description={i18nT('pages.settings.slackPanel.slackCode.agent_sessions_desc')}
          checked={draft.agent_sessions}
          onChange={v => { void enableAgentSessions(v) }}
          disabled={readOnly}
          configKey="slack.agent_sessions"
        />
        <div className="flex items-start gap-2 rounded-md border border-border bg-bg-elevated px-3 py-2 mt-1">
          <AlertTriangle size={13} className="text-warn flex-none mt-0.5" />
          <span className="text-[12px] text-muted">{i18nT('pages.settings.slackPanel.slackCode.agent_sessions_irreversible')}</span>
        </div>
        {data.agent_sessions && <CapabilityLine capability={data.agent_sessions_capability} />}

        {/* ── Code channels ── */}
        <div className="border-t border-border mt-4 pt-4">
          <SettingsToggle
            label={i18nT('pages.settings.slackPanel.slackCode.code_channels')}
            description={i18nT('pages.settings.slackPanel.slackCode.code_channels_desc')}
            checked={draft.code_channels}
            onChange={v => onChange({ code_channels: v })}
            disabled={readOnly}
            configKey="slack.code_channels"
          />
          {data.code_channels && <CapabilityLine capability={data.code_channels_capability} />}
        </div>

        {draft.code_channels && (
          <div className="mt-4 rounded-lg border border-border bg-bg-elevated p-4 flex flex-col gap-4" data-testid="slack-code-defaults">
            <div>
              <div className="text-[13px] font-semibold text-text">{i18nT('pages.settings.slackPanel.slackCode.defaults_title')}</div>
              <div className="text-[12px] text-muted mt-0.5">{i18nT('pages.settings.slackPanel.slackCode.defaults_desc')}</div>
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label={i18nT('pages.settings.slackPanel.slackCode.folder')} hint={i18nT('pages.settings.slackPanel.slackCode.folder_hint')}>
                <SimpleSelect
                  options={folderOptions.values}
                  optionLabels={folderOptions.labels}
                  value={draft.folder}
                  onChange={v => onChange({ folder: v })}
                  clearLabel={i18nT('pages.settings.slackPanel.slackCode.folder_none')}
                  disabled={readOnly}
                  aria-label={i18nT('pages.settings.slackPanel.slackCode.folder')}
                />
              </Field>
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
              <ModelField options={modelOptions} value={draft.model} onChange={v => onChange({ model: v })} hint={i18nT('pages.settings.slackPanel.slackCode.model_hint')} />
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

            {/* No hand-off for any of these three: they sit beside the unsaved
                Slack draft (the pickers above, the token fields and owner id
                further up), and the hand-off navigates away, which would
                discard it. The fix for each is on this card: reload the lists,
                or pick another folder or agent. */}
            {(foldersFailed || agentsFailed) && (
              <ErrorNotice variant="inline" message={i18nT('pages.settings.slackPanel.slackCode.options_unavailable')} />
            )}
            {savedFolderMissing && (
              <ErrorNotice variant="inline" message={i18nT('pages.settings.slackPanel.slackCode.folder_missing')} />
            )}
            {savedAgentMissing && (
              <ErrorNotice variant="inline" message={i18nT('pages.settings.slackPanel.slackCode.agent_missing', { agent: draft.agent })} />
            )}

            {/* What a new code channel will actually run with. */}
            <div className="rounded-md border border-border bg-bg px-3 py-2.5 flex flex-col gap-1.5" data-testid="slack-code-preview">
              <div className="text-[11px] uppercase tracking-wider text-muted font-medium">{i18nT('pages.settings.slackPanel.slackCode.preview_title')}</div>
              <PreviewRow icon={<Folder size={12} />} label={i18nT('pages.settings.slackPanel.slackCode.preview_folder')}
                value={folderPath || i18nT('pages.settings.slackPanel.slackCode.preview_unfiled')} />
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
              <span>{i18nT('pages.settings.slackPanel.slackCode.resolution_order')}</span>
            </div>
          </div>
        )}

        <SlackManifestUpdate data={data} draft={draft} manifestText={manifestText} />
      </SettingsCard>
      {confirmDialog}
    </SettingsSection>
  )
}
