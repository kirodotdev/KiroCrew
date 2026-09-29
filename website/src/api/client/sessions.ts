/**
 * Agent sessions under /api/sessions: restarting every session runtime, the
 * per-session memory and CPU readout, and stored transcript history (list,
 * search, detail, delete, clear).
 */

import type { ClientTransport } from './transport'
import type { CrewBoardAction, CrewBoardActionResult, WorkBoardResponse } from '../crewBoard'

export const SEARCH_MIN_CHARS = 2  // backend session search threshold (must match kiro_crew.history.SEARCH_MIN_CHARS)

export function createSessionsEndpoints({ get, post, del, j }: ClientTransport) {
  const runtimes = {
    restartSessions: () =>
      post('/api/sessions/restart').then(j) as Promise<{
        ok: boolean
        sessions_reset: number
        mcp_synced: number
        /** false when the MCP reconcile FAILED before the restart: sessions did
         *  restart, but against a config that may not match the sources. */
        mcp_sync_ok: boolean
      }>,
    sessionsMemory: () => fetch('/api/sessions/memory').then(j) as Promise<{
      sessions: {
        key: string; title: string; slot_key: string; untitled: boolean
        agent: string; pid: number | null; owns_runtime: boolean; prompts: number
        channel: string
        /**
         * Live sessions sharing this row's runtime; 1 when exclusive. Optional
         * because an older gateway does not send it — absent reads as exclusive,
         * which is the pre-sharing shape rather than a guess in either direction.
         */
        sharers?: number
        rss_mb: number | null; procs: number | null; mcp: number | null
        cpu_cores: number | null; uptime_s: number | null
        credits: number | null; turns: number | null
        /**
         * The session that opened this one through session_create, as the child's
         * own crew log records it; null for a session nobody created. `key` is the
         * creator's live session key when it is running (the edge the table nests
         * on) and null when it is not, so the citation outlives the creator.
         */
        parent: { slot: string; key: string | null } | null
      }[]
      tasks: {
        id: string; task: string; agent: string; parent: string
        rss_mb: number; peak_rss_mb: number; cpu_cores: number
        procs: number | null; mcp: number | null
        started_at: number; shared: boolean; pid: number | null; sampled: boolean
      }[]
      totals: {
        rss_mb: number; runtimes: number; host_mb: number | null
        host_pct: number | null; rss_is_upper_bound: boolean
        /**
         * Whether the store held more session logs than the lineage scan admits
         * (`lineage_cap`), on this sample; false within the cap or with the crew
         * log off (cap 0 then). The live sessions' logs are read first, so what
         * went unread is closed sessions' logs: no row on this page is affected.
         * A fact, not a count: counting would mean walking the whole store.
         */
        lineage_over_cap: boolean
        lineage_cap: number
      }
      history: { t: number; mb: number }[]
    }>,
  }

  const history = {
    // Sessions (history)
    // `excludeOpen` drops sessions already open as a tab — for the sidebar's
    // Older-sessions pane, which is the complement of the tab list above it.
    // `userOnly` drops machine namespaces (`subagent_`, `wf_`, …), whose transcripts
    // have no title and so render their own storage key as one. NOT `taskrunner_`: that
    // namespace also holds real conversations, so the server keeps it listed.
    // Both off by default: every other caller wants the full inventory.
    sessions: (limit = 30, offset = 0, preview = false, excludeOpen = false, userOnly = false) => fetch('/api/sessions?limit=' + limit + '&offset=' + offset + (preview ? '&preview=1' : '') + (excludeOpen ? '&exclude_open=1' : '') + (userOnly ? '&user_only=1' : '')).then(j),
    sessionsSearch: (q: string, limit = 50) => fetch('/api/sessions/search?q=' + encodeURIComponent(q) + '&limit=' + limit).then(j),
  }

  const historyDetail = {
    sessionDetail: (key: string) => fetch('/api/sessions/' + encodeURIComponent(key)).then(j),
    deleteSession: (key: string) => del('/api/sessions/' + encodeURIComponent(key)).then(j),
    clearSessions: () => del('/api/sessions').then(j),
  }

  const crewBoard = {
    /**
     * The Crew page's work-item board for ONE conductor.
     *
     * A MASKED projection over the work ledger, deliberately not the conductor's
     * own `/api/work-ledger` route: that path is in the gateway's strict-internal
     * list (MCP callers only) and its rows carry `worker_session_key`, which may
     * not reach a browser. Spelled `/api/crew-board` rather than under
     * `/api/work-ledger/` because that list matches by PREFIX, so a sub-path would
     * silently inherit MCP-only auth and 403 every call from here.
     */
    crewBoard: (conductor: string) =>
      get(`/api/crew-board?conductor=${encodeURIComponent(conductor)}`).then(j) as Promise<WorkBoardResponse>,
    /**
     * Act on one ORPHANED item. The worker session key is never sent and never
     * returned: the server resolves it from the store, which is what lets this call
     * stop a session the masked read deliberately does not name. A non-orphaned
     * item answers 409, so a click made from a stale poll is refused rather than
     * quietly doing nothing.
     */
    crewBoardAction: (conductor: string, itemId: string, action: CrewBoardAction) =>
      post('/api/crew-board/action', { conductor, item_id: itemId, action })
        .then(j) as Promise<CrewBoardActionResult>,
  }

  return { runtimes, history, historyDetail, crewBoard }
}
