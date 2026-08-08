// Typed fetch wrapper for the Meetings backend.
//
// The base path is `/api/apps/meetings` — the routes are registered directly on
// the gateway's aiohttp Application (see the app's
// `backend/routes/__init__.py:register_routes`), matching issue-radar's
// convention, NOT the `/apps/{name}/api` reverse-proxy prefix used by apps that
// run as a separate child process.

const API = '/api/apps/meetings'

// ── wire types ──────────────────────────────────────────────────────────────

export type MeetingStatus = 'idle' | 'active' | 'paused' | 'reviewing' | 'ended'
export type WidgetType = 'markdown' | 'html' | 'chat'
export type TaskPriority = 'high' | 'medium' | 'low'

/**
 * Full literal catalog keys per enum value, not a suffix interpolated at the call
 * site: an assembled key exists nowhere in the source, so the extractor and the
 * unused-key tooling cannot see it (`dynamicKeys.test.ts`).
 */
export const PRIORITY_LABEL_KEY: Record<TaskPriority, string> = {
  high: 'apps.meetings.priority.high',
  medium: 'apps.meetings.priority.medium',
  low: 'apps.meetings.priority.low',
}

/** Full literal catalog keys per widget type — same reason as above. */
export const WIDGET_TYPE_LABEL_KEY: Record<WidgetType, string> = {
  markdown: 'apps.meetings.widgetType.markdown',
  html: 'apps.meetings.widgetType.html',
  chat: 'apps.meetings.widgetType.chat',
}
export type ReviewStatus = 'pending' | 'archived' | 'pushed'

export interface AgentDef {
  id: string
  name: string
  agent?: string
  widget_type: WidgetType
  prompt?: string
  enabled_by_default?: boolean
  listening_by_default?: boolean
  builtin?: boolean
}

export interface Preset {
  enabled_agents: string[]
}

export interface CalendarConfig {
  provider: string
  source: string
}

export interface MeetingsConfig {
  meeting_agents: AgentDef[]
  stt_provider: string
  task_provider: string
  calendar: CalendarConfig
  presets: Record<string, Preset>
  default_preset: string
  poll_interval_active: number
  poll_interval_idle: number
}

export interface ProviderRow {
  id: string
  label: string
  requires_source?: boolean
}

export interface ConfigResponse {
  config: MeetingsConfig
  task_providers: ProviderRow[]
  calendar_providers: ProviderRow[]
  stt_providers: ProviderRow[]
}

export interface Attachment {
  type: 'file' | 'url'
  label: string
  path?: string
  url?: string
}

export interface MeetingMeta {
  event_id: string
  title: string
  status: MeetingStatus
  attachments: Attachment[]
  outputs: Record<string, string>
  muted_agents: string[]
  agents_enabled?: string[]
  attendees?: string[]
  description?: string
  preset?: string
  created_at?: string
  started_at?: string
  ended_at?: string
}

export interface MeetingSummary {
  event_id: string
  title: string
  status: MeetingStatus
  started_at: string
  ended_at: string
}

export interface AgentQueueStatus {
  busy: boolean
  queued: number
  fail_count: number
  paused: boolean
}

export interface LiveStatus {
  active_meeting: string | null
  muted_agents: string[]
  agents: Record<string, AgentQueueStatus>
  agents_paused: boolean
  expired: boolean
}

export interface Task {
  id: string
  description: string
  assignee: string
  priority: TaskPriority
  status: 'open' | 'done'
  context: string
  labels: string[]
  review_status: ReviewStatus
  filed_ref: { provider: string; id: string; url?: string } | null
}

export interface CalendarEvent {
  event_id: string
  title: string
  start: string
  end: string
  location: string
  organizer: string
  attendees: string[]
  description: string
}

export interface DictionaryTerm {
  correct: string
  aliases: string[]
}

// ── transport ───────────────────────────────────────────────────────────────

/** An error carrying the backend's status so callers can branch on 409/410/502. */
export class MeetingsApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'MeetingsApiError'
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: init?.body ? { 'Content-Type': 'application/json', ...init?.headers } : init?.headers,
  })
  if (!res.ok) {
    // The backend answers every error as `{"error": "..."}`; fall back to the
    // status text when the body is not JSON (a proxy error page, say).
    let detail = res.statusText
    try {
      const body = await res.json()
      if (body?.error) detail = String(body.error)
    } catch {
      /* non-JSON body */
    }
    throw new MeetingsApiError(detail, res.status)
  }
  if (res.status === 204) return undefined as T
  const text = await res.text()
  return (text.trim() === '' ? undefined : JSON.parse(text)) as T
}

const post = <T,>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })

