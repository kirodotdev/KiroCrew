import type { LucideIcon } from 'lucide-react'
/**
 * Data shapes exchanged with the Crew Companion desktop backend (through the gateway
 * proxy). Reminders live in the desktop app's userData JSON, which a browser page
 * cannot read; the app serves them over HTTP and we reach them through the proxy.
 */

/** How a reminder repeats. `null` means one-time. */
export interface Recurrence {
  everyMinutes: number
}

export interface Reminder {
  id: string
  /** What to say when it fires. */
  text: string
  /** When it next fires, ISO 8601. */
  fireAt: string
  /** null for one-time. */
  recurrence: Recurrence | null
  /** Set once a one-time reminder has fired. */
  done?: boolean
}

/** GET /reminders payload. */
export interface RemindersPayload {
  reminders: Reminder[]
  breakNudgesEnabled: boolean
  sessionNotificationsEnabled: boolean
  breakReminderMins: number
  /** Desktop app UI language. Kept for parity; this page formats in the dashboard's
   *  own language, so it is not used for display. */
  language: string
  /**
   * Whether the desktop overlay is currently on screen. The overlay pings
   * `/presence` roughly every 30s and the backend treats it as present for 90s
   * after; `true` means it pinged inside that window. This is the difference
   * between the companion RUNNING and merely being ENABLED — the in-process
   * backend answers this read either way, so a closed overlay is a definite
   * `false` rather than a failed request.
   */
  present: boolean
}

/** Patch accepted by POST /reminders/config. */
export interface ReminderConfigPatch {
  breakNudgesEnabled?: boolean
  sessionNotificationsEnabled?: boolean
  breakReminderMins?: number
}

/**
 * The pet's cumulative record of time spent together. Rendered read-only as
 * "Memories". Only the fields this page actually shows are declared.
 */
export interface CompanionStats {
  firstLaunch: string        // ISO 8601
  streak: number             // consecutive launch days
  companionSeconds: number   // cumulative app-open seconds
  breathingSessions: number  // guided exercises completed
  remindersCreated: number   // reminders asked for
  latestActiveTime: string   // HH:mm
  earliestActiveTime: string // HH:mm
}

/** GET /stats payload. */
export interface StatsPayload {
  stats: CompanionStats
  petName: string
  language: string
}

/** A single read-only row in the Memories section. */
export interface MemoryRow {
  /**
   * A lucide component, never an emoji: `website/AUTOSDE.yaml`
   * `no-emoji-as-icons` is blocking, because emoji ignore `currentColor` and
   * the theme tokens, render differently per OS, and are read aloud by name.
   */
  icon: LucideIcon
  text: string
}

/** Avatar states and moods, as the companion's own art packs name them. */
// ── Pet State Machine ──────────────────────────────────────────────────────

/**
 * The pack-authoring vocabulary: the states and moods a pack MAY provide art for.
 *
 * Distinct from the narrower union in `PetAvatar`, which is what the companion
 * actually displays. The repo keeps both on purpose — a pack can ship art for a mood
 * the current build never enters, and dropping it here would silently discard that
 * art on save.
 */
export type PetState =
  | 'idle'
  | 'thinking'
  | 'working'
  | 'walking'
  | 'error'
  | 'offline'

export type PetMood = 'neutral' | 'happy' | 'sleepy' | 'curious' | 'busy' | 'scared'
