/**
 * Which crewmate the Crewmates page has open. `activate` and `openMember` run
 * every open through one path (the switch guards, then the thread-open POST);
 * the drawer pick spends the drawer's history entry first; a staged-dashboard
 * link in a thread switches onto that crewmate's Dashboard tab; and the URL
 * sync turns `?member=` (or a bare `/members`) into the open crewmate.
 *
 * The `MembersPage` facade calls each hook here at the point its code sits in
 * the page's hook order, which `MembersPage.owners.test.tsx` pins: React runs
 * a component's effects in registration order, so moving one is a behaviour
 * change even when every test of the moved code still passes.
 */
import { useCallback, useEffect, type Dispatch, type SetStateAction, type MutableRefObject } from 'react'
import { type Location, type SetURLSearchParams } from 'react-router-dom'
import { type CreatedCrewmate } from './NewCrewmateDialog'
import { type MemberRosterRow } from '../../api/client'
import { type QueryClient } from '@tanstack/react-query'
import { i18nT } from '../../i18n/t'
import { DASHBOARD_PREVIEW_PARAM, DASHBOARD_PREVIEW_VALUE } from './dashboardPreview'
import { dashboardPreviewMemberFromRef, dashboardPreviewSlugFromRef } from '../../utils/dashboardPreview'
import { hasNoCrewmates } from './rosterFilter'
import { safeGetItem, safeSetItem } from '../../utils/safeStorage'
import { type MembersPageProfileState } from './MembersPageProfile'
import { type MembersPageRosterView } from './MembersPageRoster'
import { type MembersPageSidePanelState } from './MembersPageSidePanel'
import { type MembersPageThreadState } from './MembersPageThread'
import { CREW_DASHBOARD_TAB_ID, MEMBER_PARAM, resolveDefaultMember, resolveMateLanding, type GoneNotice } from './membersPageModel'
import { type MembersPageDrawers } from './useMembersPageDrawers'
import { type MembersPageLayout } from './useMembersPageLayout'
import { type MembersPageRosterRead } from './useMembersPageRoster'
import { type MembersPageTeamsRead } from './useMembersPageTeams'

/** The last member OPENED (click, link, or the page's own default open), so
 *  returning to the page (the rail's Crewmates row, a reload, a gateway
 *  restart) lands on the row the user left — opened is enough, no message
 *  need have been sent (#19341). Stored by exact name for the same reason as
 *  the URL param. One key per origin is the right scope: the roster is the
 *  gateway's global crew list, and localStorage is already per-gateway. */
const LAST_MEMBER_KEY = 'mc-members-last-member'

interface MembersPageNavigationOptions {
  rosterRead: MembersPageRosterRead
  teamsRead: MembersPageTeamsRead
  layout: MembersPageLayout
  profileCard: MembersPageProfileState
  drawers: MembersPageDrawers
  rosterView: MembersPageRosterView
  thread: MembersPageThreadState
  sidePanel: MembersPageSidePanelState
  location: Location
  queryClient: QueryClient
  activeName: string
  setActiveName: Dispatch<SetStateAction<string>>
  activeNameRef: MutableRefObject<string>
  searchParams: URLSearchParams
  setSearchParams: SetURLSearchParams
  urlMember: string
  setGone: Dispatch<SetStateAction<GoneNotice | null>>
  draftRequest: boolean
  leaveCreate: () => boolean
  followUp: CreatedCrewmate | null
  goneStandInRef: MutableRefObject<string>
  activeMemberName: string
}

