/**
 * Teams on the Crewmates page. `useMembersPageTeams` is the team read, its
 * notice, the open team (`?team=`), the collapsed groups and the team dialog.
 * `useMembersPageTeamView` opens and closes a team, groups the shown roster
 * under team headers, and gives the open team's members their live readings.
 *
 * The `MembersPage` facade calls each hook here at the point its code sits in
 * the page's hook order, which `MembersPage.owners.test.tsx` pins: React runs
 * a component's effects in registration order, so moving one is a behaviour
 * change even when every test of the moved code still passes.
 */
import { useCallback, useMemo, useState } from 'react'
import { type NavigateFunction, type Location, type SetURLSearchParams } from 'react-router-dom'
import { type CrewTeam, type MemberRosterRow } from '../../api/client'
import { teamsQuery } from '../../api/teamsQuery'
import { useQuery } from '@tanstack/react-query'
import { usePersistedString } from '../../hooks/usePersistedString'
import { type TeamMemberInput } from './TeamView'
import { TEAM_COLLAPSED_KEY, TEAM_PARAM, groupRosterByTeam, parseCollapsedTeams, serializeCollapsedTeams } from './teamGroups'
import { type MembersPageProfileState } from './MembersPageProfile'
import { type MembersPageRosterView } from './MembersPageRoster'
import { type MembersPageLayout } from './useMembersPageLayout'
import { type MembersPageRosterRead, type MembersPageRosterSignals } from './useMembersPageRoster'

const EMPTY_TEAMS: readonly CrewTeam[] = []

interface MembersPageTeamsReadOptions {
  searchParams: URLSearchParams
}

/** The team list, its notice, the open team, the collapsed groups and the team dialog. */
export function useMembersPageTeams({ searchParams }: MembersPageTeamsReadOptions) {
  // Teams: the roster's grouping and the main pane's OTHER occupant. The open
  // team rides the URL like the open member (`?team=<id>`); the two parameters
  // are exclusive -- opening one writes the URL without the other. The list is
  // small and this page's own dialog is its only writer, so a failed read is
  // the only state worth a notice. EVERY failed read, not only the first: a
  // refetch that fails after a save or a return to the page leaves the cached
  // list on screen, and that list is said to be stale rather than shown as
  // current. With no answer at all the roster renders flat.
  const teamsQ = useQuery(teamsQuery)
  const teams = teamsQ.data ?? EMPTY_TEAMS
  const teamsFailed = teamsQ.isError
  // A dismiss holds for this outage: a failed read keeps re-reading every 30s
  // (`refetchWhileFailed`), so the notice stays away until a read succeeds
  // (`dataUpdatedAt` passes the dismiss) and only a failure after that shows it.
  const [teamsDismissedAt, setTeamsDismissedAt] = useState(0)
  const teamsNoticeShown = teamsFailed && teamsDismissedAt <= teamsQ.dataUpdatedAt
  const dismissTeamsNotice = useCallback(() => setTeamsDismissedAt(teamsQ.errorUpdatedAt), [teamsQ.errorUpdatedAt])
  const urlTeam = searchParams.get(TEAM_PARAM) ?? ''
  const activeTeam = useMemo(() => teams.find((tm) => tm.id === urlTeam), [teams, urlTeam])
  // Collapsed groups persist per browser, keyed by team id (the "No team" group
  // by its fixed id), the same localStorage idiom as the roster filters.
  const [rawCollapsedTeams, setRawCollapsedTeams] = usePersistedString(TEAM_COLLAPSED_KEY, '[]')
  const collapsedTeams = useMemo(() => parseCollapsedTeams(rawCollapsedTeams), [rawCollapsedTeams])
  const toggleTeamCollapsed = useCallback(
    (id: string) =>
      setRawCollapsedTeams((prev) => {
        const next = parseCollapsedTeams(prev)
        if (next.has(id)) next.delete(id)
        else next.add(id)
        return serializeCollapsedTeams(next)
      }),
    [setRawCollapsedTeams],
  )
  // The team dialog. `team` set = edit, which is the only way in while team
  // creation is hidden for the phase (the roster header's "+" menu in MembersPageRoster.tsx says
  // what was removed and what returns it). Mounted only while
  // open, so its fields start from the team it was opened for.
  const [teamDialog, setTeamDialog] = useState<{ team?: CrewTeam } | null>(null)

  return {
    teamsQ, teams, teamsNoticeShown, dismissTeamsNotice, urlTeam, activeTeam, collapsedTeams,
    toggleTeamCollapsed, setTeamDialog, teamDialog,
  }
}

