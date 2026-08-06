import { createElement } from 'react'
import { MessageSquare } from 'lucide-react'
import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'

import { api } from '../../../api/client'
import { useAppDispatch } from '../../../store'
import { resumeFromHistory } from '../../../store/chatSlice'
import { fuzzyMatch, makeScoreThenNameComparator, substringIndices } from '../../../utils/fuzzyMatch'
import { i18nT } from '../../../i18n/t'
import type { Result, ResourceProvider } from '../types'

/**
 * Sessions provider for the Search Everywhere command palette.
 *
 * Backs the **Sessions** tab. Wraps `api.sessionsSearch(q)` (the existing
 * `/api/sessions/search` full-text endpoint) and maps each hit to a
 * {@link Result}:
 *  - `onActivate` (Enter) — open / switch to the session
 *    (`resumeFromHistory`, which re-attaches an existing slot or opens a new
 *    one optimistically).
 *  - `onCmdActivate` (⌘Enter) — open the session in a split pane (Session
 *    Grid). The grid opener is injected by the palette host (it lives in a
 *    different feature surface and may be absent in some builds); when it is
 *    not supplied, ⌘Enter is simply unbound for session rows.
 *
 * The backend already does the relevance filtering, so the {@link fuzzyMatch}
 * pass here is only for **client-side highlight indices** on the returned
 * titles (per the §2 design + `frontend-security` lint rule — highlights render
 * as React `<mark>` nodes keyed off `indices`, never as HTML strings). Title
 * matches additionally bias the client-side ordering; non-title (body-only)
 * matches are kept with a neutral score so backend results are never dropped.
 */

const PROVIDER_ID = 'sessions'
/**
 * Catalog KEY for the palette scope tab, resolved where the provider object is
 * BUILT (never here — this is module scope, so an `i18nT()` call would freeze the
 * boot language).
 *
 * Reuses `nav.sessions` rather than adding a tenth `Sessions` entry: the scope tab
 * and the nav rail item name the same surface with the same word, so a locale that
 * renders one differently from the other would be inconsistent, not nuanced.
 */
const PROVIDER_LABEL_KEY = 'nav.sessions'

/** Cache server responses briefly so retyping the same query is free. */
const SESSIONS_STALE_MS = 30_000

/**
 * Shortest query the backend will actually search. Mirrors `SEARCH_MIN_CHARS`
 * in `history.py`, where `/api/sessions/search` returns an empty list below the
 * threshold. Enforced here too so a one-character query costs no round trip at
 * all — behavior-preserving, because the response was already always empty.
 *
 * An EMPTY query is exempt: it is the recents/quick-switcher listing, not a
 * search, and the endpoint answers it.
 */
const SESSIONS_MIN_QUERY_CHARS = 2

/**
 * One session as returned by `/api/sessions/search`. `api.sessionsSearch` is
 * loosely typed at the client layer, so we pin the fields the provider reads.
 */
export interface SessionSearchItem {
  key: string
  title?: string
  created?: string
  modified?: number
  agent?: string
  /** Match-centered content snippet, present when the hit was in the body. */
  snippet?: string
  /** Folder the session is filed under, when any (maps to a chip in the row). */
  folder_id?: string
  memory_mode?: 'persistent' | 'incognito' | 'temporary'
  clean_mode?: boolean
}

/** Shape of the `/api/sessions/search` response envelope. */
export interface SessionSearchResponse {
  sessions?: SessionSearchItem[]
}

/** A session reference passed to the open / split-pane callbacks. */
export interface SessionRef {
  key: string
  title: string
}

/**
 * Injectable dependencies for {@link createSessionsProvider}. Keeping the
 * concrete provider free of React hooks makes it unit-testable with a plain
 * mock fetch + open callbacks; the {@link useSessionsProvider} hook wires the
 * real React-Query + Redux implementations.
 */
export interface SessionsProviderDeps {
  /** Fetch search hits for a query (React-Query-cached in the hook). */
  fetchSessions: (query: string) => Promise<SessionSearchResponse>
  /** Fetch chat folders for folder-chip labels. Optional (rows just omit the
   * chip when absent) so pure tests don't have to stub it. */
  fetchFolders?: () => Promise<{ id: string; name: string }[]>
  /** Open / switch to a session (Enter). */
  openSession: (ref: SessionRef) => void
  /** Open a session in a split pane / Session Grid (⌘Enter). Optional. */
  openInSplit?: (ref: SessionRef) => void
}

function sessionIcon() {
  return createElement(MessageSquare, { className: 'lucide-inline' })
}

/**
 * Build the Sessions {@link ResourceProvider} from injected dependencies.
 * Pure (no hooks) so it can be exercised directly in tests.
 */
