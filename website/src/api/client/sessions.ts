/**
 * Agent sessions under /api/sessions: restarting every session runtime, the
 * per-session memory and CPU readout, and stored transcript history (list,
 * search, detail, delete, clear).
 */

import type { ClientTransport } from './transport'
import type { CrewBoardAction, CrewBoardActionResult, WorkBoardResponse } from '../crewBoard'

export const SEARCH_MIN_CHARS = 2  // backend session search threshold (must match kiro_crew.history.SEARCH_MIN_CHARS)

export function createSessionsEndpoints({ get, post, del, j, jfetch: fetch, jCrewBoard }: ClientTransport) {
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
         *
         * `ancestor` is true when `key` is the nearest OPEN ancestor instead of the
         * creator in `slot`, which happens once the creator itself closes: the row
         * nests under the session that owns the run and the citation still names the
         * creator that is gone.
         */
        parent: { slot: string; key: string | null; ancestor?: boolean } | null
      }[]
      tasks: {
        id: string; task: string; agent: string; parent: string
        /** The key a nested task names as its `parent`. Absent from an older gateway. */
        session_key?: string
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
    // Every transcript, open or closed, minted under one short name (`chat-7`),
    // oldest first. A slot number is reused across gateway generations, so one
    // name can match several.
    sessionsNamed: (name: string) => fetch('/api/sessions?limit=200&name=' + encodeURIComponent(name))
      .then(j) as Promise<{ sessions: Array<{ key: string; title?: string }> }>,
  }

  const historyDetail = {
    sessionDetail: (key: string) => fetch('/api/sessions/' + encodeURIComponent(key)).then(j),
    // One session's key and title, or null when no transcript exists (the
    // route's 404). A cheap per-key probe: it reads no transcript body. `key` is
    // the transcript stem, which is what a resume takes.
    sessionMeta: (key: string) => fetch('/api/sessions/' + encodeURIComponent(key) + '/meta')
      .then(r => (r.status === 404 ? null : j(r))) as Promise<{ key: string; title?: string } | null>,
    deleteSession: (key: string) => del('/api/sessions/' + encodeURIComponent(key)).then(j),
    clearSessions: (userOnly = false) => del('/api/sessions' + (userOnly ? '?user_only=1' : '')).then(j),
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
      get(`/api/crew-board?conductor=${encodeURIComponent(conductor)}`).then(jCrewBoard) as Promise<WorkBoardResponse>,
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

  /**
   * The kiro-cli sessions the owner ran in a terminal (History > Kiro CLI). The
   * gateway decides which qualify (`kiro_cli_sessions.is_terminal_session`): never
   * a session Crew owns, a background run or an untitled one. Continuing one
   * COPIES it into a new chat; the terminal session is left as it was.
   */
  const kiroCli = {
    /** A page of the list; *after* is the last row already shown, for the page that follows it. */
    kiroCliSessions: (q = '', limit = 50, after: KiroCliSessionRow | null = null) =>
      fetch('/api/kiro-cli/sessions?limit=' + limit + (q ? '&q=' + encodeURIComponent(q) : '')
        + (after?.id ? '&after_updated=' + encodeURIComponent(after.updated_at) + '&after_id=' + encodeURIComponent(after.id) : ''))
        .then(j) as Promise<KiroCliSessionList>,
    /** A page of the conversation, newest last; pass the answer's `start` as `before` for the older page. */
    kiroCliSession: (id: string, before?: number, limit = 100) =>
      fetch('/api/kiro-cli/sessions/' + encodeURIComponent(id) + '?limit=' + limit + (before === undefined ? '' : '&before=' + before))
        .then(j) as Promise<KiroCliSessionPage>,
    continueKiroCliSession: (id: string) =>
      post('/api/kiro-cli/sessions/' + encodeURIComponent(id) + '/continue')
        .then(j) as Promise<KiroCliContinueResult>,
  }

  return { runtimes, history, historyDetail, kiroCli, crewBoard }
}

/** One terminal kiro-cli session in the History list. */
export interface KiroCliSessionRow {
  id: string
  title: string
  /** The folder the session ran in; '' when kiro-cli recorded none. */
  cwd: string
  created_at: string
  updated_at: string
}

export interface KiroCliSessionList {
  sessions: KiroCliSessionRow[]
  total: number
  has_more: boolean
}

export interface KiroCliMessage {
  role: 'user' | 'assistant'
  content: string
  ts: string
  /** The gateway cut this message to its preview cap. */
  truncated: boolean
}

export interface KiroCliSessionPage extends KiroCliSessionRow {
  messages: KiroCliMessage[]
  /** Index of the first message on this page; the older page's `before`. */
  start: number
  total: number
  /** Records in the log that could not be read and were left out. */
  skipped: number
  has_transcript: boolean
  /** Set when Continue would copy the transcript only, with the code Continue
   *  itself answers, so the preview can say so before the click. */
  context_skipped?: KiroCliContextSkipped
}

/** Why a continued chat carries the transcript only. Fixed codes, never a
 *  configured value: another harness than Kiro CLI; Kiro CLI with MCP Tool
 *  Search on, which starts every resumed dashboard chat afresh from its
 *  transcript; or a machine whose kiro-cli runs Crew's chats on Kiro Agent,
 *  where no setting gives full state. */
export type KiroCliContextSkipped =
  | 'kiro_cli_backend_required'
  | 'tool_search_replays_transcript'
  | 'kiro_cli_state_unavailable'

export interface KiroCliContinueResult {
  ok: boolean
  key: string
  title: string
  messages: number
  resume_mode: string
  folder_id?: string
  /** The new chat's project folder; '' when the session's folder could not be used. */
  project: string
  /** Why the session's folder was not used, when it was not. */
  project_skipped?: string
  /** Set when the copy is transcript-only; see {@link KiroCliContextSkipped}. */
  context_skipped?: KiroCliContextSkipped
}
