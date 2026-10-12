/**
 * CrewEditorNextDialog — the crewmate editor, rendered as a modal. This is the
 * single editing experience (the earlier Classic/New A/B toggle and the classic
 * dialog have been retired); every Edit entry point opens it.
 *
 * Driven by the CrewEditorController from useCrewEditor, so everything here
 * saves through the shared state machine and reuses the real field components.
 * The header shows the mate's identity; the X close button is rendered by
 * DialogContent itself.
 */
import { Loader2, Terminal, Globe, ChevronRight, Lock } from 'lucide-react'
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogBody, DialogFooter,
} from '../ui/dialog'
import { Btn, SendBtn } from '../ui'
import { i18nT } from '../../i18n/t'
import SimpleSelect from '../SimpleSelect'
import { toolMeta, toolCategoryLabel, TOOL_CATEGORY_ORDER, type ToolCategory } from './toolMeta'
import CrewAvatar from '../CrewAvatar'
import CrewAvatarBuilder from '../CrewAvatarBuilder'
import CrewAvatarButton from './CrewAvatarButton'
import { retiredCueFrom, motionsFrom, soundsFrom } from '../../lib/crewAvatarState'
import { DisplayNameField, Field, WorkspaceField, TriggersField, WorkspaceModal, ModelField, EffortField, SessionColorField, memberMemoryState } from '../../pages/KiroCrewAgentsPage'
import ErrorNotice from '../ErrorNotice'
import { useConfirm } from '../ConfirmDialog'
import { effortLabel } from '../../lib/effort'
import { crewCapabilitiesApi, crewCapabilitiesKey, type CapabilityRow } from '../../api/crewCapabilities'
import CrewApprovalsEditor from './CrewApprovalsEditor'
import CrewWakeSection from '../CrewWakeSection'
import CrewWebhookSection from '../CrewWebhookSection'
import MemoryDocCard from '../../pages/overview/MemoryDocCard'
import MemoryRecordsEditor from '../../pages/overview/MemoryRecordsEditor'
import MemoryBackupsCard from '../../pages/overview/MemoryBackupsCard'
import MemoryRetiredCard from '../../pages/overview/MemoryRetiredCard'
import MemoryCarveCard from '../../pages/overview/MemoryCarveCard'
import { api } from '../../api/client'
import { type CrewEditorController } from './useCrewEditor'

type NextSection = 'general' | 'builtFrom' | 'behavior' | 'memory' | 'danger'
type BuiltFromTab = 'details' | 'tools' | 'mcp' | 'skills'
type BehaviorTab = 'approvals' | 'model' | 'triggers' | 'schedules' | 'webhooks'
type MemoryTab = 'profile' | 'memories' | 'recovery'

/** Rows the template actually declares for a section (present ones only). */
function presentRows(rows: CapabilityRow[], section: CapabilityRow['section']): CapabilityRow[] {
  return rows.filter(r => r.section === section && r.present)
}

/** Read-only Tools inspector: grouped by category, with read-only / can-modify
 *  and auto-approve badges, a count, and a text filter. Display-only. */
