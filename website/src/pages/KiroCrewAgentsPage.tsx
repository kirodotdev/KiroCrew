import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { Boxes, FolderOpen, Database, Sparkles, Plus, MessageSquare, Users, Star, LayoutGrid, Rows3 } from 'lucide-react'
import Clickable from '../components/Clickable'
import { useQuery, useMutation } from '@tanstack/react-query'
import { useAppSelector, useAppDispatch } from '../store'
import { createSlot } from '../store/chatSlice'
import { api } from '../api/client'
import { useProvider } from '../providers'
import { useAvailableModels } from '../hooks/useAvailableModels'
import { Btn, SendBtn, Input, Badge, SearchInput, PageHeader, EmptyState } from '../components/ui'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import {
  Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from '../components/ui/dialog'
import SegmentedControl from '../components/SegmentedControl'
import InfoTip from '../components/InfoTip'
import { FOCUSABLE } from '../hooks/useDialogFocusTrap'
import SimpleSelect from '../components/SimpleSelect'
import CrewAvatar from '../components/CrewAvatar'
import type { KiroCrewAgent } from '../components/AgentSelector'
import { SourceBadge } from '../components/SourceBadge'

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
              {/* eslint-disable-next-line jsx-a11y/label-has-for */}
              <label htmlFor="ws-name" className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('pages.kiroCrewAgentsPage.name')}</label>
              <InfoTip text={i18nT('pages.kiroCrewAgentsPage.a_unique_identifier_for_this_workspace_agents_re')} />
            </div>
            <Input id="ws-name" placeholder={i18nT('pages.kiroCrewAgentsPage.e_g_oncall')} value={wsName} onChange={e => handleNameChange(e.target.value)} autoFocus />
          </div>
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-1">
              {/* Native input associated via htmlFor+id; label-has-for's nesting requirement is a false positive. */}
              {/* eslint-disable-next-line jsx-a11y/label-has-for */}
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

