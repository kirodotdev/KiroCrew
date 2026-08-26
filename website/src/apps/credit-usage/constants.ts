export const API_BASE = '/apps/credit-usage/api'

// The dashboard has no usage push channel, so we poll. Match the existing
// credit-pill / telemetry cadence closely enough to feel live without hammering
// the backend (whose reads are memoized by shard signature, so a poll between
// turns is essentially free).
export const POLL_MS = 8000

export const DEFAULT_WINDOW_DAYS = 30
export const WINDOW_CHOICES = [7, 14, 30, 60, 90] as const

export const RECENT_LIMIT = 40

export const STORAGE_KEY = 'kc:credit-usage:window:v1'
