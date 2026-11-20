/**
 * The account plan windows that an agent provider reports about itself, as
 * `GET /api/usage/kiro` carries them under `codex`.
 *
 * These are the PROVIDER'S numbers for the whole account (Codex's primary and
 * secondary windows), not this install's own token or cost totals. The server
 * reads them from logs the harness already writes; nothing here invents a
 * value. A field the provider did not send stays `null`, so a missing reading
 * is "no reading", never 0%.
 */

/** One usage window, as the provider reported it. */
export interface PlanLimitWindow {
  /** Share of the window's limit used, in percent. Can pass 100 when the provider says so. */
  usedPercent: number
  /** When the window resets, in epoch milliseconds; null when the provider did not say. */
  resetsAt: number | null
  /** The window's length, when the provider reports one. */
  windowMinutes: number | null
  /** The reset time passed after this reading, so the percentage describes an earlier window. */
  stale: boolean
}

export interface PlanLimitReading {
  fiveHour: PlanLimitWindow | null
  weekly: PlanLimitWindow | null
  /** When the reading was reported, in epoch milliseconds; null when unknown. */
  capturedAt: number | null
}

export interface PlanLimits {
  codex: PlanLimitReading | null
}

function finite(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/** An ISO timestamp as epoch milliseconds, or null when it does not parse. */
function instant(value: unknown): number | null {
  if (typeof value !== 'string' || !value) return null
  const ms = Date.parse(value)
  return Number.isFinite(ms) ? ms : null
}

function parseWindow(raw: unknown): PlanLimitWindow | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const w = raw as Record<string, unknown>
  const used = finite(w.used_percent)
  if (used === null || used < 0) return null
  const resetsAt = finite(w.resets_at)
  const minutes = finite(w.window_minutes)
  return {
    usedPercent: used,
    resetsAt: resetsAt !== null && resetsAt > 0 ? resetsAt * 1000 : null,
    windowMinutes: minutes !== null && minutes > 0 ? minutes : null,
    stale: w.stale === true,
  }
}

/** Codex's reading from the route's payload, or null when there is none. */
export function parsePlanLimitReading(raw: unknown): PlanLimitReading | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const r = raw as Record<string, unknown>
  return {
    fiveHour: parseWindow(r.five_hour),
    weekly: parseWindow(r.weekly),
    capturedAt: instant(r.captured_at),
  }
}

/** Codex's reading from a `GET /api/usage/kiro` body. */
export function parsePlanLimits(body: { codex?: unknown } | null | undefined): PlanLimits {
  return {
    codex: parsePlanLimitReading(body?.codex),
  }
}
