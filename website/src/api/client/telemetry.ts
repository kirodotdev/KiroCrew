/**
 * Local telemetry and usage readouts: crew-log projections, startup
 * telemetry, the per-session context trace and per-turn usage, WakaTime stats
 * and export, the beacon and collection privacy posture, and Kiro credit and
 * provider usage.
 *
 * The Kiro usage payload types stay defined in `api/client.ts`, next to the
 * view model normalized from them, so they are imported from there as types.
 *
 * `wakatimeExportDownload` is defined in `api/client.ts`: it reads
 * `api.wakatimeExportUrl` at call time, as it always has.
 */

import type { KiroUsagePayload, KiroUsageRefreshResponse } from '../client'
import type { ClientTransport } from './transport'

/** WakaTime coding-stats payload (GET /api/wakatime/stats). When the
 *  integration is off the endpoint returns { configured: false } instead. */
export type WakaTimeStatsEntry = { name: string; total_seconds: number }

export type WakaTimeStats = {
  configured: boolean
  range?: string
  stats?: {
    total_seconds?: number
    daily_average?: number
    languages?: WakaTimeStatsEntry[]
    projects?: WakaTimeStatsEntry[]
  }
}

export function createTelemetryEndpoints({ post, j }: ClientTransport) {
  const usageReadouts = {
    /** The five session folds of a crew log, keyed by name, in ONE request.
     *
     *  The batch route is what makes the answer coherent: it resolves the session
     *  once and folds once, so all five values come from the same file at the same
     *  moment. Five per-name requests could not promise that -- a session replaced
     *  while they were in flight would leave some describing the unit going away and
     *  some the one arriving, and the panel would show a mix it cannot detect.
     *
     *  Each fold still carries its OWN `seq`, because they really do differ: an entry
     *  advances the folds it belongs to and leaves the rest where they were. */
    sessionCrewLogProjections: async (slot: string) => {
      const body = await fetch(`/api/sessions/${encodeURIComponent(slot)}/crew-log/projections`).then(j)
      const read = body as {
        projections?: Record<string, unknown>
        resolved?: unknown
        writes_drained?: unknown
        recording?: unknown
        flag_value?: unknown
        flag_recognised?: unknown
        env_file?: unknown
      }
      return {
        folds: read.projections ?? {},
        // Whether a unit was NAMED for the id sent. An empty fold cannot say why it
        // is empty, and the two reasons need different words on screen: a slot that
        // never recorded anything, versus one whose ACP session was torn down and
        // whose record is still on disk under the retired id.
        resolved: read.resolved !== false,
        // False when the writer still owed this process entries as the fold was
        // taken, so the value may be behind the record. Absent reads as drained: an
        // older gateway does not send the field and did not race either.
        writesDrained: read.writes_drained !== false,
        // False only when the gateway says recording is switched off. Absent reads as
        // on: an older gateway does not send the field.
        recording: read.recording !== false,
        // The KIROCREW_CREW_LOG value that switched it off, so the panel can quote it.
        // Empty when the gateway does not send one.
        flagValue: typeof read.flag_value === 'string' ? read.flag_value : '',
        // False when that value is not one of the switch-off spellings.
        flagRecognised: read.flag_recognised !== false,
        // The `.env` the gateway reads; the default home's when the gateway sends none.
        envFile: typeof read.env_file === 'string' && read.env_file ? read.env_file : '~/.kiro/crew/.env',
      }
    },
    telemetryStartup: () => fetch('/api/telemetry/startup').then(j),
    // Per-turn context injection breakdown for one session. Independent of the
    // telemetry main switch: the usage rows it reads are always written.
    telemetryContextTrace: (slot: string) =>
      fetch('/api/telemetry/context-trace?slot=' + encodeURIComponent(slot)).then(j),
    /** Per-turn usage rows for one session — the Spend table's drill-down.
     *  Same always-written row store as the context trace; the dashboard reads
     *  every row (the endpoint's app-ownership filter applies to app callers). */
    usageTurns: (slot: string) =>
      fetch('/api/usage/turns?slot=' + encodeURIComponent(slot)).then(j),
    /** WakaTime coding stats for a named range. Returns { configured: false }
     *  when the integration is off; a 502 body carries { code: 'upstream_unavailable' }. */
    wakatimeStats: (range: string) =>
      fetch('/api/wakatime/stats?range=' + encodeURIComponent(range)).then(j) as Promise<WakaTimeStats>,
    /** Download URL for the billable-hours export. The browser navigates to it so
     *  the CSV/JSON arrives via the endpoint's own Content-Disposition. */
    wakatimeExportUrl: (start: string, end: string, format: 'csv' | 'json') =>
      `/api/wakatime/export?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}&format=${format}`,
  }

  const privacyPosture = {
    beaconStatus: () => fetch('/api/telemetry/beacon').then(j),
    /** Local metric-collection posture for the Privacy panel's recording switch.
     *  Separate from telemetryStartup(), which parses every shard in the window. */
    collectionStatus: () => fetch('/api/telemetry/collection').then(j),
  }

  const creditUsage = {
    sessionsUsage: () => fetch('/api/sessions/usage').then(j) as Promise<{ usage?: KiroUsagePayload }>,
    /**
     * Refresh the credit reading now (the account modal's Refresh button). Same
     * `{usage}` envelope as `sessionsUsage`, so `parseKiroUsagePayload` reads
     * both. `skipped: 'scrape_parked'` (with `retry_after` seconds) means the
     * free API returned no plan and the gateway has parked the `/usage` scrape
     * after repeated failures, so no new reading was fetched: `usage` is a
     * same-identity prior reading dimmed `stale`, or an unavailable marker. The
     * one refusal is 409 `refresh_in_flight` while a refresh is already running.
     */
    sessionsUsageRefresh: () => post('/api/sessions/usage/refresh').then(j) as Promise<KiroUsageRefreshResponse>,
    providerUsage: () => fetch('/api/usage').then(j),
  }

  const kiroUsage = {
    // A graceful no-op on a public install, where Kiro usage is stubbed; the
    // panels render empty when the feature is absent.
    kiroUsage: () => fetch('/api/usage/kiro').then(j),
  }

  return { usageReadouts, privacyPosture, creditUsage, kiroUsage }
}