export type MembersPageTeamsRead = ReturnType<typeof useMembersPageTeams>

interface MembersPageTeamViewOptions {
  rosterRead: MembersPageRosterRead
  teamsRead: MembersPageTeamsRead
  layout: MembersPageLayout
  profileCard: MembersPageProfileState
  rosterView: MembersPageRosterView
  rosterSignals: MembersPageRosterSignals
  navigate: NavigateFunction
  location: Location
  setSearchParams: SetURLSearchParams
  urlMember: string
  leaveCreate: () => boolean
  slotKeyOf: (m: MemberRosterRow) => string
}

/** Opening and closing a team, the grouped roster, and the open team's members. */
export function useMembersPageTeamView({
  rosterRead, teamsRead, layout, profileCard, rosterView, rosterSignals, navigate, location, setSearchParams,
  urlMember, leaveCreate, slotKeyOf,
}: MembersPageTeamViewOptions) {
  const { collidingSlugs } = rosterRead
  const { teams, urlTeam, activeTeam } = teamsRead
  const { isMobile } = layout
  const { schedGuardRef, schedAtStakeRef } = profileCard
  const { isRunning, liveNeedsYou, orderedMembers, shownMembers } = rosterView
  const { sortedMembers } = rosterSignals
  // Team open: the header row's click. Same history rule as openMember -- one
  // entry above md or while something is already open, a PUSHED step from the
  // bare roster below md so the back button pops.
  const openTeam = useCallback(
    (id: string) => {
      if (!leaveCreate()) return
      const go = () => {
        if (urlMember || urlTeam || !isMobile) {
          // The replace keeps the entry's own state: below md this branch is
          // reached from the team view itself (Edit team -> Save re-opens the
          // saved id over `?team=`), and dropping `fromRoster` there would send
          // the next Back to a second copy of the roster instead of off the
          // page -- closeTeamView reads that flag to choose between the two.
          setSearchParams({ [TEAM_PARAM]: id }, { replace: true, state: location.state })
          return
        }
        setSearchParams({ [TEAM_PARAM]: id }, { state: { fromRoster: true } })
      }
      // Opening a team clears the open crewmate, which unmounts the whole panel subtree
      // and with it any create form on the Schedules tab. A team header row is a click
      // away from that tab, so it asks first.
      if (!schedAtStakeRef.current()) { go(); return }
      void schedGuardRef.current().then((ok) => { if (ok) go() })
    },
    [urlMember, urlTeam, isMobile, setSearchParams, location.state, leaveCreate, schedAtStakeRef, schedGuardRef],
  )
  const closeTeamView = useCallback(() => {
    if ((location.state as { fromRoster?: boolean } | null)?.fromRoster) navigate(-1)
    else setSearchParams({}, { replace: true })
  }, [location.state, navigate, setSearchParams])
  // The roster, grouped: the DISPLAYED rows (after search / filter / sort) under
  // their team headers, in the teams' stored order, unlisted rows last.
  // Team headers are judged against the rows the roster is ABOUT (`shownMembers`),
  // not the whole roster: a team whose every crewmate is hidden still gets its
  // header (empty), so the team view, its Needs-you inbox and its editor stay
  // reachable.
  const rosterGroups = useMemo(
    () => groupRosterByTeam(teams, sortedMembers, shownMembers),
    [teams, sortedMembers, shownMembers],
  )
  const grouped = teams.length > 0
  // The open team's crewmates for the team view -- the WHOLE team in roster
  // order, not the filtered rows: a search typed into the roster narrows the
  // list, not the team. Each carries the live readings the roster rows resolve.
  const teamMembers = useMemo<TeamMemberInput[]>(() => {
    if (!activeTeam) return []
    const onTeam = new Set(activeTeam.members)
    return orderedMembers
      .filter((m) => onTeam.has(m.name))
      .map((m) => {
        const slotKey = slotKeyOf(m)
        return {
          row: m,
          slotKey,
          running: !!isRunning(m),
          needsInput: !!(slotKey && liveNeedsYou[slotKey]),
          slugCollides: collidingSlugs.has(m.slug),
        }
      })
  }, [activeTeam, orderedMembers, slotKeyOf, isRunning, liveNeedsYou, collidingSlugs])

  return {
    openTeam, closeTeamView, rosterGroups, grouped, teamMembers,
  }
}

export type MembersPageTeamView = ReturnType<typeof useMembersPageTeamView>
