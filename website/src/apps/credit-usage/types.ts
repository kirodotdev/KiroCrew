export interface HealthResponse {
  status: string
  app: string
  version: string
  hasData: boolean
}

export interface Totals {
  allTimeCredits: number
  allTimeTurns: number
  windowCredits: number
  today: number
  thisWeek: number
  thisMonth: number
}

export interface TrendPoint {
  date: string
  credits: number
}

export interface BreakdownRow {
  name: string
  credits: number
  turns: number
}

export interface TopSession {
  slot: string
  title: string
  credits: number
  turns: number
  surface: string
  lastTs: string
}

export interface SummaryResponse {
  windowDays: number
  generatedAt: string
  latestTs: string
  totals: Totals
  trend: TrendPoint[]
  byModel: BreakdownRow[]
  bySurface: BreakdownRow[]
  byAgent: BreakdownRow[]
  topSessions: TopSession[]
  sessionCount: number
}

export interface RecentRow {
  ts: string
  slot: string
  title: string
  model: string
  surface: string
  agent: string
  credits: number
  contextUsed: number
  contextWindow: number
  stopReason: string
  phase: string
}

export interface RecentResponse {
  rows: RecentRow[]
  totalRows: number
}
