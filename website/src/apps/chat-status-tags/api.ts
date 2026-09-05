// HTTP client for the Chat Status Tags backend.
//
// The one route this page needs — read and write the hourly reconciler's prompt.
// Registered directly on the main gateway aiohttp app under
// ``/api/apps/chat-status-tags``, so every call is same-origin and rides the
// dashboard session cookie — no tokens are added here. A 403 means the app is
// disabled; the page surfaces that as its own state rather than a bare HTTP error.

const API = '/api/apps/chat-status-tags'

/** Scheduler state of the hourly reconcile cron, as the backend reports it. */
export interface ReconcileCron {
  /** True when a reconcile cron job is registered with the scheduler. */
  present: boolean
  /** True when that job is present AND not paused. */
  enabled: boolean
  /** Human-readable schedule (e.g. "every hour"); empty when not present. */
  schedule: string
  /** True when the scheduler itself could not be reached — `present`/`enabled`
   *  are then not authoritative and repair cannot run. */
  schedulerUnavailable?: boolean
}

/** The reconcile prompt and whether it is still the shipped default. */
export interface ReconcilePrompt {
  /** The active prompt text the hourly reconciler runs. */
  prompt: string
  /** True when `prompt` is byte-identical to the shipped default. */
  isDefault: boolean
  /** The shipped default, so the page can show/restore it without a second call. */
  defaultPrompt: string
  /** Scheduler state of the cron that actually runs this prompt. */
  cron: ReconcileCron
}

/** The app's automation switches. Both behaviors spend model credits, so a
 *  credit-conscious operator can turn either off. Both ship enabled. */
export interface AutomationSettings {
  /** True when the hourly LLM reconciler is allowed to run. */
  reconcilerEnabled: boolean
  /** True when network-killed chats are auto-resumed. */
  autoResumeEnabled: boolean
}

/** A partial update — either switch, or both — for the settings PUT. */
export type AutomationSettingsPatch = Partial<AutomationSettings>

/** A failed request. `status` carries the HTTP code so the page can tell a
 *  disabled app (403) apart from a genuine server error. */
export class ChatStatusTagsApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ChatStatusTagsApiError'
    this.status = status
  }
}

async function parseError(r: Response): Promise<ChatStatusTagsApiError> {
  try {
    const body = (await r.json()) as { error?: string }
    return new ChatStatusTagsApiError(body.error || `HTTP ${r.status}`, r.status)
  } catch {
    return new ChatStatusTagsApiError(`HTTP ${r.status}`, r.status)
  }
}

export const chatStatusTagsApi = {
  /** Read the active reconcile prompt. */
  reconcilePrompt: async (): Promise<ReconcilePrompt> => {
    const r = await fetch(`${API}/reconcile-prompt`, { credentials: 'same-origin' })
    if (!r.ok) throw await parseError(r)
    return r.json() as Promise<ReconcilePrompt>
  },

  /** Write the reconcile prompt. Sending an empty string resets it to the
   *  shipped default; the response echoes the resulting state either way. */
  setReconcilePrompt: async (prompt: string): Promise<ReconcilePrompt> => {
    const r = await fetch(`${API}/reconcile-prompt`, {
      method: 'PUT',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt }),
    })
    if (!r.ok) throw await parseError(r)
    return r.json() as Promise<ReconcilePrompt>
  },

  /** Re-register (or unpause) the reconcile cron job. Returns the resulting
   *  cron state so the page can update in place without a full refetch. A 503
   *  means the scheduler is unavailable and nothing was changed. */
  repairCron: async (): Promise<{ ok: boolean; cron: ReconcileCron }> => {
    const r = await fetch(`${API}/reconcile-cron/repair`, {
      method: 'POST',
      credentials: 'same-origin',
    })
    if (!r.ok) throw await parseError(r)
    return r.json() as Promise<{ ok: boolean; cron: ReconcileCron }>
  },

  /** Read the automation switches. */
  fetchSettings: async (): Promise<AutomationSettings> => {
    const r = await fetch(`${API}/settings`, { credentials: 'same-origin' })
    if (!r.ok) throw await parseError(r)
    return r.json() as Promise<AutomationSettings>
  },

  /** Flip one or both automation switches. The body is a PARTIAL object; the
   *  response is the full fresh state. A 503 on a `reconcilerEnabled` write
   *  means the scheduler is unavailable and nothing was changed. */
  updateSettings: async (patch: AutomationSettingsPatch): Promise<AutomationSettings> => {
    const r = await fetch(`${API}/settings`, {
      method: 'PUT',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    })
    if (!r.ok) throw await parseError(r)
    return r.json() as Promise<AutomationSettings>
  },
}
