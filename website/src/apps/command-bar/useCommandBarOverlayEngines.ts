import { useMemo, useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../../api/client'
import type { ChatFolder } from '../../types'
import type { AppDispatch, RootState } from '../../store'
import { requestFolderReveal } from '../../store/chatSlice'
import { createArtifactsProvider } from '../../components/commandPalette/providers/artifactsProvider'
import type { ArtifactsResponse } from '../../components/commandPalette/providers/artifactsProvider'
import { createFoldersProvider, FOLDERS_STALE_MS } from './foldersProvider'
import { createMatesProvider, mateRoute } from './matesProvider'
import type { KiroCrewAgent } from '../../components/AgentSelector'
import { useFolderSortRead, type FolderSortConfigBody } from '../../hooks/useFolderSortMode'
import { useRecentsProvider } from '../../components/commandPalette/providers/recentsProvider'
import { useSessionsProvider } from '../../components/commandPalette/providers/sessionsProvider'

import { MATES_STALE_MS, type Scope } from './commandBarOverlayModel'

/**
 * The engines of the Command Bar's four views (the sessions search and listing, the
 * artifacts, folders and crewmates corpora), wired to this surface's seams, and the cache
 * entries it subscribes to without fetching (the app list, the folder list, the folder
 * sort mode). Every engine is inert until its view searches. A new view's engine, or a
 * new cache entry the bar reads, belongs here.
 */
export function useCommandBarOverlayEngines({
  scope,
  liveSlots,
  dispatch,
  navigate,
}: {
  scope: Scope
  liveSlots: RootState['dashboard']['slots']
  dispatch: AppDispatch
  navigate: (route: string) => void
}) {
  // Constructing the sessions engine is just memoized closures — it issues no
  // request until `search()` is called, and only the sessions VIEW calls it. That
  // call site, not the construction, is what the root must never reach.
  // Constructed here but INERT until the sessions view is entered: the hook's own
  // ['instances'] query would otherwise fire on a warm install the moment the root
  // opened, which is exactly the request this surface promises not to make.
  const sessions = useSessionsProvider({ active: scope === 'sessions' })
  // The sessions view's LISTING engine. Constructed here and inert: the hook reads
  // live slots out of the store and reads a localStorage preference, but issues no
  // request until `search()` is called, and the only call site is gated on the
  // scope (in `useCommandBarOverlaySlots`). Constructing it costs the root nothing;
  // entering the view is what makes it fetch.
  const recentSessions = useRecentsProvider()

  // The app list is READ, never fetched: the shell publishes its own
  // `GET /api/apps` response under this key, and `enabled: false` makes this a
  // cache subscriber that re-renders when that write lands. Fetching here would
  // reintroduce a request on any open past the stale window, which is exactly the
  // cost this surface exists to remove. Before the shell's first response the Apps
  // group is simply empty; commands and settings are local and render regardless.
  const { data: apps } = useQuery({
    queryKey: ['apps'],
    queryFn: () => api.listApps(),
    enabled: false,
  })

  // The folder list is READ the same way and for a sharper reason: `GET
  // /api/chat/folders` walks the on-disk session list synchronously to count archived
  // sessions per folder, so fetching it here would pay for a filesystem scan on every
  // command run to learn what the sidebar's own cache already holds (the WebSocket
  // seeds this key from the folder tree). A cold cache falls back to one fetch inside
  // `fileSessionInCommandFolder`.
  const { data: chatFolders } = useQuery({
    queryKey: ['chat-folders'],
    queryFn: () => api.chatFolders(),
    enabled: false,
  })
  // Held in a ref because the filing runs from an async callback, long after the render
  // that read the cache.
  const chatFoldersRef = useRef<unknown>(chatFolders)
  chatFoldersRef.current = chatFolders
  const queryClient = useQueryClient()

  // The person's folder sort mode (`dashboard.folder_sort`), read the same way as the
  // two entries above — a SUBSCRIBER to the shared `['kirocrewConfig']` key, never a
  // fetch: the shell's own read of that key holds the entry from boot and the
  // WebSocket invalidates it on a config change, so fetching here would be the
  // request-on-open this surface exists to avoid, for a value that is already in the
  // cache. Read through the sidebar hook's own derivation, so the Folders view
  // below lists in the order the sidebar draws — and says the same failed read the
  // sidebar says, above its list, when there is no body to draw from (there is no
  // sidebar on this surface to say it). A body on hand is drawn and acted on,
  // whatever the shell's last refetch did.
  const kirocrewConfigQuery = useQuery<FolderSortConfigBody>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    enabled: false,
  })
  const { mode: folderSortMode, error: folderSortError } = useFolderSortRead(kirocrewConfigQuery)

  /**
   * The artifacts view's engine.
   *
   * Built here from the palette provider's factory rather than taken from its hook,
   * for two reasons that both matter to this surface. The request is ours: the hook
   * asks for `content=1` and `snippet=1`, and `snippet=1` alone makes the server
   * read every listed artifact's body to build a preview — so a name search that
   * left it on would pay for the full-corpus content read it was avoiding. And the
   * navigation is ours: every other row in this overlay leaves through
   * `usePaletteActions().navigate`, and the hook would bring a second router
   * mechanism in beside it.
   *
   * What IS reused is the part worth reusing: how an artifact becomes a row, so a
   * result here and a result in the palette are the same row built by the same code.
   *
   * Inert on construction, like the two engines above — `fetchQuery` runs from
   * `search()`, and the only call site is gated on the artifacts scope.
   *
   * Content search is the next step, and it is this one request that changes.
   */
  const artifacts = useMemo(
    () =>
      createArtifactsProvider({
        fetchArtifacts: q =>
          queryClient.fetchQuery<ArtifactsResponse>({
            queryKey: ['artifacts', 'command-bar', 'name', q],
            queryFn: () => api.artifacts({ q: q || undefined }),
            staleTime: 15_000,
          }),
        openArtifact: slug => navigate(`/artifacts/${slug}`),
      }),
    [navigate, queryClient],
  )

  /**
   * The folders view's engine — this app's own folders corpus
   * (`./foldersProvider`), wired to THIS surface's seams.
   *
   * It lives beside this file rather than under the host palette's providers, and
   * that is the point: the host carries no Folders tab, so there is one
   * implementation of "find a folder and land on it" and the app owns it. The
   * corpus itself is hook-free precisely so the wiring stays here — React-Query for
   * the fetch, `usePaletteActions` for the route change. Every route change in this
   * overlay goes through that hook, and one component holding two navigation
   * mechanisms is how one of them ends up unexercised.
   *
   * Inert on construction, like the two engines above: the fetch runs from
   * `search()`, and the only call site is gated on the folders scope.
   */
  const folders = useMemo(
    () =>
      createFoldersProvider({
        fetchFolders: async () => {
          const rows = await queryClient.fetchQuery<ChatFolder[]>({
            // The SHARED key the sidebar and the session filing (`seedNewSession`) read,
            // so entering the view on a warm cache costs nothing and a cold one pays once.
            queryKey: ['chat-folders'],
            queryFn: () => api.chatFolders(),
            staleTime: FOLDERS_STALE_MS,
          })
          // The key is shared, so what comes back is whatever the last writer put
          // there. A non-array reaches `folders.map` inside the ordering helper and
          // throws in render, which would take the whole launcher down over a bad
          // payload that only this one view needs.
          return Array.isArray(rows) ? rows : []
        },
        revealFolder: folderId => {
          // Store write BEFORE the route change: the request is held in the store
          // precisely because the sidebar may not be mounted yet, and its consuming
          // effect runs on mount as well as on change, so an early request is
          // replayed rather than dropped.
          dispatch(requestFolderReveal(folderId))
          navigate('/chat')
        },
        // The sidebar's own mode, so the listing is the sidebar's order and not a
        // second one. A dep of the memo: a mode switch rebuilds the engine, and the
        // folders view's query keys on the mode too (`useCommandBarOverlaySlots`), so
        // the switch is never served from a 15-second-stale list in the old order.
        mode: folderSortMode,
      }),
    [dispatch, navigate, queryClient, folderSortMode],
  )

  /**
   * Which crewmates are working, keyed by crew NAME.
   *
   * Derived from the same live slot frames the root's attention group reads
   * (`useCommandBarOverlayRootRows`), so the running indicator costs this surface no
   * request and cannot disagree with what the Crewmates page shows — that page reads
   * `running || subagents_running` off these same frames.
   *
   * Keyed on `agent` rather than on the slot key, and that is sound because a member
   * slot is BORN with its crew pinned: the thread route sets `slot.agent` to the
   * member's exact name and refuses a slot whose agent is anything else. The slot key
   * would need the member's persisted `member_id` to reconstruct, which the catalog
   * rows do not carry.
   *
   * A mate with several member slots is working if ANY of them is, which is the
   * question the reader is asking: whether interrupting would land on a busy agent.
   */
  const runningMates = useMemo(() => {
    const byName = new Set<string>()
    for (const slot of liveSlots) {
      if (slot.mode !== 'member') continue
      if (!(slot.running || slot.subagents_running)) continue
      if (slot.agent) byName.add(slot.agent)
    }
    return byName
  }, [liveSlots])

  /**
   * The crewmates view's engine — this app's own roster corpus
   * (`./matesProvider`), wired to THIS surface's seams.
   *
   * Inert on construction, like the three engines above: `fetchQuery` runs from
   * `search()`, and the only call site is gated on the mates scope. That gating is
   * what keeps the root request-free, and it matters more here than for folders —
   * the catalog is not seeded into the cache by any WebSocket push, so there is no
   * warm-cache path to fall back on and an ungated read would be a real request on
   * every open.
   */
  const mates = useMemo(
    () =>
      createMatesProvider({
        fetchMates: async () => {
          const body = await queryClient.fetchQuery<{ agents?: KiroCrewAgent[] }>({
            // The SHARED key `NewCrewmateDialog` already uses for this exact read --
            // the sessionless `api.agentCatalog()`, whose answer is the GLOBAL roster.
            // A second key of this surface's own would be two caches of one response:
            // the dialog opens, fetches, and closes; the bar then refetches the same
            // bytes. (`['agents-installed']` is a DIFFERENT endpoint, `GET /api/agents`,
            // so it is not the one to join.)
            //
            // Sessionless on purpose: a session key would add whichever chat's project
            // templates are in scope, and this surface fires from anywhere in the
            // dashboard, so the crew a reader can reach must not depend on which pane
            // they last touched.
            queryKey: ['agents-catalog', 'global'],
            queryFn: () => api.agentCatalog(),
            staleTime: MATES_STALE_MS,
          })
          // The key is shared, so what comes back is whatever the last writer put
          // there. A non-array reaches `.filter` in the provider and throws in render,
          // which would take the whole launcher down over a payload only this view
          // needs.
          return Array.isArray(body?.agents) ? body.agents : []
        },
        openMate: name => navigate(mateRoute(name)),
        isRunning: name => runningMates.has(name),
      }),
    [navigate, queryClient, runningMates],
  )

  return {
    sessions,
    recentSessions,
    apps,
    chatFoldersRef,
    queryClient,
    folderSortMode,
    folderSortError,
    artifacts,
    folders,
    runningMates,
    mates,
  }
}
