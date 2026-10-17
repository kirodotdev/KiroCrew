import { useCallback, useEffect, useMemo } from 'react'
import type { Dispatch, SetStateAction } from 'react'
import { useQuery } from '@tanstack/react-query'

import type { Result } from '../../components/commandPalette/types'
import { i18nT } from '../../i18n/t'

import type { RankedRow } from './rootIndex'
import type { ContributedCommand } from './contributedCommands'
import {
  ARTIFACTS_MIN_CHARS,
  ARTIFACTS_ROW_LIMIT,
  ASK_SLOT_KEY,
  SESSIONS_MIN_CHARS,
  type Scope,
  type Slot,
} from './commandBarOverlayModel'
import type { useCommandBarOverlayEngines } from './useCommandBarOverlayEngines'

/**
 * The Command Bar's list: each scoped view's read (armed only inside its own scope, so
 * the root issues none), whether the rows on screen answer what the reader has typed,
 * and the ordered slots the bar offers with their loading state. A new view's read,
 * dead-end row or tail row belongs here.
 */
export function useCommandBarOverlaySlots({
  scope,
  debounced,
  query,
  sessions,
  recentSessions,
  artifacts,
  folderSortMode,
  folders,
  runningMates,
  mates,
  argCommand,
  crewPreview,
  ranked,
  selected,
  setSelected,
}: Pick<
  ReturnType<typeof useCommandBarOverlayEngines>,
  'sessions' | 'recentSessions' | 'artifacts' | 'folderSortMode' | 'folders' | 'runningMates' | 'mates'
> & {
  scope: Scope
  debounced: string
  query: string
  argCommand: ContributedCommand | null
  crewPreview: boolean
  ranked: RankedRow[]
  selected: number
  setSelected: Dispatch<SetStateAction<number>>
}) {
  // Sessions view. Enabled only inside the scope, so the root cannot trigger it.
  const scopedQuery = scope === 'sessions' ? debounced.trim() : ''
  /** The scoped SEARCH is armed only once the query is long enough to answer. */
  const searchArmed = scope === 'sessions' && scopedQuery.length >= SESSIONS_MIN_CHARS
  const {
    data: scopedResults,
    error: sessionsSearchError,
    isFetching,
    isError,
    refetch: refetchSessions,
  } = useQuery({
    queryKey: ['command-bar', 'sessions', scopedQuery],
    queryFn: () => Promise.resolve(sessions.search(scopedQuery)) as Promise<Result[]>,
    enabled: searchArmed,
    staleTime: 15_000,
  })

  /**
   * What the sessions view shows BEFORE a query narrows it: the recent sessions.
   *
   * Entering the view used to land on a centred "keep typing" sentence — a screen
   * with no rows, so no selection, so nothing Enter could do. That state is also
   * the only reason the input needs a focus box at all: the cue normally rides the
   * active option, and there was no option to ride. A view whose listing IS its
   * empty state has neither problem, and the listing is what the user is most often
   * after anyway — the session they were in a minute ago, reachable without
   * remembering a word from its title.
   *
   * `enabled` is what keeps the root request-free: the provider is inert until
   * `search()` runs, and this is its only call site.
   */
  const listingArmed = scope === 'sessions' && !searchArmed
  const { data: recentRows } = useQuery({
    queryKey: ['command-bar', 'recents'],
    queryFn: () => Promise.resolve(recentSessions.search('')) as Promise<Result[]>,
    enabled: listingArmed,
    staleTime: 15_000,
  })

  /**
   * Artifacts view. ONE query for both of its states, unlike the sessions view
   * above.
   *
   * The sessions view needs two engines because its listing is a different thing
   * from its search — live slots and history buckets, not a query with no words in
   * it. Artifacts has no such split: the list endpoint with no `q` already answers
   * "the newest ones", which is exactly the listing, so an empty query is a
   * narrowing of zero rather than a second mode. Sending it through one query keeps
   * the two states on one cache key and removes the window where a view could show
   * a listing and a search at once.
   */
  const artifactsQuery =
    scope === 'artifacts' && debounced.trim().length >= ARTIFACTS_MIN_CHARS ? debounced.trim() : ''
  const {
    data: artifactRows,
    error: artifactsSearchError,
    isFetching: artifactsFetching,
    isError: artifactsError,
    refetch: refetchArtifacts,
  } = useQuery({
    // Distinct from the provider's own `['artifacts', 'command-bar', 'name', q]`
    // key, which caches the RESPONSE. This one caches the mapped rows. Both live
    // under `['artifacts']` so artifact mutations invalidate them, while the
    // `view` discriminator keeps their different response shapes on separate keys.
    queryKey: ['artifacts', 'command-bar', 'view', artifactsQuery],
    queryFn: () => artifacts.search(artifactsQuery) as Promise<Result[]>,
    enabled: scope === 'artifacts',
    staleTime: 15_000,
  })
  /**
   * The rows the view may render, capped — see {@link ARTIFACTS_ROW_LIMIT}.
   *
   * The LISTING state carries a group label, so the list says what it is. Without
   * one the view opened on five unexplained rows: a reader can guess they are the
   * recent ones, but nothing on screen said so, while every group in the root
   * announces itself with a header. A filtered list gets no header — what those
   * rows are is the word the reader just typed.
   */
  const artifactSlotRows = useMemo(
    () => (artifactRows ?? [])
      .slice(0, ARTIFACTS_ROW_LIMIT)
      .map(row => (artifactsQuery ? row : { ...row, groupLabel: i18nT('apps.commandBar.group_recent') })),
    [artifactRows, artifactsQuery],
  )

  /**
   * Folders view — the same two states the sessions view has, from ONE engine, for
   * the reason the artifacts view has one.
   *
   * The provider answers an empty query with every folder in the order the sidebar
   * draws them, so the listing the view lands on and the filtered list a query
   * produces are the same call with a different argument. There is deliberately no
   * {@link SESSIONS_MIN_CHARS} or {@link ARTIFACTS_MIN_CHARS} equivalent: the corpus
   * is the folder list already cached under `['chat-folders']`, so one character
   * costs a local filter rather than a round trip, and a view that refused to narrow
   * on one character would make the shortest names the hardest to reach. There is no
   * row cap either, for the same reason the sidebar draws every folder: the count is
   * the user's own filing, not a corpus that grows on its own.
   *
   * `enabled` on the scope is what keeps the root request-free. Entering the view is
   * the activation event that may pay for one folder fetch on a cold cache — the
   * same bargain the two views above make.
   */
  const folderQuery = scope === 'folders' ? debounced.trim() : ''
  const {
    data: folderRows,
    isFetching: foldersFetching,
    isError: foldersError,
    error: foldersSearchError,
    refetch: refetchFolders,
  } = useQuery({
    // Nested UNDER the corpus key, like the artifacts view above, because these rows
    // are DERIVED from `['chat-folders']` and every folder write ends in
    // `invalidateQueries({ queryKey: ['chat-folders'] })` (the create, delete and
    // update mutations in `pages/chat-sidebar/folders.ts`). React-Query matches that by key PREFIX, so a key outside
    // the corpus namespace is a cache no folder write can reach — and this view is
    // unmounted whenever the bar is closed, so its one chance to re-derive is the
    // remount, which refetches nothing that is still fresh. A folder deleted in that
    // window would otherwise stay listed and selectable for the whole `staleTime`,
    // pointing its reveal at an id the sidebar does not hold.
    //
    // The sort mode stays part of the identity: the same query in a different mode is
    // a different list, and without it a switch made in the sidebar would be served
    // from the previous order for the rest of the stale window.
    queryKey: ['chat-folders', 'command-bar', 'view', folderSortMode, folderQuery],
    queryFn: () => Promise.resolve(folders.search(folderQuery)) as Promise<Result[]>,
    enabled: scope === 'folders',
    staleTime: 15_000,
  })

  /**
   * Crewmates view — ONE engine for both of its states, like the artifacts and
   * folders views and for the same reason: the provider answers an empty query with
   * the whole roster, so the listing the view lands on and the filtered list a query
   * produces are the same call with a different argument.
   *
   * No minimum query length. The corpus is in hand once the catalog resolves, so one
   * character costs a local filter rather than a round trip — and a floor of two
   * would put the shortest crew names out of reach of their own first letter.
   *
   * No row cap either: the roster is the reader's own crew, not a corpus that grows
   * on its own, which is the argument the folders view makes about the sidebar tree.
   *
   * The running set is part of the KEY, not just a dependency of the engine memo.
   * These rows carry each mate's running state, read at the moment the provider
   * builds them — and rebuilding the engine does not re-run a resolved query, because
   * the key is what decides that. Without this the first frame to arrive after the
   * view opened would leave a "Working…" dot on a mate that had since finished, and a
   * mate that had just started would render idle.
   *
   * A SORTED name list rather than a count or a hash: two mates swapping which of
   * them is working leaves the count identical, and that is precisely the change that
   * has to reach the screen. Frames that change nothing about the roster's running
   * set — a token tick, another surface's slot — produce the same string and cost
   * nothing. The catalog fetch stays cached under its own key (MATES_STALE_MS), so a re-derivation
   * is a local re-map, not a request.
   */
  const mateQuery = scope === 'mates' ? debounced.trim() : ''
  const runningMatesKey = useMemo(() => [...runningMates].sort().join('\u0000'), [runningMates])
  const {
    data: mateRows,
    isFetching: matesFetching,
    isError: matesError,
    error: matesSearchError,
    refetch: refetchMates,
  } = useQuery({
    queryKey: ['command-bar', 'mates', mateQuery, runningMatesKey],
    queryFn: () => mates.search(mateQuery) as Promise<Result[]>,
    enabled: scope === 'mates',
    staleTime: 15_000,
    // The rows the reader is LOOKING AT, held across the key change the running set
    // causes. This view is the only one whose key moves on an event the reader did
    // not cause: a member slot finishing anywhere in the dashboard mints a new key,
    // and without this `mateRows` goes undefined, the list blanks to placeholders,
    // and the selection clamp below (`selected >= rowCount`) pulls the reader's
    // keyboard selection back to row 0 mid-typing. The re-derivation is a local
    // re-map off a cached catalog, so the stale frame it shows lasts an instant.
    //
    // HELD ONLY ACROSS THE RUNNING SET, never across the query. `prev => prev` held
    // them across both, and the second one is the defect the hold was never meant to
    // cover: once the catalog is past its stale window, the post-debounce key change
    // starts a real request, and rows answering the PREVIOUS words stayed on screen --
    // and actionable -- for as long as it took. Dropping them there is what the view
    // already does in every other state where it has no answer yet: it shows the
    // loading rows and Enter has nothing to act on.
    placeholderData: (prev, prevQuery) =>
      prevQuery?.queryKey?.[2] === mateQuery ? prev : undefined,
  })

  /**
   * WHAT THE RESULT ROWS ON SCREEN ANSWER, as a value the activation path can compare
   * against what the reader has actually typed.
   *
   * Every scoped view ranks from the DEBOUNCED query, because every one of them hits
   * the network; the root ranks from the live query and needs none of this. So for one
   * debounce interval after a keystroke, the rows in all four views answer the previous
   * query — and an Enter in that window acts on the row that was selected against it.
   * Typing `onc` and pressing Enter immediately opened `accountant`.
   *
   * The comparison is between what the view WOULD ask for the live query and what it
   * DID ask, rather than between the two query strings, because a keystroke that does
   * not change the request must not block anything. Sessions and artifacts only search
   * above a floor, so below it every query asks for the same listing: on the string
   * comparison, a one-character query in the artifacts view would have frozen Enter on
   * a listing that was the correct answer to it.
   *
   * ONE derivation for all four views. The first spelling of this guard read
   * `scope === 'mates'`, which fixed the view I was building and left the same defect
   * in its three siblings — a symptom-level fix, correctly refused as such.
   */
  const askedFor = useCallback(
    (q: string): string => {
      switch (scope) {
        // Below the floor the view shows recent sessions, which are the same rows for
        // every sub-floor query, so they answer all of them.
        case 'sessions':
          return q.length >= SESSIONS_MIN_CHARS ? q : ''
        case 'artifacts':
          return q.length >= ARTIFACTS_MIN_CHARS ? q : ''
        default:
          return q
      }
    },
    [scope],
  )

  /**
   * Whether the rows on screen are the rows for what the reader has typed.
   *
   * ONE condition, and only because the crewmates view's `placeholderData` above no
   * longer holds rows across a change of the query. Held-over rows that answer the same
   * words as the live query are not stale in the sense this guards: what moved was a
   * working dot, not what the row opens.
   */
  const rowsAnswerTheQuery = askedFor(debounced.trim()) === askedFor(query.trim())

  const slots: Slot[] = useMemo(() => {
    // The argument state lists nothing: there is one thing to do and the field is
    // where it is done, so Enter belongs to the input rather than to a row. A
    // zero-row state is already part of this surface's keyboard contract -- it is
    // what moves the focus cue onto the field -- so this needs no new affordance.
    if (argCommand) return []
    if (scope === 'sessions') {
      const engine = searchArmed ? scopedResults : recentRows
      const out: Slot[] = (engine ?? []).map(row => ({ key: row.id, tag: 'result' as const, row }))
      if (searchArmed && isError) {
        // A failed search is not an empty one. The retry was a bare <button> inside
        // the empty-state paragraph, which the keyboard path that reached this state
        // could not get to without Tabbing out of the list.
        out.push({ key: 'slot:retry', tag: 'retry' })
      } else if (searchArmed && !isFetching && out.length === 0) {
        // A query that matched nothing still has somewhere to go — back to the
        // listing — so the view never bottoms out with nothing selectable.
        out.push({ key: 'slot:clear-query', tag: 'clear-query' })
      }
      return out
    }
    if (scope === 'artifacts') {
      const out: Slot[] = artifactSlotRows.map(row => ({ key: row.id, tag: 'result' as const, row }))
      if (artifactsError) {
        out.push({ key: 'slot:retry', tag: 'retry-artifacts' })
      } else if (!artifactsFetching && out.length === 0 && artifactsQuery) {
        // A name that matched nothing still has somewhere to go — back to the
        // listing — so the view never bottoms out with nothing selectable. Gated on
        // there BEING a query: an instance with no artifacts at all has no listing
        // to return to, and offering one would be a row that does nothing.
        out.push({ key: 'slot:clear-query', tag: 'clear-query' })
      }
      return out
    }
    if (scope === 'folders') {
      const out: Slot[] = (folderRows ?? []).map(row => ({ key: row.id, tag: 'result' as const, row }))
      // The same two dead ends the views above have, told apart the same way: a
      // folder list that could not be read is a failure to retry, an empty result for
      // a query the user typed is a filter to drop. Both are ROWS so the keyboard can
      // reach them without leaving the list. The drop row is gated on there BEING a
      // query for the same reason it is above — a reader with no folders at all has no
      // listing to return to.
      if (foldersError) {
        out.push({ key: 'slot:retry', tag: 'retry-folders' })
      } else if (!foldersFetching && out.length === 0 && folderQuery) {
        out.push({ key: 'slot:clear-query', tag: 'clear-query-folders' })
      }
      return out
    }
    if (scope === 'mates') {
      const out: Slot[] = (mateRows ?? []).map(row => ({ key: row.id, tag: 'result' as const, row }))
      // The same two dead ends the views above have, told apart the same way. The
      // retry matters more here than anywhere else in this file: the catalog fetch is
      // the one this surface has no warm-cache path for, and `useAgents` records what
      // a swallowed failure on this exact endpoint cost — a failed load and a
      // one-crew install rendered identically, through six triage passes.
      if (matesError) {
        out.push({ key: 'slot:retry', tag: 'retry-mates' })
      } else if (!matesFetching && out.length === 0 && mateQuery) {
        out.push({ key: 'slot:clear-query', tag: 'clear-query-mates' })
      } else if (crewPreview && !matesFetching && out.length === 0 && mateRows !== undefined) {
        // NO CREW AT ALL, and the reader is one keystroke from fixing that. A centred
        // sentence naming the Crewmates page was the dead end here: a reader reported
        // that "nothing looks like a link", so the one thing they came to do had no
        // affordance. As a ROW it carries both halves the way `clear-query-mates` does
        // -- what is true, and what Enter does about it -- and the keyboard can reach it
        // without leaving the list.
        //
        // Gated on `mateRows !== undefined` so a cold fetch renders the skeleton rather
        // than briefly claiming the reader has no crew.
        out.push({ key: 'slot:open-crewmates', tag: 'open-crewmates' })
      }
      return out
    }
    const out: Slot[] = ranked.map(row => ({ key: row.id, tag: 'root' as const, row }))
    if (query.trim().length > 0) {
      // The agent goes FIRST among the tail rows. Every other surface in this
      // product ends in saying something to one, so the typed text having somewhere
      // to go is not a corner-of-the-screen fallback for when search failed — it is
      // the general case, and the command list above it is the shortcut layer.
      out.push({ key: ASK_SLOT_KEY, tag: 'ask' })
      out.push({ key: 'slot:fallback', tag: 'fallback' })
      // Typed text that is an ARTIFACT's name matched nothing above it: the root
      // ranks launcher rows on their own vocabulary, so "Q3 Revenue Chart" reaches
      // the artifacts view only by first typing a word like "artifact". This row is
      // the same way out the sessions corpus already had. Placed after it rather
      // than before so the row the sessions fallback has always occupied does not
      // move under a reader who navigates by position.
      out.push({ key: 'slot:fallback-artifacts', tag: 'fallback-artifacts' })
      // And the same way out for the folders corpus, for the same reason: the root
      // holds ONE row per corpus, so a typed folder name would otherwise reach
      // nothing until the reader thought to enter the view first and retype it. Last
      // of the three because a typed word is a session title or an artifact name far
      // more often than a folder name.
      out.push({ key: 'slot:fallback-folders', tag: 'fallback-folders' })
      // And the same way out for the crew, for the same reason: the root holds ONE row
      // per corpus, so a typed crew name reaches nothing until the reader thinks to
      // enter the view first and retype it. Last of the four, because a typed word is
      // a session title or an artifact name far more often than it is a crew's name —
      // and a crew name that IS typed is often distinctive enough to have already
      // matched the view row's own keywords above.
      // Behind the same preview gate as the view row: this row is a second, independent
      // way for a typed query to reach `/members`.
      if (crewPreview) out.push({ key: 'slot:fallback-mates', tag: 'fallback-mates' })
      // The recovery row exists for the dead end — a typed query that matched
      // nothing — not for every keystroke. Riding the fallback's own condition put a
      // row about switching the feature off under every successful search, and
      // ArrowUp from the top wrapped selection straight onto it.
      if (ranked.length === 0) out.push({ key: 'slot:recovery', tag: 'recovery' })
    }
    return out
  }, [argCommand, crewPreview, isError, isFetching, query, ranked, recentRows, scope, scopedResults, searchArmed, artifactSlotRows, artifactsError, artifactsFetching, artifactsQuery, folderQuery, folderRows, foldersError, foldersFetching, mateQuery, mateRows, matesError, matesFetching])

  const rowCount = slots.length
  /**
   * True while a scoped engine owes us rows.
   *
   * Distinguished from "empty" because the two need opposite treatments: a pending
   * listing renders placeholder rows that hold the list's height, where the centred
   * "Searching…" line it replaces collapsed the panel and then jumped when results
   * landed.
   */
  const scopeLoading =
    rowCount === 0 &&
    ((scope === 'sessions' && (isFetching || (listingArmed && recentRows === undefined))) ||
      (scope === 'artifacts' && (artifactsFetching || artifactRows === undefined)) ||
      (scope === 'folders' && (foldersFetching || folderRows === undefined)) ||
      (scope === 'mates' && (matesFetching || mateRows === undefined)))

  /**
   * The scoped read for the LIVE query is still in flight.
   *
   * Distinct from {@link scopeLoading}, which is about an empty list: this is true even
   * while rows for the previous query are still on screen, which is the one frame a
   * latched Enter must not fire on — the debounced query may already match what was
   * typed while the rows answering it have not landed yet.
   */
  const scopeFetching =
    (scope === 'sessions' && isFetching) ||
    (scope === 'artifacts' && artifactsFetching) ||
    (scope === 'folders' && foldersFetching) ||
    (scope === 'mates' && matesFetching)

  useEffect(() => {
    if (selected >= rowCount) setSelected(Math.max(0, rowCount - 1))
  }, [rowCount, selected, setSelected])

  return {
    scopedQuery,
    sessionsSearchError,
    refetchSessions,
    artifactsQuery,
    artifactRows,
    artifactsSearchError,
    artifactsError,
    refetchArtifacts,
    folderQuery,
    foldersError,
    foldersSearchError,
    refetchFolders,
    mateQuery,
    matesError,
    matesSearchError,
    refetchMates,
    rowsAnswerTheQuery,
    slots,
    rowCount,
    scopeLoading,
    scopeFetching,
  }
}
