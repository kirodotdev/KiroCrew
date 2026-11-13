/** Backend session search (debounced, federated while a crew is connected) and the
 *  folder-name matches the search box adds to it. */
import { useState, useRef, useEffect, useMemo } from 'react'
import { api, SEARCH_MIN_CHARS } from '../../api/client'
import { folderNameText, collectFolderSubtreeIds } from '../../utils/folderTree'
import type { ChatFolder } from '../../types'

/** How much of this machine's session store a search read, present only when its
 *  scan window (`session.search_scan_window`) left older sessions unsearched.
 *  `local` is set by the federated search, whose rows also come from other crews
 *  while the count is this machine's alone. */
export interface SessionSearchScan { searched: number; total: number; local: boolean }

/** The response's `scan`, or `null` when it is absent or not two sane counts. */
export function sessionSearchScan(d: unknown): SessionSearchScan | null {
  const scan = (d as { scan?: unknown } | null | undefined)?.scan as
    | { searched?: unknown; total?: unknown; scope?: unknown }
    | undefined
  if (!scan || typeof scan !== 'object') return null
  const { searched, total } = scan
  if (!Number.isSafeInteger(searched) || !Number.isSafeInteger(total)) return null
  const s = searched as number
  const t = total as number
  return s >= 0 && s < t ? { searched: s, total: t, local: scan.scope === 'local' } : null
}

/** What the sidebar keeps from one session-search reply. `ranks` is the backend
 *  relevance rank per local slot key (0 = best); `snippets` is the match-centred
 *  text the server returned for a hit, already redacted there. Both are keyed by
 *  the LOCAL key, so a peer row never reads them. */
export interface SidebarSearchHits {
  ranks: Map<string, number>
  snippets: Map<string, string>
  scan: SessionSearchScan | null
}

/** One reply's rows as {@link SidebarSearchHits}. First wins on a canonical-key
 *  collision, so a duplicate file cannot demote the better-ranked entry or swap in
 *  its snippet. A remote row (`instance_id`) names a session on another gateway and
 *  is skipped: its key is not a local key. */
export function sidebarSearchHits(
  sessions: { key: string; snippet?: unknown; instance_id?: string }[],
  scan: SessionSearchScan | null,
): SidebarSearchHits {
  const ranks = new Map<string, number>()
  const snippets = new Map<string, string>()
  sessions.forEach((s, i) => {
    if (s.instance_id) return
    const key = s.key.replace(/^dashboard_/, '')
    if (ranks.has(key)) return
    ranks.set(key, i)
    const snippet = typeof s.snippet === 'string' ? s.snippet.trim() : ''
    if (snippet) snippets.set(key, snippet)
  })
  return { ranks, snippets, scan }
}

/**
 * Debounced backend session-content search.  Returns `null` until the first
 * response arrives (or whenever the query drops below `SEARCH_MIN_CHARS`),
 * and keeps the previous result visible while a new query is in flight so
 * the list doesn't blank out between keystrokes.
 *
 * `revalidateSignal` re-runs the search for the SAME query whenever it changes.
 * Callers feed a digest of the session set + titles, so a rename — which mutates
 * a title but not the query — refreshes the backend result set. Keep the digest
 * scoped to key/title and NOT status, or idle status ticks spam `sessionsSearch`.
 *
 * `transform` also receives the response's scan-window note
 * (see {@link sessionSearchScan}), so a caller can say when older sessions were
 * not searched.
 */
export function useDebouncedSessionSearch<T>(
  query: string,
  transform: SessionSearchTransform<T>,
  revalidateSignal?: string,
  federated = false,
): T | null {
  return useDebouncedSessionSearchState(query, transform, revalidateSignal, federated).result
}

type SessionSearchTransform<T> = (sessions: { key: string; title?: string; snippet?: string; created?: string; modified?: number; agent?: string; memory_mode?: 'persistent' | 'incognito' | 'temporary'; folder_id?: string; instance_id?: string; instance_name?: string }[], scan: SessionSearchScan | null) => T

/**
 * `useDebouncedSessionSearch` plus whether the latest search failed.
 *
 * A failed search drops the previous result instead of keeping it: the old rows
 * answered a different query, so showing them under the new one says the search
 * worked when it did not. `failed` lets the caller say so instead. Only the
 * newest request may write either value, so a slow, older response (success or
 * failure) never lands over a newer one.
 */