/**
 * The four binding selects. Shared by the create and edit modes of the editor
 * panel so the two paths cannot drift (and so the block is not duplicated,
 * which the jscpd gate would reject).
 */
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
  const withCurrent = (opts: string[], cur: string) => (opts.includes(cur) ? opts : [...opts, cur])
  return (
    <>
      <Field label={templateLabel} hint={i18nT('pages.kiroCrewAgentsPage.the_agent_definition_it_boots_from_tools_mcp_ser')}>
        <SimpleSelect options={withCurrent(kiroAgentOptions, kiroAgent)} value={kiroAgent} onChange={setKiroAgent} aria-label={templateLabel} />
      </Field>
      <Field
        label={i18nT('pages.kiroCrewAgentsPage.workspace_2')}
        hint={i18nT('pages.kiroCrewAgentsPage.isolated_memory_and_files_for_this_crew')}
        info={i18nT('pages.kiroCrewAgentsPage.bindings_preview_info')}
      >
        <SimpleSelect
          options={withCurrent(workspaceOptions, workspace)}
          value={workspace}
          onChange={setWorkspace}
          action={{ label: i18nT('pages.kiroCrewAgentsPage.new_workspace_action'), onSelect: onNewWorkspace }}
          aria-label={i18nT('pages.kiroCrewAgentsPage.workspace_2')}
        />
      </Field>
      <Field
        label={i18nT('pages.kiroCrewAgentsPage.memory_store')}
        hint={i18nT('pages.kiroCrewAgentsPage.which_store_its_lessons_and_history_are_written')}
        info={i18nT('pages.kiroCrewAgentsPage.bindings_preview_info')}
      >
        <SimpleSelect options={withCurrent(memoryStoreOptions, memoryStore)} value={memoryStore} onChange={setMemoryStore} aria-label={i18nT('pages.kiroCrewAgentsPage.memory_store')} />
        {/* Show the "more coming" note only when `default` is the sole option —
            an install that has declared extra `memory_stores` in config already
            has a real choice here, and the copy must not contradict a picker
            that is visibly offering other stores. */}
        {memoryStoreOptions.length <= 1 && (
          <span className="flex items-start gap-1.5 text-[11.5px] leading-relaxed text-accent">
            <Sparkles className="lucide-inline h-3 w-3 mt-0.5 shrink-0" aria-hidden="true" />
            {i18nT('pages.kiroCrewAgentsPage.additional_stores_coming_with_memory_v2')}
          </span>
        )}
      </Field>
      {modelOptions && setModel && model !== undefined && (
        <Field label={i18nT('pages.kiroCrewAgentsPage.model')}>
          <SimpleSelect
            options={withCurrent(modelOptions, model)}
            // The inherit option must NOT read as "auto": in the chat picker
            // "auto" promises task-based routing, whereas here it means "pin
            // nothing, inherit the next tier" — which can resolve to a concrete
            // model. Label it as the card does so the round trip stays honest.
            optionLabels={withCurrent(modelOptions, model).map(m => (m === INHERIT_MODEL ? i18nT('pages.kiroCrewAgentsPage.inherited') : m))}
            value={model}
            onChange={setModel}
            aria-label={i18nT('pages.kiroCrewAgentsPage.edit_model')}
          />
        </Field>
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
    >
      <div className="flex items-center gap-3">
        <CrewAvatar seed={agent.name} size={38} />
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
          <CrewAvatar seed={agent.name} size={28} />
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
  const refreshTrigger = useAppSelector(s => s.dashboard.refreshTrigger)

  const { data: agentsData, refetch: refetchAgents } = useQuery({
    queryKey: ['kirocrew-agents', refreshTrigger],
    queryFn: () => api.kirocrewAgents(),
  })
  const agents: KiroCrewAgent[] = agentsData?.agents || []
  const defaultAgent = agentsData?.default_agent || ''

  const { data: installedAgents } = useQuery({
    queryKey: ['agents-installed', refreshTrigger],
    queryFn: () => api.agentsInstalled(),
  })
  const kiroAgentOptions = Array.isArray(installedAgents) ? installedAgents.map((x: { name: string }) => x.name).filter(Boolean) : ['kirocrew']

  const { data: workspacesData, refetch: refetchWorkspaces } = useQuery({
    queryKey: ['workspaces', refreshTrigger],
    queryFn: () => api.workspaces(),
  })
  const workspaceOptions = workspacesData?.workspaces?.map((w: { name: string }) => w.name) || ['default']

  const { data: kirocrewCfg } = useQuery({
    queryKey: ['kirocrewConfig', refreshTrigger],
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
  const [kiroAgent, setKiroAgent] = useState('kirocrew')
  const [workspace, setWorkspace] = useState('default')
  const [memoryStore, setMemoryStore] = useState('default')
  const [triggers, setTriggers] = useState('')
  const [editModel, setEditModel] = useState(INHERIT_MODEL)
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

  const openCreate = useCallback(() => {
    sheetEpoch.current += 1
    setError('')
    setConfirmDelete(false)
    setName(''); setKiroAgent('kirocrew'); setWorkspace('default'); setMemoryStore('default')
    setTriggers('')
    setSheet({ mode: 'create' })
  }, [])

  const openEdit = useCallback((a: KiroCrewAgent) => {
    sheetEpoch.current += 1
    setError('')
    setConfirmDelete(false)
    setKiroAgent(a.kiro_agent); setWorkspace(a.workspace); setMemoryStore(a.memory_store)
    setTriggers(a.triggers || '')
    setEditModel(a.model || INHERIT_MODEL)
    setSheet({ mode: 'edit', name: a.name })
  }, [defaultAgent])

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
    createMut.mutate({ name: n, kiro_agent: kiroAgent, workspace, memory_store: memoryStore, triggers, epoch: sheetEpoch.current })
  }

  const saveEdit = () => {
    if (!editing) return
    setError('')
    updateMut.mutate({
      name: editing,
      epoch: sheetEpoch.current,
      data: {
        kiro_agent: kiroAgent,
        workspace,
        memory_store: memoryStore,
        triggers,
        // INHERIT_MODEL is normalized to '' server-side; send it verbatim so
        // clearing a pin is a real write rather than a skipped field.
        model: editModel,
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
      // check alone renders it as "[object Object]".
      const msg = e instanceof Error ? e.message : (e as { message?: string })?.message
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
  const collidingCrews = editing
    ? agents
      .filter(a => a.name !== editing
        && (a.workspace === workspace || a.memory_store === memoryStore))
      .map(a => a.name)
    : []

  const creating = sheet?.mode === 'create'
  const sheetBusy = createMut.isPending || updateMut.isPending || deleteMut.isPending

  return (
    <>
      {!embedded && <PageHeader title={i18nT('pages.kiroCrewAgentsPage.agents')} subtitle={i18nT('pages.kiroCrewAgentsPage.manage_agent_workspace_memory_store_bindings')} />}
      <div className={`${embedded ? '' : 'px-6'} pb-8 overflow-y-auto flex-1 min-h-0`}>
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
            <ErrorNotice message={error} variant="inline" />
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

      <Dialog open={!!sheet} onOpenChange={next => { if (!next) closeSheet() }}>
        <DialogContent
          maxWidth={560}
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
            {!creating && <CrewAvatar seed={editing} size={28} />}
            <DialogTitle className="font-mono">
              {creating ? i18nT('pages.kiroCrewAgentsPage.create_agent') : editing}
            </DialogTitle>
            {!creating && editingAgent?.source && <SourceBadge source={editingAgent.source} />}
            {!creating && (
              <Btn className="ml-auto" onClick={() => chatWith(editing)}>
                <MessageSquare className="lucide-inline" aria-hidden="true" />
                {i18nT('pages.kiroCrewAgentsPage.chat_with_this_crew')}
              </Btn>
            )}
          </DialogHeader>

          <DialogBody>
            <div className="flex flex-col gap-6">
        {creating && (
          <section className="flex flex-col gap-3">
            <h3 className="text-[12px] font-semibold uppercase tracking-wider text-muted">{i18nT('pages.kiroCrewAgentsPage.identity')}</h3>
            <Field label={i18nT('pages.kiroCrewAgentsPage.name')}>
              <Input placeholder={i18nT('pages.kiroCrewAgentsPage.e_g_oncall')} value={name} onChange={e => setName(e.target.value)} autoFocus />
            </Field>
          </section>
        )}

        <section className="flex flex-col gap-3">
          <h3 className="text-[12px] font-semibold uppercase tracking-wider text-muted">{i18nT('pages.kiroCrewAgentsPage.routing')}</h3>
          <Field label={i18nT('pages.kiroCrewAgentsPage.triggers')} hint={i18nT('pages.kiroCrewAgentsPage.triggers_hint')} info={i18nT('pages.kiroCrewAgentsPage.triggers_info')}>
            <Input
              placeholder={i18nT('pages.kiroCrewAgentsPage.triggers_placeholder')}
              value={triggers}
              onChange={e => setTriggers(e.target.value)}
              aria-label={i18nT('pages.kiroCrewAgentsPage.triggers')}
            />
          </Field>
        </section>

        <section className="flex flex-col gap-3">
          <h3 className="text-[12px] font-semibold uppercase tracking-wider text-muted">{i18nT('pages.kiroCrewAgentsPage.runtime_binding')}</h3>
          <BindingFields
            templateLabel={provider.labels.agentTemplateField}
            kiroAgentOptions={kiroAgentOptions} kiroAgent={kiroAgent} setKiroAgent={setKiroAgent}
            workspaceOptions={workspaceOptions} workspace={workspace} setWorkspace={setWorkspace}
            onNewWorkspace={() => setWsModalOpen(true)}
            memoryStoreOptions={memoryStoreOptions} memoryStore={memoryStore} setMemoryStore={setMemoryStore}
            {...(creating ? {} : { modelOptions, model: editModel, setModel: setEditModel })}
          />
          {!creating && collidingCrews.length > 0 && (
            <div className="rounded-md border border-warn-subtle bg-warn-subtle px-3 py-2.5 text-[11.5px] leading-relaxed text-muted">
              {i18nT('pages.kiroCrewAgentsPage.also_used_by_these_crews', { crews: collidingCrews.join(', ') })}
            </div>
          )}
          {!creating && resolved && (
            <div className="rounded-md border border-border bg-bg-accent px-3 py-2.5 text-[11.5px] leading-relaxed text-muted">
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
          )}
        </section>

        {!creating && editing !== defaultAgent && (
          <section className="flex flex-col gap-3">
            <h3 className="text-[12px] font-semibold uppercase tracking-wider text-muted">{i18nT('pages.kiroCrewAgentsPage.danger_zone')}</h3>
            <div className="flex flex-col gap-3 rounded-md border border-danger-subtle bg-danger-subtle p-3">
              <p className="m-0 text-[12px] leading-relaxed text-muted">
                {confirmDelete
                  ? i18nT('pages.kiroCrewAgentsPage.delete_crew_named_confirm', { name: editing })
                  : i18nT('pages.kiroCrewAgentsPage.deleting_a_crew_unbinds_it_from_new_sessions_its')}
              </p>
              {/* Two-step rather than a one-click destructive button. The
                  previous table deleted immediately too, but a misclick in a
                  slide-in panel is far likelier, and a first-run reviewer
                  flagged it as the one action they would regret. A nested
                  confirm DIALOG was the other option; inline keeps this out of
                  a third stacked focus trap. */}
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
          </section>
        )}
            </div>
          </DialogBody>

          <DialogFooter>
            <ErrorNotice message={error} variant="inline" className="mr-auto" />
            <Btn onClick={closeSheet}>{i18nT('pages.kiroCrewAgentsPage.cancel')}</Btn>
            {creating ? (
              <SendBtn onClick={create} disabled={sheetBusy}>
                {createMut.isPending ? i18nT('pages.kiroCrewAgentsPage.creating') : i18nT('pages.kiroCrewAgentsPage.create')}
              </SendBtn>
            ) : (
              <SendBtn onClick={saveEdit} disabled={sheetBusy}>{i18nT('pages.kiroCrewAgentsPage.save_changes')}</SendBtn>
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
        </DialogContent>
      </Dialog>
    </>
  )
}
