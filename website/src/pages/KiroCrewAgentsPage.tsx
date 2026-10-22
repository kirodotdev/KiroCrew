import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../api/apiError'
import { api } from '../api/client'
import { PageHeader } from '../components/ui'
import { crewDisplayName, type KiroCrewAgent } from '../components/AgentSelector'
import { useCrewEditor } from '../components/crew/useCrewEditor'
import type { CrewEditorSectionKey } from '../components/crew/CrewEditorModal'
import CrewEditorChunkLoading from '../components/crew/CrewEditorChunkLoading'
import { useGuardedLeave, usePublishNavigationStake, useRegisterNavigationLeaveGuard } from '../components/NavigationLeaveGuard'
// The Crewmates page's simple create dialog — Name, Built from, "what it looks
// after", and an Advanced fold for the rest. Creating a crew from the
// capabilities roster opens THIS same dialog: a create IS one
// `POST /api/agents`, so the two doors share one form (the dialog's own header
// note). This file exports the fields the dialog mounts, so the import is a
// call-time cycle (both reference the other's bindings only inside component
// bodies, never at module top level) — the same shape MembersPage already pulls
// both modules in through.
import NewCrewmateDialog, { type CreatedCrewmate } from './members/NewCrewmateDialog'
import { hideMateWithoutPreview } from '../lib/assistantMember'
import { usePreviewFlag } from '../hooks/usePreviewFlag'
import { PREVIEW_CREW } from '../utils/previewFlags'

import { i18nT } from '../i18n/t'
import { useGuidePredicate, useGuideSelection } from '../guide/guidePredicates'

import ErrorNotice from '../components/ErrorNotice'
import {
  type CrewView,
  VIEW_KEY,
  readStoredView,
  type SharedKind,
  CrewRosterToolbar,
  CrewRosterList,
} from './KiroCrewAgentsPageRoster'

export {
  INHERIT_MODEL,
  OWN_WORKSPACE,
  WorkspaceModal,
  Field,
  WorkspaceField,
  memberMemoryState,
  ModelField,
  EffortField,
  DisplayNameField,
  displayNameFallback,
  TriggersField,
  SessionColorField,
} from './KiroCrewAgentsPageFields'
export type { MemberMemoryState } from './KiroCrewAgentsPageFields'

// The crewmate editor — the same one the Crewmates page opens in place. Lazy so
// its code stays off the main App chunk until a crewmate is edited; it also
// imports the shared fields from this page, so a static import here would be a
// module cycle.
const CrewEditorModal = lazy(() => import('../components/crew/CrewEditorModal'))

function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.message
  return e instanceof Error ? e.message : String(e)
}