export function useDebouncedSessionSearchState<T>(
  query: string,
  transform: SessionSearchTransform<T>,
  revalidateSignal?: string,
  federated = false,
): { result: T | null; failed: boolean } {
  const [state, setState] = useState<{ result: T | null; failed: boolean }>({ result: null, failed: false })
  const setResult = (result: T | null) => setState({ result, failed: false })
  const setFailed = () => setState({ result: null, failed: true })
  const token = useRef(0)
  const queryRef = useRef(query)
  queryRef.current = query
  const debounceActive = useRef(false)
  // Read via ref so a connect/disconnect mid-debounce doesn't re-fire the
  // keystroke effect; the NEXT search simply takes the new route.
  const federatedRef = useRef(federated)
  federatedRef.current = federated

  // One fetch for both effects below: the federated endpoint merges the local
  // gateway with every connected remote instance (rank-interleaved, remote rows
  // tagged instance_id/_name); any failure — including the 403 when the
  // instances feature is off — falls back to the plain local search, which is
  // always the floor. Unreachable peers are logged, not surfaced: only
  // CONNECTED peers are fanned out, so this is a rare transient, and the local
  // results still render.
  const fetchSessions = async (q: string) => {
    if (!federatedRef.current) return api.sessionsSearch(q)
    try {
      const d = await api.instancesSearchSessions(q)
      if (Array.isArray(d?.unreachable) && d.unreachable.length) {
        // eslint-disable-next-line no-console -- the only record that a CONNECTED peer silently dropped out of the merged results; surfacing it would nag about a transient the local floor already covered
        console.warn('[sidebar] federated session search: unreachable instances', d.unreachable)
      }
      return d
    } catch {
      return api.sessionsSearch(q)
    }
  }

  // Debounced: fires 250ms after the last query keystroke.
  useEffect(() => {
    const q = query.trim()
    const myToken = ++token.current
    if (q.length < SEARCH_MIN_CHARS) { setResult(null); debounceActive.current = false; return }
    debounceActive.current = true
    // A failure described the PREVIOUS query; drop the flag for the new one
    // while its request is pending, but keep any result on screen.
    setState(s => (s.failed ? { ...s, failed: false } : s))
    let cancelled = false
    const t = setTimeout(async () => {
      try {
        const d = await fetchSessions(q)
        if (cancelled || myToken !== token.current) return
        setResult(transform(d.sessions || [], sessionSearchScan(d)))
      } catch {
        if (cancelled || myToken !== token.current) return
        setFailed()
      }
      // Cleared AFTER the await: clearing first leaves a window where the debounce
      // has "finished" but the fetch is outstanding, so the effect below duplicates it.
      // A cancelled run leaves it alone: the cleanup already cleared it, and the
      // NEXT query's debounce may have set it again since.
      finally { if (!cancelled) debounceActive.current = false }
    }, 250)
    return () => { cancelled = true; clearTimeout(t); debounceActive.current = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query])

  // Trailing-throttled re-run on signal change. Compares values rather than
  // flipping a flag so it survives StrictMode's double-mount.
  const prevSignal = useRef(revalidateSignal)
  useEffect(() => {
    if (prevSignal.current === revalidateSignal) return
    prevSignal.current = revalidateSignal
    let cancelled = false
    const t = setTimeout(async () => {
      // Preconditions are re-read here, not at effect time: only a signal change or
      // unmount clears this timer, so a keystroke would otherwise scan a stale query.
      const q = queryRef.current.trim()
      if (q.length < SEARCH_MIN_CHARS) return
      // A pending debounce or in-flight fetch serves this same query already.
      if (debounceActive.current) return
      const myToken = ++token.current
      try {
        const d = await fetchSessions(q)
        if (cancelled || myToken !== token.current) return
        setResult(transform(d.sessions || [], sessionSearchScan(d)))
      } catch {
        if (cancelled || myToken !== token.current) return
        setFailed()
      }
    }, 100)
    return () => { cancelled = true; clearTimeout(t) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revalidateSignal])

  return state
}

/** The backend ranking while the query is long enough, and folders whose name matches. */
export function useSearchMatches({ slotFilter, slotSearchRanks, folders, isFolderHidden }: {
  slotFilter: string
  slotSearchRanks: Map<string, number> | null
  folders: ChatFolder[]
  isFolderHidden: (f: ChatFolder) => boolean
}) {
  // The backend relevance ranking, live only while the query is long enough to
  // have been sent. Shared by the search dimension's row predicate and by
  // filteredSlots' sort, so the two cannot disagree about when ranking is on.
  const searchRanked = useMemo(
    () => (slotFilter.trim().length >= SEARCH_MIN_CHARS ? slotSearchRanks : null),
    [slotFilter, slotSearchRanks],
  )

  /**
   * Folders whose OWN NAME contains the search box's text, plus every folder
   * nested inside them. `null` when the box is empty or nothing matched.
   *
   * This is what makes typing a folder's name into the sidebar find the FOLDER and
   * not just sessions that happen to mention it. Without it the text filter only
   * ever asked about a session's own fields, so searching a container's name hid
   * every session in it — the row you were looking for disappeared as you typed
   * its parent's name.
   *
   * The set is the whole SUBTREE, not just the matched folder: naming a parent is
   * how you ask for what is under it, and a match that stopped at the parent's own
   * direct children would drop its grandchildren for no reason a reader could
   * state. Plain substring, not `fuzzyMatch`, to match how the same box already
   * tests a session title — one box, one notion of "matches".
   */
  const folderNameMatchIds = useMemo(() => {
    const q = slotFilter.trim().toLowerCase()
    if (!q) return null
    // `isFolderHidden` is part of the predicate, not a tidy-up. A "hide when empty"
    // folder renders no row, so counting its name as a match would make the lane's
    // empty state say "the folders above matched by name" with no folder above it --
    // the same contradiction the wording exists to remove, arriving through a
    // different door. It costs nothing in session retention either: the hide only
    // applies while the subtree holds no active session, so a hidden folder has none
    // to keep.
    const matched = folders.filter(f => !isFolderHidden(f) && folderNameText(f).toLowerCase().includes(q))
    if (matched.length === 0) return null
    const ids = new Set<string>()
    for (const f of matched) for (const id of collectFolderSubtreeIds(folders, f.id)) ids.add(id)
    return ids
  }, [slotFilter, folders, isFolderHidden])
  return { searchRanked, folderNameMatchIds }
}