function ToolsTab({
  rows, autoApproved, filter, onFilter,
}: {
  rows: CapabilityRow[]
  autoApproved: Set<string>
  filter: string
  onFilter: (v: string) => void
}) {
  const q = filter.trim().toLowerCase()
  const matches = rows.filter(r => {
    if (!q) return true
    const m = toolMeta(r.id)
    return r.id.toLowerCase().includes(q) || m.label.toLowerCase().includes(q) || m.description.toLowerCase().includes(q)
  })

  // Bucket by category in the fixed display order.
  const byCategory = new Map<ToolCategory, CapabilityRow[]>()
  for (const r of matches) {
    const cat = toolMeta(r.id).category
    const list = byCategory.get(cat) ?? []
    list.push(r)
    byCategory.set(cat, list)
  }

  return (
    <div className="flex flex-col gap-4" data-testid="next-builtfrom-tools">
      <div className="flex items-center justify-between gap-3">
        <span className="text-[11px] uppercase tracking-wide text-muted">{rows.length} {i18nT('components.crew.crewEditorNextDialog.tools')}</span>
        <input
          type="text"
          value={filter}
          onChange={e => onFilter(e.target.value)}
          placeholder={i18nT("crewEditorNext.filter_tools")}
          aria-label={i18nT("crewEditorNext.filter_tools_aria")}
          className="w-48 rounded-md border border-border bg-bg px-2.5 py-1 text-[12.5px] text-text focus-ring"
        />
      </div>

      {matches.length === 0 && (
        <div className="text-[12px] text-muted">{i18nT('components.crew.crewEditorNextDialog.no_tools_match', { filter })}</div>
      )}

      {TOOL_CATEGORY_ORDER.map(cat => {
        const list = byCategory.get(cat)
        if (!list || list.length === 0) return null
        return (
          <div key={cat} className="flex flex-col gap-1.5">
            <div className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-strong">
              {toolCategoryLabel(cat)}
            </div>
            {list.map(r => {
              const m = toolMeta(r.id)
              const auto = autoApproved.has(r.id)
              return (
                <div
                  key={`${r.section}:${r.id}`}
                  className="flex items-start justify-between gap-3 rounded-md border border-border bg-bg-accent px-3 py-2"
                >
                  <div className="flex min-w-0 flex-col">
                    <span className="truncate text-[12.5px] text-text">{m.label}</span>
                    {m.description && <span className="truncate text-[11px] text-muted">{m.description}</span>}
                    <span className="mt-0.5 font-mono text-[10.5px] text-muted-strong">{r.id}</span>
                  </div>
                  <div className="flex shrink-0 flex-col items-end gap-1">
                    {m.readOnly === true && (
                      <span className="rounded px-1.5 py-0.5 text-[10px] font-medium text-ok">{i18nT('components.crew.crewEditorNextDialog.read_only')}</span>
                    )}
                    {m.readOnly === false && (
                      <span className="rounded px-1.5 py-0.5 text-[10px] font-medium text-warn">{i18nT('components.crew.crewEditorNextDialog.can_modify_run')}</span>
                    )}
                    {auto && (
                      <span className="rounded px-1.5 py-0.5 text-[10px] text-muted">{i18nT('components.crew.crewEditorNextDialog.runs_without_asking')}</span>
                    )}
                    {r.state !== 'inherited' && (
                      <span className="rounded px-1.5 py-0.5 text-[10px] text-muted">
                        {r.state === 'local' ? i18nT('crewEditorNext.state_added_short') : i18nT('crewEditorNext.state_removed_short')}
                      </span>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        )
      })}
    </div>
  )
}

/** Read-only MCP Servers inspector. Each row resolves the real invocation
 *  (binary + its distinguishing args, not a bare "+N arg") so otherwise-identical
 *  rows read differently; transport is a glyph; the "managed by template" fact is
 *  hoisted to a single header when every server shares it. A row expands on click
 *  to reveal the full invocation, env/header NAMES (values already redacted by the
 *  backend) and transport specifics. Display-only; never exposes a secret. */
function parseTransport(value: unknown) {
  const t = (value && typeof value === 'object' && !Array.isArray(value))
    ? value as Record<string, unknown>
    : {}
  const isHttp = typeof t.url === 'string'
  const command = typeof t.command === 'string' ? t.command : ''
  const url = typeof t.url === 'string' ? t.url : ''
  const args = Array.isArray(t.args) ? t.args.filter((a): a is string => typeof a === 'string') : []
  const env = (t.env && typeof t.env === 'object' && !Array.isArray(t.env)) ? t.env as Record<string, unknown> : {}
  const headers = (t.headers && typeof t.headers === 'object' && !Array.isArray(t.headers)) ? t.headers as Record<string, unknown> : {}
  const disabled = t.disabled === true
  return { isHttp, command, url, args, env, headers, disabled }
}

/** One row of the MCP list: a compact summary that expands to full detail. */
function McpServerRow({ row, showManagedBadge }: { row: CapabilityRow; showManagedBadge: boolean }) {
  const [open, setOpen] = useState(false)
  const { isHttp, command, url, args, env, headers, disabled } = parseTransport(row.value)

  // Compact summary line: binary + its args (stdio) or URL host (HTTP).
  let summary = '—'
  if (isHttp) {
    try { summary = url ? new URL(url).host : '—' } catch { summary = url || '—' }
  } else if (command) {
    summary = [command.split('/').pop() || command, ...args].join(' ')
  }

  const Icon = isHttp ? Globe : Terminal
  const envNames = Object.keys(env)
  const headerNames = Object.keys(headers)
  const detailId = `mcp-detail-${row.id}`

  return (
    <div
      className={[
        'rounded-md border border-border bg-bg-accent',
        disabled ? 'opacity-60' : '',
      ].join(' ')}
    >
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        aria-controls={detailId}
        data-testid={`next-mcp-row-${row.id}`}
        className="flex w-full items-start justify-between gap-3 rounded-md px-3 py-2 text-left focus-ring hover:bg-bg-hover"
      >
        <div className="flex min-w-0 items-start gap-2.5">
          <ChevronRight
            size={14}
            className={['lucide-inline mt-0.5 shrink-0 text-muted transition-transform', open ? 'rotate-90' : ''].join(' ')}
            aria-hidden
          />
          <Icon
            size={14}
            className="lucide-inline mt-0.5 shrink-0 text-muted"
            aria-label={isHttp ? i18nT('components.crew.crewEditorNextDialog.transport_http') : i18nT('components.crew.crewEditorNextDialog.transport_stdio')}
          />
          <div className="flex min-w-0 flex-col">
            <span className="truncate text-[12.5px] text-text">{row.label || row.id}</span>
            <span className="truncate font-mono text-[11px] text-muted">{summary}</span>
          </div>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          {disabled && <span className="rounded px-1.5 py-0.5 text-[10px] font-medium text-warn">{i18nT('components.crew.crewEditorNextDialog.off')}</span>}
          {showManagedBadge && row.managed && <span className="rounded px-1.5 py-0.5 text-[10px] text-muted">{i18nT('components.crew.crewEditorNextDialog.managed')}</span>}
          {row.shared_reference && <span className="rounded px-1.5 py-0.5 text-[10px] text-muted">{i18nT('components.crew.crewEditorNextDialog.shared')}</span>}
          {row.state !== 'inherited' && (
            <span className="rounded px-1.5 py-0.5 text-[10px] text-muted">{row.state === 'local' ? i18nT('crewEditorNext.state_added_short') : i18nT('crewEditorNext.state_removed_short')}</span>
          )}
        </div>
      </button>

      {open && (
        <dl
          id={detailId}
          className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 border-t border-border px-3 py-2.5 text-[11.5px]"
          data-testid={`next-mcp-detail-${row.id}`}
        >
          <dt className="text-muted">{i18nT('components.crew.crewEditorNextDialog.server_id')}</dt>
          <dd className="m-0 break-all font-mono text-text">{row.id}</dd>

          <dt className="text-muted">{i18nT('components.crew.crewEditorNextDialog.transport')}</dt>
          <dd className="m-0 text-text">{isHttp ? i18nT('components.crew.crewEditorNextDialog.transport_http_value') : i18nT('components.crew.crewEditorNextDialog.transport_stdio_value')}</dd>

          {isHttp ? (
            <>
              <dt className="text-muted">{i18nT('components.crew.crewEditorNextDialog.url')}</dt>
              <dd className="m-0 break-all font-mono text-text">{url || '—'}</dd>
              {headerNames.length > 0 && (
                <>
                  <dt className="text-muted">{i18nT('components.crew.crewEditorNextDialog.headers')}</dt>
                  <dd className="m-0 font-mono text-text">{headerNames.join(', ')}</dd>
                </>
              )}
            </>
          ) : (
            <>
              <dt className="text-muted">{i18nT('components.crew.crewEditorNextDialog.command')}</dt>
              <dd className="m-0 break-all font-mono text-text">{command || '—'}</dd>
              <dt className="text-muted">{i18nT('components.crew.crewEditorNextDialog.args')}</dt>
              <dd className="m-0 break-all font-mono text-text">{args.length ? args.join(' ') : '—'}</dd>
              {envNames.length > 0 && (
                <>
                  <dt className="text-muted">{i18nT('components.crew.crewEditorNextDialog.env')}</dt>
                  <dd className="m-0 font-mono text-text">{envNames.join(', ')}</dd>
                </>
              )}
            </>
          )}

          <dt className="text-muted">{i18nT('components.crew.crewEditorNextDialog.state')}</dt>
          <dd className="m-0 text-text">
            {row.state === 'inherited' ? i18nT('crewEditorNext.state_from_template') : row.state === 'local' ? i18nT('crewEditorNext.state_added') : i18nT('crewEditorNext.state_removed')}
            {disabled && <span className="text-muted"> · {i18nT('crewEditorNext.state_disabled_word')}</span>}
          </dd>
        </dl>
      )}
    </div>
  )
}

function McpServersTab({ rows }: { rows: CapabilityRow[] }) {
  if (rows.length === 0) {
    return <div className="text-[12px] text-muted" data-testid="next-builtfrom-mcp">{i18nT('components.crew.crewEditorNextDialog.this_template_declares_no_mcp_servers')}</div>
  }

  // Hoist "managed by template" to a single header line when ALL servers share it,
  // instead of repeating an identical badge on every row.
  const allManaged = rows.every(r => r.managed)

  return (
    <div className="flex flex-col gap-1.5" data-testid="next-builtfrom-mcp">
      <div className="mb-1 flex items-center justify-between gap-3">
        <span className="text-[11px] uppercase tracking-wide text-muted">{rows.length} {i18nT('components.crew.crewEditorNextDialog.servers')}</span>
        {allManaged && <span className="text-[11px] text-muted">{i18nT('components.crew.crewEditorNextDialog.all_managed_by_template')}</span>}
      </div>
      {rows.map(r => (
        <McpServerRow key={`${r.section}:${r.id}`} row={r} showManagedBadge={!allManaged} />
      ))}
    </div>
  )
}

/** Split a skill id into owner/namespace + skill name, and de-slugify the name
 *  into a readable Title Case label (hyphens/underscores → spaces). An id with no
 *  "/" is treated as a bare skill name with no owner. */
function parseSkillId(id: string): { owner: string | null; name: string; display: string } {
  const slash = id.lastIndexOf('/')
  const owner = slash >= 0 ? id.slice(0, slash) : null
  const name = slash >= 0 ? id.slice(slash + 1) : id
  const display = name
    .replace(/[-_]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\b\w/g, c => c.toUpperCase())
  return { owner, name, display: display || name }
}

/** One skill row: readable Title-Case name + muted owner, expands to show the
 *  raw id, owner, state and shared-reference. Display-only; no description/path
 *  is available from the capability payload (would need backend work). */
function SkillRow({ row, showTemplateBadge }: { row: CapabilityRow; showTemplateBadge: boolean }) {
  const [open, setOpen] = useState(false)
  const { owner, display } = parseSkillId(row.id)
  const detailId = `skill-detail-${row.id}`
  return (
    <div className="rounded-md border border-border bg-bg-accent">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        aria-controls={detailId}
        data-testid={`next-skill-row-${row.id}`}
        className="flex w-full items-start justify-between gap-3 rounded-md px-3 py-2 text-left focus-ring hover:bg-bg-hover"
      >
        <div className="flex min-w-0 items-start gap-2.5">
          <ChevronRight
            size={14}
            className={['lucide-inline mt-0.5 shrink-0 text-muted transition-transform', open ? 'rotate-90' : ''].join(' ')}
            aria-hidden
          />
          <div className="flex min-w-0 flex-col">
            <span className="truncate text-[12.5px] text-text">{row.label || display}</span>
            {owner && <span className="truncate font-mono text-[11px] text-muted">{owner}</span>}
          </div>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          {row.shared_reference && <span className="rounded px-1.5 py-0.5 text-[10px] text-muted">{i18nT('components.crew.crewEditorNextDialog.shared')}</span>}
          {showTemplateBadge && row.state === 'inherited' && (
            <span className="rounded px-1.5 py-0.5 text-[10px] text-muted">{i18nT('crewEditorNext.state_from_template')}</span>
          )}
          {row.state !== 'inherited' && (
            <span className="rounded px-1.5 py-0.5 text-[10px] text-muted">{row.state === 'local' ? i18nT('crewEditorNext.state_added_short') : i18nT('crewEditorNext.state_removed_short')}</span>
          )}
        </div>
      </button>

      {open && (
        <dl
          id={detailId}
          className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 border-t border-border px-3 py-2.5 text-[11.5px]"
          data-testid={`next-skill-detail-${row.id}`}
        >
          <dt className="text-muted">{i18nT('components.crew.crewEditorNextDialog.skill_id')}</dt>
          <dd className="m-0 break-all font-mono text-text">{row.id}</dd>
          {owner && (
            <>
              <dt className="text-muted">{i18nT('components.crew.crewEditorNextDialog.owner')}</dt>
              <dd className="m-0 font-mono text-text">{owner}</dd>
            </>
          )}
          <dt className="text-muted">{i18nT('components.crew.crewEditorNextDialog.state')}</dt>
          <dd className="m-0 text-text">
            {row.state === 'inherited' ? i18nT('crewEditorNext.state_from_template') : row.state === 'local' ? i18nT('crewEditorNext.state_added') : i18nT('crewEditorNext.state_removed')}
          </dd>
          <dt className="text-muted">{i18nT('components.crew.crewEditorNextDialog.shared_reference')}</dt>
          <dd className="m-0 text-text">{row.shared_reference ? i18nT('components.crew.crewEditorNextDialog.shared_yes') : i18nT('components.crew.crewEditorNextDialog.shared_no')}</dd>
        </dl>
      )}
    </div>
  )
}

/** Read-only Skills inspector: de-slugified name + owner, a count + filter, the
 *  universal "from template" fact hoisted to a header, and per-skill expand. */
function SkillsTab({ rows, filter, onFilter }: { rows: CapabilityRow[]; filter: string; onFilter: (v: string) => void }) {
  if (rows.length === 0) {
    return <div className="text-[12px] text-muted" data-testid="next-builtfrom-skills">{i18nT('components.crew.crewEditorNextDialog.this_template_declares_no_skills')}</div>
  }
  const q = filter.trim().toLowerCase()
  const matches = rows.filter(r => {
    if (!q) return true
    const { owner, display } = parseSkillId(r.id)
    return r.id.toLowerCase().includes(q) || display.toLowerCase().includes(q) || (owner?.toLowerCase().includes(q) ?? false)
  })
  // Hoist "from template" to a header when every skill is inherited.
  const allInherited = rows.every(r => r.state === 'inherited')

  return (
    <div className="flex flex-col gap-1.5" data-testid="next-builtfrom-skills">
      <div className="mb-1 flex items-center justify-between gap-3">
        <span className="text-[11px] uppercase tracking-wide text-muted">{rows.length} {i18nT('components.crew.crewEditorNextDialog.skills')}</span>
        <div className="flex items-center gap-3">
          {allInherited && <span className="text-[11px] text-muted">{i18nT('components.crew.crewEditorNextDialog.all_from_template')}</span>}
          <input
            type="text"
            value={filter}
            onChange={e => onFilter(e.target.value)}
            placeholder={i18nT("crewEditorNext.filter_skills")}
            aria-label={i18nT("crewEditorNext.filter_skills_aria")}
            className="w-44 rounded-md border border-border bg-bg px-2.5 py-1 text-[12.5px] text-text focus-ring"
          />
        </div>
      </div>
      {matches.length === 0 && <div className="text-[12px] text-muted">{i18nT('components.crew.crewEditorNextDialog.no_skills_match', { filter })}</div>}
      {matches.map(r => (
        <SkillRow key={`${r.section}:${r.id}`} row={r} showTemplateBadge={!allInherited} />
      ))}
    </div>
  )
}

export default function CrewEditorNextDialog({
  ctl,
}: {
  ctl: CrewEditorController
}) {
  // Which sidebar section is showing. Local; hook declared before any early
  // return so hook order stays stable.
  const [section, setSection] = useState<NextSection>('general')
  // Which Built From sub-tab is showing.
  const [builtFromTab, setBuiltFromTab] = useState<BuiltFromTab>('details')
  // Which Behavior sub-tab is showing.
  const [behaviorTab, setBehaviorTab] = useState<BehaviorTab>('approvals')
  // Which Memory sub-tab is showing.
  const [memoryTab, setMemoryTab] = useState<MemoryTab>('profile')
  // Free-text filter for the Tools tab.
  const [toolFilter, setToolFilter] = useState('')
  // Free-text filter for the Skills tab.
  const [skillFilter, setSkillFilter] = useState('')

  // Read-only capability view of the agent template this crewmate is built on.
  // Same source the classic Capabilities pane reads; here it is display-only.
  const capsQuery = useQuery({
    queryKey: ctl.open ? crewCapabilitiesKey(ctl.editing) : ['crew-capabilities', '__closed__'],
    queryFn: () => crewCapabilitiesApi.get(ctl.editing),
    enabled: ctl.open,
    retry: false,
  })
  const { confirm, confirmDialog } = useConfirm()

  if (!ctl.open) {
    if (ctl.loading) {
      return (
        <Dialog open onOpenChange={next => { if (!next) ctl.requestClose() }}>
          <DialogContent maxWidth={620} aria-busy data-testid="crew-editor-next-loading">
            <DialogTitle className="sr-only">{i18nT('pages.chatPage.loading')}</DialogTitle>
            <div className="flex items-center justify-center gap-2 py-10 text-[13px] text-muted">
              <Loader2 size={16} className="lucide-inline animate-spin" />
              {i18nT('pages.chatPage.loading')}
            </div>
          </DialogContent>
        </Dialog>
      )
    }
    return null
  }

  const {
    editing, editingAgent, displayName, setDisplayName,
    kiroAgent, kiroAgentOptions, templateFieldLabel, persistTemplateSwitch, templateSwitchError,
    templateProvenance, capabilityDirty, capabilityBusy, capabilityLoading, capabilityReadFailed,
    editAvatar, openAvatarBuilder, avatarBuilderOpen, applyAvatar, closeAvatarBuilder, onAvatarImageError,
    sheetBusy, dirtyPanes, schedDraft, requestClose, save, resolved,
    error, sheetHint, editorOptionsError,
    childDirtyHandlers, setCapabilityBusy, memoryStore, onCapabilitiesSaved, kirocrewCfg,
    workspace, setWorkspace, triggers, setTriggers, workspaceOptions,
    sessionColor, setSessionColor,
    wsModalOpen, openWsModal, closeWsModal, onWorkspaceCreated, setWsDirty, onPaneSaveChain,
    editModel, setEditModel, editEffort, setEditEffort,
    modelOptions, modelsDegraded, resolvedError, effortCapable, effortModel,
    memberId, isDefaultCrew, guardedNavigate,
    setSchedDraft, setSchedSaving, requestCancelDraft,
    confirmDelete, setConfirmDelete, deleteCrew, confirmRef,
    discardAsk, setDiscardAsk, discardTakesSheet, askSchedOnly, schedSaving, discardForce, confirmDiscard,
  } = ctl

  // A nested child panel (approvals, a memory doc, the memory records editor)
  // owns its OWN unsaved text; the host cannot discard it on the child's
  // behalf. Switching section/subtab unmounts that panel, which would destroy
  // the draft and clear its dirty flag with no confirmation. So navigation is
  // blocked while such a draft is live (the unsaved note tells the user to
  // save or discard it in place first) and while a save is in flight — the
  // schedule draft keeps its own prompt via the controller's discardAsk.
  const childDraftLive = dirtyPanes.has('capabilities') || schedDraft
  const guardNav = (perform: () => void) => {
    if (sheetBusy || childDraftLive) return
    perform()
  }

  // Switching "Built from" rebinds the mate. If the current binding is this
  // mate's OWN private copy (private_to === editing), the switch silently drops
  // that customized copy — and it is filtered out of the options, so it cannot
  // be re-selected. Mirror the classic editor: confirm (discard_and_switch)
  // before leaving an own-copy binding; the plain switch runs straight through.
  const onTemplateSwitch = (v: string) => {
    if (v === kiroAgent) return
    const leavingOwnCopy = templateProvenance[kiroAgent]?.private_to === editing
    if (leavingOwnCopy) {
      void (async () => {
        const ok = await confirm({
          title: i18nT('crewEditorNext.switch_own_copy_title'),
          body: i18nT('crewEditorNext.switch_own_copy_body'),
          confirmLabel: i18nT('components.agentTemplateDetail.discard_and_switch'),
          layer: 'top',
        })
        if (ok) persistTemplateSwitch(v)
      })()
      return
    }
    persistTemplateSwitch(v)
  }
  // The classic selector was read-only while the capability view was dirty,
  // busy, loading or failed — a switch then would discard an in-flight edit or
  // act on an unknown state. Keep that guard.
  const templateSwitchLocked = sheetBusy || capabilityDirty || capabilityBusy || capabilityLoading || capabilityReadFailed

  // "Built from" options: keep the current template visible even if it is not
  // in the installed list (parity with the classic selector's `opts`).
  const templateOptions = kiroAgent && !kiroAgentOptions.includes(kiroAgent)
    ? [kiroAgent, ...kiroAgentOptions]
    : kiroAgentOptions

  const caps = capsQuery.data
  // Tools auto-approved to run without a prompt: present rows in `allowedTools`.
  const autoApprovedTools = new Set(
    (caps?.rows ?? []).filter(r => r.section === 'allowedTools' && r.present).map(r => r.id),
  )

  const sections: { key: NextSection; label: string }[] = [
    { key: 'general', label: i18nT('crewEditorNext.section_general') },
    { key: 'builtFrom', label: i18nT('crewEditorNext.section_built_from') },
    { key: 'behavior', label: i18nT('crewEditorNext.section_behavior') },
    { key: 'memory', label: i18nT('crewEditorNext.section_memory') },
    // The default crewmate cannot be deleted — the backend rejects it with 409
    // — so the classic editor hid its Danger Zone. Keep that guard.
    ...(isDefaultCrew ? [] : [{ key: 'danger' as NextSection, label: i18nT('crewEditorNext.section_danger') }]),
  ]

  // The dialog stays mounted across opens and `section` is not reset per mate,
  // so a section selected for a previous mate (e.g. 'danger') can be absent from
  // THIS mate's rail (the default mate has no Danger Zone) — which would render
  // no pane and no selected tab, a blank body. Fall back to the first section.
  const effectiveSection: NextSection = sections.some(s => s.key === section) ? section : 'general'

  // privateMemory is NOT always true: the default mate's store is the global
  // legacy store, where MemoryRecordsEditor must run in global (not private)
  // mode. Resolve the real state and pass private only when it is private V2.
  const memoryIsPrivate = memberMemoryState(editing, memoryStore, kirocrewCfg?.memory_stores) === 'private'

  const builtFromTabs: { key: BuiltFromTab; label: string }[] = [
    { key: 'details', label: i18nT('crewEditorNext.tab_details') },
    { key: 'tools', label: i18nT('crewEditorNext.tab_tools') },
    { key: 'mcp', label: i18nT('crewEditorNext.tab_mcp') },
    { key: 'skills', label: i18nT('crewEditorNext.tab_skills') },
  ]

  const behaviorTabs: { key: BehaviorTab; label: string }[] = [
    { key: 'approvals', label: i18nT('crewEditorNext.tab_approvals') },
    { key: 'model', label: i18nT('crewEditorNext.tab_model') },
    { key: 'triggers', label: i18nT('crewEditorNext.tab_triggers') },
    { key: 'schedules', label: i18nT('crewEditorNext.tab_schedules') },
    { key: 'webhooks', label: i18nT('crewEditorNext.tab_webhooks') },
  ]

  const memoryTabs: { key: MemoryTab; label: string }[] = [
    { key: 'profile', label: i18nT('crewEditorNext.tab_profile') },
    { key: 'memories', label: i18nT('memoryV2.tab_memories') },
    { key: 'recovery', label: i18nT('crewEditorNext.tab_recovery') },
  ]

  return (
    <>
      <Dialog open onOpenChange={next => { if (!next) requestClose() }}>
        <DialogContent
          maxWidth={1200}
          style={{ height: 'min(90vh, 760px)' }}
          aria-label={i18nT('crewEditorNext.editor_aria')}
          data-testid="crew-editor-next-dialog"
        >
          {/* Header: the mate's identity (avatar + name). The X lives in
              DialogContent's corner. */}
          <DialogHeader>
            <div className="flex w-full items-center gap-3 pr-8">
              <CrewAvatar seed={editing} avatar={editAvatar ?? undefined} size={30} onImageError={onAvatarImageError} />
              <DialogTitle className="flex-1">
                {displayName.trim() || editing}
              </DialogTitle>
            </div>
          </DialogHeader>

          {/* A failed options read (installed templates / workspaces / config)
              must not pass the ['kirocrew'] / ['default'] fallbacks off as the
              real lists — a Save would then persist a binding the operator never
              chose. askAgent=false: the hand-off navigates to /chat and would
              discard this editor's unsaved edits. */}
          <ErrorNotice
            className="mx-5 mt-1"
            title={i18nT('pages.kiroCrewAgentsPage.editor_options_load_failed')}
            message={editorOptionsError ? (editorOptionsError instanceof Error ? editorOptionsError.message : String(editorOptionsError)) : null}
            askAgent={false}
            testId="next-editor-options-load-error"
          />

          <div className="flex min-h-0 flex-1 flex-col overflow-hidden sm:flex-row">
            {/* Sidebar */}
            <div
              role="tablist"
              aria-orientation="vertical"
              aria-label={i18nT("crewEditorNext.sections_aria")}
              className="flex shrink-0 gap-px overflow-x-auto border-b border-border bg-bg-accent p-2
                         sm:w-[180px] sm:flex-col sm:overflow-x-visible sm:border-b-0 sm:border-r"
            >
              {sections.map(s => {
                const isActive = s.key === effectiveSection
                const isDanger = s.key === 'danger'
                return (
                  <button
                    key={s.key}
                    type="button"
                    role="tab"
                    aria-selected={isActive}
                    data-testid={`next-rail-${s.key}`}
                    onClick={() => guardNav(() => setSection(s.key))}
                    disabled={!isActive && (sheetBusy || childDraftLive)}
                    className={[
                      'flex w-full rounded-md px-2.5 py-1.5 text-left text-[12.5px] focus-ring',
                      // Danger Zone sits directly below the ordinary sections,
                      // with a small top margin to set it off from the group
                      // above; its resting tint is danger-toned so it never
                      // reads as a routine pane.
                      isDanger ? 'mt-2' : '',
                      isActive
                        ? (isDanger ? 'bg-danger-subtle text-danger' : 'bg-accent-subtle text-text')
                        : (isDanger
                            ? 'text-danger hover:bg-danger-subtle'
                            : 'text-muted hover:bg-bg-hover hover:text-text'),
                    ].join(' ')}
                  >
                    {s.label}
                  </button>
                )
              })}
            </div>

            {/* Panel */}
            <div
              role="tabpanel"
              className="flex min-h-0 min-w-0 flex-1 flex-col gap-5 overflow-y-auto px-6 py-5"
              data-testid="crew-editor-next"
            >
              {/* Panel heading — names the selected section */}
              <h2 className="m-0 text-[15px] font-semibold text-text" data-testid="next-panel-title">
                {sections.find(s => s.key === effectiveSection)?.label}
              </h2>

              {effectiveSection === 'general' && (
                <fieldset disabled={sheetBusy} className="flex flex-col gap-6 border-0 p-0 m-0 min-w-0" style={{ maxWidth: 520 }}>
                  {/* Avatar row — the name now lives in the header, so this is
                      just the avatar + its edit button. */}
                  <div className="flex items-center gap-4">
                    <CrewAvatarButton size={56} onEdit={openAvatarBuilder} disabled={sheetBusy} data-testid="next-avatar-button">
                      <CrewAvatar seed={editing} avatar={editAvatar ?? undefined} size={56} onImageError={onAvatarImageError} />
                    </CrewAvatarButton>
                    <div className="flex flex-col gap-1">
                      <Btn onClick={openAvatarBuilder} disabled={sheetBusy} data-testid="next-open-avatar-builder">
                        {i18nT('components.avatarBuilder.edit_avatar')}
                      </Btn>
                      {editAvatar && (
                        <span className="text-[11px] text-muted">{i18nT('components.avatarBuilder.customized_note')}</span>
                      )}
                    </div>
                  </div>

                  {/* Single clean column — each field is itself: label on top,
                      control, hint below. No side-by-side columns, no row
                      wrappers wrapping components that already carry their own
                      label/hint. This is the layout these fields are built for. */}
                  <DisplayNameField value={displayName} onChange={setDisplayName} fallback={editing} />

                  <Field label={templateFieldLabel} hint={i18nT('crewEditorNext.built_from_hint')}>
                    <SimpleSelect
                      options={templateOptions}
                      value={kiroAgent}
                      onChange={onTemplateSwitch}
                      disabled={templateSwitchLocked}
                      aria-label={templateFieldLabel}
                    />
                    {/* A refused repoint (foreign private copy, stale binding,
                        unverifiable lineage) must say why — otherwise the
                        dropdown silently snaps back and the user is left
                        guessing. The controller already produces the reason.
                        No hand-off: the hand-off navigates to /chat and would
                        discard the open editor's unsaved crew fields (display
                        name, workspace, model, color) and any approvals/memory
                        draft, so keep it off here. */}
                    <ErrorNotice
                      message={templateSwitchError}
                      variant="inline"
                      askAgent={false}
                      testId="next-template-switch-error"
                    />
                  </Field>

                  <WorkspaceField
                    options={workspaceOptions}
                    value={workspace}
                    onChange={setWorkspace}
                    onNewWorkspace={openWsModal}
                    subject="agent"
                    hint={(!workspace || workspace === 'default')
                      ? i18nT('crewEditorNext.workspace_hint_default')
                      : i18nT('crewEditorNext.workspace_hint_custom')}
                  />

                  <SessionColorField value={sessionColor} onChange={setSessionColor} subject="agent" />
                </fieldset>
              )}

              {effectiveSection === 'builtFrom' && (
                <div className="flex min-h-0 flex-1 flex-col" data-testid="next-pane-builtFrom">
                  {/* Horizontal tab switcher */}
                  <div
                    role="tablist"
                    aria-label={i18nT("crewEditorNext.builtfrom_sections_aria")}
                    className="flex shrink-0 gap-1 border-b border-border"
                  >
                    {builtFromTabs.map(t => {
                      const isActive = t.key === builtFromTab
                      return (
                        <button
                          key={t.key}
                          type="button"
                          role="tab"
                          aria-selected={isActive}
                          data-testid={`next-builtfrom-tab-${t.key}`}
                          onClick={() => setBuiltFromTab(t.key)}
                          className={[
                            'relative -mb-px px-3 py-2 text-[12.5px] focus-ring',
                            isActive
                              ? 'border-b-2 border-accent font-medium text-text'
                              : 'border-b-2 border-transparent text-muted hover:text-text',
                          ].join(' ')}
                        >
                          {t.label}
                        </button>
                      )
                    })}
                  </div>

                  {/* Tab content */}
                  <div className="min-h-0 flex-1 overflow-y-auto pt-4">
                    {capsQuery.isLoading && (
                      <div className="flex items-center gap-2 text-[12px] text-muted">
                        <Loader2 size={14} className="lucide-inline animate-spin" />
                        {i18nT('crewEditorNext.loading_template')}
                      </div>
                    )}
                    {capsQuery.error && (
                      /* No hand-off: Built From is display-only (no unsaved
                         draft), and the hand-off would navigate away from the
                         open editor and discard the crew/approvals/memory
                         drafts the user may have in progress. */
                      <ErrorNotice
                        message={capsQuery.error instanceof Error ? capsQuery.error.message : i18nT('crewEditorNext.load_error')}
                        variant="inline"
                        askAgent={false}
                        testId="next-builtfrom-error"
                      />
                    )}
                    {caps && (
                      <>
                        {builtFromTab === 'details' && (
                          <div className="flex flex-col gap-4" data-testid="next-builtfrom-details" style={{ maxWidth: 560 }}>
                            {/* Minimal read-only summary. The real substance of
                                "built from" is the Tools / MCP Servers / Skills
                                tabs; Details just names the template, marks it
                                read-only, and points there. The old raw
                                source/scope/mode/available dump was internal
                                jargon — we surface a row ONLY when it is
                                actionable (template missing, or user-authored). */}
                            <div className="flex flex-col gap-2 rounded-md border border-border bg-bg-elevated p-4">
                              <div className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-muted">
                                <Lock size={12} className="lucide-inline" />
                                {i18nT('crewEditorNext.read_only')}
                              </div>
                              <div className="text-[14px] text-text">
                                {i18nT('crewEditorNext.built_from_headline')} <span className="font-mono font-semibold">{caps.template.name}</span>
                              </div>
                              <p className="m-0 text-[12px] leading-relaxed text-muted">
                                {i18nT('crewEditorNext.built_from_body')}
                              </p>
                            </div>

                            {/* At-a-glance summary: how much this template gives
                                the mate, plus its effective model. Counts are the
                                present rows per section — the same sets the Tools /
                                MCP / Skills tabs list. Clicking a stat jumps to it. */}
                            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                              {([
                                { tab: 'tools' as const, label: i18nT('crewEditorNext.tab_tools'), n: presentRows(caps.rows, 'tools').length },
                                { tab: 'mcp' as const, label: i18nT('crewEditorNext.stat_mcp_servers'), n: presentRows(caps.rows, 'mcpServers').length },
                                { tab: 'skills' as const, label: i18nT('crewEditorNext.tab_skills'), n: presentRows(caps.rows, 'skills').length },
                              ]).map(stat => (
                                <button
                                  key={stat.tab}
                                  type="button"
                                  onClick={() => setBuiltFromTab(stat.tab)}
                                  data-testid={`next-builtfrom-stat-${stat.tab}`}
                                  className="flex flex-col items-start gap-0.5 rounded-md border border-border bg-bg-elevated p-3 text-left hover:bg-bg-hover focus-ring"
                                >
                                  <span className="text-[18px] font-semibold leading-none text-text">{stat.n}</span>
                                  <span className="text-[11px] text-muted">{stat.label}</span>
                                </button>
                              ))}
                              {/* Model is not a Built-From tab (it lives in
                                  Behavior → Model), so this stat is a readout, not
                                  a jump target. */}
                              <div className="flex flex-col items-start gap-0.5 rounded-md border border-border bg-bg-elevated p-3">
                                <span className="truncate text-[14px] font-semibold leading-tight text-text" title={resolved?.model || i18nT('pages.kiroCrewAgentsPage.inherited')}>
                                  {resolved?.model || i18nT('pages.kiroCrewAgentsPage.inherited')}
                                </span>
                                <span className="text-[11px] text-muted">{i18nT('crewEditorNext.stat_model')}</span>
                              </div>
                            </div>

                            {/* Warnings only — shown when something is actually
                                worth the user's attention. */}
                            {!caps.template.available && (
                              /* No hand-off: a removed/unresolvable parent
                                 template is a read-side resolution failure, and
                                 the hand-off would navigate away from the open
                                 editor and discard its unsaved crew fields
                                 (display name, workspace, model, color) and any
                                 approvals/memory draft. */
                              <ErrorNotice
                                message={i18nT('crewEditorNext.template_unavailable', { name: caps.template.name })}
                                variant="inline"
                                askAgent={false}
                                testId="next-builtfrom-unavailable"
                              />
                            )}
                            {caps.template.available && caps.template.source !== 'builtin' && caps.template.source !== 'package' && (
                              <p className="m-0 text-[12px] text-muted" data-testid="next-builtfrom-authored">
                                {i18nT('crewEditorNext.template_authored')}
                              </p>
                            )}
                          </div>
                        )}
                        {builtFromTab === 'tools' && (
                          <ToolsTab
                            rows={presentRows(caps.rows, 'tools')}
                            autoApproved={autoApprovedTools}
                            filter={toolFilter}
                            onFilter={setToolFilter}
                          />
                        )}
                        {builtFromTab === 'mcp' && (
                          <McpServersTab rows={presentRows(caps.rows, 'mcpServers')} />
                        )}
                        {builtFromTab === 'skills' && (
                          <SkillsTab
                            rows={presentRows(caps.rows, 'skills')}
                            filter={skillFilter}
                            onFilter={setSkillFilter}
                          />
                        )}
                      </>
                    )}
                  </div>
                </div>
              )}

              {effectiveSection === 'behavior' && (
                <div className="flex min-h-0 flex-1 flex-col" data-testid="next-pane-behavior">
                  {/* Behavior sub-tabs: approvals, workspace, triggers. */}
                  <div
                    role="tablist"
                    aria-label={i18nT("crewEditorNext.behavior_sections_aria")}
                    className="flex shrink-0 gap-1 border-b border-border"
                  >
                    {behaviorTabs.map(t => {
                      const isActive = t.key === behaviorTab
                      return (
                        <button
                          key={t.key}
                          type="button"
                          role="tab"
                          aria-selected={isActive}
                          data-testid={`next-behavior-tab-${t.key}`}
                          onClick={() => guardNav(() => setBehaviorTab(t.key))}
                          disabled={!isActive && (sheetBusy || childDraftLive)}
                          className={[
                            'relative -mb-px px-3 py-2 text-[12.5px] focus-ring',
                            isActive
                              ? 'border-b-2 border-accent font-medium text-text'
                              : 'border-b-2 border-transparent text-muted hover:text-text',
                          ].join(' ')}
                        >
                          {t.label}
                        </button>
                      )
                    })}
                  </div>

                  {/* Tab content */}
                  <div className="min-h-0 flex-1 overflow-y-auto pt-4">
                    {behaviorTab === 'approvals' && (
                      <fieldset disabled={sheetBusy} className="flex min-h-0 flex-col border-0 p-0 m-0 min-w-0" data-testid="next-behavior-approvals">
                        <CrewApprovalsEditor
                          member={editing}
                          onDirtyChange={childDirtyHandlers.approvals}
                          onBusyChange={setCapabilityBusy}
                          onSaved={onCapabilitiesSaved}
                        />
                      </fieldset>
                    )}
                    {behaviorTab === 'model' && (
                      <fieldset disabled={sheetBusy} className="flex flex-col gap-3 border-0 p-0 m-0 min-w-0" data-testid="next-behavior-model">
                        <p className="text-[12px] text-muted">
                          {i18nT('components.crew.crewEditorNextDialog.model_frame')}
                        </p>
                        <ModelField options={modelOptions} value={editModel} onChange={setEditModel} />
                        {/* Model list resolved DEGRADED (transport failure → auto-only/cached list).
                            No hand-off: this is an advisory read-degradation, not a
                            failure, and the hand-off would discard the open editor's
                            unsaved crew/approvals/memory drafts. */}
                        {modelsDegraded && (
                          <ErrorNotice
                            className="mt-1"
                            message={i18nT('pages.chatSidebar.model_list_failed')}
                            askAgent={false}
                            testId="next-model-degraded"
                          />
                        )}
                        {(effortCapable || !!editEffort) && (
                          <EffortField value={editEffort} onChange={setEditEffort} />
                        )}
                        {!effortCapable && !!editEffort && (
                          <div className="rounded-md border border-warn-subtle bg-warn-subtle px-3 py-2.5 text-[11.5px] leading-relaxed text-muted">
                            {effortModel
                              ? i18nT('pages.kiroCrewAgentsPage.effort_ignored_on_this_model', { model: effortModel })
                              : i18nT('pages.kiroCrewAgentsPage.effort_pin_needs_a_model')}
                          </div>
                        )}
                        {/* No hand-off: a model-resolution read failure leaves the
                            editor's unsaved crew/approvals/memory drafts in place; a
                            hand-off would navigate away and discard them. */}
                        <ErrorNotice
                          message={resolvedError ? (resolvedError instanceof Error ? resolvedError.message : String(resolvedError)) : null}
                          askAgent={false}
                          testId="next-model-resolved-error"
                        />
                        {resolved && (
                          <div className="flex flex-col gap-1 rounded-md border border-border bg-bg-accent px-3 py-2.5 text-[11.5px] leading-relaxed text-muted">
                            <div>
                              <span className="text-text">
                                {i18nT('pages.kiroCrewAgentsPage.resolves_to', { model: resolved.model || i18nT('pages.kiroCrewAgentsPage.inherited') })}
                              </span>
                              {' — '}
                              {resolved.pinned
                                ? i18nT('pages.kiroCrewAgentsPage.pinned_on_this_crew')
                                : resolved.model
                                  ? i18nT('pages.kiroCrewAgentsPage.inherited_from_the_agent_template')
                                  : i18nT('pages.kiroCrewAgentsPage.no_pin_anywhere_the_backend_chooses')}
                            </div>
                            {(effortCapable || !editEffort) && (
                              <div>
                                {effortCapable ? (
                                  <>
                                    <span className="text-text">
                                      {i18nT('pages.kiroCrewAgentsPage.effort_resolves_to', {
                                        effort: resolved.reasoning_effort
                                          ? effortLabel(resolved.reasoning_effort)
                                          : i18nT('lib.effort.default'),
                                      })}
                                    </span>
                                    {' — '}
                                    {resolved.effort_pinned
                                      ? i18nT('pages.kiroCrewAgentsPage.pinned_on_this_crew')
                                      : resolved.reasoning_effort
                                        ? i18nT('pages.kiroCrewAgentsPage.effort_inherited_from_the_global_default')
                                        : i18nT('pages.kiroCrewAgentsPage.no_effort_pin_the_model_decides')}
                                  </>
                                ) : effortModel ? (
                                  i18nT('pages.kiroCrewAgentsPage.effort_unavailable_on_this_model', { model: effortModel })
                                ) : (
                                  i18nT('pages.kiroCrewAgentsPage.effort_needs_a_model')
                                )}
                              </div>
                            )}
                          </div>
                        )}
                      </fieldset>
                    )}
                    {behaviorTab === 'triggers' && (
                      <fieldset disabled={sheetBusy} className="flex flex-col gap-4 border-0 p-0 m-0 min-w-0" data-testid="next-behavior-triggers">
                        <p className="text-[12px] text-muted">
                          {i18nT('crewEditorNext.triggers_frame')}
                        </p>
                        <TriggersField value={triggers} onChange={setTriggers} subject="agent" />
                      </fieldset>
                    )}
                    {behaviorTab === 'schedules' && (
                      <fieldset disabled={sheetBusy} className="flex flex-col gap-4 border-0 p-0 m-0 min-w-0" data-testid="next-behavior-schedules">
                        <CrewWakeSection
                          crew={editing}
                          memberId={memberId}
                          agentTemplate={kiroAgent}
                          isDefaultCrew={isDefaultCrew}
                          hideKindBadge
                          heading={i18nT('crewEditorNext.schedules_heading')}
                          blurb={i18nT('crewEditorNext.schedules_blurb')}
                          onDraftChange={setSchedDraft}
                          onSavingChange={setSchedSaving}
                          onRequestCancel={requestCancelDraft}
                          onNavigateAway={guardedNavigate}
                        />
                      </fieldset>
                    )}
                    {behaviorTab === 'webhooks' && (
                      <fieldset disabled={sheetBusy} className="flex flex-col gap-4 border-0 p-0 m-0 min-w-0" data-testid="next-behavior-webhooks">
                        <p className="text-[12px] text-muted">
                          {i18nT('crewEditorNext.webhooks_frame')}
                        </p>
                        <CrewWebhookSection crew={editing} onNavigateAway={guardedNavigate} />
                      </fieldset>
                    )}
                  </div>
                </div>
              )}

              {effectiveSection === 'memory' && (
                <div className="flex min-h-0 flex-1 flex-col" data-testid="next-pane-memory">
                  {/* Memory sub-tabs — mirrors the global V2 memory surface. */}
                  <div
                    role="tablist"
                    aria-label={i18nT("crewEditorNext.memory_sections_aria")}
                    className="flex shrink-0 gap-1 border-b border-border"
                  >
                    {memoryTabs.map(t => {
                      const isActive = t.key === memoryTab
                      return (
                        <button
                          key={t.key}
                          type="button"
                          role="tab"
                          aria-selected={isActive}
                          data-testid={`next-memory-tab-${t.key}`}
                          onClick={() => guardNav(() => setMemoryTab(t.key))}
                          disabled={!isActive && (sheetBusy || childDraftLive)}
                          className={[
                            'relative -mb-px px-3 py-2 text-[12.5px] focus-ring',
                            isActive
                              ? 'border-b-2 border-accent font-medium text-text'
                              : 'border-b-2 border-transparent text-muted hover:text-text',
                          ].join(' ')}
                        >
                          {t.label}
                        </button>
                      )
                    })}
                  </div>

                  <div className="min-h-0 flex-1 overflow-y-auto pt-4">
                    {!memoryStore ? (
                      <div className="rounded-md border border-border bg-bg-accent px-3 py-2 text-[12px] text-muted">
                        {i18nT('crewEditorNext.memory_empty')}
                      </div>
                    ) : (
                      <>
                        {memoryTab === 'profile' && (
                          <fieldset disabled={sheetBusy} className="flex flex-col gap-4 border-0 p-0 m-0 min-w-0" data-testid="next-memory-profile">
                            <p className="text-[12px] text-muted">
                              {i18nT('crewEditorNext.memory_profile_frame')}
                            </p>
                            <MemoryDocCard
                              key={`prefs-${memoryStore}`}
                              docKey="preferences"
                              store={memoryStore}
                              title={i18nT('pages.overview.memoryTab.preferences')}
                              rows={7}
                              placeholder={i18nT('pages.overview.memoryTab.loading')}
                              read={api.memoryPreferences}
                              write={api.saveMemoryPreferences}
                              onDirtyChange={childDirtyHandlers['memory-preferences']}
                            />
                            <MemoryDocCard
                              key={`projects-${memoryStore}`}
                              docKey="projects"
                              store={memoryStore}
                              title={i18nT('pages.overview.memoryTab.projects')}
                              rows={7}
                              placeholder={i18nT('pages.overview.memoryTab.loading')}
                              read={api.memoryProjects}
                              write={api.saveMemoryProjects}
                              onDirtyChange={childDirtyHandlers['memory-projects']}
                            />
                          </fieldset>
                        )}
                        {memoryTab === 'memories' && (
                          <fieldset disabled={sheetBusy} className="border-0 p-0 m-0 min-w-0" data-testid="next-memory-memories">
                            <MemoryRecordsEditor
                              key={`memories-${memoryStore}`}
                              store={memoryStore}
                              privateMemory={memoryIsPrivate}
                              onDirtyChange={childDirtyHandlers['memory-records']}
                              onRecovery={() => guardNav(() => setMemoryTab('recovery'))}
                            />
                          </fieldset>
                        )}
                        {memoryTab === 'recovery' && (
                          <fieldset disabled={sheetBusy} className="flex flex-col gap-4 border-0 p-0 m-0 min-w-0" data-testid="next-memory-recovery">
                            <MemoryBackupsCard key={`backups-${memoryStore}`} store={memoryStore} privateMemory={memoryIsPrivate} />
                            <MemoryRetiredCard key={`retired-${memoryStore}`} store={memoryStore} privateMemory={memoryIsPrivate} />
                            <MemoryCarveCard key={`carve-${memoryStore}`} store={memoryStore} />
                          </fieldset>
                        )}
                      </>
                    )}
                  </div>
                </div>
              )}

              {effectiveSection === 'danger' && !isDefaultCrew && (
                <div className="flex min-h-0 flex-1 flex-col gap-4" data-testid="next-pane-danger">
                  <div className="flex max-w-[560px] flex-col gap-4">
                    <p className="m-0 text-[12px] leading-relaxed text-muted">
                      {i18nT('pages.kiroCrewAgentsPage.deleting_a_crew_unbinds_it_from_new_sessions_its')}
                    </p>
                  <div className="flex flex-col items-start gap-3 rounded-md border border-danger-subtle bg-danger-subtle p-3">
                    <div className="flex flex-col gap-0.5">
                      <span className="text-[12.5px] font-medium text-text-strong">
                        {i18nT('pages.kiroCrewAgentsPage.delete_crew')}
                      </span>
                      {confirmDelete && (
                        <span className="text-[11.5px] leading-relaxed text-muted">
                          {i18nT('pages.kiroCrewAgentsPage.delete_crew_named_confirm', { name: editing })}
                        </span>
                      )}
                    </div>
                    <div ref={confirmRef} className="flex items-center gap-2">
                      {confirmDelete ? (
                        <>
                          <Btn onClick={() => setConfirmDelete(false)} data-testid="next-cancel-delete-crew">
                            {i18nT('pages.kiroCrewAgentsPage.cancel')}
                          </Btn>
                          <Btn danger onClick={deleteCrew} disabled={sheetBusy} data-testid="next-confirm-delete-crew">
                            {i18nT('pages.kiroCrewAgentsPage.yes_delete_it')}
                          </Btn>
                        </>
                      ) : (
                        <Btn danger onClick={() => setConfirmDelete(true)} disabled={sheetBusy} data-testid="next-delete-crew">
                          {i18nT('pages.kiroCrewAgentsPage.delete_crew')}
                        </Btn>
                      )}
                    </div>
                  </div>
                  </div>
                </div>
              )}
            </div>
          </div>

          <DialogFooter>
            {/* Left-aligned feedback: a failed-save error, a blocking hint
                (e.g. finish the schedule draft first), or an unsaved-changes
                note — mutually exclusive, error first.
                No hand-off: a failed save means the user's edits are still in
                the open panes; the hand-off would navigate away and discard
                them, so the user must stay to retry. */}
            <ErrorNotice message={error} variant="inline" className="mr-auto" askAgent={false} testId="next-sheet-error" />
            {sheetHint && !error && (
              <span className="mr-auto text-[12px] text-danger" data-testid="next-sheet-hint">{sheetHint}</span>
            )}
            {dirtyPanes.size > 0 && !error && !sheetHint && (
              <span className="mr-auto text-[11.5px] text-muted" data-testid="next-unsaved-note">
                {i18nT('components.crewEditor.unsaved_changes')}
              </span>
            )}
            <Btn onClick={requestClose}>{i18nT('pages.kiroCrewAgentsPage.cancel')}</Btn>
            <SendBtn
              onClick={save}
              /* Footer Save persists only the crew-level fields; approvals and
                 memory ('capabilities') and the schedule draft ('schedules')
                 save through their OWN controls. Enabling Save on those would
                 run the roster-only save and close, silently dropping the
                 child draft. So Save is disabled whenever a child/capability
                 draft is live (it saves through its own control) OR there is no
                 crew-level dirt to persist, while the unsaved note and Cancel
                 confirm still see the full set. */
              disabled={sheetBusy || schedDraft || dirtyPanes.has('capabilities') || [...dirtyPanes].every(p => p === 'capabilities' || p === 'schedules')}
            >{i18nT('pages.kiroCrewAgentsPage.save_changes')}</SendBtn>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {editing && (
        <CrewAvatarBuilder
          open={avatarBuilderOpen}
          name={editing}
          value={editAvatar}
          retiredCue={retiredCueFrom(editingAgent?.avatar)}
          savedReactions={{
            motions: motionsFrom(editingAgent?.avatar) !== null,
            sounds: soundsFrom(editingAgent?.avatar) !== null,
          }}
          onCancel={closeAvatarBuilder}
          onSave={applyAvatar}
        />
      )}

      <WorkspaceModal
        open={wsModalOpen}
        workspaceOptions={workspaceOptions}
        onCreated={onWorkspaceCreated}
        onClose={closeWsModal}
        onDirtyChange={setWsDirty}
        onSaveChain={onPaneSaveChain}
      />

      {/* Discard-confirm prompt — driven by the SAME controller state the
          classic dialog uses (discardAsk / confirmDiscard). Without it a dirty
          close would set discardAsk='close' on the controller with no UI,
          leaving the editor stuck open and silently swallowing the Cancel. */}
      <Dialog open={discardAsk !== null} onOpenChange={next => { if (!next) setDiscardAsk(null) }}>
        <DialogContent
          maxWidth={440}
          className="z-[110]"
          aria-label={askSchedOnly
            ? i18nT('pages.kiroCrewAgentsPage.discard_new_schedule')
            : i18nT('pages.kiroCrewAgentsPage.discard_unsaved_changes')}
        >
          <DialogHeader>
            <DialogTitle className="whitespace-normal">
              {askSchedOnly
                ? i18nT('pages.kiroCrewAgentsPage.discard_new_schedule')
                : i18nT('pages.kiroCrewAgentsPage.discard_unsaved_changes')}
            </DialogTitle>
          </DialogHeader>
          <DialogBody>
            <p className="m-0 text-sm text-text">
              {schedDraft
                ? i18nT('pages.kiroCrewAgentsPage.discard_new_schedule_body')
                : i18nT('pages.kiroCrewAgentsPage.discard_unsaved_body', { name: editing })}
            </p>
            {schedDraft && discardTakesSheet && (
              <p className="mb-0 mt-2 text-sm text-text" data-testid="next-sched-discard-also-crew">
                {i18nT('pages.kiroCrewAgentsPage.discard_also_crew_edits')}
              </p>
            )}
            {schedSaving && (
              <p className="mb-0 mt-2 text-[12px] text-muted" data-testid="next-sched-discard-saving-note">
                {discardForce
                  ? i18nT('pages.kiroCrewAgentsPage.discard_anyway_note')
                  : i18nT('pages.kiroCrewAgentsPage.discard_locked_while_saving')}
              </p>
            )}
          </DialogBody>
          <DialogFooter>
            <Btn onClick={() => setDiscardAsk(null)} data-testid="crew-sched-discard-keep">
              {i18nT('pages.kiroCrewAgentsPage.keep_editing')}
            </Btn>
            <Btn
              danger
              onClick={confirmDiscard}
              disabled={schedSaving && !discardForce}
              title={schedSaving ? i18nT('components.jobForm.saving') : undefined}
              data-testid="crew-sched-discard-confirm"
            >
              {askSchedOnly
                ? i18nT('pages.kiroCrewAgentsPage.discard_schedule_confirm')
                : i18nT('pages.kiroCrewAgentsPage.discard_confirm')}
            </Btn>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {confirmDialog}
    </>
  )
}
