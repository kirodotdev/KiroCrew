import type { LucideIcon } from 'lucide-react'

// The lifecycle of the page. Every render branch keys off this.
export type Phase = 'new' | 'uploading' | 'scanning' | 'scoping' | 'analyzing' | 'report' | 'error'

export type SeverityKey = 'catastrophe' | 'major' | 'minor' | 'cosmetic'

export interface SevInfo {
  label: string
  rank: number
  color: string
  icon: LucideIcon
}

// The approximate region of a finding within its screen, as fractions 0-1.
export interface Box {
  x: number
  y: number
  w?: number
  h?: number
}

export interface Finding {
  severity: SeverityKey
  title: string
  category?: string
  scope?: 'screen' | 'flow'
  steps?: number[]
  location?: string
  evidence?: string
  fix?: string
  rules?: string[]
  box?: Box | null
}

// A screen as the critic reports it (path is the absolute image path on disk).
export interface ReportScreen {
  step?: number
  label?: string
  path?: string
}

export interface Report {
  overallRead?: string
  health?: string
  tally?: Partial<Record<SeverityKey, number>>
  screens?: ReportScreen[]
  findings?: Finding[]
  keep?: string[]
  couldNotSee?: string[]
}

// A screen as the UI shows it (url is a browser-loadable src).
export interface Screen {
  step: number
  label: string
  url: string
}

// One candidate screen discovered in a repo / figma / url.
export interface DiscoveryScreen {
  id: string
  label: string
  ref?: string
  group?: string
  canSee?: boolean
  why?: string
}

export interface Flow {
  label?: string
  why?: string
  basis?: 'observed' | 'guess'
  screenIds?: string[]
}

export interface BlockedInfo {
  reason: string
  detail?: string
}

// The discovery result — what STEP 1 returns, filtered for the picker.
export interface Scope {
  framework?: string
  note?: string
  blocked?: BlockedInfo | null
  screens: DiscoveryScreen[]
  flows: Flow[]
  cannotSee?: string[]
}

// The way-forward copy shown when the critic couldn't get in at all.
export interface Blocked {
  say: string
  fix: string
  hint: string
  auth?: { lead: string; cmds: string[]; tail: string }
  detail?: string
}

export interface AskTurn {
  t: number
  q: string
  a: string
  pending: boolean
  failed?: boolean
}

export interface Ask {
  id: string
  quote: string
  turns: AskTurn[]
}

// One saved critique in History.
export interface HistoryEntry {
  id: number
  ts: number
  slotKey: string
  screens: Screen[]
  thumbUrl: string
  read: string
  /**
   * Null while the run is still in flight. A critique backgrounded with `+ New`
   * appears in the list immediately so there is something to come back to, and
   * the report is filled in on the same row when the run finishes.
   */
  report: Report | null
  asks?: Ask[]
  pending?: boolean
}

// The single in-flight job persisted to localStorage so a run survives navigation.
export interface Job {
  stage: string
  slotKey: string
  screens?: Screen[]
  kind?: string | null
  value?: string
  ts: number
  scope?: Scope
  picked?: string[]
  refBrief?: string
  // The backend render handle (clone id or local:/url: marker) for a scoping job.
  handle?: string
  // Backend background-job ids. The scan (discover) and render now run as detached
  // server-side jobs; these let a resumed page reconnect by POLLING the same job
  // instead of re-POSTing (which would start a second scan).
  discoverJob?: string
  renderJob?: string
}

// A screenshot staged in the composer, not sent yet.
export interface StagedItem {
  id: string
  file: File
  url: string
}

// A pending text selection in the report ("Ask about this").
export interface Sel {
  quote: string
  top: number
  left: number
}

// What detectKind() decided the pasted text is.
export interface Detected {
  kind: 'figma' | 'repo' | 'url' | 'local' | 'unknown'
  value: string
  local?: boolean
}

// One transient toast (local, DevFleet-style — see hooks.useToasts).
export interface Toast {
  id: number
  msg: string
  type: 'info' | 'success' | 'error'
}

// The shape returned by GET /api/chat/slots/{key}.
export interface SlotData {
  running?: boolean
  messages?: Array<{ role?: string; type?: string; content?: string }>
}
