import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { Boxes, FolderOpen, Database, Sparkles, Plus, MessageSquare, Users, Star, LayoutGrid, Rows3 } from 'lucide-react'
import Clickable from '../components/Clickable'
import { useQuery, useMutation } from '@tanstack/react-query'
import { useAppDispatch } from '../store'
import { createSlot } from '../store/chatSlice'
import { api, type WebhookTokenEntry } from '../api/client'
import { useProvider } from '../providers'
import { useAvailableModels } from '../hooks/useAvailableModels'
import { FOLDER_COLOR_PALETTE } from '../components/folderColorCatalog'
import { Btn, SendBtn, Input, Badge, SearchInput, PageHeader, EmptyState } from '../components/ui'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import {
  Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from '../components/ui/dialog'
import SegmentedControl from '../components/SegmentedControl'
import InfoTip from '../components/InfoTip'
import { FOCUSABLE } from '../hooks/useDialogFocusTrap'
import SimpleSelect from '../components/SimpleSelect'
import CrewAvatar, { ghostTraitsFrom, imageAvatarFrom, type CrewAvatarOverride } from '../components/CrewAvatar'
import CrewAvatarBuilder from '../components/CrewAvatarBuilder'
import CrewWakeSection from '../components/CrewWakeSection'
import CrewWebhookSection from '../components/CrewWebhookSection'
import CrewEditorRail from '../components/crew/CrewEditorRail'
import CrewOverviewPane from '../components/crew/CrewOverviewPane'
import { useCrewEditorSections, type CrewPaneKey } from '../components/crew/crewEditorSections'
import { wakesCrew, crewWakeQueryKey, crewWebhooksQueryKey, webhookBoundToCrew, webhookCanCallIn } from '../components/crew/wakesCrew'
import type { CronJob } from '../types'
import type { KiroCrewAgent } from '../components/AgentSelector'
import { SourceBadge } from '../components/SourceBadge'
import { errMessage } from '../utils/thunkError'
import { EFFORT_LEVELS, effortLabel, modelSupportsEffort } from '../lib/effort'

import { i18nT } from '../i18n/t'
import ErrorNotice from '../components/ErrorNotice'
/** Common shape returned by the agent/workspace mutation endpoints. */
interface AgentMutationResult {
  error?: string
  name?: string
}

/** Fields sent when creating a crew. */
interface CreatePayload {
  name: string
  kiro_agent: string
  workspace: string
  memory_store: string
  triggers: string
  session_color: string
}

/** Editable fields sent when updating an existing agent binding. */
interface AgentUpdatePayload {
  kiro_agent: string
  workspace: string
  memory_store: string
  /** Free-text routing intent for orchestrator crew selection. */
  triggers: string
  /** '' = inherit (the kiro template's pin, then the global fallback). */
  model: string
  /** '' = inherit the global default effort. Otherwise one of the levels the
   *  backend accepts (low..max); a level is only honoured on a model that
   *  supports effort at all. */
  reasoning_effort: string
  /** Default session color (#rrggbb hex) for new sessions. '' = no default. */
  session_color: string
  /** Pinned ghost face from the avatar builder. `{}` = the name-derived face. */
  avatar: CrewAvatarOverride | Record<string, never>
}

/** The stored spelling for "no per-agent pin, inherit the next tier down". The
 *  select shows this as a real option; the backend normalizes it back to ''. */
const INHERIT_MODEL = 'auto'

/** Which crew the editor dialog is pointed at. `null` = closed. */
type SheetTarget = { mode: 'create' } | { mode: 'edit'; name: string } | null

/** Roster layout. `cards` is the roomy grid, `list` the compact table. */
type CrewView = 'cards' | 'list'

/** Where the roster layout is remembered. Mirrors `mc-artifacts-view`, which is
 *  how the Artifacts page persists the same grid/table choice — one convention
 *  for both surfaces rather than a second scheme for this one. */
const VIEW_KEY = 'mc-crews-view'

/** How long a schedule-draft discard confirm stays fully locked while the
 *  create request is in flight. The lock exists because discarding cannot
 *  cancel the POST; the unlock exists because a HUNG request (the client
 *  sets no timeout) must not seal every exit from the modal editor. A
 *  healthy create settles well under this, so the escape only surfaces for
 *  genuinely stalled requests. */
const DISCARD_FORCE_GRACE_MS = 8000

/** Read the remembered layout. Guarded because `localStorage` throws outright
 *  in a partitioned/blocked-storage context rather than returning null. */
function readStoredView(): CrewView {
  try {
    return localStorage.getItem(VIEW_KEY) === 'list' ? 'list' : 'cards'
  } catch {
    return 'cards'
  }
}

/**
 * Which of a crew's two stores another crew also points at. Drives a specific
 * badge instead of a bare "Shared", which a first-run reviewer read as "shared
 * with my teammates" — the scariest possible reading and the wrong one.
 */
type SharedKind = 'none' | 'memory' | 'files' | 'both'

/* ── Workspace Creation Dialog (a nested Radix layer inside the crew editor) ── */

/** The form itself, mounted only while the dialog is open.
 *
 *  Split out from `WorkspaceModal` on purpose: Radix unmounts `DialogContent`'s
 *  children on close, so keeping the state HERE resets a half-typed workspace
 *  name between openings for free. Hoisting it into the parent (which stays
 *  mounted so Radix can run its own close transition) would persist it. */
function WorkspaceForm({
  workspaceOptions,
  onCreated,
  onClose,
}: {
  workspaceOptions: string[]
  onCreated: (name: string) => void
  onClose: () => void
}) {
  const [wsName, setWsName] = useState('')
  const [wsDir, setWsDir] = useState('workspace')
  const [dirTouched, setDirTouched] = useState(false)
  const [copyFrom, setCopyFrom] = useState('')
  const [wsError, setWsError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  // Auto-fill directory from workspace name (unless user manually edited it)
  const handleNameChange = (v: string) => {
    setWsName(v)
    if (!dirTouched) {
      const slug = v.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')
      setWsDir(slug ? `workspace-${slug}` : 'workspace')
    }
  }

  const submit = async () => {
    setWsError('')
    const n = wsName.trim()
    if (!n) { setWsError(i18nT('pages.kiroCrewAgentsPage.workspace_name_is_required')); return }
    setSubmitting(true)
    try {
      const body: Record<string, string> = { name: n, dir: wsDir }
      if (copyFrom) body.copy_from = copyFrom
      const r: AgentMutationResult = await api.createWorkspace(body)
      if (r.error) { setWsError(r.error); setSubmitting(false); return }
      onCreated(r.name || n)
    } catch (e) {
      setWsError(e instanceof Error ? e.message : i18nT('pages.kiroCrewAgentsPage.failed_to_create_workspace'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle>{i18nT('pages.kiroCrewAgentsPage.create_workspace')}</DialogTitle>
      </DialogHeader>
      <DialogBody>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-1">
              {/* Native input associated via htmlFor+id; label-has-for's nesting requirement is a false positive. */}
              <label htmlFor="ws-name" className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('pages.kiroCrewAgentsPage.name')}</label>
              <InfoTip text={i18nT('pages.kiroCrewAgentsPage.a_unique_identifier_for_this_workspace_agents_re')} />
            </div>
            <Input id="ws-name" placeholder={i18nT('pages.kiroCrewAgentsPage.e_g_oncall')} value={wsName} onChange={e => handleNameChange(e.target.value)} autoFocus />
          </div>
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-1">
              {/* Native input associated via htmlFor+id; label-has-for's nesting requirement is a false positive. */}
              <label htmlFor="ws-dir" className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('pages.kiroCrewAgentsPage.directory')}</label>
              <InfoTip text={i18nT('pages.kiroCrewAgentsPage.subdirectory_inside_kiro_crew_where_this_workspa')} />
            </div>
            <Input id="ws-dir" placeholder={i18nT('pages.kiroCrewAgentsPage.workspace')} value={wsDir} onChange={e => { setDirTouched(true); setWsDir(e.target.value) }} />
          </div>
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-1">
              <span className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('pages.kiroCrewAgentsPage.copy_from_optional')}</span>
              <InfoTip text={i18nT('pages.kiroCrewAgentsPage.copy_the_contents_of_an_existing_workspace_into')} />
            </div>
            <SimpleSelect
              options={workspaceOptions}
              value={copyFrom}
              onChange={setCopyFrom}
              clearLabel={i18nT('pages.kiroCrewAgentsPage.none')}
              aria-label={i18nT('pages.kiroCrewAgentsPage.copy_from_workspace')}
            />
          </div>
          {wsError && <div className="text-danger text-[13px]">{wsError}</div>}
        </div>
      </DialogBody>
      <DialogFooter>
        <Btn onClick={onClose}>{i18nT('pages.kiroCrewAgentsPage.cancel')}</Btn>
        <SendBtn onClick={submit} disabled={submitting}>{submitting ? i18nT('pages.kiroCrewAgentsPage.creating') : i18nT('pages.kiroCrewAgentsPage.create')}</SendBtn>
      </DialogFooter>
    </>
  )
}

function WorkspaceModal({
  open,
  workspaceOptions,
  onCreated,
  onClose,
}: {
  open: boolean
  workspaceOptions: string[]
  onCreated: (name: string) => void
  onClose: () => void
}) {
  /* Kept MOUNTED and driven by `open`, rather than conditionally rendered.
     Radix tracks dismissable layers in a global stack, and tearing this whole
     subtree out the instant it closes skipped the layer's own deregistration —
     the editor underneath was then left believing it was no longer the top
     layer, so Escape stopped closing it. Verified in a real browser
     (scripts/verify-crews-dialog-select.mjs), which is the only place the bug
     showed: happy-dom does not reproduce it.

     `z-[110]` because both layers are centered overlays and the editor's own
     content sits at z-[101]; at an equal z-index this would render behind its
     own opener. */
  return (
    <Dialog open={open} onOpenChange={next => { if (!next) onClose() }}>
      <DialogContent maxWidth={448} className="z-[110]" aria-label={i18nT('pages.kiroCrewAgentsPage.create_workspace')}>
        <WorkspaceForm workspaceOptions={workspaceOptions} onCreated={onCreated} onClose={onClose} />
      </DialogContent>
    </Dialog>
  )
}

/** One labelled control in the editor panel, with an optional explainer. */
function Field({ label, hint, info, children }: { label: string; hint?: string; info?: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="flex items-center gap-1.5 text-[11px] text-muted uppercase tracking-wider font-medium">
        {label}
        {info && <InfoTip text={info} />}
      </span>
      {children}
      {hint && <span className="text-[11.5px] leading-relaxed text-muted">{hint}</span>}
    </div>
  )
}

/** One binding shown on a roster card: icon, what it is, what it points at. */
function Binding({ icon, label, value, muted, note }: {
  icon: React.ReactNode
  label: string
  value: string
  muted?: boolean
  /** Warning suffix, e.g. that another crew points at this same store. */
  note?: string
}) {
  return (
    <div className="flex items-center gap-2 min-w-0">
      <span className="text-muted">{icon}</span>
      <span className="min-w-0">
        <span className="block text-[10px] uppercase tracking-wider text-muted">{label}</span>
        {/* `pr-0.5` is load-bearing with `truncate`: an italic glyph leans past
            its own advance width, and `overflow:hidden` clips that overhang
            rather than showing an ellipsis — "Inherited" rendered as
            "Inheritea". Two pixels of gutter is enough for the lean. */}
        <span className="block truncate pr-0.5 text-[12px]">
          <span className={muted ? 'italic text-muted' : 'font-mono text-text'}>{value}</span>
          {note && <span className="ml-1.5 text-[11px] text-warn">{note}</span>}
        </span>
      </span>
    </div>
  )
}

/** An EMPTY current value means "nothing selected" and must NOT be appended as an
 *  option: SimpleSelect treats an options list containing '' as making empty
 *  selectable, which suppresses the trigger placeholder and adds a blank row. */
function withCurrent(opts: string[], cur: string): string[] {
  return !cur || opts.includes(cur) ? opts : [...opts, cur]
}

/**
 * One component per binding, so the create form and the editor's panes render the
 * SAME control rather than two copies that drift. Create composes them through
 * `BindingFields`; the editor mounts them individually, one per rail pane.
 */
export function TemplateField({ label, options, value, onChange }: {
  label: string; options: string[]; value: string; onChange: (v: string) => void
}) {
  return (
    <Field label={label} hint={i18nT('pages.kiroCrewAgentsPage.the_agent_definition_it_boots_from_tools_mcp_ser')}>
      <SimpleSelect
        options={withCurrent(options, value)}
        value={value}
        onChange={onChange}
        triggerFallback={i18nT('pages.kiroCrewAgentsPage.select_an_agent_template')}
        aria-label={label}
      />
    </Field>
  )
}

export function WorkspaceField({ options, value, onChange, onNewWorkspace }: {
  options: string[]; value: string; onChange: (v: string) => void; onNewWorkspace: () => void
}) {
  return (
    <Field
      label={i18nT('pages.kiroCrewAgentsPage.workspace_2')}
      hint={i18nT('pages.kiroCrewAgentsPage.isolated_memory_and_files_for_this_crew')}
      info={i18nT('pages.kiroCrewAgentsPage.bindings_preview_info')}
    >
      <SimpleSelect
        options={withCurrent(options, value)}
        value={value}
        onChange={onChange}
        action={{ label: i18nT('pages.kiroCrewAgentsPage.new_workspace_action'), onSelect: onNewWorkspace }}
        aria-label={i18nT('pages.kiroCrewAgentsPage.workspace_2')}
      />
    </Field>
  )
}

export function MemoryStoreField({ options, value, onChange }: {
  options: string[]; value: string; onChange: (v: string) => void
}) {
  return (
    <Field
      label={i18nT('pages.kiroCrewAgentsPage.memory_store')}
      hint={i18nT('pages.kiroCrewAgentsPage.which_store_its_lessons_and_history_are_written')}
      info={i18nT('pages.kiroCrewAgentsPage.bindings_preview_info')}
    >
      <SimpleSelect options={withCurrent(options, value)} value={value} onChange={onChange} aria-label={i18nT('pages.kiroCrewAgentsPage.memory_store')} />
      {/* Show the "more coming" note only when `default` is the sole option —
          an install that has declared extra `memory_stores` in config already
          has a real choice here, and the copy must not contradict a picker
          that is visibly offering other stores. */}
      {options.length <= 1 && (
        <span className="flex items-start gap-1.5 text-[11.5px] leading-relaxed text-accent">
          <Sparkles className="lucide-inline h-3 w-3 mt-0.5 shrink-0" aria-hidden="true" />
          {i18nT('pages.kiroCrewAgentsPage.additional_stores_coming_with_memory_v2')}
        </span>
      )}
    </Field>
  )
}

export function ModelField({ options, value, onChange }: {
  options: string[]; value: string; onChange: (v: string) => void
}) {
  return (
    <Field label={i18nT('pages.kiroCrewAgentsPage.model')}>
      <SimpleSelect
        options={withCurrent(options, value)}
        // The inherit option must NOT read as "auto": in the chat picker "auto"
        // promises task-based routing, whereas here it means "pin nothing,
        // inherit the next tier" — which can resolve to a concrete model. Label
        // it as the card does so the round trip stays honest.
        optionLabels={withCurrent(options, value).map(m => (m === INHERIT_MODEL ? i18nT('pages.kiroCrewAgentsPage.inherited') : m))}
        value={value}
        onChange={onChange}
        aria-label={i18nT('pages.kiroCrewAgentsPage.edit_model')}
      />
    </Field>
  )
}

/** The crew's reasoning-effort pin. Rendered only when the model the crew will
 *  actually run on supports effort — the same gate the chat picker uses, so a
 *  crew on Haiku is not offered a control the backend would drop. */
export function EffortField({ value, onChange }: {
  value: string; onChange: (v: string) => void
}) {
  return (
    <Field label={i18nT('pages.kiroCrewAgentsPage.reasoning_effort')} hint={i18nT('pages.kiroCrewAgentsPage.reasoning_effort_hint')}>
      <SimpleSelect
        options={[...EFFORT_LEVELS]}
        // '' is the inherit sentinel, labelled as such rather than as a level:
        // it means "take the global default", which may itself be a level.
        optionLabels={EFFORT_LEVELS.map(l => (l === '' ? i18nT('pages.kiroCrewAgentsPage.inherited') : effortLabel(l)))}
        value={value}
        onChange={onChange}
        aria-label={i18nT('pages.kiroCrewAgentsPage.edit_reasoning_effort')}
      />
    </Field>
  )
}

/** The routing-keyword input. Rendered by the create form and by the editor's
 *  routing pane, so it is a component rather than two copies. */
export function TriggersField({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <Field label={i18nT('pages.kiroCrewAgentsPage.triggers')} hint={i18nT('pages.kiroCrewAgentsPage.triggers_hint')} info={i18nT('pages.kiroCrewAgentsPage.triggers_info')}>
      <Input
        placeholder={i18nT('pages.kiroCrewAgentsPage.triggers_placeholder')}
        value={value}
        onChange={e => onChange(e.target.value)}
        aria-label={i18nT('pages.kiroCrewAgentsPage.triggers')}
      />
    </Field>
  )
}

/** Session color picker for agent configuration. Sets the default session
 *  tint color for new sessions created with this agent. */
export function SessionColorField({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const HEX_RE = /^#[0-9a-f]{6}$/i
  const [draft, setDraft] = useState(value || '')
  // Re-sync the draft when the committed value changes from outside (e.g. the
  // swatch, Clear, or opening the editor on a different crew).
  useEffect(() => { setDraft(value || '') }, [value])
  const commit = (raw: string) => {
    const v = raw.trim().toLowerCase()
    if (v === '') { onChange(''); setDraft('') }
    else if (HEX_RE.test(v)) { onChange(v); setDraft(v) }
    else { setDraft(value || '') } // invalid on blur → revert to committed
  }
  return (
    <Field label={i18nT('pages.kiroCrewAgentsPage.session_color')} hint={i18nT('pages.kiroCrewAgentsPage.session_color_hint')}>
      {/* Quick picks first, exact entry below — the order the session
       *  right-click menu uses, so the two surfaces read the same way.
       *
       *  These are FOLDER_COLOR_PALETTE, the repo's existing fixed-hex identity
       *  catalog, NOT the sidebar's generated palette. The sidebar's swatches
       *  are a `color_index` into a palette derived from the theme accent, so
       *  they re-derive when the theme changes; a crew's `session_color` is a
       *  stored hex, so a swatch here has to commit exactly the literal it
       *  shows and must not drift. That is the same job the folder catalog
       *  already does, and reusing it keeps one visual language across folders,
       *  tags and crews — as that file's own comment argues — instead of a
       *  second preset list that would silently diverge from it. Read-only:
       *  the catalog's KEEP IN SYNC contract with chat_folders.py governs
       *  changes to its entries, and consuming it adds no such coupling.
       *
       *  The active ring is matched by hex, so a custom colour outside the
       *  catalog correctly rings nothing.
       *
       *  No "no color" cell here: Clear already owns that, and two controls for
       *  one action is worse than one. */}
      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        {FOLDER_COLOR_PALETTE.map(({ value: c, label }) => {
          const active = HEX_RE.test(value) && value.toLowerCase() === c
          return (
            <Btn
              type="button"
              key={c}
              aria-label={label()}
              aria-pressed={active}
              title={label()}
              // Btn, not a raw <button>, so the swatches inherit the standard
              // press and disabled treatment. `p-0` and the sizing below win
              // over Btn's own padding/radius/border because Btn twMerges
              // `className` last; the inline background beats its
              // `bg-transparent` (and its hover background) on specificity, so
              // the dot keeps its colour in every state.
              //
              // `border-text-strong`, not `border-accent`: the accent is itself a
              // purple in most themes, so an accent ring on the indigo and violet
              // entries reads as no ring at all. The near-white ring is what
              // SessionColorSwatches uses, and it separates from every hue here.
              className={`h-5 w-5 p-0 cursor-pointer rounded-full border-2 transition-transform hover:scale-110 ${active ? 'border-text-strong scale-110' : 'border-border'}`}
              style={{ background: c }}
              onClick={() => onChange(c)}
            />
          )
        })}
      </div>
      <div className="flex items-center gap-2">
        <Input
          type="color"
          value={value || '#6366f1'}
          onChange={e => onChange(e.target.value.toLowerCase())}
          className="h-8 w-8 flex-none cursor-pointer p-0.5"
          aria-label={i18nT('pages.kiroCrewAgentsPage.session_color')}
        />
        <Input
          placeholder="#rrggbb"
          value={draft}
          onChange={e => {
            const v = e.target.value.trim().toLowerCase()
            setDraft(v)
            // Live-commit only when the draft is a complete hex or cleared;
            // partial values stay local so typing is never swallowed.
            if (v === '' || HEX_RE.test(v)) onChange(v)
          }}
          onBlur={e => commit(e.target.value)}
          className="flex-1 font-mono text-[13px]"
          aria-label={i18nT('pages.kiroCrewAgentsPage.session_color_hex')}
        />
        {value && (
          <Btn
            type="button"
            onClick={() => onChange('')}
            className="text-[11px]"
            aria-label={i18nT('pages.kiroCrewAgentsPage.session_color_clear')}
          >
            {i18nT('pages.kiroCrewAgentsPage.session_color_clear')}
          </Btn>
        )}
      </div>
    </Field>
  )
}

/** The create form's binding block. */
function BindingFields({
  templateLabel, kiroAgentOptions, kiroAgent, setKiroAgent,
  workspaceOptions, workspace, setWorkspace, onNewWorkspace,
  memoryStoreOptions, memoryStore, setMemoryStore,
  modelOptions, model, setModel,
}: {
  templateLabel: string
  kiroAgentOptions: string[]; kiroAgent: string; setKiroAgent: (v: string) => void
  workspaceOptions: string[]; workspace: string; setWorkspace: (v: string) => void; onNewWorkspace: () => void
  memoryStoreOptions: string[]; memoryStore: string; setMemoryStore: (v: string) => void
  modelOptions?: string[]; model?: string; setModel?: (v: string) => void
}) {
  return (
    <>
      <TemplateField label={templateLabel} options={kiroAgentOptions} value={kiroAgent} onChange={setKiroAgent} />
      <WorkspaceField options={workspaceOptions} value={workspace} onChange={setWorkspace} onNewWorkspace={onNewWorkspace} />
      <MemoryStoreField options={memoryStoreOptions} value={memoryStore} onChange={setMemoryStore} />
      {modelOptions && setModel && model !== undefined && (
        <ModelField options={modelOptions} value={model} onChange={setModel} />
      )}
    </>
  )
}

/** One crew in the roster. The whole card opens the editor panel. */
function CrewCard({ agent, isDefault, shared, onOpen }: {
  agent: KiroCrewAgent
  isDefault: boolean
  shared: SharedKind
  onOpen: () => void
}) {
  const provider = useProvider()
  const sharedNote = i18nT('pages.kiroCrewAgentsPage.shared_lower')
  const filesShared = shared === 'files' || shared === 'both'
  const memoryShared = shared === 'memory' || shared === 'both'
  const desc = describeCrew(agent, isDefault)
  return (
    <Clickable
      onClick={onOpen}
      aria-label={i18nT('pages.kiroCrewAgentsPage.edit_crew_named', { name: agent.name })}
      data-testid="crew-card"
      className={`group flex flex-col gap-3 rounded-lg border bg-card p-3.5 transition-all
                  hover:border-border-strong hover:shadow-md focus-ring
                  ${isDefault ? 'border-accent-subtle' : 'border-border'}`}
      style={agent.session_color ? { borderLeftColor: agent.session_color, borderLeftWidth: '3px' } : undefined}
    >
      <div className="flex items-center gap-3">
        <CrewAvatar seed={agent.name} avatar={agent.avatar} size={38} />
        {/* Fixed height for the whole header block. Badges are slightly taller
            than plain text, so cards carrying a `default` badge would otherwise
            push their binding grid lower than a card without one, and the row
            would read as ragged. Sized for one name line plus TWO description
            lines: 20px + 34px, and the description reserves its 34px whether or
            not it fills them. */}
        <div className="flex h-[54px] min-w-0 flex-1 flex-col justify-center">
          {/* Kept to a single line: a wrapping badge row made this header one
              line taller than its neighbours', which knocked the binding grids
              out of alignment across the row. The name truncates and the
              badges hold their size, so the row can never wrap. */}
          <div className="flex items-center gap-2 min-w-0">
            <span className="truncate font-mono text-[14px] font-semibold text-text-strong">{agent.name}</span>
            {isDefault && <Badge variant="ok" className="shrink-0">{i18nT('pages.kiroCrewAgentsPage.default_2')}</Badge>}
            {agent.source && agent.source !== 'kirocrew' && <SourceBadge source={agent.source} />}
          </div>
          {/* Two lines rather than one. A crew description is a sentence about
              what the crew is FOR, and a single truncated line cut nearly all
              of them mid-word. `line-clamp-2` with an explicit line-height and
              a matching fixed height: without the fixed height the clamp leaks
              a sliver of a third line at some font sizes, and cards with a
              one-line description sit shorter than their neighbours. The full
              text stays reachable via the native tooltip and the list view. */}
          <div className="mt-0.5 min-w-0">
            <span
              className={`line-clamp-2 h-[34px] text-[12px] leading-[17px] text-muted ${desc.placeholder ? 'italic' : ''}`}
              title={desc.placeholder ? undefined : desc.text}
            >
              {desc.text}
            </span>
          </div>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-2 border-t border-border pt-3">
        <Binding icon={<Boxes className="lucide-inline" aria-hidden="true" />} label={provider.labels.agentTemplateField} value={agent.kiro_agent} />
        <Binding icon={<FolderOpen className="lucide-inline" aria-hidden="true" />} label={i18nT('pages.kiroCrewAgentsPage.workspace_2')} value={agent.workspace} note={filesShared ? sharedNote : undefined} />
        <Binding icon={<Database className="lucide-inline" aria-hidden="true" />} label={i18nT('pages.kiroCrewAgentsPage.memory_store')} value={agent.memory_store} note={memoryShared ? sharedNote : undefined} />
        <Binding
          icon={<Sparkles className="lucide-inline" aria-hidden="true" />}
          label={i18nT('pages.kiroCrewAgentsPage.model')}
          value={agent.model || i18nT('pages.kiroCrewAgentsPage.inherited')}
          muted={!agent.model}
        />
      </div>
    </Clickable>
  )
}

/**
 * What a crew's description line shows, so the card and the row cannot drift.
 *
 * A crew with no description read as blank in the card but italic "No
 * description" in the row, i.e. the same crew looked different per view. The
 * default crew keeps its own hint instead — that line is what tells a first-run
 * user why this crew matters, and a test asserts it.
 *
 * Returns `text` plus whether it is real copy: a placeholder must render italic
 * and must NOT become a tooltip, or every empty crew advertises a blank bubble.
 */
function describeCrew(agent: KiroCrewAgent, isDefault: boolean): { text: string; placeholder: boolean } {
  if (agent.description) return { text: agent.description, placeholder: false }
  if (isDefault) return { text: i18nT('pages.kiroCrewAgentsPage.used_for_all_new_chats'), placeholder: true }
  return { text: i18nT('pages.kiroCrewAgentsPage.no_description'), placeholder: true }
}

/** One crew as a table row. The row opens the editor; the accessible target is
 *  the real button in the name cell, so table semantics stay intact — a `<tr>`
 *  given `role="button"` stops being announced as a row at all. */
function CrewRow({ agent, isDefault, shared, onOpen }: {
  agent: KiroCrewAgent
  isDefault: boolean
  shared: SharedKind
  onOpen: () => void
}) {
  const sharedNote = i18nT('pages.kiroCrewAgentsPage.shared_lower')
  const filesShared = shared === 'files' || shared === 'both'
  const memoryShared = shared === 'memory' || shared === 'both'
  const desc = describeCrew(agent, isDefault)
  return (
    <TableRow
      data-testid="crew-row"
      className={`cursor-pointer ${isDefault ? 'bg-accent-subtle/30' : ''}`}
      // Convenience only: the whole row is a click target, but a click that
      // landed on the name control must not fire this too or the editor would be
      // asked to open twice for one gesture. Reuses the focus trap's FOCUSABLE
      // selector rather than spelling out a second list: `Clickable` renders a
      // `div[role=button][tabindex=0]`, so a hand-written `closest('button')`
      // would silently never match it, and one definition of "interactive
      // element" cannot drift out of step with itself.
      onClick={e => { if (!(e.target as HTMLElement).closest(FOCUSABLE)) onOpen() }}
    >
      <TableCell>
        <div className="flex items-center gap-2.5 min-w-0">
          <CrewAvatar seed={agent.name} avatar={agent.avatar} size={28} />
          <div className="min-w-0">
            <div className="flex items-center gap-2 min-w-0">
              <Clickable
                onClick={onOpen}
                aria-label={i18nT('pages.kiroCrewAgentsPage.edit_crew_named', { name: agent.name })}
                className="truncate rounded font-mono text-[12.5px] font-semibold text-text-strong focus-ring"
              >
                {agent.name}
              </Clickable>
              {isDefault && <Badge variant="ok" className="shrink-0">{i18nT('pages.kiroCrewAgentsPage.default_2')}</Badge>}
              {agent.source && agent.source !== 'kirocrew' && <SourceBadge source={agent.source} />}
            </div>
            {/* One line here is the point of this view — the row is wide, so a
                single line already carries far more of the sentence than the
                card's clamp does, and the full text is in the tooltip. Same
                fallback chain as the card (see describeCrew). */}
            <span
              className={`block max-w-[380px] truncate text-[11.5px] text-muted ${desc.placeholder ? 'italic' : ''}`}
              title={desc.placeholder ? undefined : desc.text}
            >
              {desc.text}
            </span>
          </div>
        </div>
      </TableCell>
      <TableCell className="font-mono text-muted">{agent.kiro_agent}</TableCell>
      <TableCell className="font-mono text-muted">
        {agent.workspace}
        {filesShared && <Badge variant="warn" className="ml-1.5">{sharedNote}</Badge>}
      </TableCell>
      <TableCell className="font-mono text-muted">
        {agent.memory_store}
        {memoryShared && <Badge variant="warn" className="ml-1.5">{sharedNote}</Badge>}
      </TableCell>
      <TableCell className={`font-mono ${agent.model ? 'text-muted' : 'italic text-muted'}`}>
        {agent.model || i18nT('pages.kiroCrewAgentsPage.inherited')}
      </TableCell>
    </TableRow>
  )
}

export default function KiroCrewAgentsPage({ embedded }: { embedded?: boolean } = {}) {
  const provider = useProvider()
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const { data: agentsData, refetch: refetchAgents } = useQuery({
    queryKey: ['kirocrew-agents'],
    queryFn: () => api.kirocrewAgents(),
  })
  // Memoised for the empty case: a bare `|| []` hands out a new array on every
  // render, which defeats every `useMemo` downstream that keys on the roster
  // (`sharedTargets`). React Query's structural sharing keeps `agentsData`
  // identical until the roster actually changes, so this is stable between
  // fetches that return the same rows.
  const agents = useMemo<KiroCrewAgent[]>(() => agentsData?.agents || [], [agentsData])
  const defaultAgent = agentsData?.default_agent || ''

  const { data: installedAgents } = useQuery({
    queryKey: ['agents-installed'],
    queryFn: () => api.agentsInstalled(),
  })
  const kiroAgentOptions = Array.isArray(installedAgents) ? installedAgents.map((x: { name: string }) => x.name).filter(Boolean) : ['kirocrew']

  const { data: workspacesData, refetch: refetchWorkspaces } = useQuery({
    queryKey: ['workspaces'],
    queryFn: () => api.workspaces(),
  })
  const workspaceOptions = workspacesData?.workspaces?.map((w: { name: string }) => w.name) || ['default']

  const { data: kirocrewCfg } = useQuery({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const memoryStoreOptions = kirocrewCfg?.memory_stores ? Object.keys(kirocrewCfg.memory_stores) : ['default']

  // Model list for the per-agent default. Same query key as every other model
  // picker so the list is fetched once. INHERIT_MODEL leads so "no pin" is the
  // obvious choice rather than an absent option.
  const availableModels = useAvailableModels()
  const modelOptions = [
    INHERIT_MODEL,
    ...(availableModels || []).map((m: { name: string }) => m.name).filter((n: string) => n && n !== INHERIT_MODEL),
  ]

  const [filter, setFilter] = useState('')
  const [view, setView] = useState<CrewView>(readStoredView)
  const [error, setError] = useState('')
  const [sheet, setSheet] = useState<SheetTarget>(null)
  const [name, setName] = useState('')
  // Starts UNSELECTED, not at the built-in 'kirocrew'. Pre-filling the built-in
  // made every crew created without touching this field an alias for the DEFAULT
  // agent: the crew is offered in the chat picker, then dispatch flattens the
  // alias to its `kiro_agent` pointer and the default answers — indistinguishable
  // from "the picker reverted to default" (#1684). An empty value forces the
  // choice to be explicit and is rejected by `create()` below.
  const [kiroAgent, setKiroAgent] = useState('')
  const [workspace, setWorkspace] = useState('default')
  const [memoryStore, setMemoryStore] = useState('default')
  const [triggers, setTriggers] = useState('')
  const [sessionColor, setSessionColor] = useState('')
  const [editModel, setEditModel] = useState(INHERIT_MODEL)
  const [editEffort, setEditEffort] = useState('')
  /** Draft avatar override. null = the name-derived face (no override). */
  const [editAvatar, setEditAvatar] = useState<CrewAvatarOverride | null>(null)
  const [avatarBuilderOpen, setAvatarBuilderOpen] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  /** The armed confirm row, scrolled into view when it appears: the danger zone
   *  is the last section, so on a short window the confirm buttons land under
   *  the sticky footer and the user cannot see what they are being asked. */
  const confirmRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (confirmDelete) confirmRef.current?.scrollIntoView({ block: 'nearest' })
  }, [confirmDelete])
  const [wsModalOpen, setWsModalOpen] = useState(false)

  /** Remember the layout across visits. Wrapped because `localStorage` can throw
   *  outright (blocked/partitioned storage) — losing the preference is fine,
   *  taking the roster down with it is not. */
  const pickView = useCallback((v: CrewView) => {
    setView(v)
    try { localStorage.setItem(VIEW_KEY, v) } catch { /* preference is best-effort */ }
  }, [])

  const editing = sheet?.mode === 'edit' ? sheet.name : ''
  const editingAgent = agents.find(a => a.name === editing)

  /** The model a new session on this crew would actually run on, resolved by
   *  the backend so the precedence is not re-derived (and drifted) here. */
  const { data: resolved } = useQuery({
    queryKey: ['agent-resolved-model', editing],
    queryFn: () => api.agentResolvedModel(editing),
    enabled: !!editing,
  })

  /** The model an effort level would be applied to: the pending pick when the
   *  crew pins one, otherwise whatever the inherit chain resolves to. Reading
   *  the PENDING value is deliberate — the effort control has to appear and
   *  disappear as the user moves the model select, not one save later.
   *
   *  `resolved` describes the SAVED state, so it only answers for a pending
   *  Inherited when the saved state was Inherited too. Once a stored pin is
   *  cleared but not yet saved, `resolved.model` is still that pin — reusing it
   *  would keep offering an effort control on the strength of a model the crew is
   *  about to stop using, and the level would then be dropped at spawn. Nothing
   *  here can know what the inherit chain lands on until the write happens, so
   *  that state reports unresolved and says so. */
  const modelPinPendingClear = editModel === INHERIT_MODEL && !!editingAgent?.model
  const effortModel = editModel !== INHERIT_MODEL
    ? editModel
    : modelPinPendingClear ? '' : (resolved?.model || '')
  const effortCapable = modelSupportsEffort(effortModel)

  const openCreate = useCallback(() => {
    sheetEpoch.current += 1
    setError('')
    setConfirmDelete(false)
    setName(''); setKiroAgent(''); setWorkspace('default'); setMemoryStore('default')
    setTriggers('')
    setSessionColor('')
    setSheet({ mode: 'create' })
  }, [])

  const openEdit = useCallback((a: KiroCrewAgent) => {
    sheetEpoch.current += 1
    setError('')
    setConfirmDelete(false)
    setKiroAgent(a.kiro_agent); setWorkspace(a.workspace); setMemoryStore(a.memory_store)
    setTriggers(a.triggers || '')
    setSessionColor(a.session_color || '')
    setEditModel(a.model || INHERIT_MODEL)
    setEditEffort(a.reasoning_effort || '')
    // Normalized through the same coercion the renderer applies, so the dirty
    // check compares like with like (a junk stored value reads as "no
    // override" everywhere).
    const storedTraits = ghostTraitsFrom(a.avatar)
    const storedImage = imageAvatarFrom(a.avatar)
    setEditAvatar(
      storedTraits
        ? { kind: 'ghost', traits: storedTraits }
        : storedImage
          ? { kind: 'image', v: storedImage.v }
          : null,
    )
    setAvatarBuilderOpen(false)
    setSheet({ mode: 'edit', name: a.name })
  }, [])

  const closeSheet = useCallback(() => { sheetEpoch.current += 1; setSheet(null); setError(''); setConfirmDelete(false) }, [])

  /**
   * Identity of the CURRENT panel opening, bumped on every open and every
   * close.
   *
   * An async completion must only act on the panel it was fired from. Comparing
   * the crew name is not enough: dismissing and reopening the SAME crew is a
   * different panel holding different unsaved edits, and a name comparison
   * cannot tell those two apart. A per-opening counter can.
   */
  const sheetEpoch = useRef(0)

  /**
   * Apply a finished write's outcome ONLY if the panel it was fired from is
   * still the one on screen.
   *
   * Without this, a write that resolves after the user has moved on lands on
   * the wrong panel: save, dismiss while it is in flight, reopen — the stale
   * success then dismisses the replacement and discards its unsaved edits, and
   * a stale failure is reported as though it belonged to whatever is open now.
   */
  const settleFor = useCallback((epoch: number, err?: string) => {
    if (epoch !== sheetEpoch.current) return
    if (err) { setError(err); return }
    closeSheet()
  }, [closeSheet])

  const handleWsCreated = useCallback((newName: string) => {
    setWsModalOpen(false)
    refetchWorkspaces().then(() => setWorkspace(newName))
  }, [refetchWorkspaces])

  const createMut = useMutation({
    mutationFn: ({ epoch: _epoch, ...data }: CreatePayload & { epoch: number }) => api.createKirocrewAgent(data),
    onSuccess: (r: AgentMutationResult, vars) => { settleFor(vars.epoch, r.error); refetchAgents() },
    onError: (e: Error, vars) => settleFor(vars.epoch, e.message || i18nT('pages.kiroCrewAgentsPage.failed_to_create_agent')),
  })
  const updateMut = useMutation({
    mutationFn: ({ name, data }: { name: string; data: AgentUpdatePayload; epoch: number }) => api.updateKirocrewAgent(name, data),
    onSuccess: (r: AgentMutationResult, vars) => { settleFor(vars.epoch, r.error); refetchAgents() },
    onError: (e: Error, vars) => settleFor(vars.epoch, e.message || i18nT('pages.kiroCrewAgentsPage.failed_to_update_agent')),
  })
  /** Promotion is its own write, fired straight from the roster bar — it is not
   *  part of saving a crew's bindings, so it must not wait for a Save. */
  const defaultMut = useMutation({
    mutationFn: (n: string) => api.setDefaultAgent(n),
    onSuccess: (r: AgentMutationResult) => { if (r.error) { setError(r.error); return }; refetchAgents() },
    onError: (e: Error) => setError(e.message || i18nT('pages.kiroCrewAgentsPage.failed_to_update_agent')),
  })
  const deleteMut = useMutation({
    mutationFn: ({ name }: { name: string; epoch: number }) => api.deleteKirocrewAgent(name),
    onSuccess: (r: AgentMutationResult, vars) => { settleFor(vars.epoch, r.error); refetchAgents() },
    onError: (e: Error, vars) => settleFor(vars.epoch, e.message || i18nT('pages.kiroCrewAgentsPage.failed_to_delete_agent')),
  })

  const create = () => {
    setError('')
    const n = name.trim()
    if (!n) { setError(i18nT('pages.kiroCrewAgentsPage.name_is_required')); return }
    // Refuse an unset template rather than letting the server apply its
    // 'kirocrew' default: that default is what silently turns a new crew into an
    // alias for the DEFAULT agent (#1684).
    if (!kiroAgent) { setError(i18nT('pages.kiroCrewAgentsPage.agent_template_is_required')); return }
    createMut.mutate({ name: n, kiro_agent: kiroAgent, workspace, memory_store: memoryStore, triggers, session_color: sessionColor, epoch: sheetEpoch.current })
  }

  const saveEdit = async () => {
    if (!editing) return
    setError('')
    // Snapshot the identity of THIS save before the first await: the epoch
    // moves when the editor closes or reopens, and a save that awaited an
    // upload must neither write through to a different opening's crew nor
    // settle (close) a dialog it no longer owns. The FIELD VALUES are
    // snapshotted here too — what commits is exactly what was on screen when
    // Save was pressed, and the pane is fenced (disabled) for the upload's
    // duration so no edit can land mid-save only to be discarded by the
    // post-save close.
    const epoch = sheetEpoch.current
    const name = editing
    const data = {
      kiro_agent: kiroAgent,
      workspace,
      memory_store: memoryStore,
      triggers,
      // INHERIT_MODEL is normalized to '' server-side; send it verbatim so
      // clearing a pin is a real write rather than a skipped field.
      model: editModel,
      // Sent unconditionally for the same reason as `model`: '' is a real
      // value (clear the pin), so a skipped field would make clearing
      // impossible.
      reasoning_effort: editEffort,
      session_color: sessionColor,
    }
    let avatarPayload: CrewAvatarOverride | Record<string, never> = editAvatar ?? {}
    if (editAvatar?.kind === 'image') {
      let stagedToken: string | null = null
      if (editAvatar.pendingData) {
        // STAGE the picture; nothing live changes until the PUT below
        // promotes it under the server's config lock — so a failed or
        // abandoned Save never costs the previously saved picture.
        setAvatarUploading(true)
        try {
          // Decode the data URI directly — fetch(data:) trips the
          // dashboard's connect-src CSP, which only allows network origins.
          const comma = editAvatar.pendingData.indexOf(',')
          const mime = /data:([^;,]+)/.exec(editAvatar.pendingData)?.[1] ?? 'image/png'
          const bin = atob(editAvatar.pendingData.slice(comma + 1))
          const bytes = new Uint8Array(bin.length)
          for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
          const up = await api.uploadCrewAvatar(name, new Blob([bytes], { type: mime }))
          if (!up.ok || !up.token) {
            settleFor(epoch, up.error || i18nT('pages.kiroCrewAgentsPage.failed_to_update_agent'))
            return
          }
          stagedToken = up.token
        } catch (e) {
          // A thrown error here is transport-level ("Failed to fetch", a
          // decode failure) — vocabulary the user never saw. The banner gets
          // the localized failure; the raw message goes to the console.
          // eslint-disable-next-line no-console -- the only record of WHY a staged upload failed once the banner shows the localized string
          console.warn('avatar upload failed', e)
          settleFor(epoch, i18nT('pages.kiroCrewAgentsPage.failed_to_update_agent'))
          return
        } finally {
          setAvatarUploading(false)
        }
      }
      // `promote` + the staging token only when THIS save staged the file —
      // the token is what stops an overlapping save's staging from being
      // committed by this one, and a plain {kind:'image'} keeps the current
      // picture while the server discards any stale staging. The server
      // stamps the cache-buster `v` at the commit.
      avatarPayload = stagedToken
        ? { kind: 'image', promote: true, token: stagedToken }
        : { kind: 'image' }
    }
    if (epoch !== sheetEpoch.current) return
    updateMut.mutate({
      name,
      epoch,
      data: {
        ...data,
        // {} is the wire spelling for "reset to the name-derived face".
        avatar: avatarPayload,
      },
    })
  }

  const chatWith = async (crew: string) => {
    const epoch = sheetEpoch.current
    setError('')
    try {
      // `dispatch(thunk)` resolves with a REJECTED action on failure; only
      // `unwrap()` throws. Without it a failed create still navigated to /chat
      // and silently showed whatever session happened to be active.
      await dispatch(createSlot(crew)).unwrap()
    } catch (e) {
      // `unwrap()` rethrows Redux Toolkit's SERIALIZED error, which is a plain
      // object carrying `message` rather than a real Error — an `instanceof`
      // check alone renders it as "[object Object]". `errMessage` owns that
      // extraction for every thunk-boundary reader, so this site cannot drift
      // from the classifier in `utils/thunkError` that depends on the same fact.
      const msg = errMessage(e)
      settleFor(epoch, msg || i18nT('pages.kiroCrewAgentsPage.failed_to_update_agent'))
      return
    }
    // Navigating away is the most disruptive thing this page does, so it too
    // must only happen for the panel that actually asked for it.
    if (epoch !== sheetEpoch.current) return
    closeSheet()
    navigate('/chat')
  }

  const filtered = agents.filter(a =>
    !filter || (a.name + ' ' + a.kiro_agent + ' ' + a.workspace + ' ' + a.memory_store).toLowerCase().includes(filter.toLowerCase())
  )

  /** Workspaces and memory stores that more than one crew points at. Surfacing
   *  this is the one thing a flat list cannot show: two crews on one store
   *  share their lessons and history, which is easy to do by accident and
   *  confusing to debug later. Reported as WHICH store collides, because a bare
   *  "Shared" badge was read by a first-run reviewer as "shared with other
   *  people" — the wrong meaning and the alarming one. */
  const sharedTargets = useMemo(() => {
    const ws = new Map<string, number>()
    const ms = new Map<string, number>()
    agents.forEach(a => {
      ws.set(a.workspace, (ws.get(a.workspace) || 0) + 1)
      ms.set(a.memory_store, (ms.get(a.memory_store) || 0) + 1)
    })
    return { ws, ms }
  }, [agents])
  const sharedKind = (a: KiroCrewAgent): SharedKind => {
    const files = (sharedTargets.ws.get(a.workspace) || 0) > 1
    const memory = (sharedTargets.ms.get(a.memory_store) || 0) > 1
    if (files && memory) return 'both'
    if (memory) return 'memory'
    if (files) return 'files'
    return 'none'
  }
  /** The other crews the edited crew collides with, named so the warning in the
   *  editor panel is concrete rather than abstract.
   *
   *  Compared against the IN-FLIGHT select values, not the persisted ones: the
   *  whole point of the warning is to catch the collision you are about to
   *  create, and reading `editingAgent.*` here meant re-pointing a crew at a
   *  store another crew already uses stayed silent until after a save and a
   *  reopen. */
  /** Crews sharing the IN-FLIGHT selection, split by WHICH resource collides.
   *
   *  Split rather than OR-ed because the overview tags each node from its own
   *  resource: one OR-ed value labels a private workspace "Shared" whenever only
   *  the memory store is. `collidingCrews` is their union, so the stat, the
   *  warning and the two pills are three readings of one predicate and cannot
   *  disagree. Read off the in-flight values, never the persisted per-agent
   *  counts in `sharedTargets` — those answer a question about the ROSTER, and
   *  against a selection the user has just changed they report the collision the
   *  crew used to have instead of the one it is about to create. */
  const sharingWorkspace = editing
    ? agents.filter(a => a.name !== editing && a.workspace === workspace).map(a => a.name)
    : []
  const sharingMemoryStore = editing
    ? agents.filter(a => a.name !== editing && a.memory_store === memoryStore).map(a => a.name)
    : []
  const collidingCrews = [...new Set([...sharingWorkspace, ...sharingMemoryStore])]

  const creating = sheet?.mode === 'create'
  const [avatarUploading, setAvatarUploading] = useState(false)
  const sheetBusy =
    createMut.isPending || updateMut.isPending || deleteMut.isPending || avatarUploading

  /** Which rail pane the editor body is showing. Reset whenever the editor is
   *  pointed somewhere else, so a crew never opens on the pane the previous one
   *  happened to be left on. */
  const [pane, setPane] = useState<CrewPaneKey>('overview')
  const [schedDraft, setSchedDraft] = useState(false)
  /** True while the schedule draft's create request is in flight. Discarding
   *  then would unmount the form WITHOUT cancelling the POST, so the schedule
   *  the user watched being "discarded" persists — the confirm's destructive
   *  button locks on this for exactly the reason the pane's own toggle does. */
  const [schedSaving, setSchedSaving] = useState(false)
  /** Where the schedule-draft discard confirm would go if confirmed: a pane
   *  key to switch to, 'close' to dismiss the editor, or 'chat' to open a
   *  chat with this crew. null = no confirm showing. One state drives EVERY
   *  destruction path so they get the same guard for the same reason -- the
   *  footer's Save is already disabled for this draft, and a rail click, an
   *  Escape, or the header's chat jump destroying it silently would make the
   *  one tracked-dirty pane the one pane whose work a click erases. */
  const [discardAsk, setDiscardAsk] = useState<CrewPaneKey | 'close' | 'chat' | 'collapse' | null>(null)
  /** Escape hatch for a hung save: the create POST has no client timeout, so
   *  a stalled request would otherwise lock EVERY exit from the editor for
   *  as long as it stalls. After a grace period with the confirm open and
   *  the save still in flight, the destructive button unlocks — the note
   *  then says the honest thing: discarding cannot cancel the POST, so the
   *  schedule may still be created. */
  const [discardForce, setDiscardForce] = useState(false)
  useEffect(() => {
    if (discardAsk === null || !schedSaving) { setDiscardForce(false); return }
    const t = setTimeout(() => setDiscardForce(true), DISCARD_FORCE_GRACE_MS)
    return () => clearTimeout(t)
  }, [discardAsk, schedSaving])
  /** The section-owned collapse to run if the user confirms discarding via
   *  the toggle ('collapse' target). A ref, not state: it is a continuation,
   *  not something the render reads. */
  const collapseProceed = useRef<(() => void) | null>(null)
  useEffect(() => { setPane('overview'); setSchedDraft(false); setSchedSaving(false); setDiscardAsk(null) }, [sheet])

  /** Rail-driven pane changes route through here: leaving the schedules pane
   *  while a schedule draft is open asks before destroying the typed work
   *  (the form's state is component-local and unmounts with the pane). */
  const requestPane = useCallback((key: CrewPaneKey) => {
    if (schedDraft && key !== pane) { setDiscardAsk(key); return }
    setPane(key)
  }, [schedDraft, pane])

  /** Editor dismissal (footer Cancel, Escape, overlay click) routes through
   *  here: same draft, same guard, same reason as the pane switch above. */
  const requestClose = useCallback(() => {
    if (schedDraft) { setDiscardAsk('close'); return }
    closeSheet()
  }, [schedDraft, closeSheet])

  /** The header's "Chat with this crew" routes through here: it creates a
   *  chat slot, closes the sheet and navigates -- three steps that would
   *  destroy an open schedule draft as silently as an unguarded Escape. */
  const requestChat = useCallback(() => {
    if (schedDraft) { setDiscardAsk('chat'); return }
    void chatWith(editing)
  }, [schedDraft, editing]) // eslint-disable-line react-hooks/exhaustive-deps -- chatWith is re-created per render; depping it would make this callback churn for no behavioural gain

  /** The wake section's own cancel toggle asks here before collapsing a
   *  dirty draft -- the one destruction path the page cannot intercept
   *  itself (at narrow widths it is a bare icon-only X). */
  const requestCancelDraft = useCallback((proceed: () => void) => {
    collapseProceed.current = proceed
    setDiscardAsk('collapse')
  }, [])

  const confirmDiscard = useCallback(() => {
    const target = discardAsk
    setDiscardAsk(null)
    // The form unmounts with the pane or the sheet; its unmount cleanup is
    // what clears `schedDraft`, so nothing here resets the flag by hand. The
    // chat path only destroys on SUCCESS: a failed slot-create keeps the
    // sheet open (chatWith settles the error and returns), draft intact.
    if (target === 'close') closeSheet()
    else if (target === 'chat') void chatWith(editing)
    else if (target === 'collapse') { collapseProceed.current?.(); collapseProceed.current = null }
    else if (target) setPane(target)
  }, [discardAsk, closeSheet, editing]) // eslint-disable-line react-hooks/exhaustive-deps -- same chatWith identity note as requestChat

  /** Pane changes driven from INSIDE a pane (an overview diagram node) rather
   *  than from the rail. The clicked node unmounts with its pane, which would
   *  drop keyboard focus to the body — so focus moves to the arriving panel,
   *  which carries `tabIndex={-1}` for exactly this hand-off. Rail clicks keep
   *  focus on the rail row and never set this flag. */
  const paneFocusPending = useRef(false)
  const goToPane = useCallback((key: CrewPaneKey) => {
    setPane(prev => {
      // Arm only on a real change: a same-pane call never reruns the focus
      // effect, so an armed flag would fire on the NEXT rail-driven change and
      // steal focus the rail contract says stays on the rail row. The ref
      // write is idempotent, so a double-invoked updater is harmless.
      if (prev !== key) paneFocusPending.current = true
      return key
    })
  }, [])
  const panelId = `crew-editor-pane-${editing || 'new'}`
  useEffect(() => {
    if (!paneFocusPending.current) return
    paneFocusPending.current = false
    document.getElementById(`${panelId}-${pane}`)?.focus()
  }, [pane, panelId])

  /** The rail's schedule count reads the SAME cached query the wake pane uses, so
   *  opening the editor costs one request rather than two. */
  const wakeQuery = useQuery({
    queryKey: crewWakeQueryKey(editing),
    queryFn: () => api.crons(),
    enabled: !!editing,
  })
  const wakeJobs = useMemo<CronJob[]>(
    () => (wakeQuery.data?.jobs || []).filter(
      (j: CronJob) => wakesCrew(j, editing, editing === defaultAgent)),
    [wakeQuery.data, editing, defaultAgent],
  )

  /** Same one-fetch rule for webhooks: the rail badge, the overview node and
   *  the webhook pane all read this single cached entry. */
  const webhooksQuery = useQuery({
    queryKey: crewWebhooksQueryKey,
    queryFn: () => api.webhooks(),
    enabled: !!editing,
  })
  const boundWebhookTokens = useMemo(
    () => (webhooksQuery.data?.tokens || []).filter(
      (t: WebhookTokenEntry) => webhookBoundToCrew(t, editing)),
    [webhooksQuery.data, editing],
  )
  const boundWebhooks = boundWebhookTokens.length
  const activeWebhooks = boundWebhookTokens.filter(
    (t: WebhookTokenEntry) => webhookCanCallIn(t, webhooksQuery.data?.switch_on !== false)).length

  /** Keywords the orchestrator can match, counted the way the field is authored:
   *  comma-separated, blanks ignored, so a trailing comma is not a keyword. */
  const routingWords = triggers.split(',').map(s => s.trim()).filter(Boolean).length

  /** Which panes hold an edit not yet saved. Compared against the SAVED crew,
   *  so a value the user typed and then typed back is not reported as pending. */
  const dirtyPanes = useMemo(() => {
    const out = new Set<CrewPaneKey>()
    if (!editingAgent) return out
    if (kiroAgent !== (editingAgent.kiro_agent || '')) out.add('template')
    if (workspace !== (editingAgent.workspace || '') || memoryStore !== (editingAgent.memory_store || '')) {
      out.add('place')
    }
    if (editModel !== (editingAgent.model || INHERIT_MODEL)) out.add('model')
    if (editEffort !== (editingAgent.reasoning_effort || '')) out.add('model')
    if (triggers !== (editingAgent.triggers || '')) out.add('routing')
    if (sessionColor !== (editingAgent.session_color || '')) out.add('routing')
    // Both tiers in one comparison: ghost traits normalize through
    // ghostTraitsFrom (flat record, stable key order) and an image override
    // through imageAvatarFrom — so a picture pick, replace, or removal is
    // dirty exactly like a trait edit. pendingData is part of the draft's
    // identity on purpose: a newly chosen picture IS an unsaved change.
    const savedNorm = ghostTraitsFrom(editingAgent.avatar) ?? imageAvatarFrom(editingAgent.avatar)
    const draftNorm =
      editAvatar?.kind === 'ghost'
        ? (editAvatar.traits ?? null)
        : editAvatar?.kind === 'image'
          ? { v: editAvatar.v, pendingData: editAvatar.pendingData }
          : null
    if (JSON.stringify(draftNorm) !== JSON.stringify(savedNorm)) out.add('routing')
    // An open inline schedule-create form is pending work too: it gets the
    // rail's unsaved dot and the note, so closing the editor cannot silently
    // eat a half-typed schedule the way an untracked surface would.
    if (schedDraft) out.add('schedules')
    return out
  }, [editingAgent, kiroAgent, workspace, memoryStore, editModel, editEffort, triggers, sessionColor, schedDraft, editAvatar])

  const sections = useCrewEditorSections({
    templateLabel: provider.labels.agentTemplateField,
    activeSchedules: wakeJobs.filter(j => j.enabled).length,
    totalSchedules: wakeJobs.length,
    routingWords,
    sharesStorage: collidingCrews.length > 0,
    canDelete: !!editing && editing !== defaultAgent,
    schedulesUnknown: wakeQuery.isError,
    webhookTokens: boundWebhooks,
    webhookTokensActive: activeWebhooks,
    webhooksUnknown: webhooksQuery.isError,
    dirtyPanes,
  })

  return (
    <>
      {!embedded && <PageHeader title={i18nT('pages.kiroCrewAgentsPage.agents')} subtitle={i18nT('pages.kiroCrewAgentsPage.manage_agent_workspace_memory_store_bindings')} />}
      <div className={`${embedded ? '' : 'px-4 md:px-6'} pb-8 overflow-y-auto flex-1 min-h-0`}>
        {/* Says out loud what the bindings below cannot: a crew's workspace and
            memory store are shown and editable, but the isolation they imply is
            only partly built — every crew still reads one shared semantic
            memory. Page-level rather than per-card: the claim is about the whole
            surface, and repeating it on every card would put two "?" glyphs on
            each of them. The editor panel and the list header carry the same
            copy as a tooltip, because neither can see this line. */}
        <div className="mb-3.5 flex items-start gap-2 rounded-lg border border-accent-subtle bg-bg-accent px-3 py-2.5">
          <Sparkles className="lucide-inline mt-0.5 shrink-0 text-accent" aria-hidden="true" />
          <span className="text-[12.5px] leading-relaxed text-muted">
            {i18nT('pages.kiroCrewAgentsPage.bindings_preview_notice')}
          </span>
        </div>

        {/* Which crew a new chat starts as, hoisted out of the cards. Two jobs:
            it answers "which one is the default" without hunting for a badge,
            and it is the one place that CHANGES it — a per-crew toggle could
            only ever offer promotion (the backend refuses to unset a default
            without naming a replacement), which read as a broken switch.
            Pointless with a single crew, so it only appears past that. */}
        {agents.length > 1 && (
          <div className="mb-3.5 flex flex-wrap items-center gap-2.5 rounded-lg border border-border bg-bg-accent px-3 py-2.5">
            <Star className="lucide-inline text-accent" aria-hidden="true" />
            <span className="text-[13px]">{i18nT('pages.kiroCrewAgentsPage.new_sessions_use')}</span>
            <SimpleSelect
              options={agents.map(a => a.name)}
              value={defaultAgent}
              onChange={n => { setError(''); defaultMut.mutate(n) }}
              aria-label={i18nT('pages.kiroCrewAgentsPage.new_sessions_use')}
              style={{ width: 190 }}
            />
            {/* Hand-off is safe here: the select commits immediately on change,
                so there is no unsaved draft for the navigation to destroy. */}
            <ErrorNotice message={error} variant="inline" askAgent />
          </div>
        )}

        <div className="mb-4 flex flex-wrap items-center gap-2">
          {/* No point offering a filter over an empty roster — it just adds a
              control a first-run user has to reason about. */}
          {agents.length > 0 && (
            <SearchInput
              className="w-[240px]"
              placeholder={i18nT('pages.kiroCrewAgentsPage.filter_agents')}
              aria-label={i18nT('pages.kiroCrewAgentsPage.filter_agents')}
              value={filter}
              onChange={e => setFilter(e.target.value)}
            />
          )}
          {/* Same control and the same persistence convention as the Artifacts
              page — NOT the same labels. Artifacts says "Gallery"/"Table"
              because its grid really is a preview gallery; a crew card is not a
              preview, so this reads "Cards"/"List". Hidden on an empty roster
              for the reason the filter is: there is no layout to choose.
              `collapse={false}` because this sits in a `flex-wrap` toolbar whose
              width the control itself contributes to — the responsive
              measurement would be circular and drop it to a dropdown. */}
          {agents.length > 0 && (
            <SegmentedControl<CrewView>
              layoutId="crews-view"
              collapse={false}
              value={view}
              onChange={pickView}
              segments={[
                {
                  key: 'cards',
                  label: i18nT('pages.kiroCrewAgentsPage.view_cards'),
                  icon: <LayoutGrid size={13} />,
                  tooltip: i18nT('pages.kiroCrewAgentsPage.view_cards_tooltip'),
                },
                {
                  key: 'list',
                  label: i18nT('pages.kiroCrewAgentsPage.view_list'),
                  icon: <Rows3 size={13} />,
                  tooltip: i18nT('pages.kiroCrewAgentsPage.view_list_tooltip'),
                },
              ]}
            />
          )}
          <div className="flex-1" />
          <SendBtn onClick={openCreate} data-testid="new-crew">
            <Plus className="lucide-inline" aria-hidden="true" />
            {i18nT('pages.kiroCrewAgentsPage.new_crew')}
          </SendBtn>
        </div>

        {agents.length === 0 ? (
          <div className="flex flex-col items-center">
            <EmptyState
              icon={<Users className="lucide-inline" aria-hidden="true" />}
              title={i18nT('pages.kiroCrewAgentsPage.no_crews_yet')}
              subtitle={i18nT('pages.kiroCrewAgentsPage.create_a_crew_to_give_an_agent_its_own_workspace')}
            />
            {/* The call to action belongs where the explanation is, not only in
                the toolbar above it. */}
            <SendBtn onClick={openCreate}>{i18nT('pages.kiroCrewAgentsPage.create_your_first_crew')}</SendBtn>
          </div>
        ) : filtered.length === 0 ? (
          <EmptyState
            icon={<Users className="lucide-inline" aria-hidden="true" />}
            title={i18nT('pages.kiroCrewAgentsPage.no_crews_match_your_filter')}
          />
        ) : view === 'list' ? (
          <div className="rounded-lg border border-border bg-card">
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead>{i18nT('pages.kiroCrewAgentsPage.crew_column')}</TableHead>
                  <TableHead>{provider.labels.agentTemplateField}</TableHead>
                  {/* `aria-label` keeps the column's accessible name to the
                      label itself. Without it the InfoTip's own name is
                      concatenated into the header, and a screen reader
                      announces every cell in the column as "Workspace,
                      Preview. Isolated memory per crew is…". */}
                  <TableHead aria-label={i18nT('pages.kiroCrewAgentsPage.workspace_2')}>
                    <span className="inline-flex items-center gap-1.5">
                      {i18nT('pages.kiroCrewAgentsPage.workspace_2')}
                      <InfoTip text={i18nT('pages.kiroCrewAgentsPage.bindings_preview_info')} />
                    </span>
                  </TableHead>
                  <TableHead aria-label={i18nT('pages.kiroCrewAgentsPage.memory_store')}>
                    <span className="inline-flex items-center gap-1.5">
                      {i18nT('pages.kiroCrewAgentsPage.memory_store')}
                      <InfoTip text={i18nT('pages.kiroCrewAgentsPage.bindings_preview_info')} />
                    </span>
                  </TableHead>
                  <TableHead>{i18nT('pages.kiroCrewAgentsPage.model')}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filtered.map(a => (
                  <CrewRow
                    key={a.name}
                    agent={a}
                    isDefault={a.name === defaultAgent}
                    shared={sharedKind(a)}
                    onOpen={() => openEdit(a)}
                  />
                ))}
              </TableBody>
            </Table>
          </div>
        ) : (
          <div className="grid gap-3.5 grid-cols-[repeat(auto-fill,minmax(290px,1fr))]">
            {filtered.map(a => (
              <CrewCard
                key={a.name}
                agent={a}
                isDefault={a.name === defaultAgent}
                shared={sharedKind(a)}
                onOpen={() => openEdit(a)}
              />
            ))}
            <Clickable
              onClick={openCreate}
              aria-label={i18nT('pages.kiroCrewAgentsPage.create_a_new_crew')}
              className="flex min-h-[150px] flex-col items-center justify-center gap-2 rounded-lg border
                         border-dashed border-border-strong text-muted transition-colors focus-ring
                         hover:border-accent hover:bg-accent-subtle hover:text-accent"
            >
              <Plus className="lucide-inline" aria-hidden="true" />
              <span className="text-[13px]">{i18nT('pages.kiroCrewAgentsPage.new_crew')}</span>
            </Clickable>
          </div>
        )}
      </div>

      <Dialog open={!!sheet} onOpenChange={next => { if (!next) requestClose() }}>
        <DialogContent
          /* The rail needs horizontal room; the create form does not have one. */
          maxWidth={creating ? 560 : 790}
          /* The visible title is just the crew name, which is not a usable
             accessible name on its own — it has to say what you are doing to it.
             An explicit aria-label outranks Radix's aria-labelledby, and the
             DialogTitle still has to EXIST or Radix warns. */
          aria-label={creating ? i18nT('pages.kiroCrewAgentsPage.create_a_new_crew') : i18nT('pages.kiroCrewAgentsPage.edit_crew_named', { name: editing })}
          /* Radix closes on an outside pointerdown and on Escape. Dismissing
             mid-write is DELIBERATELY still allowed: the sheetEpoch/settleFor
             machinery below exists to make the abandoned write land harmlessly,
             and suppressing it would break that. */
        >
          <DialogHeader>
            {/* The avatar is itself the entry point to the builder: the
                first-run review's top finding was that a face setting filed
                under "Triggers" has no scent — but everyone tries clicking
                the face. The Triggers-pane row remains as the discoverable
                text route. */}
            {!creating && (
              <button
                type="button"
                onClick={() => setAvatarBuilderOpen(true)}
                // Outside the pane's <fieldset> fence, so it carries the same
                // busy gate itself: a builder opened mid-save could Apply a newer
                // draft that the completing save's close then discards.
                disabled={sheetBusy}
                className="rounded-md transition-opacity hover:opacity-80 focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-60"
                aria-label={i18nT('components.avatarBuilder.title')}
                title={i18nT('components.avatarBuilder.title')}
                data-testid="header-avatar-button"
              >
                <CrewAvatar seed={editing} avatar={editAvatar ?? undefined} size={28} onImageError={() => setError(i18nT('components.avatarBuilder.image_load_failed'))} />
              </button>
            )}
            <DialogTitle className="font-mono">
              {creating ? i18nT('pages.kiroCrewAgentsPage.create_agent') : editing}
            </DialogTitle>
            {!creating && editingAgent?.source && <SourceBadge source={editingAgent.source} />}
            {!creating && (
              <Btn className="ml-auto" onClick={requestChat}>
                <MessageSquare className="lucide-inline" aria-hidden="true" />
                {i18nT('pages.kiroCrewAgentsPage.chat_with_this_crew')}
              </Btn>
            )}
          </DialogHeader>

          {/* Create is a short form and keeps the stacked layout. Edit is a rail:
              an existing crew has surfaces (schedules, bindings, removal) that a
              new one does not, and a wizard for creation is a separate decision. */}
          <DialogBody className={creating ? undefined : 'flex flex-col overflow-hidden p-0 sm:flex-row'}>
            {/* The fence that makes the Save-time snapshot honest: while a
                save is in flight (staged upload, the committing PUT, create
                or delete), every control in the pane is disabled (fieldset
                covers form controls, pointer-events the custom widgets), so
                no edit can land mid-save only to be silently dropped when
                the post-save close unmounts the pane.
                display:contents keeps the flex layout unchanged. */}
            <fieldset
              disabled={sheetBusy}
              aria-busy={sheetBusy}
              className={`contents ${sheetBusy ? '[&>*]:pointer-events-none [&>*]:opacity-60' : ''}`}
            >
            {creating ? (
              <div className="flex flex-col gap-6">
                <section className="flex flex-col gap-3">
                  <h3 className="text-[12px] font-semibold uppercase tracking-wider text-muted">{i18nT('pages.kiroCrewAgentsPage.identity')}</h3>
                  <Field label={i18nT('pages.kiroCrewAgentsPage.name')}>
                    <Input placeholder={i18nT('pages.kiroCrewAgentsPage.e_g_oncall')} value={name} onChange={e => setName(e.target.value)} autoFocus />
                  </Field>
                </section>
                <section className="flex flex-col gap-3">
                  <h3 className="text-[12px] font-semibold uppercase tracking-wider text-muted">{i18nT('pages.kiroCrewAgentsPage.routing')}</h3>
                  <TriggersField value={triggers} onChange={setTriggers} />
                  <SessionColorField value={sessionColor} onChange={setSessionColor} />
                </section>
                <section className="flex flex-col gap-3">
                  <h3 className="text-[12px] font-semibold uppercase tracking-wider text-muted">{i18nT('pages.kiroCrewAgentsPage.runtime_binding')}</h3>
                  <BindingFields
                    templateLabel={provider.labels.agentTemplateField}
                    kiroAgentOptions={kiroAgentOptions} kiroAgent={kiroAgent} setKiroAgent={setKiroAgent}
                    workspaceOptions={workspaceOptions} workspace={workspace} setWorkspace={setWorkspace}
                    onNewWorkspace={() => setWsModalOpen(true)}
                    memoryStoreOptions={memoryStoreOptions} memoryStore={memoryStore} setMemoryStore={setMemoryStore}
                  />
                </section>
              </div>
            ) : (
              <>
                <CrewEditorRail
                  sections={sections}
                  value={pane}
                  onChange={requestPane}
                  ariaLabel={i18nT('components.crewEditor.rail_label')}
                  unsavedLabel={i18nT('components.crewEditor.unsaved_changes')}
                  sharedLabel={i18nT('components.crewEditor.tag_shared')}
                  panelIdPrefix={panelId}
                />
                {/* `tabIndex={-1}` so moving focus here after a rail change is
                    possible without adding a Tab stop. The pane scrolls, not the
                    dialog body, which keeps the rail in view at any height. */}
                <div
                  id={`${panelId}-${pane}`}
                  role="tabpanel"
                  aria-labelledby={`${panelId}-tab-${pane}`}
                  tabIndex={-1}
                  className="flex min-w-0 flex-1 flex-col gap-3.5 overflow-y-auto px-5 py-4"
                >
                  {pane === 'overview' && (
                    <CrewOverviewPane
                      // The largest face in the editor opens the builder too, so
                      // the hub does not teach the opposite lesson from the
                      // header face (same title, same entry point).
                      hub={
                        <button
                          type="button"
                          onClick={() => setAvatarBuilderOpen(true)}
                          className="rounded-full transition-opacity hover:opacity-80 focus-visible:ring-2 focus-visible:ring-ring"
                          aria-label={i18nT('components.avatarBuilder.title')}
                          title={i18nT('components.avatarBuilder.title')}
                          data-testid="hub-avatar-button"
                        >
                          <CrewAvatar seed={editing} avatar={editAvatar ?? undefined} size={34} />
                        </button>
                      }
                      templateLabel={provider.labels.agentTemplateField}
                      template={kiroAgent}
                      workspace={workspace}
                      memoryStore={memoryStore}
                      modelLabel={editModel === INHERIT_MODEL ? i18nT('pages.kiroCrewAgentsPage.inherited') : editModel}
                      modelInherited={editModel === INHERIT_MODEL}
                      resolvedModel={resolved?.model || ''}
                      activeSchedules={wakeJobs.filter(j => j.enabled).length}
                      schedulesUnknown={wakeQuery.isError}
                      routingWords={routingWords}
                      sharingCrews={collidingCrews.length}
                      workspaceShared={sharingWorkspace.length > 0}
                      memoryShared={sharingMemoryStore.length > 0}
                      webhookTokens={boundWebhooks}
                      webhooksUnknown={webhooksQuery.isError}
                      onNavigate={goToPane}
                    />
                  )}

                  {pane === 'template' && (
                    <TemplateField
                      label={provider.labels.agentTemplateField}
                      options={kiroAgentOptions}
                      value={kiroAgent}
                      onChange={setKiroAgent}
                    />
                  )}

                  {pane === 'model' && (
                    <>
                      <ModelField options={modelOptions} value={editModel} onChange={setEditModel} />
                      {/* Offered when the model the crew will actually run on
                          accepts effort — OR when a pin is already stored on a
                          model that does not, so the only way to clear a
                          stranded pin is not to first switch the model back. */}
                      {(effortCapable || !!editEffort) && (
                        <EffortField value={editEffort} onChange={setEditEffort} />
                      )}
                      {!effortCapable && !!editEffort && (
                        <div className="rounded-md border border-warn-subtle bg-warn-subtle px-3 py-2.5 text-[11.5px] leading-relaxed text-muted">
                          {/* Two different reasons a stored pin cannot apply, and
                              they need different sentences: naming a model only
                              works when there IS one. With nothing resolved,
                              substituting the "Inherited" label would read as
                              "Inherited does not take a reasoning effort", which
                              names no model and states nothing true. */}
                          {effortModel
                            ? i18nT('pages.kiroCrewAgentsPage.effort_ignored_on_this_model', { model: effortModel })
                            : i18nT('pages.kiroCrewAgentsPage.effort_pin_needs_a_model')}
                        </div>
                      )}
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
                          {/* The effort half of the same readout. It answers
                              "what will this crew think at" in every case,
                              including the one where no level can apply — an
                              absent control with no line about it is what makes
                              the setting look missing rather than unavailable.
                              Suppressed only for the stranded pin, where the
                              warning above already says it and says what to do. */}
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
                    </>
                  )}

                  {pane === 'place' && (
                    <>
                      <WorkspaceField
                        options={workspaceOptions}
                        value={workspace}
                        onChange={setWorkspace}
                        onNewWorkspace={() => setWsModalOpen(true)}
                      />
                      <MemoryStoreField options={memoryStoreOptions} value={memoryStore} onChange={setMemoryStore} />
                      {collidingCrews.length > 0 && (
                        <div className="rounded-md border border-warn-subtle bg-warn-subtle px-3 py-2.5 text-[11.5px] leading-relaxed text-muted">
                          {i18nT('pages.kiroCrewAgentsPage.also_used_by_these_crews', { crews: collidingCrews.join(', ') })}
                        </div>
                      )}
                    </>
                  )}

                  {pane === 'schedules' && (
                    <CrewWakeSection crew={editing} isDefaultCrew={editing === defaultAgent} onDraftChange={setSchedDraft} onSavingChange={setSchedSaving} onRequestCancel={requestCancelDraft} />
                  )}

                  {pane === 'webhook' && <CrewWebhookSection crew={editing} />}

                  {pane === 'routing' && (
                    <>
                      <TriggersField value={triggers} onChange={setTriggers} />
                      <SessionColorField value={sessionColor} onChange={setSessionColor} />
                      <Field
                        label={i18nT('components.avatarBuilder.field_label')}
                        hint={i18nT('components.avatarBuilder.field_hint')}
                      >
                        <div className="flex items-center gap-2.5">
                          <CrewAvatar seed={editing || ''} avatar={editAvatar ?? undefined} size={36} />
                          <Btn onClick={() => setAvatarBuilderOpen(true)} data-testid="open-avatar-builder">
                            {i18nT('components.avatarBuilder.customize')}
                          </Btn>
                          {editAvatar && (
                            <span className="text-[11px] text-muted">
                              {i18nT('components.avatarBuilder.customized_note')}
                            </span>
                          )}
                        </div>
                      </Field>
                    </>
                  )}

                  {pane === 'danger' && (
                    <div className="flex flex-col gap-3 rounded-md border border-danger-subtle bg-danger-subtle p-3">
                      <p className="m-0 text-[12px] leading-relaxed text-muted">
                        {confirmDelete
                          ? i18nT('pages.kiroCrewAgentsPage.delete_crew_named_confirm', { name: editing })
                          : i18nT('pages.kiroCrewAgentsPage.deleting_a_crew_unbinds_it_from_new_sessions_its')}
                      </p>
                      {/* Two-step rather than a one-click destructive button: a
                          misclick in an overlay is far likelier than in a table,
                          and a first-run reviewer flagged it as the one action
                          they would regret. A nested confirm DIALOG was the other
                          option; inline keeps this out of a stacked focus trap. */}
                      <div ref={confirmRef} className="flex items-center gap-2">
                        <div className="flex-1" />
                        {confirmDelete ? (
                          <>
                            <Btn onClick={() => setConfirmDelete(false)} data-testid="cancel-delete-crew">{i18nT('pages.kiroCrewAgentsPage.cancel')}</Btn>
                            <Btn danger onClick={() => deleteMut.mutate({ name: editing, epoch: sheetEpoch.current })} disabled={sheetBusy} data-testid="confirm-delete-crew">
                              {i18nT('pages.kiroCrewAgentsPage.yes_delete_it')}
                            </Btn>
                          </>
                        ) : (
                          <Btn danger onClick={() => setConfirmDelete(true)} disabled={sheetBusy}>
                            {i18nT('pages.kiroCrewAgentsPage.delete_crew')}
                          </Btn>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              </>
            )}
            </fieldset>
          </DialogBody>

          {/* Save is disabled while nothing is pending. The schedule pause/run
              controls in the wake pane apply IMMEDIATELY, so a live Save button
              beside them implies those toggles are drafts that Cancel would roll
              back — which it cannot. */}
          <DialogFooter>
            <ErrorNotice message={error} variant="inline" className="mr-auto" />
            {!creating && dirtyPanes.size > 0 && !error && (
              <span className="mr-auto text-[11.5px] text-muted" data-testid="crew-unsaved-note">
                {/* While the open schedule draft is what disables Save, the note
                    names that reason in visible text — the `title` on the button
                    is hover-only, which keyboard and touch users never see. */}
                {schedDraft
                  ? i18nT('pages.kiroCrewAgentsPage.finish_the_new_schedule_first')
                  : i18nT('components.crewEditor.unsaved_changes')}
              </span>
            )}
            <Btn onClick={requestClose}>{i18nT('pages.kiroCrewAgentsPage.cancel')}</Btn>
            {creating ? (
              <SendBtn onClick={create} disabled={sheetBusy}>
                {createMut.isPending ? i18nT('pages.kiroCrewAgentsPage.creating') : i18nT('pages.kiroCrewAgentsPage.create')}
              </SendBtn>
            ) : (
              <SendBtn
                onClick={saveEdit}
                disabled={sheetBusy || dirtyPanes.size === 0 || schedDraft}
                title={schedDraft ? i18nT('pages.kiroCrewAgentsPage.finish_the_new_schedule_first') : undefined}
              >{i18nT('pages.kiroCrewAgentsPage.save_changes')}</SendBtn>
            )}
          </DialogFooter>

          {/* Nested INSIDE the editor's DialogContent so Radix treats it as a
              stacked layer of the same dialog tree — that is what makes Escape
              close only this one and focus return to the editor afterwards.
              Always mounted; `open` drives it (see WorkspaceModal). */}
          <WorkspaceModal
            open={wsModalOpen}
            workspaceOptions={workspaceOptions}
            onCreated={handleWsCreated}
            onClose={() => setWsModalOpen(false)}
          />

          {/* The schedule-draft discard confirm. Same nesting rule and same
              always-mounted rule as WorkspaceModal above — conditional
              rendering skips Radix's layer deregistration and leaves the
              editor believing it is no longer the top layer. The confirm
              button restates the action; the dismiss restates the
              alternative, because a bare "Cancel" beside the editor's own
              footer Cancel is the ambiguity this PR removes elsewhere. */}
          <Dialog open={discardAsk !== null} onOpenChange={next => { if (!next) setDiscardAsk(null) }}>
            <DialogContent maxWidth={440} className="z-[110]" aria-label={i18nT('pages.kiroCrewAgentsPage.discard_new_schedule')}>
              <DialogHeader>
                <DialogTitle>{i18nT('pages.kiroCrewAgentsPage.discard_new_schedule')}</DialogTitle>
              </DialogHeader>
              <DialogBody>
                <p className="m-0 text-sm text-text">{i18nT('pages.kiroCrewAgentsPage.discard_new_schedule_body')}</p>
                {/* The reason Discard is locked, as VISIBLE text: the button's
                    `title` never reaches keyboard or touch users, and browsers
                    often suppress titles on disabled controls entirely. */}
                {schedSaving && (
                  <p className="mb-0 mt-2 text-[12px] text-muted" data-testid="crew-sched-discard-saving-note">
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
                  // While the create request is in flight, discarding would not
                  // cancel it -- the schedule would persist after the user
                  // watched it "discarded". Locked until the request settles,
                  // EXCEPT after the grace period: a hung request must not
                  // seal every exit from the editor, so the button unlocks
                  // and the visible note carries the may-still-persist caveat.
                  disabled={schedSaving && !discardForce}
                  title={schedSaving ? i18nT('components.jobForm.saving') : undefined}
                  data-testid="crew-sched-discard-confirm"
                >
                  {i18nT('pages.kiroCrewAgentsPage.discard_schedule_confirm')}
                </Btn>
              </DialogFooter>
            </DialogContent>
          </Dialog>
          {/* Same stacked-layer contract as WorkspaceModal: mounted inside the
              editor's DialogContent, `open`-driven. Save only lands in the
              editor's draft state — the crew record is written by Save changes. */}
          {!creating && editing && (
            <CrewAvatarBuilder
              open={avatarBuilderOpen}
              name={editing}
              value={editAvatar}
              onCancel={() => setAvatarBuilderOpen(false)}
              onSave={next => { setEditAvatar(next); setAvatarBuilderOpen(false) }}
            />
          )}
        </DialogContent>
      </Dialog>
    </>
  )
}
