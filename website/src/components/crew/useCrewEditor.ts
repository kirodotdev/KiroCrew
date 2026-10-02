/**
 * useCrewEditor — the whole EDIT-mode brain of the crew editor, extracted from
 * `KiroCrewAgentsPage` so more than one surface can open it.
 *
 * The crew editor was born inside the Crews page and reached from elsewhere by
 * navigating to `/capabilities?tab=crews&crew=<name>`. CREW-18688 asks the
 * Crewmates page to edit a bot WITHOUT leaving the page — a modal on the bot
 * page. The invariant-dense logic that makes editing correct (the stale-write
 * epoch guard, the serialized template-switch chain, avatar staging, the
 * discard question) must not be re-implemented per surface, so it lives here
 * once and `CrewEditorDialog` renders it. Today the ONLY consumer is
 * `MembersPage` (the in-place Crewmates modal); folding `KiroCrewAgentsPage`'s
 * own inline edit sheet onto this hook/dialog is the committed follow-up, after
 * which both surfaces share this one state machine.
 *
 * This hook owns EDIT only. Creating a crew stays in `KiroCrewAgentsPage`'s own
 * form: the two share field COMPONENTS, not this state machine, and a create
 * wizard is a separate decision (see the page's create branch).
 *
 * Every reference-carrying comment below (issue numbers, the epoch rationale,
 * the avatar tier spellings) is moved verbatim from the page — this is a
 * relocation, not a rewrite, so the pinned behaviour is preserved exactly.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { useAppDispatch } from '../../store'
import { createSlot } from '../../store/chatSlice'
import { api, type WebhookTokenEntry } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { useAvailableModelsQuery } from '../../hooks/useAvailableModels'
import {
  ghostTraitsFrom,
  iconAvatarFrom,
  imageAvatarFrom,
  packAvatarFrom,
  unclaimedAvatarFrom,
  type CrewAvatarOverride,
} from '../CrewAvatar'
import {
  motionsFrom,
  retiredCarryFrom,
  soundsFrom,
  type RetiredCarry,
} from '../../lib/crewAvatarState'
import { crewCapabilitiesApi, crewCapabilitiesKey } from '../../api/crewCapabilities'
import { useCrewEditorSections, type CrewPaneKey } from './crewEditorSections'
import {
  wakesCrew,
  crewWakeQueryKey,
  crewWebhooksQueryKey,
  webhookBoundToCrew,
  webhookCanCallIn,
} from './wakesCrew'
import { ApiError } from '../../api/apiError'
import type { CronJob } from '../../types'
import { type KiroCrewAgent } from '../AgentSelector'
import { errMessage } from '../../utils/thunkError'
import { modelSupportsEffort } from '../../lib/effort'
import { type TemplateProvenance } from '../../lib/templateSource'

/** The stored spelling for "no per-agent pin, inherit the next tier down". The
 *  select shows this as a real option; the backend normalizes it back to ''.
 *  Same value as KiroCrewAgentsPage's export; defined here so the hook (and the
 *  dialog that consumes it) never import back into the page — that would close
 *  a cycle (page -> CrewEditorDialog -> useCrewEditor -> page). */
export const INHERIT_MODEL = 'auto'

/** How long a schedule-draft discard confirm stays fully locked while the
 *  create request is in flight. The lock exists because discarding cannot
 *  cancel the POST; the unlock exists because a HUNG request (the client sets
 *  no timeout) must not seal every exit from the modal editor. */
const DISCARD_FORCE_GRACE_MS = 8000

/** Common shape returned by the agent/workspace mutation endpoints. */
interface AgentMutationResult {
  error?: string
  name?: string
  memory_store?: string
}

/** Editable fields sent when updating an existing agent binding. */
interface AgentUpdatePayload {
  kiro_agent: string
  workspace: string
  memory_store: string
  triggers: string
  display_name: string
  model: string
  reasoning_effort: string
  session_color: string
  avatar: CrewAvatarOverride | Record<string, unknown> | null
}

function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.message
  return e instanceof Error ? e.message : String(e)
}

export interface UseCrewEditorArgs {
  /** The crew being edited, or '' when the editor is closed. Driving the hook
   *  by NAME (not a boolean + record) lets both surfaces open it with just the
   *  name they already hold; the record is looked up from `agents`. */
  editingName: string
  /** The crew's IMMUTABLE id (its slug) — what a private schedule's `member_id`
   *  holds, so the Schedules pane's `wakesCrew` match works for any crewmate
   *  whose display name is not its own slug ("Radar" → `radar`). Optional:
   *  defaults to `editingName`, which is correct for the crew manager, where the
   *  edited handle IS the agent name. MembersPage passes the roster row's
   *  canonical `slug` so a renamed crewmate's schedules still resolve. */
  memberId?: string
  /** The current roster — the source of the edited record and of the
   *  shared-storage collision reads. Both callers already hold it. */
  agents: KiroCrewAgent[]
  /** Whether `agents` (the crew roster the editor reads) has actually arrived.
   *  The page gates its roster read on the editor being opened, so between the
   *  pill click and the roster settling there is a window where `editingAgent`
   *  is not yet resolvable; the hook exposes that window as `loading` so the
   *  dialog can show a spinner instead of the click being a silent dead one. */
  agentsLoaded: boolean
  /** The name of the default crew — delete is refused for it, and its wake
   *  jobs resolve slightly differently (`wakesCrew`'s default arm). */
  defaultAgent: string
  /** Invalidate the roster + config queries after a write. Owned by the caller
   *  because the SAME prefix invalidation heals the Crewmates roster (see the
   *  page's refetchAgents comment); both callers pass the identical function. */
  refetchAgents: () => void
  /** Close the editor. The hook calls this when a write settles or the discard
   *  question resolves to "close". */
  onClose: () => void
  /** A delete that landed, so the caller can retire anything keyed on the crew
   *  (MembersPage closes the thread). Optional. */
  onDeleted?: (name: string) => void
  /** Route the editor's own route-leaving navigations (Chat with this crew,
   *  Manage memory) through the caller's draft guard. MembersPage passes its
   *  `useGuardedLeave()` so a typed Schedules draft in the panel behind the
   *  modal is not silently discarded when one of those buttons leaves `/members`.
   *  Optional — defaults to running the navigation directly (the crew manager
   *  has no such sibling draft to protect). */
  leave?: (perform: () => void | Promise<void>, to?: string) => void
}

