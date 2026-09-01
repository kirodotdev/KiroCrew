import { createElement } from 'react'
import { MessageSquare } from 'lucide-react'
import { useMemo, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../../../api/client'
import { useAppDispatch, useAppSelector } from '../../../store'
import { resumeFromHistory } from '../../../store/chatSlice'
import { useSelectInstance } from '../../../hooks/useSelectInstance'
import { fuzzyMatch, substringIndices } from '../../../utils/fuzzyMatch'
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
export const SESSIONS_MIN_QUERY_CHARS = 2

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
  /** Owning remote instance, present only on federated-search rows. */
  instance_id?: string
  /** Display name of the owning remote instance (raw, never translated). */
  instance_name?: string
}

/** Shape of the `/api/sessions/search` response envelope. */
export interface SessionSearchResponse {
  sessions?: SessionSearchItem[]
}

/** A session reference passed to the open / split-pane callbacks. */
export interface SessionRef {
  key: string
  title: string
  /** Set for a remote instance's session: activating it switches panes. */
  instanceId?: string
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
    // Declared from the SAME constant the search short-circuit enforces below,
    // so the palette's "keep typing" empty state and the provider's behavior
    // cannot drift apart.
    minQueryChars: SESSIONS_MIN_QUERY_CHARS,
    async search(query: string): Promise<Result[]> {
      const q = query.trim()
      if (q.length > 0 && q.length < SESSIONS_MIN_QUERY_CHARS) return []
      // Folders resolve in parallel with the search. Folder chips are cosmetic,
      // so a folders failure must NEVER blank the session results — but
      // swallowing it as a bare `[]` silently conflated "the folder fetch
      // FAILED" with "there are no folders", hiding a real backend error. Keep
      // the graceful degradation (results render, chips omitted) but surface the
      // failure distinctly via a log instead of masquerading as an empty list.
      const [data, folders] = await Promise.all([
        fetchSessions(q),
        fetchFolders
          ? fetchFolders().catch((err) => {
              // eslint-disable-next-line no-console -- the distinct signal the comment above requires: results still render, so a swallowed folder failure is otherwise indistinguishable from "no folders"
              console.warn(
                '[palette] session folder fetch failed; rendering results without folder chips',
                err,
              )
              return []
            })
          : Promise.resolve([]),
      ])
      const folderName = (fid?: string): string | undefined =>
        fid ? folders.find((f) => f.id === fid)?.name : undefined
      const sessions = data?.sessions ?? []

      // Backend position per row id, used as the sort tiebreak below. Kept in a
      // parallel Map rather than on the row itself so the shared `Result` type
      // (../types) is not widened with a field only this provider can populate.
      const backendIndexById = new Map<string, number>()

      const results: Result[] = sessions.map((s, backendIndex) => {
        const title = s.title || s.key
        // Highlight + client-side rank bias; never used to drop backend hits.
        const match = fuzzyMatch(q, title)
        const remote = Boolean(s.instance_id)
        const ref: SessionRef = { key: s.key, title, instanceId: s.instance_id }
        // Body match: show the snippet (why it surfaced) with the query
        // highlighted; else fall back to the agent name. Remote rows prefix the
        // owning instance's raw name so identical titles from two gateways are
        // distinguishable at a glance.
        const snippet = s.snippet?.trim()
        const subIdx = snippet ? substringIndices(q, snippet) : undefined
        const baseSubtitle = snippet || s.agent || undefined
        const prefix = remote ? `${s.instance_name || s.instance_id} · ` : ''
        const subtitle = remote
          ? [s.instance_name || s.instance_id, baseSubtitle].filter(Boolean).join(' · ')
          : baseSubtitle
        // Highlight offsets are computed against the bare snippet; a remote
        // row's instance-name prefix shifts the snippet within the rendered
        // subtitle, so shift the offsets by the prefix length instead of
        // dropping the highlights (identical-looking rows should behave the
        // same). Only applicable when the snippet is actually the subtitle
        // body (subIdx exists only when a snippet does).
        const shiftedIdx =
          subIdx && subIdx.length
            ? remote && snippet
              ? subIdx.map((i) => i + prefix.length)
              : subIdx
            : undefined
        const id = remote ? `${PROVIDER_ID}:${s.instance_id}:${s.key}` : `${PROVIDER_ID}:${s.key}`
        backendIndexById.set(id, backendIndex)
        return {
          id,
          providerId: PROVIDER_ID,
          title,
          subtitle,
          subtitleIndices: shiftedIdx,
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
          // A remote session cannot open in the LOCAL split grid — its
          // transcript lives on the other gateway. A bare `undefined` here is
          // NOT enough: the palette's dispatchEnter falls back to onActivate
          // when onCmdActivate is absent, so ⌘Enter would silently switch
          // panes. Bind an explicit no-op so the modifier chord stays inert
          // on remote rows.
          onCmdActivate: remote ? () => {} : openInSplit ? () => openInSplit(ref) : undefined,
        }
      })

      // Title matches first, then the BACKEND's relevance order as the tiebreak
      // (issue #4568). `search_sessions` already ranked these rows (weighted
      // occurrence counts, phrase bonus, recency boost); an alphabetical
      // fallback threw that ranking away whenever every hit was a body match
      // (all scores 0 — e.g. searching a PR number that appears in transcripts
      // but never in a title). Backend index is just as deterministic for
      // rendering, and strictly more useful. Deliberately NOT the shared
      // `makeScoreThenNameComparator`: its name tiebreak is right for the nine
      // providers that score locally, wrong here where the server has already
      // ranked. Skip the re-rank on an empty query so the backend's recency
      // ordering is preserved as-is (Sessions tab + All-tab recents rely on it).
      if (q.length > 0) {
        results.sort(
          (a, b) =>
            b.score - a.score ||
            (backendIndexById.get(a.id) ?? 0) - (backendIndexById.get(b.id) ?? 0),
        )
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
 * @param opts.active - Whether this provider's own background queries may run.
 *   Defaults to true, which is the palette's behaviour: it mounts its providers
 *   when the palette opens and searching is the point. A host that constructs the
 *   provider BEFORE the user has asked for session search -- a launcher whose first
 *   page is deliberately request-free -- passes false until the search surface is
 *   actually entered, so merely building the provider issues nothing. `search()`
 *   is unaffected either way; it is an imperative call, not a subscription.
 */
export function useSessionsProvider(opts?: {
  openInSplit?: (ref: SessionRef) => void
  active?: boolean
}): ResourceProvider {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const openInSplit = opts?.openInSplit
  const active = opts?.active ?? true
  // True when at least one remote instance holds a live connection; drives the
  // federated-vs-local endpoint choice per keystroke without rebuilding the
  // provider (read via ref inside the memoized fetch). Guarded read: the
  // palette mounts in many test harnesses whose partial stores omit the
  // instances slice.
  const hasWarmInstances = useAppSelector(
    (s) => Object.keys(s.instances?.warm ?? {}).length > 0,
  )
  const hasWarmRef = useRef(hasWarmInstances)
  hasWarmRef.current = hasWarmInstances
  // Shared ['instances'] cache (same key the tab bar subscribes to) feeds the
  // shared select-and-maybe-reconnect semantics; enabled only while a warm
  // connection exists, so a peerless install never issues the query (and never
  // hits the 403 when the instances feature is disabled).
  const instancesQuery = useQuery({
    queryKey: ['instances'],
    queryFn: () => api.listInstances(),
    // `active` is what keeps a request-free host request-free: constructing the
    // provider must not subscribe anything until its surface is entered.
    enabled: active && hasWarmInstances,
  })
  // Memoize the `[]` fallback so its identity is stable across renders (same
  // pattern as InstanceTabBar) — it feeds selectInstance's useCallback deps.
  const instancesData = instancesQuery.data?.instances
  const instances = useMemo(() => instancesData ?? [], [instancesData])
  const { selectInstance } = useSelectInstance(instances)

  return useMemo(
    () =>
      createSessionsProvider({
        fetchSessions: (q) =>
          queryClient.fetchQuery<SessionSearchResponse>({
            // The endpoint is part of the key: federated and local replies for
            // the same q are different result sets and must not share a cache
            // entry across a connect/disconnect.
            queryKey: ['palette', 'sessions', hasWarmRef.current ? 'federated' : 'local', q],
            queryFn: async () => {
              if (!hasWarmRef.current) return api.sessionsSearch(q)
              try {
                // Backend merges local + every connected peer (rank-interleaved),
                // so this REPLACES the local call rather than adding to it.
                return (await api.instancesSearchSessions(q)) as SessionSearchResponse
              } catch {
                // 403 (instances feature off), a peerless hub mid-teardown, or
                // any transient failure: the local search is always the floor.
                return api.sessionsSearch(q)
              }
            },
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
          if (ref.instanceId) {
            // A remote session lives on another gateway: switch to (and, if
            // needed, reconnect) that instance's pane. Deep-linking into the
            // specific session inside the embedded SPA is a follow-up — the
            // iframe protocol has no open-session message yet.
            selectInstance(ref.instanceId)
            return
          }
          void dispatch(resumeFromHistory(ref))
          // The palette can be opened from ANY page (artifacts, settings, …);
          // resumeFromHistory only activates the slot in the store, so land
          // the user on the chat surface where that slot renders.
          navigate('/chat')
        },
        openInSplit,
      }),
    [dispatch, navigate, queryClient, openInSplit, selectInstance],
  )
}