/** Opening a crewmate, and the URL sync that decides which one is open. */
export function useMembersPageNavigation({
  rosterRead, teamsRead, layout, profileCard, drawers, rosterView, thread, sidePanel, location, queryClient,
  activeName, setActiveName, activeNameRef, searchParams, setSearchParams, urlMember, setGone, draftRequest,
  leaveCreate, followUp, goneStandInRef, activeMemberName,
}: MembersPageNavigationOptions) {
  const { rosterQuery, loaded, loadError, members } = rosterRead
  const { teamsQ, urlTeam } = teamsRead
  const { isMobile } = layout
  const { schedGuardRef, schedAtStakeRef } = profileCard
  const { closeRosterDrawer } = drawers
  const { orderedMembers, listedMembers } = rosterView
  const { crewPreview, mateLandedRef, postThread } = thread
  const { showActionError, revealPanelAfterOpen, openArtifact, openArtifactGuarded, focusPanelTab } = sidePanel
  // Open a member's thread and remember it as the last one opened. Called by
  // the URL sync effect only (plus the same-member re-click below), so every
  // way of arriving at a member — click, back/forward, shallow link, restore
  // on return — runs one code path. `remember` is false only for the member
  // opened IN PLACE OF one a link named that is gone: that open is the page's
  // choice, not the user's, so one stale link must not overwrite the member
  // they had actually chosen. The built-in `default` crewmate is remembered
  // like any other row (#17210).
  const activate = useCallback(
    (m: MemberRosterRow, remember = true) => {
      activeNameRef.current = m.name
      setActiveName(m.name)
      if (remember) safeSetItem(LAST_MEMBER_KEY, m.name)
      // A Side Chat belongs to the member it was asked about; nothing to reset
      // here — the panel's strip is bucketed per member slot, so switching
      // members swaps the whole strip and a Side tab stays with its member.
      // ALWAYS post, even when a slot key is already cached: the endpoint is
      // the idempotent creator/repairer, and the backend can lose the live
      // slot between opens (archive, restart with a stale binding) — a cached
      // key mounted without the POST would point at nothing. The cache only
      // decides what to render while the POST is in flight — and, for the side
      // panel, not even that (see confirmedSlot).
      postThread(m)
    },
    [postThread, activeNameRef, setActiveName],
  )

  /** Opens `m`, or answers `false` when the Schedules guard refuses to let go of a
   *  dirty draft. A caller that parked state on this open HAPPENING must read that
   *  answer: `openCreated` holds the create button on the open releasing it, and a
   *  refused guard opens no thread to do it. Everything but the
   *  guard's own await runs synchronously, so a switch with nothing at stake still
   *  lands in one tick. */
  const openMember = useCallback(
    async (m: MemberRosterRow): Promise<boolean> => {
      if (!leaveCreate()) return false
      // Re-clicking the open member is the repair gesture (re-POST); the URL
      // is unchanged so the sync effect would not fire — call through. It is
      // also an explicit choice of that member, so a swap notice still
      // standing over it (the user was routed here from a dead link) has
      // been acknowledged: retire it.
      if (m.name === activeName) {
        activate(m)
        setGone(null)
        return true
      }
      const go = () => {
        // Opening a crewmate REPLACES the entry at every width: the page holds
        // one history entry however many crewmates are visited, and Back leaves
        // it in one press — the Sessions sidebar's rule. A phone included: its
        // bare `/members` lands on a crewmate by itself, so a pushed step back
        // to it would bounce straight back into that crewmate and Back could
        // never leave the page.
        setSearchParams({ [MEMBER_PARAM]: m.name }, { replace: true })
      }
      // A switch to ANOTHER crewmate remounts the Schedules section (it is keyed on the
      // crewmate), so it destroys an open create form just as leaving the tab does. It
      // used to do that silently, which is what made "every exit asks" untrue. Nothing
      // at stake keeps the switch synchronous, exactly as before.
      if (!schedAtStakeRef.current()) { go(); return true }
      const ok = await schedGuardRef.current()
      if (ok) go()
      return ok
    },
    [activeName, activate, setSearchParams, leaveCreate, schedAtStakeRef, schedGuardRef, setGone],
  )
  /** A roster row picked in the phone drawer: close the drawer and spend its
   *  history entry FIRST, then open — see `consumeRosterDrawerEntry`. */
  const pickFromRosterDrawer = useCallback((m: MemberRosterRow) => {
    closeRosterDrawer(() => { void openMember(m) })
  }, [closeRosterDrawer, openMember])

  // ONE preview surface on this page: a staged-dashboard link clicked in a thread
  // (either form) opens the named crewmate's Dashboard tab on the staged page, the
  // same place `?dashboard=preview` lands, never a second side-panel tab. The chat
  // page keeps its own side-panel preview. Answers whether the reference was one.
  const openDashboardPreviewHere = useCallback((ref: string): boolean => {
    const byName = dashboardPreviewMemberFromRef(ref)
    const bySlug = byName ? null : dashboardPreviewSlugFromRef(ref)
    if (!byName && !bySlug) return false
    const matches = byName ? members.filter(m => m.name === byName) : members.filter(m => m.slug === bySlug)
    if (matches.length !== 1) {
      showActionError(i18nT('pages.chat.dashboardPreviewPanel.no_member'))
      return true
    }
    const target = matches[0]
    // Every open re-reads the staged page, as the chat page's opener does: staging
    // sends no frame and the link never changes, so a cached read could show page A
    // while B is what `dashboard_apply` would install.
    void queryClient.invalidateQueries({ queryKey: ['member-dashboard', target.slug] })
    if (target.name === activeMemberName) {
      // The URL may already ask for the preview, so the link effect would not run
      // again: open the tab here as well.
      const next = new URLSearchParams(searchParams)
      next.set(DASHBOARD_PREVIEW_PARAM, DASHBOARD_PREVIEW_VALUE)
      setSearchParams(next, { replace: true, state: location.state })
      revealPanelAfterOpen()
      focusPanelTab(CREW_DASHBOARD_TAB_ID)
      return true
    }
    // Another crewmate: through the same switch guards, then the link's own query.
    void openMember(target).then((ok) => {
      if (ok) setSearchParams({ [MEMBER_PARAM]: target.name, [DASHBOARD_PREVIEW_PARAM]: DASHBOARD_PREVIEW_VALUE }, { replace: true })
    })
    return true
  }, [members, activeMemberName, queryClient, searchParams, setSearchParams, location.state, revealPanelAfterOpen, focusPanelTab, openMember, showActionError])
  const openArtifactInThread = useCallback((slug: string) => {
    if (!openDashboardPreviewHere(slug)) openArtifactGuarded(slug)
  }, [openDashboardPreviewHere, openArtifactGuarded])
  const openArtifactInPanel = useCallback((slug: string) => {
    if (!openDashboardPreviewHere(slug)) void openArtifact(slug)
  }, [openDashboardPreviewHere, openArtifact])

  // URL -> open crewmate. Once the roster is in: a URL that names a crewmate
  // opens it; a URL that names none (a fresh visit, the sidebar entry, a
  // reload) is REPLACED with the remembered crewmate if one is still on the
  // roster, so returning users land back on the conversation they left, else
  // with the conversation holding the newest message
  // (`resolveDefaultMember`), like a messages app. "Nothing
  // to open" therefore means an EMPTY roster, and only that. A URL naming a
  // crewmate that is gone (deleted or renamed) falls back the same way, with
  // a one-line notice above the chat naming the swap — the user asked for
  // someone specific, and a silently mounted other chat is the misroute this
  // page exists to prevent. The phone follows the same rule: its roster is a
  // drawer over the open chat (the chat page's sessions drawer), so landing on
  // a crewmate costs it nothing and Back is not a step back to a list.
  useEffect(() => {
    if (!loaded || loadError) return
    // A create link's URL is rewritten by its own effect in this same commit
    // (keeping the chat it was followed from); land on what that leaves.
    if (draftRequest) return
    if (urlMember) {
      const hit = members.find((m) => m.name === urlMember)
      if (hit) {
        // Opened in place of a gone member a link named? Then it is not the
        // user's choice and must not become the memory (see `activate`).
        const standIn = goneStandInRef.current === hit.name
        goneStandInRef.current = ''
        if (hit.name !== activeNameRef.current) activate(hit, !standIn)
        // The notice belongs to the member shown in place of the gone one;
        // opening anyone else retires it. Functional updates throughout, and
        // `gone` is NOT a dependency: the URL write below is a router
        // transition, and a plain state write that re-armed this effect
        // before the transition committed would re-issue both writes and
        // keep interrupting the transition — the thread would never open.
        setGone((prev) => (prev && prev.shown !== hit.name ? null : prev))
        return
      }
      // Named but not (yet) on the roster while a refetch is in flight: a link
      // may simply have outrun the cache — the crew manager's create lands
      // here with the just-made member's name before the invalidated roster
      // has re-read (#9513). Hold the "gone" verdict until the fetch answers;
      // a member the fresh roster still lacks takes the fallback then.
      if (rosterQuery.isFetching) return
    }
    if (urlTeam) {
      // A team is open in the main pane: no thread stands beside it, and the
      // remembered-member fallback below must not overrule an explicit team
      // link. A link naming a team that is gone (deleted elsewhere) returns to
      // the bare roster once the team list has answered.
      if (teamsQ.data !== undefined && !teamsQ.data.some((tm) => tm.id === urlTeam)) {
        setSearchParams({}, { replace: true })
        return
      }
      if (activeName) {
        activeNameRef.current = ''
        setActiveName('')
      }
      return
    }
    // Mate's first visit wins first: never chatted with, Mate opens ahead of a
    // remembered or more recently used crewmate. Every rule below is the
    // page's ordinary landing, untouched by Mate.
    if (!urlMember && crewPreview && !mateLandedRef.current) {
      const mate = resolveMateLanding(members)
      if (mate) {
        mateLandedRef.current = true
        setSearchParams({ [MEMBER_PARAM]: mate.name }, { replace: true })
        return
      }
    }
    // URL names no crewmate (or names a gone one): restore the remembered
    // crewmate if it is still on the roster, else open the conversation with
    // the newest message among the listed rows — at every width, the phone
    // included, where the roster is a drawer over the open chat rather than a
    // page of its own. `undefined` here means nothing is listed — the chat
    // column shows the New crewmate hero instead (or, with only hidden rows,
    // stays empty until a search or a link names one).
    // A remembered crewmate the user opened themselves is restored even when the
    // listing rule hides it (it was reached through the search): it is listed
    // again while open (`chosen`), and resolving among listed rows only would
    // open a substitute and overwrite the memory. Only with no usable memory
    // does the fallback pick among the listed rows -- and when nothing but the
    // default crew is listed while hidden crewmates exist, it opens the most
    // recently used of those (listed while open), so the page never lands on an
    // empty pane or claims there are no crewmates.
    // A create is opening the crewmate it just made (`openCreated` ->
    // `openMember`): the fallback must not land on the newest conversation
    // first, which on a roster whose other threads hold messages is not the
    // new, still-empty one. The follow-up's own open writes the URL.
    if (followUp) return
    const remembered = safeGetItem(LAST_MEMBER_KEY)
    const rememberedRow = remembered ? members.find((m) => m.name === remembered) : undefined
    // Like a messages app: the conversation the user last had open (the
    // memory), else the one with the newest message, whoever sent it. The
    // built-in `default` crewmate takes part like any row. A roster holding
    // ONLY `default` still shows the empty-state hero (`hasNoCrewmates`), so it
    // is never auto-opened there.
    const target = hasNoCrewmates(members)
      ? undefined
      : rememberedRow ??
        resolveDefaultMember(listedMembers) ??
        resolveDefaultMember(orderedMembers)
    if (!target) {
      // Named a gone crewmate on an empty roster: say where they went above
      // the roster (shown: '' marks the roster variant of the notice) and
      // clear the URL back to the bare list.
      if (urlMember) {
        setGone((prev) =>
          prev && prev.name === urlMember && prev.shown === '' ? prev : { name: urlMember, shown: '' },
        )
        setSearchParams({}, { replace: true })
      }
      // Nothing to open means nothing may STAY open: a chat can still be
      // mounted for a crewmate the
      // roster no longer lists (deleted while open), and returning to a bare
      // `/members` from there (the crew editor's exit, the rail's Crewmates
      // row) would otherwise leave that chat standing over a URL that names
      // no one, next to the empty roster's hero.
      if (activeName) {
        activeNameRef.current = ''
        setActiveName('')
      }
      return
    }
    if (urlMember) {
      setGone((prev) =>
        prev && prev.name === urlMember && prev.shown === target.name
          ? prev
          : { name: urlMember, shown: target.name },
      )
      goneStandInRef.current = target.name
    }
    setSearchParams({ [MEMBER_PARAM]: target.name }, { replace: true })
  }, [loaded, loadError, draftRequest, urlMember, urlTeam, teamsQ.data, members, listedMembers, activeName, isMobile, activate, setSearchParams, rosterQuery.isFetching, orderedMembers, followUp, crewPreview, activeNameRef, goneStandInRef, mateLandedRef, setActiveName, setGone])

  return {
    openMember, pickFromRosterDrawer, openArtifactInThread, openArtifactInPanel,
  }
}

export type MembersPageNavigation = ReturnType<typeof useMembersPageNavigation>