export default function KiroCrewAgentsPage({ embedded }: { embedded?: boolean } = {}) {
  const queryClient = useQueryClient()
  const { data: agentsData, error: agentsError } = useQuery({
    queryKey: ['kirocrew-agents'],
    queryFn: () => api.kirocrewAgents(),
  })
  // `config.json`'s `memory_stores` says whose each store is, so the roster can
  // show a crewmate's own store as private rather than as a bare store id.
  const { data: kirocrewCfg } = useQuery({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
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
  // its `agents` record, and the editor's memory section reads that record
  // through this query. Invalidating only the registry leaves the pre-write
  // config snapshot in cache, the new store key is absent from it, and the
  // freshly-created member reads as having no usable memory store.
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

  const [filter, setFilter] = useState('')
  const [view, setView] = useState<CrewView>(readStoredView)
  // The simple create dialog's open flag. Config mode — after a create the user
  // STAYS on this roster; the dialog navigates nowhere and seeds no greeting
  // (that is the Crewmates page's behaviour, not this one's), it just closes
  // and the new card appears.
  const [createOpen, setCreateOpen] = useState(false)
  // The crewmate being edited ('' = editor closed). The editor itself is the
  // shared `useCrewEditor` controller rendered by `CrewEditorModal`.
  const [editingCrew, setEditingCrew] = useState('')
  // The section a deep link asked the editor to open on; cleared on close so
  // an ordinary Edit lands on General.
  const [landingSection, setLandingSection] = useState<CrewEditorSectionKey | undefined>(undefined)
  // The section the open editor shows, reported back for the guide facts below.
  const [shownSection, setShownSection] = useState<CrewEditorSectionKey | null>(null)

  /** Remember the layout across visits. Wrapped because `localStorage` can throw
   *  outright (blocked/partitioned storage) — losing the preference is fine,
   *  taking the roster down with it is not. */
  const pickView = useCallback((v: CrewView) => {
    setView(v)
    try { localStorage.setItem(VIEW_KEY, v) } catch { /* preference is best-effort */ }
  }, [])

  // The editor saves the WHOLE agent snapshot, so it must OPEN from a roster
  // read that is current — never React Query's RETAINED copy, which after an
  // invalidation keeps the pre-edit snapshot while the refetch is in flight. A
  // full-snapshot save from that copy would stomp newer config it predates.
  // `staleTime: 0` makes opening the editor always refetch; the latch below
  // then admits the roster only once THAT fetch has settled for the crewmate
  // being edited, and keeps showing live data afterwards so the editor's own
  // post-save refetch does not tear the open dialog down. Same rule as the
  // Crewmates page's editor (useMembersPageCrewEditor).
  const editorRosterQuery = useQuery({
    queryKey: ['kirocrew-agents'],
    queryFn: () => api.kirocrewAgents(),
    enabled: !!editingCrew,
    staleTime: 0,
  })
  const rosterAdmittedRef = useRef('')
  const editorRosterSettledFresh = editorRosterQuery.isSuccess && !editorRosterQuery.isFetching
  if (!editingCrew) {
    rosterAdmittedRef.current = ''
  } else if (editorRosterSettledFresh && rosterAdmittedRef.current !== editingCrew) {
    rosterAdmittedRef.current = editingCrew
  }
  const rosterAdmitted = !!editingCrew && rosterAdmittedRef.current === editingCrew
  const editorAgents = useMemo<KiroCrewAgent[]>(
    () => (rosterAdmitted ? hideMateWithoutPreview(editorRosterQuery.data?.agents || [], crewPreview) : []),
    [rosterAdmitted, editorRosterQuery.data, crewPreview],
  )
  const closeEditor = useCallback(() => {
    setEditingCrew('')
    setLandingSection(undefined)
  }, [])
  // The editor's own links (Webhooks "open", Memory "open page") navigate in
  // the app; without this gate they skip the leave guard registered below and
  // unmount the editor with its draft. Same wiring as MembersPage.
  const leave = useGuardedLeave()
  const crewEditor = useCrewEditor({
    editingName: editingCrew,
    agents: editorAgents,
    // `|| isError` so a FAILED roster read also counts as settled: otherwise the
    // editor's loading dialog never clears, hiding the roster-load notice.
    agentsLoaded: rosterAdmitted || editorRosterQuery.isError,
    defaultAgent: rosterAdmitted ? editorRosterQuery.data?.default_agent || '' : '',
    refetchAgents,
    onClose: closeEditor,
    leave,
  })
  // While the editor holds unsaved work or a write is in flight, a deep link
  // must not swap it to another crewmate: the switch would drop those edits.
  const editorHoldsWork = !!editingCrew && (crewEditor.dirtyPanes.size > 0 || crewEditor.sheetBusy || crewEditor.schedSaving)

  // The modal intercepts its OWN close, but leaving the route, browser Back
  // and a reload are outside its reach: without these guards a typed draft
  // (a Memory doc, a Schedules draft, the avatar builder, the nested
  // workspace form) was discarded silently. Same stakes MembersPage folds in
  // (`useMembersPageCrewEditor`). The shell's registry needs a synchronous
  // answer, so an at-stake editor REFUSES the leave and raises the editor's
  // own discard confirm (`requestClose`), as the artifact editor does; after a
  // discard the user repeats the navigation. A nested dialog (avatar builder,
  // workspace form) holding the only draft is already on screen, so the
  // refusal alone keeps it. An in-flight write refuses outright: the request
  // could still land after a confirmed discard.
  const editorAtStake = !!editingCrew && (
    crewEditor.dirtyPanes.size > 0 || crewEditor.memoryDirty || (crewEditor.wsModalOpen && crewEditor.wsDirty)
    || crewEditor.avatarBuilderOpen || crewEditor.templateDirty
  )
  const editorBusy = !!editingCrew && (crewEditor.sheetBusy || crewEditor.schedSaving)
  const { dirtyPanes: editorDirtyPanes, requestClose: requestEditorClose } = crewEditor
  useRegisterNavigationLeaveGuard(() => {
    if (editorBusy) return false
    if (!editorAtStake) return true
    if (editorDirtyPanes.size > 0) requestEditorClose()
    return false
  })
  // Back arms off the published stake, not off the guard.
  usePublishNavigationStake(editorAtStake || editorBusy)
  // A reload or tab close is not a route change; only the browser's own prompt
  // can catch it. Registered only while something is at stake.
  useEffect(() => {
    if (!editorAtStake && !editorBusy) return
    const onUnload = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = '' }
    window.addEventListener('beforeunload', onUnload)
    return () => window.removeEventListener('beforeunload', onUnload)
  }, [editorAtStake, editorBusy])

  /** Argument-free so a click event is never mistaken for one. Opens the
   *  simple create dialog (`NewCrewmateDialog`); it owns its own form state. */
  const openCreate = useCallback(() => {
    setCreateOpen(true)
  }, [])

  const openEdit = useCallback((a: KiroCrewAgent) => {
    setLandingSection(undefined)
    setEditingCrew(a.name)
  }, [])

  /**
   * Deep link: `?crew=<name>` opens that crew's editor; `&avatar=1` opens the
   * avatar builder on top of it; `&pane=capabilities` lands on Approvals (a chat
   * error row's fix link: a member agent file that changed since its review is
   * re-reviewed and saved there). Latched once the roster has loaded, then
   * stripped from the URL (same idiom as SkillsTab's `?review=`): reading the
   * param on every render would re-open the editor after the user closed it.
   */
  const [params, setParams] = useSearchParams()
  useEffect(() => {
    // A bare capabilities URL becomes the mobile root list. An open editor —
    // OR the open create dialog, which carries a typed draft — needs an
    // explicit pane route before a resize can remove its ancestry; without it
    // narrowing past the breakpoint unmounts this page and the dialog, losing
    // the draft with no confirm.
    if (!embedded || (!editingCrew && !createOpen) || params.get('tab') === 'crews') return
    setParams(current => {
      const next = new URLSearchParams(current)
      next.set('tab', 'crews')
      return next
    }, { replace: true })
  }, [embedded, editingCrew, createOpen, params, setParams])
  const linkedCrew = params.get('crew')
  const linkedAvatar = params.get('avatar') === '1'
  const linkedApprovals = params.get('pane') === 'capabilities'
  // The crewmate whose avatar builder a deep link asked for. Opened once the
  // editor has seeded that crewmate, because seeding closes the builder.
  const pendingAvatarFor = useRef<string | null>(null)
  useEffect(() => {
    if (!linkedCrew || !agentsData || editorHoldsWork) return
    const target = agents.find(a => a.name === linkedCrew)
    if (target) {
      setLandingSection(linkedApprovals ? 'approvals' : undefined)
      setEditingCrew(target.name)
      pendingAvatarFor.current = linkedAvatar ? target.name : null
    }
    // An unknown name strips silently: the roster below is the honest answer.
    setParams(prev => {
      const next = new URLSearchParams(prev)
      next.delete('crew'); next.delete('avatar'); next.delete('pane')
      return next
    }, { replace: true })
  }, [linkedCrew, linkedAvatar, linkedApprovals, agentsData, agents, setParams, editorHoldsWork])
  const { open: editorOpen, editing: editorTarget, openAvatarBuilder } = crewEditor
  useEffect(() => {
    if (!editorOpen || pendingAvatarFor.current !== editorTarget) return
    pendingAvatarFor.current = null
    openAvatarBuilder()
  }, [editorOpen, editorTarget, openAvatarBuilder])

  /**
   * Deep link: `?new=1` opens the create dialog straight away. Unlike `?crew=`
   * nothing here waits on the roster: the create form has no saved record to
   * load. Latched once, then stripped from the URL for the same reason as
   * `?crew=`: a reload or Back must not re-open a closed form.
   */
  const linkedNew = params.get('new') === '1'
  useEffect(() => {
    if (!linkedNew || editorHoldsWork) return
    openCreate()
    setParams(prev => {
      const next = new URLSearchParams(prev)
      next.delete('new')
      return next
    }, { replace: true })
  }, [linkedNew, openCreate, setParams, editorHoldsWork])

  /** The simple create dialog reported a new crew. Config mode: close the
   *  dialog and stay on this roster — no navigation, no greeting. The dialog
   *  has already invalidated the roster and config caches in its own success
   *  path, and `refetchAgents` re-reads the leaf this page renders from, so the
   *  new card appears where the user is. */
  const handleCapCreated = useCallback((_created: CreatedCrewmate) => {
    setCreateOpen(false)
    refetchAgents()
  }, [refetchAgents])

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

  // Guide facts (whether, never which): a crewmate's editor is open (the
  // "choose the crewmate" step of a guide to an editor control), and, while it
  // is, which section shows. Model is on General and the memory is its own
  // section. With the editor closed those read unknown rather than unmet.
  useGuideSelection('crewmate_editor_open', { selected: crewEditor.open, available: !!agentsData && agents.length > 0, name: crewEditor.editingAgent ? crewDisplayName(crewEditor.editingAgent) : undefined })
  const sectionFact = (key: CrewEditorSectionKey) => (crewEditor.open && shownSection !== null ? shownSection === key : null)
  useGuidePredicate('crewmate_danger_zone_open', sectionFact('danger'))
  useGuidePredicate('crewmate_place_pane_open', sectionFact('memory'))
  useGuidePredicate('crewmate_model_pane_open', sectionFact('general'))

  return (
    <>
      {!embedded && <PageHeader title={i18nT('pages.kiroCrewAgentsPage.agents')} subtitle={i18nT('pages.kiroCrewAgentsPage.manage_agent_workspace_memory_store_bindings')} />}
      <div className={`${embedded ? '' : 'px-4 md:px-6'} pb-8 overflow-y-auto flex-1 min-h-0`}>
        {/* A roster that failed to load must not read as "you have no crews":
            the empty state below would say exactly that. Hand-off only while the
            editor AND the create dialog are closed — open, the editor's unsaved
            edits or the create dialog's typed draft would go with the
            navigation and be lost with no confirm. */}
        <ErrorNotice
          className="mb-3.5"
          title={i18nT('components.agentSelector.roster_load_failed')}
          message={agentsError ? errorText(agentsError) : null}
          askAgent={!editingCrew && !createOpen}
          testId="crews-roster-load-error"
        />

        <CrewRosterToolbar agents={agents} filter={filter} setFilter={setFilter} view={view} pickView={pickView} openCreate={openCreate} />

        {/* The crewmate list, in either view: what a guide's "choose the
            crewmate" step points at (picking one opens its editor). */}
        <CrewRosterList agents={agents} filtered={filtered} view={view} defaultAgent={defaultAgent} sharedKind={sharedKind} memoryStores={kirocrewCfg?.memory_stores} openEdit={openEdit} openCreate={openCreate} />
      </div>

      {/* The editor renders nothing until a crewmate is being edited, and holds
          its own loading dialog until the fresh roster read resolves the
          record. The fallback covers only the first download of its chunk. */}
      <Suspense fallback={editingCrew ? <CrewEditorChunkLoading /> : null}>
        <CrewEditorModal ctl={crewEditor} initialSection={landingSection} onSectionChange={setShownSection} />
      </Suspense>
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
