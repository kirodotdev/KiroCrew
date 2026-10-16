import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/apiError'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useAppDispatch } from '../store'
import { createSlot } from '../store/chatSlice'
import { api, type WebhookTokenEntry } from '../api/client'
import { acpBackendName } from '../api/acpBackend'
import { useAvailableModels, useAvailableModelsQuery } from '../hooks/useAvailableModels'
import { Btn, SendBtn, PageHeader } from '../components/ui'
import {
  Dialog, DialogBody, DialogContent, DialogFooter,
} from '../components/ui/dialog'
import ErrorBoundary from '../components/ErrorBoundary'
import CrewAvatar, { ghostTraitsFrom, iconAvatarFrom, imageAvatarFrom, packAvatarFrom, unclaimedAvatarFrom, type CrewAvatarOverride } from '../components/CrewAvatar'
import CrewAvatarBuilder from '../components/CrewAvatarBuilder'
import {
  motionsFrom,
  retiredCarryFrom,
  retiredCueFrom,
  soundsFrom,
  type RetiredCarry,
} from '../lib/crewAvatarState'
import CrewAvatarButton from '../components/crew/CrewAvatarButton'
import CrewWakeSection from '../components/CrewWakeSection'
import CrewWebhookSection from '../components/CrewWebhookSection'
import CrewEditorRail from '../components/crew/CrewEditorRail'
import CrewOverviewPane from '../components/crew/CrewOverviewPane'
import AgentTemplateDetail from '../components/crew/AgentTemplateDetail'
import CrewCapabilitiesPane from '../components/crew/CrewCapabilitiesPane'
import { crewCapabilitiesApi, crewCapabilitiesKey } from '../api/crewCapabilities'
import { useCrewEditorSections, type CrewPaneKey } from '../components/crew/crewEditorSections'
import { wakesCrew, crewWakeQueryKey, crewWebhooksQueryKey, webhookBoundToCrew, webhookCanCallIn } from '../components/crew/wakesCrew'
import type { CronJob } from '../types'
import { crewDisplayName, type KiroCrewAgent } from '../components/AgentSelector'
import { errMessage } from '../utils/thunkError'
// The Crewmates page's simple create dialog — Name, Built from, "what it looks
// after", and an Advanced fold for the rest. Creating a crew from the
// capabilities roster opens THIS same dialog, not the full editor sheet: a
// create IS one `POST /api/agents`, so the two doors share one form (the
// dialog's own header note). This file exports the fields the dialog mounts,
// so the import is a call-time cycle (both reference the other's bindings only
// inside component bodies, never at module top level) — the same shape
// MembersPage already pulls both modules in through.
import NewCrewmateDialog, { type CreatedCrewmate } from './members/NewCrewmateDialog'
import { modelSupportsEffort } from '../lib/effort'
import { type TemplateProvenance } from '../lib/templateSource'
import { hideMateWithoutPreview } from '../lib/assistantMember'
import { usePreviewFlag } from '../hooks/usePreviewFlag'
import { PREVIEW_CREW } from '../utils/previewFlags'

import { i18nT } from '../i18n/t'
import { uiLocation } from '../uiLocations/uiLocation'
import { useGuidePredicate, useGuideSelection } from '../guide/guidePredicates'

import ErrorNotice from '../components/ErrorNotice'
import {
  type AgentMutationResult,
  INHERIT_MODEL,
  WorkspaceModal,
  memberMemoryState,
  DisplayNameField,
  displayNameFallback,
} from './KiroCrewAgentsPageFields'
import {
  type CrewView,
  VIEW_KEY,
  readStoredView,
  type SharedKind,
  CrewRosterToolbar,
  CrewRosterList,
} from './KiroCrewAgentsPageRoster'
import { type SheetTarget, useCrewDiscardGuard, CrewDiscardDialog } from './KiroCrewAgentsPageDiscard'
import { errorText, CrewEditorHeader, CrewModelPane, CrewPlacePane, CrewRoutingPane } from './KiroCrewAgentsPagePanes'

export {
  INHERIT_MODEL,
  OWN_WORKSPACE,
  WorkspaceModal,
  Field,
  WorkspaceField,
  memberMemoryState,
  MemoryStoreField,
  ModelField,
  EffortField,
  DisplayNameField,
  displayNameFallback,
  TriggersField,
  SessionColorField,
} from './KiroCrewAgentsPageFields'
export type { MemberMemoryState } from './KiroCrewAgentsPageFields'

/** Editable fields sent when updating an existing agent binding. */
interface AgentUpdatePayload {
  kiro_agent: string
  workspace: string
  memory_store: string
  /** Free-text routing intent for orchestrator crew selection. */
  triggers: string
  /** Presentation label shown in place of the name; '' clears it back to the
   *  name. Sent unconditionally so clearing is a real write. */
  display_name: string
  /** '' = inherit (the kiro template's pin, then the global fallback). */
  model: string
  /** '' = inherit the global default effort. Otherwise one of the levels the
   *  backend accepts (low..max); a level is only honoured on a model that
   *  supports effort at all. */
  reasoning_effort: string
  /** Default session color (#rrggbb hex) for new sessions. '' = no default. */
  session_color: string
  /** The face this save commits: a record pins a face; `null` is an explicit
   *  RESET to the name-derived face; `{}` is the record for a crew with no
   *  override, which the backend also stores as no override. */
  avatar: CrewAvatarOverride | Record<string, unknown> | null
}