export const meetingsApi = {
  // config + dictionary
  config: () => request<ConfigResponse>('/config'),
  saveConfig: (config: Partial<MeetingsConfig>) =>
    request<{ config: MeetingsConfig }>('/config', {
      method: 'PUT',
      body: JSON.stringify({ config }),
    }),
  dictionary: () => request<{ terms: DictionaryTerm[] }>('/dictionary'),
  addTerm: (correct: string, aliases: string[]) =>
    post<{ terms: DictionaryTerm[] }>('/dictionary', { correct, aliases }),
  removeTerm: (correct: string) =>
    post<{ terms: DictionaryTerm[] }>('/dictionary/remove', { correct }),

  // calendar
  calendar: () =>
    request<{ events: CalendarEvent[]; provider: string; configured: boolean }>('/calendar'),
  syncCalendar: () =>
    post<{ ok: boolean; count: number; events: CalendarEvent[] }>('/calendar/sync'),

  // agents
  agents: () => request<{ agents: AgentDef[]; task_extractor_id: string }>('/agents'),
  status: () => request<LiveStatus>('/status'),

  // meetings
  meetings: () => request<{ meetings: MeetingSummary[] }>('/meetings'),
  meeting: (id: string) =>
    request<{ meta: MeetingMeta; live: LiveStatus | null }>(`/meetings/${encodeURIComponent(id)}`),
  deleteMeeting: (id: string) =>
    request<void>(`/meetings/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  init: (id: string, title: string) =>
    post<{ meeting_id: string; meta: MeetingMeta }>(
      `/meetings/${encodeURIComponent(id)}/init`,
      { title },
    ),
  start: (
    id: string,
    body: { title?: string; preset?: string; agents_enabled?: string[]; muted_agents?: string[]; restart?: boolean },
  ) =>
    post<{ status: MeetingStatus; agents: string[]; meta: MeetingMeta }>(
      `/meetings/${encodeURIComponent(id)}/start`,
      body,
    ),
  setStatus: (id: string, status: MeetingStatus) =>
    post<{ status: MeetingStatus }>(`/meetings/${encodeURIComponent(id)}/status`, { status }),
  stop: (id: string) =>
    post<{ status: MeetingStatus; meta: MeetingMeta }>(`/meetings/${encodeURIComponent(id)}/stop`),
  outputs: (id: string) =>
    request<{ outputs: Record<string, string>; tasks: Task[] }>(
      `/meetings/${encodeURIComponent(id)}/outputs`,
    ),
  attachments: (id: string, body: { action: 'add' | 'remove'; attachments?: Attachment[]; index?: number }) =>
    post<{ attachments: Attachment[] }>(`/meetings/${encodeURIComponent(id)}/attachments`, body),

  // per-meeting agent control
  toggleAgent: (id: string, agentId: string, enable: boolean) =>
    post<{ agents_enabled: string[] }>(`/meetings/${encodeURIComponent(id)}/agents`, {
      agent_id: agentId,
      enable,
    }),
  mute: (id: string, agentId: string, muted: boolean) =>
    post<{ muted_agents: string[] }>(`/meetings/${encodeURIComponent(id)}/mute`, {
      agent_id: agentId,
      muted,
    }),
  dispatch: (id: string, text: string, chat = false) =>
    post<{ dispatched: number; text: string }>(`/meetings/${encodeURIComponent(id)}/dispatch`, {
      text,
      chat,
    }),
  message: (id: string, agentId: string, text: string) =>
    post<{ agent_id: string }>(`/meetings/${encodeURIComponent(id)}/message`, {
      agent_id: agentId,
      text,
    }),
  resetAgents: (id: string) =>
    post<{ resumed: string[] }>(`/meetings/${encodeURIComponent(id)}/reset`),

  // tasks
  tasks: (id: string) => request<{ tasks: Task[] }>(`/meetings/${encodeURIComponent(id)}/tasks`),
  addTask: (id: string, description: string) =>
    post<{ task: Task; tasks: Task[] }>(`/meetings/${encodeURIComponent(id)}/tasks`, {
      description,
    }),
  updateTask: (id: string, taskId: string, fields: Partial<Task>) =>
    request<{ task: Task; tasks: Task[] }>(`/meetings/${encodeURIComponent(id)}/tasks`, {
      method: 'PATCH',
      body: JSON.stringify({ id: taskId, fields }),
    }),
  deleteTask: (id: string, taskId: string) =>
    request<{ tasks: Task[] }>(`/meetings/${encodeURIComponent(id)}/tasks`, {
      method: 'DELETE',
      body: JSON.stringify({ id: taskId }),
    }),
  fileTask: (id: string, taskId: string) =>
    post<{ ref: { provider: string; id: string; url?: string }; tasks: Task[] }>(
      `/meetings/${encodeURIComponent(id)}/tasks/file`,
      { id: taskId },
    ),
  reviewTask: (id: string, taskId: string, reviewStatus: ReviewStatus) =>
    post<{ tasks: Task[] }>(`/meetings/${encodeURIComponent(id)}/tasks/review`, {
      id: taskId,
      review_status: reviewStatus,
    }),
  taskProviders: () => request<{ providers: ProviderRow[]; active: string }>('/task-providers'),
}

/** A calendar event id becomes a path segment, so it is normalized the same way
 *  the backend's `safe_meeting_id` does. Kept here (not inlined) so the client
 *  and the server agree on one rule. */
export function safeMeetingId(eventId: string): string {
  return eventId.replace(/:/g, '_')
}
