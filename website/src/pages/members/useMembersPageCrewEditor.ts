/**
 * The crew editor this page opens in place (`CrewEditorDialog`): the registry
 * reads behind it and behind the Profile card's description, the latch that
 * opens the editor only from a fresh roster read, the `useCrewEditor` wiring,
 * and the unsaved-pane and in-flight-write stakes the page's exit guards read.
 *
 * The `MembersPage` facade calls each hook here at the point its code sits in
 * the page's hook order, which `MembersPage.owners.test.tsx` pins: React runs
 * a component's effects in registration order, so moving one is a behaviour
 * change even when every test of the moved code still passes.
 */
import { useCallback, useMemo, useRef, type Dispatch, type SetStateAction } from 'react'
import { type SetURLSearchParams } from 'react-router-dom'
import { api } from '../../api/client'
import { type KiroCrewAgent } from '../../components/AgentSelector'
import { useCrewEditor } from '../../components/crew/useCrewEditor'
import { useQuery, type QueryClient } from '@tanstack/react-query'
import { useGuardedLeave } from '../../components/NavigationLeaveGuard'
import { type MembersPageRosterRead } from './useMembersPageRoster'

interface MembersPageCrewEditingOptions {
  rosterRead: MembersPageRosterRead
  leave: ReturnType<typeof useGuardedLeave>
  queryClient: QueryClient
  activeName: string
  setSearchParams: SetURLSearchParams
  urlMember: string
  editingCrew: string
  setEditingCrew: Dispatch<SetStateAction<string>>
}