/**
 * Everything `CrewEditorDialog` needs. Grouped loosely by the section of the
 * dialog that reads it; kept flat because a dialog this size reads most of it.
 */
export interface CrewEditorController {
  /** Whether the dialog should be open at all (an editable record was found). */
  open: boolean
  /** The editor is requested but its roster read has not resolved the record
   *  yet — the dialog renders a loading state so the pill click is not silent. */
  loading: boolean
  editing: string
  /** The crew's canonical slug (see the arg of the same name) — what the
   *  Schedules pane passes to `wakesCrew` as `member_id`. */
  memberId: string
  editingAgent: KiroCrewAgent | undefined

  // Field state (edit's OWN copies, seeded from the record on open).
  kiroAgent: string
  setKiroAgent: (v: string) => void
  workspace: string
  setWorkspace: (v: string) => void
  memoryStore: string
  triggers: string
  setTriggers: (v: string) => void
  displayName: string
  setDisplayName: (v: string) => void
  sessionColor: string
  setSessionColor: (v: string) => void
  editModel: string
  setEditModel: (v: string) => void
  editEffort: string
  setEditEffort: (v: string) => void
  editAvatar: CrewAvatarOverride | null

  // Option lists + provider labels the fields render from.
  kiroAgentOptions: string[]
  workspaceOptions: string[]
  modelOptions: string[]
  availableModels: { name: string }[] | undefined
  templateProvenance: Record<string, TemplateProvenance>
  templateFieldLabel: string
  kirocrewCfg: { memory_stores?: Record<string, { memory_version?: number; owner_member?: string }> } | undefined
  editorOptionsError: unknown
  /** The model list resolved, but DEGRADED (the adapter returned an auto-only /
   *  cached list after a transport failure rather than rejecting). Surfaced as a
   *  warn-toned status beside the Model field, NOT as an options-load error. */
  modelsDegraded: boolean

  // Derived model/effort readout.
  resolved: { model?: string; pinned?: boolean; reasoning_effort?: string; effort_pinned?: boolean } | undefined
  resolvedError: unknown
  effortCapable: boolean
  effortModel: string

  // Sharing collisions.
  collidingCrews: string[]
  sharingWorkspace: string[]
  sharingMemoryStore: string[]

  // Panes + rail.
  pane: CrewPaneKey
  requestPane: (key: CrewPaneKey) => void
  goToPane: (key: CrewPaneKey) => void
  panelId: string
  sections: ReturnType<typeof useCrewEditorSections>
  routingWords: number
  templatePaneActive: boolean

  // Wake / webhook summaries the panes and rail read.
  wakeJobs: CronJob[]
  wakeUnknown: boolean
  boundWebhooks: number
  webhooksUnknown: boolean

  // Capabilities pane wiring.
  capabilityManaged: boolean
  capabilityReadFailed: boolean
  capabilityLoading: boolean
  setCapabilityDirty: (v: boolean) => void
  setCapabilityBusy: (v: boolean) => void
  /** Set by the template pane when it holds unsaved typed input in one of its
   *  own nested dialogs (the publish-copy name); folded into dirtyPanes so the
   *  host's navigation stake covers it without enumerating the field. */
  setTemplateDirty: (v: boolean) => void
  onCapabilitiesSaved: () => void

  // Template pane wiring.
  templateSwitchError: string
  persistTemplateSwitch: (v: string) => void
  onPaneSaveChain: (p: Promise<unknown>) => void

  // Avatar builder.
  avatarBuilderOpen: boolean
  openAvatarBuilder: () => void
  closeAvatarBuilder: () => void
  applyAvatar: (next: CrewAvatarOverride | null) => void
  onAvatarImageError: () => void

  // Busy + dirty.
  sheetBusy: boolean
  dirtyPanes: Set<CrewPaneKey>
  schedDraft: boolean
  setSchedDraft: (v: boolean) => void
  setSchedSaving: (v: boolean) => void
  requestCancelDraft: (proceed: () => void) => void
  capabilityDirty: boolean
  capabilityBusy: boolean
  /** The template pane holds unsaved typed input in a nested dialog (publish
   *  name). Exposed for the host's page-level navigation stake only — it does
   *  NOT gate the editor's own Save, since publishing a copy is a separate
   *  action from the pane edits Save commits. */
  templateDirty: boolean

  // Danger zone.
  confirmDelete: boolean
  setConfirmDelete: (v: boolean) => void
  deleteCrew: () => void
  confirmRef: React.RefObject<HTMLDivElement>
  /** Whether the edited crew is the default (delete refused; wake resolution). */
  isDefaultCrew: boolean
  /** Navigate to this crew's private-memory management page. */
  navigateManageMemory: () => void
  /** Navigate to an arbitrary destination through the editor's leave guard, for
   *  the embedded sub-sections that would otherwise discard the modal's unsaved
   *  panes with a raw navigate. */
  guardedNavigate: (to: string) => void
  /** Every crew's name — the capabilities pane's `members` list. */
  memberNames: string[]

