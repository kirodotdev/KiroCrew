/** Backend session search (debounced, federated while a crew is connected) and the
 *  folder-name matches the search box adds to it. */
import { useState, useRef, useEffect, useMemo } from 'react'
import { api, SEARCH_MIN_CHARS } from '../../api/client'
import { folderNameText, collectFolderSubtreeIds } from '../../utils/folderTree'
import type { ChatFolder } from '../../types'

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
 */
export function useDebouncedSessionSearch<T>(
  query: string,
  transform: (sessions: { key: string; title?: string; created?: string; modified?: number; agent?: string; memory_mode?: 'persistent' | 'incognito' | 'temporary'; folder_id?: string; instance_id?: string; instance_name?: string }[]) => T,
  revalidateSignal?: string,
  federated = false,
): T | null {
  const [result, setResult] = useState<T | null>(null)
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
    let cancelled = false
    const t = setTimeout(async () => {
      try {
        const d = await fetchSessions(q)
        if (cancelled || myToken !== token.current) return
        setResult(transform(d.sessions || []))
      } catch { /* keep previous result on error */ }
      // Cleared AFTER the await: clearing first leaves a window where the debounce
      // has "finished" but the fetch is outstanding, so the effect below duplicates it.
      finally { debounceActive.current = false }
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
        setResult(transform(d.sessions || []))
      } catch { /* keep previous result on error */ }
    }, 100)
    return () => { cancelled = true; clearTimeout(t) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revalidateSignal])

  return result
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