/** The in-page crew editor's reads, admission latch and stakes. */
export function useMembersPageCrewEditor({
  rosterRead, leave, queryClient, activeName, setSearchParams, urlMember, editingCrew, setEditingCrew,
}: MembersPageCrewEditingOptions) {
  const { rows } = rosterRead
  // The profile card's description. The member roster carries no description, so
  // this reads the crew registry under the SAME key the editor and the crew
  // manager share — a save anywhere reaches the card. Display-only: the card
  // never writes, so a retained copy here is fine (the editor keeps its own
  // staleTime:0 observer below).
  const profileRegistryQuery = useQuery<{ agents: KiroCrewAgent[]; default_agent?: string }>({
    queryKey: ['kirocrew-agents'],
    queryFn: () => api.kirocrewAgents(),
    enabled: !!activeName,
  })
  // The crew editor needs the KiroCrewAgent roster (not the member roster):
  // same query key KiroCrewAgentsPage uses, so the cache is shared and a write
  // here reaches both. Only fetched while the editor is open.
  const crewAgentsQuery = useQuery({
    queryKey: ['kirocrew-agents'],
    queryFn: () => api.kirocrewAgents(),
    enabled: !!editingCrew,
    // Always refetch when the editor opens — never serve a cached roster as
    // "fresh" without a network read. The editor saves the WHOLE snapshot, so a
    // cached copy that went stale while nothing invalidated it (an external edit
    // the WebSocket missed while disconnected) would be admitted by the latch
    // below and let a save stomp newer config. staleTime:0 forces the fetch; the
    // rosterAdmitted latch then waits for IT to settle before opening.
    staleTime: 0,
  })
  // The editor saves the WHOLE agent snapshot, so it must OPEN from a roster
  // read that is current — never React Query's RETAINED copy. After this key is
  // invalidated `.data` keeps the pre-edit snapshot while the refetch is in
  // flight; opening the editor from that would let a full-snapshot save stomp
  // newer workspace/model/config values the retained copy predates.
  //
  // So gate the OPENING, not every render: `rosterAdmitted` latches true once a
  // fetch has settled successfully (isSuccess && !isFetching) for the CURRENT
  // editingCrew, and resets when the editor closes/switches. Before admission
  // the editor sees `[]` (→ editingAgent undefined → the hook holds the dialog
  // in its loading state) so a stale snapshot never opens it. AFTER admission we
  // keep showing live `.data`: the editor's own save invalidates this key and
  // the brief in-flight window then carries the just-written snapshot, not a
  // stale one — latching avoids tearing the open dialog down on every save. The
  // error path is handled separately (the roster-load notice), so it is not
  // treated as admission here.
  const rosterAdmittedRef = useRef('')
  const crewRosterSettledFresh = crewAgentsQuery.isSuccess && !crewAgentsQuery.isFetching
  if (!editingCrew) {
    rosterAdmittedRef.current = ''
  } else if (crewRosterSettledFresh && rosterAdmittedRef.current !== editingCrew) {
    rosterAdmittedRef.current = editingCrew
  }
  const rosterAdmitted = !!editingCrew && rosterAdmittedRef.current === editingCrew
  const crewAgents = useMemo<KiroCrewAgent[]>(
    () => (rosterAdmitted ? crewAgentsQuery.data?.agents || [] : []),
    [rosterAdmitted, crewAgentsQuery.data],
  )
  const crewDefaultAgent = rosterAdmitted ? crewAgentsQuery.data?.default_agent || '' : ''
  // The same prefix invalidation KiroCrewAgentsPage uses: it refreshes the
  // member roster (a projection under the same prefix) AND the config the
  // memory row reads, so an edit here updates the open thread's row without a
  // manual refetch.
  const refetchCrewAgents = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['kirocrew-agents'] })
    void queryClient.invalidateQueries({ queryKey: ['kirocrewConfig'] })
  }, [queryClient])
  const closeCrewEditor = useCallback(() => setEditingCrew(''), [setEditingCrew])
  // Points at `releaseSchedRetention` (defined in MembersPageProfile.tsx with the
  // schedule guard machinery) so `onCrewDeleted` above can stand down the retention when
  // a confirmed crew delete clears the URL. A ref because the fn is declared
  // after this; no-op until assigned, which is before any delete can fire.
  const releaseSchedRef = useRef<() => void>(() => {})
  // A delete of the OPEN member's crew retires the thread: the record is gone,
  // so clear `?member=` back to the roster rather than leave a thread mounted
  // on a crew that no longer exists. (The editor cannot rename the identity —
  // only display_name — so a save never needs a URL repoint.)
  const onCrewDeleted = useCallback((name: string) => {
    // Deleting the crew is already a confirmed, deliberate action (the editor's
    // own delete-confirm), and the Schedules draft in the panel behind the modal
    // is a schedule FOR this now-deleted crew — so it is intentionally discarded
    // with the crew, not silently lost. Release the schedule retention first so
    // clearing `?member=` unmounts the panel as an acknowledged discard rather
    // than bypassing the draft guard. (The release fn is defined in
    // MembersPageProfile.tsx, so it is reached through a ref.)
    if (name === urlMember) {
      releaseSchedRef.current()
      setSearchParams({}, { replace: true })
    }
  }, [urlMember, setSearchParams])
  const crewEditor = useCrewEditor({
    editingName: editingCrew,
    // The canonical slug for `member_id` matching in the Schedules pane. The
    // pill sets `editingCrew` to a crew's display NAME; the roster row's `slug`
    // is the immutable id a private schedule's `member_id` actually holds, so a
    // renamed crewmate's schedules still resolve. Falls back to the name (the
    // hook then defaults memberId to editingName) when no row matches.
    memberId: rows.find((r) => r.name === editingCrew)?.slug,
    agents: crewAgents,
    // `|| isError` so a FAILED roster read also counts as "settled": otherwise
    // the editor's `loading` flag never clears on error, leaving a permanent
    // spinner dialog whose overlay hides the roster-load error notice behind it.
    // `rosterAdmitted` (not `.data !== undefined`) so a RETAINED stale snapshot
    // during the initial post-invalidation refetch keeps `loading` true rather
    // than opening the editor on data a full-snapshot save would stomp.
    agentsLoaded: rosterAdmitted || crewAgentsQuery.isError,
    defaultAgent: crewDefaultAgent,
    refetchAgents: refetchCrewAgents,
    onClose: closeCrewEditor,
    onDeleted: onCrewDeleted,
    // The editor's Chat and Manage-memory buttons leave `/members`; route them
    // through the page's guard so a typed Schedules draft behind the modal is
    // not silently discarded.
    leave,
  })

  // The crew-editor MODAL (opened by the identity pill) holds its own unsaved
  // pane edits (template, model, routing, …) in `crewEditor.dirtyPanes`. The
  // modal intercepts its OWN in-app close, but the page-level exits in
  // MembersPage.tsx (route leave, browser Back, reload) are outside its reach — so without
  // folding its dirty state in here, editing a field then pressing Back or
  // reloading discarded the edits silently, exactly as an unguarded Schedules
  // draft would. Mirrored to a ref for the synchronous guards and to state for
  // the published stake / `beforeunload` arm. Gated on the modal being open so
  // a stale set never arms a guard over a closed editor. The nested
  // create-workspace modal is a second typed draft the editor can hold open, so
  // `wsModalOpen && wsDirty` is folded in too — the same parity NewCrewmateDialog
  // keeps for its own workspace sub-form. The avatar builder is a third: it
  // composes an override across many controls and commits only on Apply (which
  // then surfaces in `dirtyPanes`), so while it is OPEN the in-progress edit is
  // in no pane yet — the open state itself is the at-stake signal, since the
  // builder exposes no dirty callback to narrow it. The template pane reports
  // its OWN nested-dialog drafts (publish-copy name) through `templateDirty`, a
  // single signal so a future nested template input is covered without adding
  // another term here.
  const crewModalAtStake = !!editingCrew && (crewEditor.dirtyPanes.size > 0 || (crewEditor.wsModalOpen && crewEditor.wsDirty) || crewEditor.avatarBuilderOpen || crewEditor.templateDirty)
  const crewModalAtStakeRef = useRef(crewModalAtStake)
  crewModalAtStakeRef.current = crewModalAtStake
  // In-flight editor WRITE (an update/delete/avatar-upload/capability save still
  // pending), distinct from a dirty draft: navigating away + confirming discard
  // would unmount the modal while the request is in the air, and it could still
  // land — persisting a change the user believes was discarded. So the page
  // guards REFUSE (not merely prompt) while this holds, matching `requestClose`
  // which refuses a close mid-commit. Folded into the stake too so the browser's
  // own beforeunload arms during a write. `sheetBusy` covers the editor's direct
  // mutations (update / delete / avatar upload / capability save); `schedSaving`
  // is the NESTED Schedules-create POST (CrewWakeSection's in-flight create),
  // which lives outside sheetBusy and would otherwise persist a schedule after a
  // confirmed discard unmounts the modal.
  const crewModalBusy = !!editingCrew && (crewEditor.sheetBusy || crewEditor.schedSaving)
  const crewModalBusyRef = useRef(crewModalBusy)
  crewModalBusyRef.current = crewModalBusy

  return {
    profileRegistryQuery, crewAgentsQuery, closeCrewEditor, releaseSchedRef, crewEditor, crewModalAtStake,
    crewModalAtStakeRef, crewModalBusy, crewModalBusyRef,
  }
}

export type MembersPageCrewEditing = ReturnType<typeof useMembersPageCrewEditor>