export function createSessionsProvider(deps: SessionsProviderDeps): ResourceProvider {
  const { fetchSessions, fetchFolders, openSession, openInSplit } = deps

  return {
    id: PROVIDER_ID,
    // A GETTER, not a plain call: the provider object is built inside a `useMemo`
    // whose deps do not include the language, so `label: i18nT(...)` would resolve
    // once and keep the pre-switch wording forever. `LanguageProvider` forces a
    // re-RENDER via `cloneElement` (it deliberately does NOT remount — see its own
    // comment rejecting `key={active}`), and a re-render does not recompute a memo.
    // An accessor moves the lookup to the consumer's render, where the tab strip
    // reads it. Satisfies `ResourceProvider.label: string`.
    get label() { return i18nT(PROVIDER_LABEL_KEY) },
    icon: sessionIcon(),
    async search(query: string): Promise<Result[]> {
      const q = query.trim()
      if (q.length > 0 && q.length < SESSIONS_MIN_QUERY_CHARS) return []
      // Folders resolve in parallel with the search; a folders failure only
      // costs the chips, never the results.
      const [data, folders] = await Promise.all([
        fetchSessions(q),
        fetchFolders ? fetchFolders().catch(() => []) : Promise.resolve([]),
      ])
      const folderName = (fid?: string): string | undefined =>
        fid ? folders.find((f) => f.id === fid)?.name : undefined
      const sessions = data?.sessions ?? []

      const results: Result[] = sessions.map((s) => {
        const title = s.title || s.key
        // Highlight + client-side rank bias; never used to drop backend hits.
        const match = fuzzyMatch(q, title)
        const ref: SessionRef = { key: s.key, title }
        // Body match: show the snippet (why it surfaced) with the query
        // highlighted; else fall back to the agent name.
        const snippet = s.snippet?.trim()
        const subIdx = snippet ? substringIndices(q, snippet) : undefined
        return {
          id: `${PROVIDER_ID}:${s.key}`,
          providerId: PROVIDER_ID,
          title,
          subtitle: snippet || s.agent || undefined,
          subtitleIndices: subIdx && subIdx.length ? subIdx : undefined,
          folder: folderName(s.folder_id),
          icon: sessionIcon(),
          score: match ? match.score : 0,
          indices: match ? match.indices : [],
          // Declarative Enter contract (§2). The central
          // `dispatchEnter` in CommandPalette routes on this; for sessions
          // both Enter and ⌘Enter open/switch to the session (no distinct
          // modifier action). `onActivate`/`onCmdActivate` are kept
          // as the payload-bound execution path (`open-session` invokes
          // `onActivate`) and as the legacy/mouse fallback.
          enter: { kind: 'open-session', sessionKey: s.key, title },
          onActivate: () => openSession(ref),
          onCmdActivate: openInSplit ? () => openInSplit(ref) : undefined,
        }
      })

      // Title matches first, then deterministic name order. Skip the re-rank on
      // an empty query so the backend's recency ordering is preserved (Sessions
      // tab + All-tab recents rely on it).
      if (q.length > 0) {
        results.sort(makeScoreThenNameComparator<Result>(r => r.score, r => r.title))
      }
      return results
    },
  }
}

/**
 * React hook that returns a live Sessions provider wired to React-Query and
 * the chat store.
 *
 * Per the `use-react-query` lint rule the server fetch goes through
 * React-Query with the key `['palette', 'sessions', q]` (mirrors
 * `SkillPickerMenu`'s `['skills']` keying). `queryClient.fetchQuery` is used
 * rather than `useQuery` because a {@link ResourceProvider}'s `search` is an
 * imperative call from the palette, not a render-time subscription — the cache
 * (key + `staleTime`) is still shared with any `useQuery(['palette','sessions',q])`.
 *
 * @param opts.openInSplit - Optional Session Grid opener supplied by the
 *   palette host; when omitted, ⌘Enter is unbound for session rows.
 */
export function useSessionsProvider(opts?: {
  openInSplit?: (ref: SessionRef) => void
}): ResourceProvider {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const openInSplit = opts?.openInSplit

  return useMemo(
    () =>
      createSessionsProvider({
        fetchSessions: (q) =>
          queryClient.fetchQuery<SessionSearchResponse>({
            queryKey: ['palette', 'sessions', q],
            queryFn: () => api.sessionsSearch(q),
            staleTime: SESSIONS_STALE_MS,
          }),
        // Shared ['chat-folders'] cache (sidebar + recents use the same key),
        // so the chip lookup is usually a cache hit.
        fetchFolders: () =>
          queryClient.fetchQuery<{ id: string; name: string }[]>({
            queryKey: ['chat-folders'],
            queryFn: () => api.chatFolders(),
            staleTime: SESSIONS_STALE_MS,
          }),
        openSession: (ref) => {
          void dispatch(resumeFromHistory(ref))
          // The palette can be opened from ANY page (artifacts, settings, …);
          // resumeFromHistory only activates the slot in the store, so land
          // the user on the chat surface where that slot renders.
          navigate('/chat')
        },
        openInSplit,
      }),
    [dispatch, navigate, queryClient, openInSplit],
  )
}
