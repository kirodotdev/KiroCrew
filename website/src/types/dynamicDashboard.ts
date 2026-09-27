export interface DynamicDashboardCard {
  card: { html: string; data: Record<string, string> } | null
  status: 'disabled' | 'waiting' | 'queued' | 'generating' | 'budget' | 'failed' | 'unavailable' | 'published'
  published_at: number | null
  content_event_at: number | null
  stale: boolean
}
