import { API_BASE } from './constants'
import type { HealthResponse, RecentResponse, SummaryResponse } from './types'

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: 'same-origin' })
  if (!r.ok) {
    const body = await r.text().catch(() => '')
    throw new Error(body || `HTTP ${r.status}`)
  }
  return r.json() as Promise<T>
}

// The client's UTC offset in minutes (positive east of UTC), so the backend
// buckets days and the today/week/month windows in the user's local time.
function tzOffsetMinutes(): number {
  return -new Date().getTimezoneOffset()
}

export const creditUsageApi = {
  health: () => get<HealthResponse>(`${API_BASE}/health`),

  summary: (days: number) =>
    get<SummaryResponse>(
      `${API_BASE}/summary?days=${encodeURIComponent(String(days))}&tz=${encodeURIComponent(
        String(tzOffsetMinutes()),
      )}`,
    ),

  recent: (limit: number) =>
    get<RecentResponse>(`${API_BASE}/recent?limit=${encodeURIComponent(String(limit))}`),
}