export default function KiroCrewAgentsPage({ embedded }: { embedded?: boolean } = {}) {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { data: agentsData, error: agentsError } = useQuery({
    queryKey: ['kirocrew-agents'],
    queryFn: () => api.kirocrewAgents(),
  })
  // After a registry write, invalidate the PREFIX rather than refetching this
  // page's own query: the Crew Members roster is a projection of the same
  // registry and lives under the same prefix (see api/membersQuery.ts), so a
  // crew created, renamed or deleted here reaches it without this page
  // knowing who else reads the registry. This query is active, so the
  // invalidation refetches it exactly as `refetch()` did.
  //
  // `['kirocrewConfig']` goes with it because the SAME write lands in
  // config.json: creating a crew writes its `memory_stores` record alongside
  // its `agents` record, and the edit sheet's memory row reads that record
  // through this query (`memberMemoryState(editing, memoryStore,
  // kirocrewCfg?.memory_stores)`). Invalidating only the registry leaves the
  // pre-write config snapshot in cache, the new store key is absent from it,
  // and the row renders the freshly-created member as
  // `unavailable` — "configured memory store is unavailable, check
  // `kirocrew doctor`" — for a store the gateway reports as valid. The generic
  // server refresh broadcast (hooks/useWebSocket.ts) heals it eventually;
  // healing it here makes the row correct on the write that caused it.
  const refetchAgents = useCallback(
    () => {
      void queryClient.invalidateQueries({ queryKey: ['kirocrew-agents'] })
      void queryClient.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    },
    [queryClient],
  )
  // Memoised for the empty case: a bare `|| []` hands out a new array on every
  // render, which defeats every `useMemo` downstream that keys on the roster
  // (`sharedTargets`). React Query's structural sharing keeps `agentsData`
  // identical until the roster actually changes, so this is stable between
  // fetches that return the same rows.
  const crewPreview = usePreviewFlag(PREVIEW_CREW)
  const agents = useMemo<KiroCrewAgent[]>(() => hideMateWithoutPreview(agentsData?.agents || [], crewPreview), [agentsData, crewPreview])
  const defaultAgent = agentsData?.default_agent || ''

  const { data: installedAgents, error: installedError } = useQuery({
    queryKey: ['agents-installed'],
    queryFn: () => api.agentsInstalled(),
  })
  // Private fork copies (blueprint semantics: one crew's own definition) are
  // not offered as bindable templates — a copy named after crew A means
  // nothing in crew B's dropdown. The edit sheet re-adds the CURRENT binding
  // below when it is the editing crew's own copy, same pattern as a pinned
  // model missing from the advertised list.
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
  })
  const workspaceOptions = workspacesData?.workspaces?.map((w: { name: string }) => w.name) || ['default']

  const { data: kirocrewCfg, error: cfgError } = useQuery({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  // The three option lists above fall back to a built-in default when their
  // fetch fails, so without this the editor would offer `default` / `kirocrew`
  // as though those were the only choices. One notice, first failure wins.
  const editorOptionsError = installedError ?? workspacesError ?? cfgError

  // Model list for the template pane. Same query key as every other model
  // picker so the list is fetched once.
  const availableModels = useAvailableModels()

  const [filter, setFilter] = useState('')
  const [view, setView] = useState<CrewView>(readStoredView)
  const [error, setError] = useState('')
  // Client-side validation for the create form ("name is required"). Kept apart
  // from `error`, which holds the outcome of a request that FAILED: a blank name
  // never left the browser, so it must not render as a failed operation.
  const [sheetHint, setSheetHint] = useState('')
  const [sheet, setSheet] = useState<SheetTarget>(null)
  // The simple create dialog's open flag. Creating a crew from this page opens
  // `NewCrewmateDialog` (the Crewmates page's own create form) rather than the
  // full editor sheet: a create is one `POST /api/agents`, so the two doors
  // share one form. Config mode — after a create the user STAYS on this roster;
  // the dialog navigates nowhere and seeds no greeting (that is the Crewmates
  // page's behaviour, not this one's), it just closes and the new card appears.
  const [createOpen, setCreateOpen] = useState(false)
  // The EDIT path's template value: the "Built from" the editor sheet reads and
  // writes. Seeded from the crew being edited (`setKiroAgent(a.kiro_agent)` when
  // the editor opens); the initial '' is only the pre-edit idle state, never
  // submitted. This page no longer creates crews — the create doors open
  // `NewCrewmateDialog`, per accepted rfc-crewmates-launch.md — so the old
  // create-form guard against an unselected picker aliasing the default agent
  // (#1684) now lives in that dialog, where "Built from" offers the default
  // agent as an EXPLICIT, labelled choice rather than a silent fallback.
  const [kiroAgent, setKiroAgent] = useState('')
  const [workspace, setWorkspace] = useState('default')
  const [memoryStore, setMemoryStore] = useState('default')
  const [triggers, setTriggers] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [sessionColor, setSessionColor] = useState('')
  const [editModel, setEditModel] = useState(INHERIT_MODEL)
  const [editEffort, setEditEffort] = useState('')
  /** Draft avatar override. null = the name-derived face (no override). */
  const [editAvatar, setEditAvatar] = useState<CrewAvatarOverride | null>(null)
  /**
   * The stored `avatar` when no reader understood it — a tier a newer client
   * wrote, or a pack id this build cannot parse.
   *
   * Held so Save writes it back VERBATIM. The payload below is `draft ?? {}`, so
   * without this an unrecognised record is erased by the first unrelated edit —
   * and that is a property of the enumeration rather than of any one tier, so
   * teaching the editor a third reader would just leave the fourth to break the
   * same way. Cleared the moment the builder commits a draft, because that is
   * the user deciding this crew's avatar.
   */
  const [avatarPassthrough, setAvatarPassthrough] = useState<Record<string, unknown> | null>(null)
  /** The open crew's retired reaction keys, re-attached at save time so an edit
   *  that never touched the face cannot delete them. Sibling of
   *  `avatarPassthrough`: that one preserves a whole record no reader claimed,
   *  this one preserves the dead keys ON a record the readers DID claim. */
  const [retiredCarry, setRetiredCarry] = useState<RetiredCarry | null>(null)
  /** The builder committed an explicit RESET — the user pressed "Reset to default"
   *  and then Apply — as opposed to never having opened it. Both leave
   *  `editAvatar` null. `{}` is the record for a crew that carries no override;
   *  `null` is the reset spelling the backend documents for every tier, so a
   *  reset the user meant goes on the wire as exactly that. */
  const [avatarReset, setAvatarReset] = useState(false)
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
  const [capabilityDirty, setCapabilityDirty] = useState(false)
  const [capabilityBusy, setCapabilityBusy] = useState(false)
  const capabilityQuery = useQuery({
    queryKey: crewCapabilitiesKey(editing),
    queryFn: () => crewCapabilitiesApi.get(editing),
    enabled: !!editing,
    retry: false,
  })
  useEffect(() => { setCapabilityDirty(false); setCapabilityBusy(false) }, [sheet])
  const capabilityManaged = capabilityQuery.data?.mode === 'inherited'
  const capabilityReadFailed = capabilityQuery.isError && !(capabilityQuery.error instanceof ApiError && [404, 405, 501].includes(capabilityQuery.error.status))
  useEffect(() => {
    if (editing) void queryClient.invalidateQueries({ queryKey: crewCapabilitiesKey(editing) })
  }, [editing, kiroAgent, queryClient])

  /** The model a new session on this crew would actually run on, resolved by
   *  the backend so the precedence is not re-derived (and drifted) here. */
  const { data: resolved, error: resolvedError } = useQuery({
    queryKey: ['agent-resolved-model', editing],
    queryFn: () => api.agentResolvedModel(editing),
    enabled: !!editing,
  })
  // The pin's list is the harness's that judges it, when that is not the configured one.
  // INHERIT_MODEL leads so "no pin" is the obvious choice rather than an absent option.
  const pinModelsQuery = useAvailableModelsQuery({ backend: resolved?.models_backend })
  const modelOptions = [
    INHERIT_MODEL,
    ...pinModelsQuery.data.map((m: { name: string }) => m.name).filter((n: string) => n && n !== INHERIT_MODEL),
  ]
  const pinHarness = acpBackendName({ id: resolved?.models_backend ?? kirocrewCfg?.agent?.acp_backend ?? '' })

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
  // The pin backend's own answer, where its build decides rather than the model.
  const effortCapable = resolved?.effort_supported ?? modelSupportsEffort(effortModel)
  const effortLevels: string[] | undefined = resolved?.effort_levels?.length ? resolved.effort_levels : undefined

  /** Argument-free so a click event is never mistaken for one. Opens the
   *  simple create dialog (`NewCrewmateDialog`); it owns its own form state, so
   *  no editor field is reset here. */
  const openCreate = useCallback(() => {
    setCreateOpen(true)
  }, [])

  const openEdit = useCallback((a: KiroCrewAgent) => {
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
                ? // Loaded so Save writes the pack back verbatim. Without this the
                  // draft was null for a pack crew, and `avatarPayload`'s
                  // `editAvatar ?? {}` reset the record to "no override" — so
                  // changing only the model undressed the crew.
                  { kind: 'pack', id: storedPack.id }
                : Object.keys(reactions).length
                ? { kind: 'ghost', ...reactions }
                : null,
    )
    setAvatarPassthrough(storedUnclaimed)
    setRetiredCarry(retiredCarryFrom(a.avatar))
    setAvatarReset(false)
    setAvatarBuilderOpen(false)
    setSheet({ mode: 'edit', name: a.name })
  }, [])

  /** THE way the avatar builder opens. Every face and every "Edit avatar"
   *  button in the editor calls this one function (issue #9103), so no entry
   *  can drift to a different gate or a different destination. */
  const openAvatarBuilder = useCallback(() => setAvatarBuilderOpen(true), [])

  /**
   * Deep link: `?crew=<name>` opens that crew's editor; `&avatar=1` opens the
   * avatar builder on top of it. This is how the read-only Crew Members page
   * reaches the builder without becoming a second editor — it navigates here,
   * the single write path. Latched once the roster has loaded, then stripped
   * from the URL (same idiom as SkillsTab's `?review=`): reading the param on
   * every render would re-open the editor after the user closed it.
   */
  const [params, setParams] = useSearchParams()
  useEffect(() => {
    // A bare capabilities URL becomes the mobile root list. An open editor —
    // OR the open create dialog, which carries a typed draft — needs an
    // explicit pane route before a resize can remove its ancestry; without it
    // narrowing past the breakpoint unmounts this page and the dialog, losing
    // the draft with no confirm.
    if (!embedded || (!sheet && !createOpen) || params.get('tab') === 'crews') return
    setParams(current => {
      const next = new URLSearchParams(current)
      next.set('tab', 'crews')
      return next
    }, { replace: true })
  }, [embedded, sheet, createOpen, params, setParams])
  const linkedCrew = params.get('crew')
  const linkedAvatar = params.get('avatar') === '1'
  // `&pane=capabilities` lands on the Capabilities pane (a chat error row's
  // fix link). Held in a ref because opening the editor resets the pane.
  const linkedCapabilities = params.get('pane') === 'capabilities'
  const linkedPaneRef = useRef<CrewPaneKey | null>(null)
  useEffect(() => {
    if (!linkedCrew || !agentsData || capabilityDirty || capabilityBusy) return
    const target = agents.find(a => a.name === linkedCrew)
    if (target) {
      if (linkedCapabilities) linkedPaneRef.current = 'capabilities'
      openEdit(target)
      if (linkedAvatar) setAvatarBuilderOpen(true)
    }
    // An unknown name strips silently: the roster below is the honest answer.
    setParams(prev => {
      const next = new URLSearchParams(prev)
      next.delete('crew'); next.delete('avatar'); next.delete('pane')
      return next
    }, { replace: true })
  }, [linkedCrew, linkedAvatar, linkedCapabilities, agentsData, agents, openEdit, setParams, capabilityDirty, capabilityBusy])

  /**
   * Deep link: `?new=1` opens the editor in create mode straight away. Unlike
   * `?crew=` nothing here waits on the roster: the create form has no saved
   * record to load. Latched once, then stripped from the URL for the same
   * reason as `?crew=`: a reload or Back must not re-open a closed form. (The
   * Crewmates page once sent its "+" here with `&from=members`; it now creates
   * in place with `NewCrewmateDialog`, so no origin rides on the sheet.)
   */
  const linkedNew = params.get('new') === '1'
  useEffect(() => {
    if (!linkedNew || capabilityDirty || capabilityBusy) return
    openCreate()
    setParams(prev => {
      const next = new URLSearchParams(prev)
      next.delete('new')
      return next
    }, { replace: true })
  }, [linkedNew, openCreate, setParams, capabilityDirty, capabilityBusy])

  /** Reset the panel's state; the user stays on this page. */
  const dismissSheet = useCallback(() => { sheetEpoch.current += 1; setSheet(null); setError(''); setSheetHint(''); setConfirmDelete(false); setTemplateSwitchError('') }, [])
  const closeSheet = dismissSheet

  /**
   * The pending answer to a discard question that is on screen, as a promise
   * plus its resolver; `null` when nothing is being asked.
   *
   * `sheetEpoch` below cannot express a question that has not been answered
   * yet — it only moves once the sheet actually closes. A save still STAGING a
   * picture upload therefore waits on this: committing through a live question
   * would persist exactly the edits the question asks to throw away, and
   * Discard would then close the editor over a record the server had already
   * been told to keep. Backing out resolves it `false`, so holding the PUT
   * never kills the save the user asked for.
   */
  const discardAnswer = useRef<{ answered: Promise<boolean>; settle: (discarded: boolean) => void } | null>(null)

  /** Answer a pending discard question, if one is still open. First caller
   *  wins and clears the ref, so a confirm and the sheet-change reset arriving
   *  together cannot settle the same question twice. */
  const settleDiscardAnswer = useCallback((discarded: boolean) => {
    const pending = discardAnswer.current
    if (!pending) return
    discardAnswer.current = null
    pending.settle(discarded)
  }, [])

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

  const updateMut = useMutation({
    mutationFn: ({ name, data }: { name: string; data: AgentUpdatePayload; epoch: number }) => api.updateKirocrewAgent(name, data),
    onSuccess: (r: AgentMutationResult, vars) => { settleFor(vars.epoch, r.error); refetchAgents() },
    onError: (e: Error, vars) => settleFor(vars.epoch, e.message || i18nT('pages.kiroCrewAgentsPage.failed_to_update_agent')),
  })
  const deleteMut = useMutation({
    mutationFn: ({ name }: { name: string; epoch: number }) => api.deleteKirocrewAgent(name),
    onSuccess: (r: AgentMutationResult, vars) => { settleFor(vars.epoch, r.error); refetchAgents() },
    onError: (e: Error, vars) => settleFor(vars.epoch, e.message || i18nT('pages.kiroCrewAgentsPage.failed_to_delete_agent')),
  })

  /** The simple create dialog reported a new crew. Config mode: close the
   *  dialog and stay on this roster — no navigation, no greeting. The dialog
   *  has already invalidated the roster and config caches in its own success
   *  path, and `refetchAgents` re-reads the leaf this page renders from, so the
   *  new card appears where the user is. */
  const handleCapCreated = useCallback((_created: CreatedCrewmate) => {
    setCreateOpen(false)
    refetchAgents()
  }, [refetchAgents])

  /** Template switches from the definition pane persist IMMEDIATELY. The
   *  pane hides the sheet footer (its contract is saved-as-you-go, and fork /
   *  reset / publish already write server-side), so a dropdown switch left in
   *  local state would silently die when the sheet closes. A failed PUT keeps
   *  the switch as local state — `dirtyPanes` marks the pane and the close
   *  path falls back to the discard confirm — and renders the failure in the
   *  pane through ErrorNotice. The in-flight write is TRACKED so the close
   *  path can hold until it settles: a "discard" confirmed while the request
   *  is still in the air cannot un-send it, so answering the dialog before
   *  the write lands would let a discarded binding commit anyway. */
  const [templateSwitchError, setTemplateSwitchError] = useState('')
  const templateSwitchInflight = useRef<Promise<void> | null>(null)
  // The pane's shared instant-save chain (model pick, skill toggle), reported
  // via onSaveChain. Tracked HERE so requestClose can hold a close until it
  // settles: closing mid-PATCH unmounts the pane, its failure notice renders
  // nowhere, and the edit is silently lost (GPT round-53).
  const instantSaveInflight = useRef<Promise<unknown> | null>(null)
  const onPaneSaveChain = useCallback((p: Promise<unknown>) => {
    instantSaveInflight.current = p
    void p.catch(() => undefined).then(() => {
      if (instantSaveInflight.current === p) instantSaveInflight.current = null
    })
  }, [])
  const latestTemplateSwitch = useRef<string | null>(null)
  /** The binding the SERVER is known to hold — the roster's value, advanced by
   *  each successful commit. Read inside the serialized chain (not at call
   *  time), so a coalesced skip does not desynchronize the staleness check. */
  const serverTemplateBinding = useRef<string | null>(null)
  useEffect(() => {
    if (editingAgent) serverTemplateBinding.current = editingAgent.kiro_agent || ''
  }, [editingAgent])
  const persistTemplateSwitch = useCallback(
    (v: string) => {
      setKiroAgent(v)
      setTemplateSwitchError('')
      if (!editing) return
      // SERIALIZED and COALESCED: each write chains behind the previous one
      // (two concurrent PUTs could acquire the server lock in reverse and
      // persist the older choice), and a superseded selection is skipped so
      // only the LATEST commits. The payload is binding-only and carries the
      // expected prior binding, so the server's locked delta writer can 409 a
      // switch racing another surface's rebind instead of clobbering it.
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
          // deferred close then re-evaluates dirtiness against reality.
          await refetchAgents()
          setTemplateSwitchError('')
        } catch (e) {
          setTemplateSwitchError(errorText(e))
          // A conflict means another surface moved the binding. Resync BOTH the
          // tracked server binding and the LOCAL pick from the authoritative
          // roster: `setKiroAgent(v)` above already advanced the local state to
          // the failed pick, and a later pane Save sends that local value on
          // the generic path — left alone it would overwrite the other
          // surface's binding with the very value the server just refused.
          // Fetched directly (not via the query cache) so the value is in hand
          // before this chain link settles; the roster query is invalidated
          // too so the rest of the page catches up. Skipped when the user has
          // since picked again: that newer pick is now the one in flight.
          try {
            const fresh = (await api.kirocrewAgents()) as { agents?: KiroCrewAgent[] } | undefined
            const row = fresh?.agents?.find(a => a.name === editing)
            if (row && latestTemplateSwitch.current === v) {
              const actual = row.kiro_agent || ''
              serverTemplateBinding.current = actual
              setKiroAgent(actual)
            }
          } catch {
            // The roster read failed too; the error above already tells the
            // user the switch did not land, and the tracked binding still
            // carries the last known server value for the next attempt.
          }
          refetchAgents()
        }
      })
      templateSwitchInflight.current = commit
      void commit.finally(() => {
        if (templateSwitchInflight.current === commit) templateSwitchInflight.current = null
      })
    },
    [editing, refetchAgents],
  )

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
      // Sent unconditionally like `model`/`reasoning_effort`: '' is a real
      // value (clear the label back to the name), so a skipped field would
      // make clearing impossible. The server trims.
      display_name: displayName,
      // INHERIT_MODEL is normalized to '' server-side; send it verbatim so
      // clearing a pin is a real write rather than a skipped field.
      model: editModel,
      // Sent unconditionally for the same reason as `model`: '' is a real
      // value (clear the pin), so a skipped field would make clearing
      // impossible.
      reasoning_effort: editEffort,
      session_color: sessionColor,
    }
    // Three spellings: a draft the user built; `null` when they explicitly RESET
    // (see `avatarReset`); `avatarPassthrough` before `{}` so a record this build
    // did not understand is preserved rather than erased; and `{}` last, for a
    // crew that really has no override.
    let avatarPayload: CrewAvatarOverride | Record<string, unknown> | null = avatarReset
      ? null
      : (editAvatar ?? avatarPassthrough ?? {})
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
      // Rebuilt rather than spread from the draft, because the staging token and
      // `promote` are wire-only. Nothing else on a picture record needs carrying
      // over: a picture is static and silent, so it holds no reaction keys.
      avatarPayload = stagedToken
        ? { kind: 'image', promote: true, token: stagedToken }
        : { kind: 'image' }
    }
    // Put the retired keys back. AFTER the picture branch on purpose: that
    // branch REBUILDS the record from scratch for the staging token, so a carry
    // spliced in earlier would be the thing it dropped. Gated on the tier still
    // matching the one they were read from, and skipped entirely for an explicit
    // reset, which is the user asking for the record to be gone.
    if (retiredCarry && !avatarReset && avatarPayload && typeof avatarPayload === 'object') {
      const outgoing = avatarPayload as { kind?: unknown }
      if (outgoing.kind === retiredCarry.kind) {
        avatarPayload = { ...avatarPayload, ...retiredCarry.keys }
      }
    }
    // A discard question raised WHILE this save was staging owns the outcome,
    // so wait for the answer instead of racing it: a PUT fired mid-question
    // persists exactly the edits the question is about, and the epoch check
    // below cannot see an answer that has not arrived yet. Once answered there
    // is nothing left to wait for — Discard closed the sheet and moved the
    // epoch, so that check carries the same decision.
    const pendingDiscard = discardAnswer.current
    if (pendingDiscard && (await pendingDiscard.answered)) return
    if (epoch !== sheetEpoch.current) return
    updateMut.mutate({
      name,
      epoch,
      data: {
        ...data,
        // `null` = reset on purpose, `{}` = this save says nothing about the
        // face. On a pack crew those two differ, so the distinction has to
        // survive all the way onto the wire.
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
    !filter || (a.name + ' ' + (a.display_name ?? '') + ' ' + a.kiro_agent + ' ' + a.workspace + ' ' + a.memory_store).toLowerCase().includes(filter.toLowerCase())
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

  const [avatarUploading, setAvatarUploading] = useState(false)
  const sheetBusy =
    updateMut.isPending || deleteMut.isPending || avatarUploading || capabilityBusy

  /**
   * The subset of `sheetBusy` that has already COMMITTED something — a write
   * the server holds and that no answer in the editor can recall.
   *
   * `avatarUploading` is deliberately excluded. That leg of saveEdit only
   * STAGES a picture, and its own epoch check abandons the PUT when the sheet
   * closes underneath it, so while it stages nothing is committed and every
   * edit is still genuinely discardable. `requestClose` reads this rather than
   * `sheetBusy` for that reason: skipping the discard question during staging
   * would let a dismissal drop the whole save silently.
   */
  const committing = updateMut.isPending || deleteMut.isPending
  const {
    pane,
    schedDraft,
    setSchedDraft,
    schedSaving,
    setSchedSaving,
    discardAsk,
    setDiscardAsk,
    discardForce,
    dirtyPanes,
    requestPane,
    requestClose,
    requestChat,
    requestCancelDraft,
    confirmDiscard,
    discardTakesSheet,
    askSchedOnly,
    goToPane,
    panelId,
  } = useCrewDiscardGuard({
    sheet,
    editing,
    editingAgent,
    kiroAgent,
    workspace,
    memoryStore,
    editModel,
    editEffort,
    triggers,
    displayName,
    sessionColor,
    editAvatar,
    capabilityDirty,
    capabilityBusy,
    committing,
    linkedPaneRef,
    discardAnswer,
    templateSwitchInflight,
    instantSaveInflight,
    closeSheet,
    settleDiscardAnswer,
    chatWith,
  })

  /** The rail's schedule count reads the SAME cached query the wake pane uses, so
   *  opening the editor costs one request rather than two. */
  const wakeQuery = useQuery({
    queryKey: crewWakeQueryKey(editing),
    queryFn: () => api.crons(),
    enabled: !!editing,
  })
  const wakeJobs = useMemo<CronJob[]>(
    () => (wakeQuery.data?.jobs || []).filter(
      (j: CronJob) => wakesCrew(j, editing, editing === defaultAgent, editing)),
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

  // While the agent-template pane is the active surface, its edits are saved as
  // you go (a fork/patch lands immediately), so the sheet's Cancel + "Save
  // changes" footer would advertise a second, contradictory save model over the
  // pane's own "saved as you go" copy. Hide the footer there — the dialog's
  // built-in ✕ still closes it, and the pane surfaces its own errors.
  const templatePaneActive = pane === 'template'

  // Guide facts (whether, never which): a crewmate's editor is open (the
  // "choose the crewmate" step of a guide to an editor control), and, while
  // it is, which section shows and whether its memory is one to manage. With
  // the editor closed those read unknown rather than unmet.
  useGuideSelection('crewmate_editor_open', { selected: !!editing, available: !!agentsData && agents.length > 0, name: editingAgent ? crewDisplayName(editingAgent) : undefined })
  useGuidePredicate('crewmate_danger_zone_open', editing ? pane === 'danger' : null)
  useGuidePredicate('crewmate_place_pane_open', editing ? pane === 'place' : null)
  useGuidePredicate('crewmate_model_pane_open', editing ? pane === 'model' : null)
  const editingMemory = editing && kirocrewCfg ? memberMemoryState(editing, memoryStore, kirocrewCfg.memory_stores) : null
  useGuidePredicate(
    'crewmate_memory_manageable',
    editingMemory === null ? null : editingMemory === 'private' || (editing === 'default' && editingMemory === 'legacy'),
  )

  return (
    <>
      {!embedded && <PageHeader title={i18nT('pages.kiroCrewAgentsPage.agents')} subtitle={i18nT('pages.kiroCrewAgentsPage.manage_agent_workspace_memory_store_bindings')} />}
      <div className={`${embedded ? '' : 'px-4 md:px-6'} pb-8 overflow-y-auto flex-1 min-h-0`}>
        {/* A roster that failed to load must not read as "you have no crews":
            the empty state below would say exactly that. Hand-off only while the
            crew sheet AND the create dialog are closed — open, its unsaved pane
            edits (dirtyPanes) or the create dialog's typed draft (createOpen)
            would go with the navigation and be lost with no confirm. */}
        <ErrorNotice
          className="mb-3.5"
          title={i18nT('components.agentSelector.roster_load_failed')}
          message={agentsError ? errorText(agentsError) : null}
          askAgent={!sheet && !createOpen}
          testId="crews-roster-load-error"
        />
        {/* Same hand-off decision as the roster notice above (dirtyPanes / createOpen). */}
        <ErrorNotice
          className="mb-3.5"
          title={i18nT('pages.kiroCrewAgentsPage.editor_options_load_failed')}
          message={editorOptionsError ? errorText(editorOptionsError) : null}
          askAgent={!sheet && !createOpen}
          testId="crews-editor-options-load-error"
        />

        <CrewRosterToolbar agents={agents} filter={filter} setFilter={setFilter} view={view} pickView={pickView} openCreate={openCreate} />

        {/* The crewmate list, in either view: what a guide's "choose the
            crewmate" step points at (picking one opens its editor). */}
        <CrewRosterList agents={agents} filtered={filtered} view={view} defaultAgent={defaultAgent} sharedKind={sharedKind} openEdit={openEdit} openCreate={openCreate} />
      </div>

      <Dialog open={!!sheet} onOpenChange={next => { if (!next) requestClose() }}>
        <DialogContent
          /* The edit rail needs horizontal room. (Create is a separate dialog,
             `NewCrewmateDialog`, opened from this page's create doors.) */
          maxWidth={790}
          /* The visible title is just the crew name, which is not a usable
             accessible name on its own — it has to say what you are doing to it.
             An explicit aria-label outranks Radix's aria-labelledby, and the
             DialogTitle still has to EXIST or Radix warns. */
          aria-label={i18nT('pages.kiroCrewAgentsPage.edit_crew_named', { name: crewDisplayName({ name: editing, display_name: displayName, kiro_agent: editingAgent?.kiro_agent }) })}
          /* Radix closes on an outside pointerdown and on Escape. Dismissing
             mid-write is DELIBERATELY still allowed: the sheetEpoch/settleFor
             machinery below exists to make the abandoned write land harmlessly,
             and suppressing it would break that. */
        >
          <CrewEditorHeader editing={editing} editingAgent={editingAgent} displayName={displayName} editAvatar={editAvatar} sheetBusy={sheetBusy} openAvatarBuilder={openAvatarBuilder} requestChat={requestChat} setError={setError} />

          {/* Edit is a rail: an existing crew has surfaces (schedules,
              bindings, removal) that the create dialog does not. */}
          <DialogBody className="flex flex-col overflow-hidden p-0 sm:flex-row">
            {/* The fence that makes the Save-time snapshot honest: while a
                save is in flight (staged upload, the committing PUT, or
                delete), every control in the pane is disabled (fieldset
                covers form controls, pointer-events the custom widgets), so
                no edit can land mid-save only to be silently dropped when
                the post-save close unmounts the pane.
                display:contents keeps the flex layout unchanged. */}
            <fieldset
              disabled={sheetBusy}
              aria-busy={sheetBusy}
              className={`contents ${sheetBusy ? '[&>*]:pointer-events-none [&>*]:opacity-60' : ''}`}
            >
              <>
                <CrewEditorRail
                  sections={sections}
                  value={pane}
                  onChange={requestPane}
                  ariaLabel={i18nT('components.crewEditor.rail_label')}
                  unsavedLabel={i18nT('components.crewEditor.unsaved_changes')}
                  sharedLabel={i18nT('components.crewEditor.tag_shared')}
                  panelIdPrefix={panelId}
                  // The sections a guide opens on the way to a control inside
                  // them (Delete crewmate, the model, the memory).
                  segments={[
                    { key: 'model', label: i18nT('pages.kiroCrewAgentsPage.model'), ...uiLocation('agents.section-model') },
                    { key: 'place', label: i18nT('components.crewEditor.pane_workspace_memory'), ...uiLocation('agents.section-place') },
                    { key: 'danger', label: i18nT('pages.kiroCrewAgentsPage.danger_zone'), ...uiLocation('agents.section-danger') },
                  ]}
                />
                {/* `tabIndex={-1}` so moving focus here after a rail change is
                    possible without adding a Tab stop. The pane scrolls, not the
                    dialog body, which keeps the rail in view at any height. */}
                <div
                  id={`${panelId}-${pane}`}
                  role="tabpanel"
                  aria-labelledby={`${panelId}-tab-${pane}`}
                  tabIndex={-1}
                  className={pane === 'capabilities'
                    ? 'flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden'
                    : 'flex min-w-0 flex-1 flex-col gap-3.5 overflow-y-auto px-5 py-4'}
                >
                  {pane === 'overview' && (
                    <>
                    {/* Renaming is identity work, so it lives on the pane a
                        user opens to answer "who is this crew" — not under
                        routing (the UX lane's finding on this PR). The create
                        form keeps its copy beside Name for the same reason. */}
                    <DisplayNameField value={displayName} onChange={setDisplayName} fallback={displayNameFallback(editingAgent, editing)} />
                    <CrewOverviewPane
                      // The largest face in the editor opens the builder too, so
                      // the hub does not teach the opposite lesson from the
                      // header face (same title, same entry point).
                      hub={
                        <CrewAvatarButton size={34} onEdit={openAvatarBuilder} data-testid="hub-avatar-button">
                          <CrewAvatar seed={editing} avatar={editAvatar ?? undefined} size={34} />
                        </CrewAvatarButton>
                      }
                      templateLabel={i18nT('pages.kiroCrewAgentsPage.built_from')}
                      template={kiroAgent}
                      workspace={workspace}
                      memoryStore={memoryStore}
                      modelLabel={editModel === INHERIT_MODEL ? i18nT('pages.kiroCrewAgentsPage.inherited') : editModel}
                      modelInherited={editModel === INHERIT_MODEL}
                      resolvedModel={resolved?.model || ''}
                      // `editing` for BOTH identities here, which keeps this page's
                      // attribution exactly as it has always been. This page holds no
                      // slug for the crew it is editing (the list route does not carry
                      // `member_id`), so matching on the immutable id is its own change;
                      // the Crewmates panel, which does have one, passes it.
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
                    </>
                  )}

                  {editing && <CrewCapabilitiesPane
                    key={editing}
                    member={editing}
                    members={agents.map(agent => agent.name)}
                    hidden={pane !== 'capabilities'}
                    onDirtyChange={setCapabilityDirty}
                    onBusyChange={setCapabilityBusy}
                    onSaved={() => {
                      const saved = queryClient.getQueryData<{ agents: KiroCrewAgent[] }>(['kirocrew-agents'])?.agents.find(agent => agent.name === editing)
                      if (saved) setKiroAgent(saved.kiro_agent)
                    }}
                  />}
                  {pane === 'template' && (
                    /* The panel owns the selector: the template picker is
                       the header bar of the container holding the
                       definition it names (v5 design, usability-reviewed).
                       The crew's private copy is filtered from the shared
                       catalog; the panel re-adds the current binding when
                       needed. The pane is imported statically — no lazy
                       chunk, so no load-failure path — and the local
                       ErrorBoundary contains a render crash to this pane;
                       retryOnly keeps the fallback free of the /chat
                       hand-off, which would discard the sheet's unsaved
                       pane edits (dirtyPanes). */
                    <ErrorBoundary scope="agent-template-pane" retryOnly>
                      <>
                        {/* No askAgent hand-off: it navigates to /chat,
                            unmounting this sheet and destroying its
                            unsaved pane edits (dirtyPanes). Errors inside
                            the editor render in place, never as a
                            hand-off. */}
                        <ErrorNotice
                          message={templateSwitchError || (capabilityReadFailed ? i18nT('crewCapabilities.failed') : null)}
                          variant="inline"
                          testId="crew-template-switch-error"
                        />
                        <AgentTemplateDetail
                          actionsDisabled={capabilityDirty || capabilityBusy}
                          readOnly={capabilityManaged || capabilityDirty || capabilityBusy || capabilityQuery.isLoading || capabilityReadFailed}
                          onCapabilities={() => requestPane('capabilities')}
                          template={kiroAgent}
                          models={(availableModels || []).map((m: { name: string }) => m.name).filter(Boolean)}
                          crew={editing || undefined}
                          onForked={setKiroAgent}
                          options={kiroAgentOptions}
                          onSelect={persistTemplateSwitch}
                          onRebound={setKiroAgent}
                          provenance={templateProvenance}
                          fieldLabel={i18nT('pages.kiroCrewAgentsPage.built_from')}
                          onSaveChain={onPaneSaveChain}
                        />
                      </>
                    </ErrorBoundary>
                  )}

                  {pane === 'model' && (
                    <CrewModelPane modelOptions={modelOptions} pinModelsQuery={pinModelsQuery} pinHarness={pinHarness} servedAs={editModel === editingAgent?.model ? resolved?.pin_served_as : undefined} editModel={editModel} setEditModel={setEditModel} editEffort={editEffort} setEditEffort={setEditEffort} effortModel={effortModel} effortCapable={effortCapable} effortLevels={effortLevels} resolved={resolved} resolvedError={resolvedError} />
                  )}

                  {pane === 'place' && (
                    <CrewPlacePane editing={editing} workspaceOptions={workspaceOptions} workspace={workspace} setWorkspace={setWorkspace} setWsModalOpen={setWsModalOpen} memoryStore={memoryStore} kirocrewCfg={kirocrewCfg} sheetBusy={sheetBusy} dirtyPanes={dirtyPanes} schedDraft={schedDraft} navigate={navigate} collidingCrews={collidingCrews} />
                  )}

                  {pane === 'schedules' && (
                    <CrewWakeSection crew={editing} memberId={editing} agentTemplate={kiroAgent} isDefaultCrew={editing === defaultAgent} onDraftChange={setSchedDraft} onSavingChange={setSchedSaving} onRequestCancel={requestCancelDraft} />
                  )}

                  {pane === 'webhook' && <CrewWebhookSection crew={editing} />}

                  {pane === 'routing' && (
                    <CrewRoutingPane editing={editing} triggers={triggers} setTriggers={setTriggers} sessionColor={sessionColor} setSessionColor={setSessionColor} editAvatar={editAvatar} openAvatarBuilder={openAvatarBuilder} />
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
                            {/* Not "Cancel": the sheet footer's Cancel is on screen at
                                the same time, and two Cancels leave the reader asking
                                which one to press. This one says what pressing it keeps. */}
                            <Btn onClick={() => setConfirmDelete(false)} data-testid="cancel-delete-crew">{i18nT('pages.kiroCrewAgentsPage.keep_crew')}</Btn>
                            <Btn danger onClick={() => deleteMut.mutate({ name: editing, epoch: sheetEpoch.current })} disabled={sheetBusy} data-testid="confirm-delete-crew">
                              {i18nT('pages.kiroCrewAgentsPage.yes_delete_it')}
                            </Btn>
                          </>
                        ) : (
                          <Btn danger onClick={() => setConfirmDelete(true)} disabled={sheetBusy} {...uiLocation('agents.delete')}>
                            {i18nT('pages.kiroCrewAgentsPage.delete_crew')}
                          </Btn>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              </>
            </fieldset>
          </DialogBody>

          {/* Save is disabled while nothing is pending. The schedule pause/run
              controls in the wake pane apply IMMEDIATELY, so a live Save button
              beside them implies those toggles are drafts that Cancel would roll
              back — which it cannot.

              Hidden entirely while the agent-template pane is the active surface:
              that pane saves as you go, so a Cancel + "Save changes" footer there
              is a second, contradictory save model (see templatePaneActive). */}
          {!templatePaneActive && pane !== 'capabilities' && (
          <DialogFooter>
            {/* No hand-off: the crew sheet's unsaved pane edits (dirtyPanes) —
                a failed save is exactly what did not persist them. */}
            <ErrorNotice message={error} variant="inline" className="mr-auto" testId="crew-sheet-error" />
            {/* Client-side validation hint — the form never reached the server,
                so this is plain text, not an error surface. */}
            {sheetHint && <span className="mr-auto text-[12px] text-danger" data-testid="crew-sheet-hint">{sheetHint}</span>}
            {dirtyPanes.size > 0 && !error && !sheetHint && (
              <span className="mr-auto text-[11.5px] text-muted" data-testid="crew-unsaved-note">
                {/* While the open schedule draft is what disables Save, the note
                    names that reason in visible text — the `title` on the button
                    is hover-only, which keyboard and touch users never see. */}
                {capabilityDirty
                  ? i18nT('crewCapabilities.finishDraftFirst')
                  : schedDraft
                    ? i18nT('pages.kiroCrewAgentsPage.finish_the_new_schedule_first')
                    : i18nT('components.crewEditor.unsaved_changes')}
              </span>
            )}
            <Btn onClick={requestClose}>{i18nT('pages.kiroCrewAgentsPage.cancel')}</Btn>
            <SendBtn
              onClick={saveEdit}
              disabled={sheetBusy || dirtyPanes.size === 0 || schedDraft || capabilityDirty || capabilityBusy}
              title={capabilityDirty ? i18nT('crewCapabilities.finishDraftFirst') : schedDraft ? i18nT('pages.kiroCrewAgentsPage.finish_the_new_schedule_first') : undefined}
            >{i18nT('pages.kiroCrewAgentsPage.save_changes')}</SendBtn>
          </DialogFooter>
          )}

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

          {/* The editor's discard confirm. Same nesting rule and same
              always-mounted rule as WorkspaceModal above — conditional
              rendering skips Radix's layer deregistration and leaves the
              editor believing it is no longer the top layer. Nested INSIDE the
              editor's DialogContent is also what keeps Escape addressed to this
              layer alone, so the editor never treats the confirm's own
              dismissal as a dismissal of itself. The confirm button restates
              the action; the dismiss restates the alternative, because a bare
              "Cancel" beside the editor's own footer Cancel would be ambiguous.
              The `crew-sched-*` test ids predate the generalization and stay
              stable: one dialog now asks about a typed schedule, about unsaved
              pane edits, or about both. */}
          <CrewDiscardDialog discardAsk={discardAsk} setDiscardAsk={setDiscardAsk} askSchedOnly={askSchedOnly} schedDraft={schedDraft} discardTakesSheet={discardTakesSheet} schedSaving={schedSaving} discardForce={discardForce} editing={editing} confirmDiscard={confirmDiscard} />
          {/* Same stacked-layer contract as WorkspaceModal: mounted inside the
              editor's DialogContent, `open`-driven. Save only lands in the
              editor's draft state — the crew record is written by Save changes. */}
          {editing && (
            <CrewAvatarBuilder
              open={avatarBuilderOpen}
              name={editing}
              value={editAvatar}
              // Read off the SAVED record, which is the only place a served
              // tier's retired cue still exists: the draft cannot carry one,
              // because the readers that build it are ghost-gated.
              retiredCue={retiredCueFrom(editingAgent?.avatar)}
              // Also the STORED record: an emptied reaction map has to be NAMED
              // on the wire when the record carries one, and the draft forgets
              // that after the first Apply lands the empty map.
              savedReactions={{
                motions: motionsFrom(editingAgent?.avatar) !== null,
                sounds: soundsFrom(editingAgent?.avatar) !== null,
              }}
              onCancel={() => setAvatarBuilderOpen(false)}
              onSave={next => {
                setEditAvatar(next)
                // Any Apply is the user deciding the avatar, so a record they
                // never saw must not ride along behind their choice.
                setAvatarPassthrough(null)
                // A null draft out of the builder is the Reset link having been
                // pressed — an intent, not an absence. Recorded so Save sends
                // the reset spelling, `null`.
                setAvatarReset(next === null)
                setAvatarBuilderOpen(false)
              }}
            />
          )}
        </DialogContent>
      </Dialog>
      {/* The create door. Config mode: `handleCapCreated` closes it and
          refetches the roster — the new crew appears here, and nothing
          navigates away. `existingNames` is the roster the dialog refuses a
          duplicate against before any request. */}
      <NewCrewmateDialog
        open={createOpen}
        startExpanded
        onClose={() => setCreateOpen(false)}
        onCreated={handleCapCreated}
        existingNames={agents.flatMap(a => (a.display_name ? [a.name, a.display_name] : [a.name]))}
      />
    </>
  )
}