  // Footer + close.
  error: string
  sheetHint: string
  save: () => void
  requestClose: () => void
  requestChat: () => void

  // Discard confirm dialog.
  discardAsk: CrewPaneKey | 'close' | 'chat' | 'collapse' | null
  setDiscardAsk: (v: CrewPaneKey | 'close' | 'chat' | 'collapse' | null) => void
  discardTakesSheet: boolean
  askSchedOnly: boolean
  schedSaving: boolean
  discardForce: boolean
  confirmDiscard: () => void

  // Workspace-create modal.
  wsModalOpen: boolean
  openWsModal: () => void
  closeWsModal: () => void
  /** Whether the nested create-workspace form holds unsaved input, so the host
   *  can fold `wsModalOpen && wsDirty` into its navigation stake. */
  wsDirty: boolean
  setWsDirty: (dirty: boolean) => void
  onWorkspaceCreated: (name: string) => void
}

export function useCrewEditor(args: UseCrewEditorArgs): CrewEditorController {
  const {
    editingName,
    memberId: memberIdArg,
    agents,
    agentsLoaded,
    defaultAgent,
    refetchAgents,
    onClose,
    onDeleted,
    leave = (perform) => { void perform() },
  } = args

  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const editing = editingName
  // Canonical slug for `member_id` matching. Defaults to the editing NAME, which
  // is correct for the crew manager (the handle it edits is the agent name);
  // MembersPage overrides it with the roster row's slug so a renamed crewmate's
  // private schedules still resolve in the Schedules pane.
  const memberId = memberIdArg || editingName
  const editingAgent = agents.find(a => a.name === editing)
  const open = !!editing && !!editingAgent
  // The pill click sets `editing` before the page's gated roster read has
  // resolved the record, so there is a window where the editor is requested
  // but `open` is not yet true. Surface it so the dialog can render a loading
  // state rather than being a silent dead click (consumes agentsLoaded).
  const loading = !!editing && !agentsLoaded && !editingAgent

  // ── Option lists (shared React Query keys, so no duplicate fetches) ──
  // Gated on `open`: the hook is mounted unconditionally by MembersPage for the
  // whole Crewmates page, but these reads must not fire until the editor is
  // actually opened — merely rendering the page must not fetch the installed
  // templates, workspaces, config, or (below) spawn `kiro-cli --list-models`
  // for an editor the user never opened. Same gate NewCrewmateDialog applies.
  const { data: installedAgents, error: installedError } = useQuery({
    queryKey: ['agents-installed'],
    queryFn: () => api.agentsInstalled(),
    enabled: open,
  })
  const kiroAgentOptions = Array.isArray(installedAgents)
    ? installedAgents
      .filter((x: { name: string; private_to?: string }) => Boolean(x.name) && !x.private_to)
      .map((x: { name: string }) => x.name)
    : ['kirocrew']
  const templateProvenance: Record<string, TemplateProvenance> = Array.isArray(installedAgents)
    ? Object.fromEntries(
      installedAgents
        .filter((x: { name: string }) => Boolean(x.name))
        .map((x: TemplateProvenance & { name: string }) => [x.name, x]),
    )
    : {}

  const { data: workspacesData, refetch: refetchWorkspaces, error: workspacesError } = useQuery({
    queryKey: ['workspaces'],
    queryFn: () => api.workspaces(),
    enabled: open,
  })
  const workspaceOptions = workspacesData?.workspaces?.map((w: { name: string }) => w.name) || ['default']

  const { data: kirocrewCfg, error: cfgError } = useQuery({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    enabled: open,
  })

  // The model list surfaces a transport failure as `isDegraded` (the adapter
  // resolves with an auto-only/cached list rather than rejecting). A genuine
  // query ERROR (the fetch rejected) belongs in the shared options-load notice;
  // a merely DEGRADED resolve does NOT — folding it in showed the generic
  // "couldn't load the editor's workspace/template/memory options" over the raw
  // untranslated body `model-list-degraded` on every open until a live fetch
  // landed, for options that never failed. Degraded is surfaced beside the Model
  // field instead (see `modelsDegraded` below), not as an options-load error.
  const modelsQuery = useAvailableModelsQuery({ enabled: open })
  const availableModels = modelsQuery.data
  const modelsDegraded = !modelsQuery.error && modelsQuery.isDegraded
  const editorOptionsError = installedError ?? workspacesError ?? cfgError ?? modelsQuery.error

  const modelOptions = [
    INHERIT_MODEL,
    ...(availableModels || []).map((m: { name: string }) => m.name).filter((n: string) => n && n !== INHERIT_MODEL),
  ]

  // ── Edit field state (own copies, seeded from the record on open) ──
  const [error, setError] = useState('')
  const [sheetHint, setSheetHint] = useState('')
  const [kiroAgent, setKiroAgent] = useState('')
  const [workspace, setWorkspace] = useState('default')
  const [memoryStore, setMemoryStore] = useState('default')
  const [triggers, setTriggers] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [sessionColor, setSessionColor] = useState('')
  const [editModel, setEditModel] = useState(INHERIT_MODEL)
  const [editEffort, setEditEffort] = useState('')
  const [editAvatar, setEditAvatar] = useState<CrewAvatarOverride | null>(null)
  const [avatarPassthrough, setAvatarPassthrough] = useState<Record<string, unknown> | null>(null)
  const [retiredCarry, setRetiredCarry] = useState<RetiredCarry | null>(null)
  const [avatarReset, setAvatarReset] = useState(false)
  const [avatarBuilderOpen, setAvatarBuilderOpen] = useState(false)
  const [avatarUploading, setAvatarUploading] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  /** The armed confirm row, scrolled into view when it appears: the danger zone
   *  is the last section, so on a short window the confirm buttons land under
   *  the sticky footer and the user cannot see what they are being asked. */
  const confirmRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (confirmDelete) confirmRef.current?.scrollIntoView({ block: 'nearest' })
  }, [confirmDelete])
  const [wsModalOpen, setWsModalOpen] = useState(false)
  // Whether the nested create-workspace modal holds unsaved typed input. The host
  // folds `wsModalOpen && wsDirty` into its own navigation stake exactly as
  // NewCrewmateDialog does, so a route change with this sub-form dirty asks first
  // instead of discarding the typed name/directory/copy-from silently.
  const [wsDirty, setWsDirty] = useState(false)
  // Generation of the current workspace-modal SESSION, bumped on every open and
  // every CANCEL/close. A create POST fired in one session but still in flight
  // when the user cancels (X / Escape / backdrop) must NOT select its workspace
  // on completion — that would bind the editor to a workspace the user backed
  // out of, persisted on the next Save. (The editor epoch does not catch this:
  // cancelling the sub-modal leaves the editor open, epoch unchanged.)
  const wsGen = useRef(0)
  // The gen captured when the modal OPENED — the session a create belongs to.
  // The success callback must compare against THIS, not against wsGen.current at
  // success time: a cancel during the POST bumps wsGen BEFORE success fires, so
  // capturing at success would read the post-cancel value and wrongly pass. A
  // cancel makes wsGen.current advance past wsOpenGen, so the stale apply is
  // dropped.
  const wsOpenGen = useRef(0)
  const [templateSwitchError, setTemplateSwitchError] = useState('')

  /**
   * Identity of the CURRENT panel opening, bumped on every open and every
   * close. An async completion must only act on the panel it was fired from;
   * comparing the crew name is not enough (dismiss + reopen the SAME crew is a
   * different panel holding different unsaved edits). A per-opening counter can.
   */
  const sheetEpoch = useRef(0)

  /** Seed all edit state from the record every time the editor points at a new
   *  crew. This is `openEdit` from the page, run as an effect keyed on the
   *  editing name (the page called it imperatively; here the name drives it). */
  const seededFor = useRef<string | null>(null)
  useEffect(() => {
    if (!open || !editingAgent) { seededFor.current = null; return }
    if (seededFor.current === editing) return
    seededFor.current = editing
    const a = editingAgent
    sheetEpoch.current += 1
    setError(''); setSheetHint('')
    setConfirmDelete(false)
    setKiroAgent(a.kiro_agent); setWorkspace(a.workspace); setMemoryStore(a.memory_store)
    setTriggers(a.triggers || '')
    setDisplayName(a.display_name || '')
    setSessionColor(a.session_color || '')
    setEditModel(a.model || INHERIT_MODEL)
    setEditEffort(a.reasoning_effort || '')
    // Normalized through the same coercion the renderer applies, so the dirty
    // check compares like with like (a junk stored value reads as "no
    // override" everywhere).
    const storedTraits = ghostTraitsFrom(a.avatar)
    const storedImage = imageAvatarFrom(a.avatar)
    const storedPack = packAvatarFrom(a.avatar)
    const storedIcon = iconAvatarFrom(a.avatar)
    // The reaction layer is the ghost's, and rides on a ghost that pins nothing:
    // a record with no traits that carries motions or sounds is still an
    // override ("the name-derived face, plus these reactions"). Both readers are
    // ghost-gated, so a picture or a pack record yields no reactions here.
    const storedMotions = motionsFrom(a.avatar)
    const storedSounds = soundsFrom(a.avatar)
    const reactions = {
      ...(storedMotions ? { motions: storedMotions } : {}),
      ...(storedSounds ? { sounds: storedSounds } : {}),
    }
    // A record NO reader claimed is owned WHOLE by the passthrough, reactions
    // included, so it gets no draft at all. Synthesizing `{kind:'ghost',
    // …reactions}` for it looks harmless and is not: `avatarPayload` prefers a
    // draft over the passthrough, so the lifted reaction layer would go back as
    // a GHOST record and the tier it was lifted out of would be dropped — by
    // exactly the unrelated save the passthrough exists to survive. The builder's
    // Apply is the only thing allowed to overrule such a record.
    const storedUnclaimed = unclaimedAvatarFrom(a.avatar)
    setEditAvatar(
      storedUnclaimed
        ? null
        : storedTraits
          ? { kind: 'ghost', traits: storedTraits, ...reactions }
          : storedIcon
            ? { kind: 'icon', pose: storedIcon.pose, bg: storedIcon.bg }
            : storedImage
              ? { kind: 'image', v: storedImage.v }
              : storedPack
                ? { kind: 'pack', id: storedPack.id }
                : Object.keys(reactions).length
                  ? { kind: 'ghost', ...reactions }
                  : null,
    )
    setAvatarPassthrough(storedUnclaimed)
    setRetiredCarry(retiredCarryFrom(a.avatar))
    setAvatarReset(false)
    setAvatarBuilderOpen(false)
  }, [open, editing, editingAgent])

  const openAvatarBuilder = useCallback(() => setAvatarBuilderOpen(true), [])
  const closeAvatarBuilder = useCallback(() => setAvatarBuilderOpen(false), [])

  // ── Capabilities pane ──
  const [capabilityDirty, setCapabilityDirty] = useState(false)
  const [capabilityBusy, setCapabilityBusy] = useState(false)
  const [templateDirty, setTemplateDirty] = useState(false)
  const capabilityQuery = useQuery({
    queryKey: crewCapabilitiesKey(editing),
    queryFn: () => crewCapabilitiesApi.get(editing),
    enabled: !!editing,
    retry: false,
  })
  useEffect(() => { setCapabilityDirty(false); setCapabilityBusy(false) }, [editing])
  useEffect(() => { setTemplateDirty(false) }, [editing])
  const capabilityManaged = capabilityQuery.data?.mode === 'inherited'
  const capabilityReadFailed = capabilityQuery.isError && !(capabilityQuery.error instanceof ApiError && [404, 405, 501].includes(capabilityQuery.error.status))
  const capabilityLoading = capabilityQuery.isLoading
  useEffect(() => {
    if (editing) void queryClient.invalidateQueries({ queryKey: crewCapabilitiesKey(editing) })
  }, [editing, kiroAgent, queryClient])
  const onCapabilitiesSaved = useCallback(() => {
    const saved = queryClient.getQueryData<{ agents: KiroCrewAgent[] }>(['kirocrew-agents'])?.agents.find(agent => agent.name === editing)
    if (saved) setKiroAgent(saved.kiro_agent)
  }, [queryClient, editing])

  // ── Resolved model + effort ──
  const { data: resolved, error: resolvedError } = useQuery({
    queryKey: ['agent-resolved-model', editing],
    queryFn: () => api.agentResolvedModel(editing),
    enabled: !!editing,
  })
  const modelPinPendingClear = editModel === INHERIT_MODEL && !!editingAgent?.model
  const effortModel = editModel !== INHERIT_MODEL
    ? editModel
    : modelPinPendingClear ? '' : (resolved?.model || '')
  const effortCapable = modelSupportsEffort(effortModel)

  // ── Mutations ──
  const settleFor = useCallback((epoch: number, err?: string) => {
    if (epoch !== sheetEpoch.current) return
    if (err) { setError(err); return }
    closeSheetRef.current()
  }, [])

  const updateMut = useMutation({
    mutationFn: ({ name, data }: { name: string; data: AgentUpdatePayload; epoch: number }) => api.updateKirocrewAgent(name, data),
    onSuccess: (r: AgentMutationResult, vars) => {
      settleFor(vars.epoch, r.error)
      refetchAgents()
    },
    onError: (e: Error, vars) => settleFor(vars.epoch, e.message || i18nT('pages.kiroCrewAgentsPage.failed_to_update_agent')),
  })
  const deleteMut = useMutation({
    mutationFn: ({ name }: { name: string; epoch: number }) => api.deleteKirocrewAgent(name),
    onSuccess: (r: AgentMutationResult, vars) => {
      if (!r.error && vars.epoch === sheetEpoch.current) onDeleted?.(vars.name)
      settleFor(vars.epoch, r.error)
      refetchAgents()
    },
    onError: (e: Error, vars) => settleFor(vars.epoch, e.message || i18nT('pages.kiroCrewAgentsPage.failed_to_delete_agent')),
  })

  // ── Template switch: serialized + coalesced + 409-aware ──
  const templateSwitchInflight = useRef<Promise<void> | null>(null)
  const instantSaveInflight = useRef<Promise<unknown> | null>(null)
  // Reactive mirror of the ref-tracked in-flight writes above (template-switch
  // PUT, instant-save chain). The refs serialize/coalesce the requests but a ref
  // is not reactive, so the page's navigation guard (crewModalBusy via sheetBusy)
  // cannot see them — a template selection during a slow PUT could be discarded
  // by a confirmed route-leave while the PUT still persisted the binding. This
  // counter makes those writes visible to sheetBusy. Incremented when a write is
  // registered, decremented when it settles.
  const [pendingWrites, setPendingWrites] = useState(0)
  const trackWrite = useCallback((p: Promise<unknown>) => {
    setPendingWrites(n => n + 1)
    void p.catch(() => undefined).then(() => setPendingWrites(n => Math.max(0, n - 1)))
  }, [])
  const onPaneSaveChain = useCallback((p: Promise<unknown>) => {
    instantSaveInflight.current = p
    trackWrite(p)
    void p.catch(() => undefined).then(() => {
      if (instantSaveInflight.current === p) instantSaveInflight.current = null
    })
  }, [trackWrite])
  const latestTemplateSwitch = useRef<string | null>(null)
  const serverTemplateBinding = useRef<string | null>(null)
  useEffect(() => {
    if (editingAgent) serverTemplateBinding.current = editingAgent.kiro_agent || ''
  }, [editingAgent])
  const persistTemplateSwitch = useCallback(
    (v: string) => {
      setKiroAgent(v)
      setTemplateSwitchError('')
      if (!editing) return
      latestTemplateSwitch.current = v
      const prev = templateSwitchInflight.current ?? Promise.resolve()
      const commit = prev.then(async () => {
        if (latestTemplateSwitch.current !== v) return
        try {
          const expected = serverTemplateBinding.current
          await api.updateKirocrewAgent(editing, {
            kiro_agent: v,
            ...(expected ? { expected_kiro_agent: expected } : {}),
          })
          serverTemplateBinding.current = v
          // Awaited so the settled promise implies fresh server state: the
          // deferred close then re-evaluates dirtiness against reality. The
          // caller's refetchAgents invalidates; we await the roster query's
          // refetch so this chain link does not settle before the cache does.
          await queryClient.refetchQueries({ queryKey: ['kirocrew-agents'] })
          setTemplateSwitchError('')
        } catch (e) {
          setTemplateSwitchError(errorText(e))
          try {
            const fresh = (await api.kirocrewAgents()) as { agents?: KiroCrewAgent[] } | undefined
            const row = fresh?.agents?.find(a => a.name === editing)
            if (row && latestTemplateSwitch.current === v) {
              const actual = row.kiro_agent || ''
              serverTemplateBinding.current = actual
              setKiroAgent(actual)
            }
          } catch {
            /* roster read failed too; the error above already told the user */
          }
          refetchAgents()
        }
      })
      templateSwitchInflight.current = commit
      trackWrite(commit)
      void commit.finally(() => {
        if (templateSwitchInflight.current === commit) templateSwitchInflight.current = null
      })
    },
    [editing, refetchAgents, queryClient, trackWrite],
  )

  // ── Save ──
  const save = useCallback(() => { void saveEditRef.current() }, [])
  const saveEditRef = useRef<() => Promise<void>>(async () => {})
  useEffect(() => {
    saveEditRef.current = async () => {
      if (!editing) return
      setError('')
      const epoch = sheetEpoch.current
      const name = editing
      const data = {
        kiro_agent: kiroAgent,
        workspace,
        memory_store: memoryStore,
        triggers,
        display_name: displayName,
        model: editModel,
        reasoning_effort: editEffort,
        session_color: sessionColor,
      }
      let avatarPayload: CrewAvatarOverride | Record<string, unknown> | null = avatarReset
        ? null
        : (editAvatar ?? avatarPassthrough ?? {})
      if (editAvatar?.kind === 'image') {
        let stagedToken: string | null = null
        if (editAvatar.pendingData) {
          setAvatarUploading(true)
          try {
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
            // eslint-disable-next-line no-console -- the only record of WHY a staged upload failed once the banner shows the localized string
            console.warn('avatar upload failed', e)
            settleFor(epoch, i18nT('pages.kiroCrewAgentsPage.failed_to_update_agent'))
            return
          } finally {
            setAvatarUploading(false)
          }
        }
        avatarPayload = stagedToken
          ? { kind: 'image', promote: true, token: stagedToken }
          : { kind: 'image' }
      }
      if (retiredCarry && !avatarReset && avatarPayload && typeof avatarPayload === 'object') {
        const outgoing = avatarPayload as { kind?: unknown }
        if (outgoing.kind === retiredCarry.kind) {
          avatarPayload = { ...avatarPayload, ...retiredCarry.keys }
        }
      }
      const pendingDiscard = discardAnswer.current
      if (pendingDiscard && (await pendingDiscard.answered)) return
      if (epoch !== sheetEpoch.current) return
      updateMut.mutate({
        name,
        epoch,
        data: { ...data, avatar: avatarPayload },
      })
    }
  })

  const deleteCrew = useCallback(() => {
    deleteMut.mutate({ name: editing, epoch: sheetEpoch.current })
  }, [deleteMut, editing])

  // ── Chat with this crew ──
  const chatWith = useCallback((crew: string) => {
    setError('')
    // Everything — slot creation, closing the editor, navigating — happens
    // INSIDE the guard's perform callback. `useGuardedLeave` skips `perform`
    // on a veto ("stay"), so a `createSlot` run before `leave` would mint a
    // durable slot the user never reaches when they choose to stay, orphaning
    // it; and closing before the veto would unmount the editor while they
    // stayed, leaving neither chat nor editor. The whole side-effecting path
    // runs only once the leave is confirmed.
    leave(async () => {
      const epoch = sheetEpoch.current
      try {
        await dispatch(createSlot(crew)).unwrap()
      } catch (e) {
        const msg = errMessage(e)
        settleFor(epoch, msg || i18nT('pages.kiroCrewAgentsPage.failed_to_update_agent'))
        return
      }
      if (epoch !== sheetEpoch.current) return
      closeSheetRef.current()
      navigate('/chat')
    }, '/chat')
  }, [dispatch, navigate, settleFor, leave])
  const chatWithRef = useRef(chatWith)
  chatWithRef.current = chatWith

  // ── Sharing collisions (read off the IN-FLIGHT selection, not persisted) ──
  const sharingWorkspace = editing
    ? agents.filter(a => a.name !== editing && a.workspace === workspace).map(a => a.name)
    : []
  const sharingMemoryStore = editing
    ? agents.filter(a => a.name !== editing && a.memory_store === memoryStore).map(a => a.name)
    : []
  const collidingCrews = [...new Set([...sharingWorkspace, ...sharingMemoryStore])]

  // ── Busy + committing ──
  const sheetBusy =
    updateMut.isPending || deleteMut.isPending || avatarUploading || capabilityBusy || pendingWrites > 0
  const committing = updateMut.isPending || deleteMut.isPending

  // ── Panes ──
  const [pane, setPane] = useState<CrewPaneKey>('overview')
  const [schedDraft, setSchedDraft] = useState(false)
  const [schedSaving, setSchedSaving] = useState(false)
  const [discardAsk, setDiscardAsk] = useState<CrewPaneKey | 'close' | 'chat' | 'collapse' | null>(null)
  const [discardForce, setDiscardForce] = useState(false)
  useEffect(() => {
    if (discardAsk === null || !schedSaving) { setDiscardForce(false); return }
    const t = setTimeout(() => setDiscardForce(true), DISCARD_FORCE_GRACE_MS)
    return () => clearTimeout(t)
  }, [discardAsk, schedSaving])
  const collapseProceed = useRef<(() => void) | null>(null)
  // Reset the pane whenever the editor points at a new crew (or closes).
  const paneSeededFor = useRef<string | null>(null)
  useEffect(() => {
    if (open) {
      if (paneSeededFor.current !== editing) {
        paneSeededFor.current = editing
        setPane('overview')
      }
    } else {
      paneSeededFor.current = null
      setPane('overview')
    }
    setSchedDraft(false); setSchedSaving(false); setDiscardAsk(null)
  }, [open, editing])

  // ── Discard answer promise (a save staging an upload waits on this) ──
  const discardAnswer = useRef<{ answered: Promise<boolean>; settle: (discarded: boolean) => void } | null>(null)
  const settleDiscardAnswer = useCallback((discarded: boolean) => {
    const pending = discardAnswer.current
    if (!pending) return
    discardAnswer.current = null
    pending.settle(discarded)
  }, [])

  // ── Close ──
  const closeSheet = useCallback(() => {
    sheetEpoch.current += 1
    setError(''); setSheetHint(''); setConfirmDelete(false); setTemplateSwitchError('')
    onClose()
  }, [onClose])
  const closeSheetRef = useRef(closeSheet)
  closeSheetRef.current = closeSheet

  // ── Dirty panes ──
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
    if (displayName.trim() !== (editingAgent.display_name || '').trim()) out.add('routing')
    if (sessionColor !== (editingAgent.session_color || '')) out.add('routing')
    const savedNorm =
      ghostTraitsFrom(editingAgent.avatar) ??
      iconAvatarFrom(editingAgent.avatar) ??
      imageAvatarFrom(editingAgent.avatar) ??
      packAvatarFrom(editingAgent.avatar)
    const draftNorm =
      editAvatar?.kind === 'ghost'
        ? (editAvatar.traits ?? null)
        : editAvatar?.kind === 'icon'
          ? { pose: editAvatar.pose, bg: editAvatar.bg }
          : editAvatar?.kind === 'image'
            ? { v: editAvatar.v, pendingData: editAvatar.pendingData }
            : editAvatar?.kind === 'pack'
              ? { id: editAvatar.id }
              : null
    if (JSON.stringify(draftNorm) !== JSON.stringify(savedNorm)) out.add('routing')
    const unclaimed = unclaimedAvatarFrom(editingAgent.avatar) !== null
    const savedReactions = unclaimed
      ? [null, null]
      : [motionsFrom(editingAgent.avatar), soundsFrom(editingAgent.avatar)]
    const draftReactions = [motionsFrom(editAvatar), soundsFrom(editAvatar)]
    if (JSON.stringify(draftReactions) !== JSON.stringify(savedReactions)) out.add('routing')
    if (schedDraft) out.add('schedules')
    if (capabilityDirty) out.add('capabilities')
    return out
  }, [editingAgent, kiroAgent, workspace, memoryStore, editModel, editEffort, triggers, displayName, sessionColor, schedDraft, editAvatar, capabilityDirty])

  const requestPane = useCallback((key: CrewPaneKey) => {
    if (schedDraft && key !== pane) { setDiscardAsk(key); return }
    setPane(key)
  }, [schedDraft, pane])

  const requestClose = useCallback(() => {
    if (capabilityBusy) return
    if (schedDraft) { setDiscardAsk('close'); return }
    if (templateSwitchInflight.current) {
      void templateSwitchInflight.current.then(() => requestCloseRef.current())
      return
    }
    if (instantSaveInflight.current) {
      void instantSaveInflight.current.catch(() => undefined).then(() => requestCloseRef.current())
      return
    }
    // A sheet save is in flight: do NOT close here. settleFor (the mutation's
    // own onSuccess/onError) owns the outcome — success closes the sheet, a
    // rejection keeps it open and renders the error so the draft is not lost.
    // Closing now would bump the epoch and make that settlement epoch-stale,
    // silently dropping a rejected edit (GPT: closing during a pending save
    // loses rejected edits). So while committing, the Cancel/Escape is a no-op
    // until the save settles — matching the two in-flight branches above.
    if (committing) return
    if (dirtyPanes.size === 0) { closeSheet(); return }
    let settle: (discarded: boolean) => void = () => {}
    const answered = new Promise<boolean>(resolve => { settle = resolve })
    discardAnswer.current = { answered, settle }
    setDiscardAsk('close')
  }, [schedDraft, committing, dirtyPanes, closeSheet, capabilityBusy])
  const requestCloseRef = useRef<() => void>(() => {})
  useEffect(() => { requestCloseRef.current = requestClose }, [requestClose])

  const requestChat = useCallback(() => {
    if (capabilityBusy) return
    // Any unsaved pane, not just a schedule/capability draft: chatWith closes
    // the editor unconditionally, so an ordinary identity/model/place edit would
    // be lost with no prompt otherwise.
    if (dirtyPanes.size > 0) { setDiscardAsk('chat'); return }
    void chatWithRef.current(editing)
  }, [dirtyPanes, editing, capabilityBusy])

  const requestCancelDraft = useCallback((proceed: () => void) => {
    collapseProceed.current = proceed
    setDiscardAsk('collapse')
  }, [])

  const confirmDiscard = useCallback(() => {
    const target = discardAsk
    setDiscardAsk(null)
    settleDiscardAnswer(true)
    if (target === 'close') closeSheet()
    else if (target === 'chat') void chatWithRef.current(editing)
    else if (target === 'collapse') { collapseProceed.current?.(); collapseProceed.current = null }
    else if (target) setPane(target)
  }, [discardAsk, closeSheet, editing, settleDiscardAnswer])

  useEffect(() => {
    if (discardAsk === null) settleDiscardAnswer(false)
  }, [discardAsk, settleDiscardAnswer])

  const discardTakesSheet =
    (discardAsk === 'close' || discardAsk === 'chat')
    && [...dirtyPanes].some(k => k !== 'schedules')
  const askSchedOnly = schedDraft && !discardTakesSheet

  // ── In-pane navigation (focus hand-off) ──
  const paneFocusPending = useRef(false)
  const goToPane = useCallback((key: CrewPaneKey) => {
    setPane(prev => {
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

  // ── Wake + webhook summaries ──
  const wakeQuery = useQuery({
    queryKey: crewWakeQueryKey(editing),
    queryFn: () => api.crons(),
    enabled: !!editing,
  })
  const wakeJobs = useMemo<CronJob[]>(
    () => (wakeQuery.data?.jobs || []).filter(
      (j: CronJob) => wakesCrew(j, editing, editing === defaultAgent, memberId)),
    [wakeQuery.data, editing, defaultAgent, memberId],
  )
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

  const routingWords = triggers.split(',').map(s => s.trim()).filter(Boolean).length

  const sections = useCrewEditorSections({
    templateLabel: i18nT('pages.kiroCrewAgentsPage.built_from'),
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

  const templatePaneActive = pane === 'template'

  const isDefaultCrew = editing === defaultAgent
  const memberNames = useMemo(() => agents.map(a => a.name), [agents])
  const navigateManageMemory = useCallback(() => {
    const to = `/settings/overview?view=memory&store=${encodeURIComponent(editing === 'default' ? 'default' : memoryStore)}`
    leave(() => navigate(to), to)
  }, [navigate, editing, memoryStore, leave])
  // A guarded navigate for the embedded sub-sections (CrewWakeSection's
  // "/schedule", CrewWebhookSection's "/webhooks"): their own raw `navigate`
  // leaves the host editor's OTHER dirty panes (template, model, routing)
  // unprotected, so a jump from inside the modal would discard them silently.
  // Routed through the same `leave` guard the editor uses everywhere else, the
  // jump now asks first when the modal holds unsaved edits. A standalone mount
  // (the crew manager) passes no handler and keeps its direct navigate.
  const guardedNavigate = useCallback((to: string) => {
    leave(() => navigate(to), to)
  }, [navigate, leave])

  // ── Avatar builder apply ──
  const applyAvatar = useCallback((next: CrewAvatarOverride | null) => {
    setEditAvatar(next)
    setAvatarPassthrough(null)
    setAvatarReset(next === null)
    setAvatarBuilderOpen(false)
  }, [])
  const onAvatarImageError = useCallback(() => {
    setError(i18nT(packAvatarFrom(editAvatar) ? 'components.avatarBuilder.pack_load_failed' : 'components.avatarBuilder.image_load_failed'))
  }, [editAvatar])

  // ── Workspace-create modal ──
  const onWorkspaceCreated = useCallback((newName: string) => {
    // Validate against the OPEN-session token, not wsGen.current read now: a
    // cancel during the POST already bumped wsGen before this success fired, so
    // reading it here would capture the post-cancel value and wrongly pass. The
    // create belongs to the session that was open when the modal opened
    // (wsOpenGen); if a cancel/close advanced wsGen past it, drop the apply. The
    // editor epoch guards the orthogonal editor dismiss+reopen case; both hold.
    setWsModalOpen(false)
    setWsDirty(false)
    const openGen = wsOpenGen.current
    const epoch = sheetEpoch.current
    refetchWorkspaces().then(() => {
      if (epoch === sheetEpoch.current && openGen === wsGen.current) setWorkspace(newName)
    })
  }, [refetchWorkspaces])

  return {
    open,
    loading,
    editing,
    memberId,
    editingAgent,
    kiroAgent, setKiroAgent,
    workspace, setWorkspace,
    memoryStore,
    triggers, setTriggers,
    displayName, setDisplayName,
    sessionColor, setSessionColor,
    editModel, setEditModel,
    editEffort, setEditEffort,
    editAvatar,
    kiroAgentOptions,
    workspaceOptions,
    modelOptions,
    availableModels,
    templateProvenance,
    templateFieldLabel: i18nT('pages.kiroCrewAgentsPage.built_from'),
    kirocrewCfg,
    editorOptionsError,
    modelsDegraded,
    resolved,
    resolvedError,
    effortCapable,
    effortModel,
    collidingCrews,
    sharingWorkspace,
    sharingMemoryStore,
    pane,
    requestPane,
    goToPane,
    panelId,
    sections,
    routingWords,
    templatePaneActive,
    wakeJobs,
    wakeUnknown: wakeQuery.isError,
    boundWebhooks,
    webhooksUnknown: webhooksQuery.isError,
    capabilityManaged,
    capabilityReadFailed,
    capabilityLoading,
    setCapabilityDirty,    setCapabilityBusy,
    setTemplateDirty,
    onCapabilitiesSaved,
    templateSwitchError,
    persistTemplateSwitch,
    onPaneSaveChain,
    avatarBuilderOpen,
    openAvatarBuilder,
    closeAvatarBuilder,
    applyAvatar,
    onAvatarImageError,
    sheetBusy,
    dirtyPanes,
    schedDraft, setSchedDraft,
    setSchedSaving,
    requestCancelDraft,
    capabilityDirty,
    capabilityBusy,
    templateDirty,
    confirmDelete, setConfirmDelete,
    deleteCrew,
    confirmRef,
    isDefaultCrew,
    navigateManageMemory,
    guardedNavigate,
    memberNames,
    error,
    sheetHint,
    save,
    requestClose,
    requestChat,
    discardAsk, setDiscardAsk,
    discardTakesSheet,
    askSchedOnly,
    schedSaving,
    discardForce,
    confirmDiscard,
    wsModalOpen,
    openWsModal: useCallback(() => { wsGen.current += 1; wsOpenGen.current = wsGen.current; setWsModalOpen(true) }, []),
    closeWsModal: useCallback(() => { wsGen.current += 1; setWsModalOpen(false); setWsDirty(false) }, []),
    wsDirty,
    setWsDirty,
    onWorkspaceCreated,
  }
}
