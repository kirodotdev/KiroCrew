/**
 * The demo's stand-in for gateway state, kept in localStorage so a reload
 * resumes where the tester left off. `?reset` in the URL starts over.
 */
import type { ChatMessage } from '../types'

/** First-run setup gate: Kiro CLI missing, installed but signed out, or ready. */
export type SetupPhase = 'not_installed' | 'signed_out' | 'ready'

export interface DemoSlot {
  key: string
  title: string
  agent: string
  created: string
  last_ts: string
  messages: ChatMessage[]
}

export interface DemoState {
  setup: SetupPhase
  theme: {
    mode: string
    color: string
    translucent_panels: string
    onboarded: boolean
    import_onboarded: boolean
    privacy_acked: boolean
  }
  /** Config written through PATCH /api/config/kirocrew, merged shallowly per section. */
  config: Record<string, Record<string, unknown>>
  slots: DemoSlot[]
  seq: number
}

const STATE_KEY = 'kc-demo-state'

function freshState(setup: SetupPhase): DemoState {
  return {
    setup,
    theme: { mode: 'system', color: '', translucent_panels: '', onboarded: false, import_onboarded: false, privacy_acked: false },
    config: {},
    slots: [],
    seq: 0,
  }
}

/** Where the setup gate starts: `?setup=ready` skips it, the default shows it. */
function initialSetup(params: URLSearchParams): SetupPhase {
  const v = params.get('setup')
  return v === 'ready' || v === 'signed_out' ? v : 'not_installed'
}

/**
 * `?reset` wipes the demo AND every browser-side key the dashboard wrote, so the
 * next tester gets a genuinely first-run browser. Runs before the app reads any
 * of them. Returns true when the URL was rewritten.
 */
export function applyResetParam(win: Window = window): void {
  const url = new URL(win.location.href)
  if (!url.searchParams.has('reset')) return
  try { win.localStorage.clear() } catch { /* storage blocked */ }
  try { win.sessionStorage.clear() } catch { /* storage blocked */ }
  const setup = initialSetup(url.searchParams)
  url.searchParams.delete('reset')
  url.searchParams.delete('setup')
  saveState(freshState(setup))
  win.history.replaceState(null, '', url.pathname + url.search + url.hash)
}

let cached: DemoState | null = null

export function loadState(): DemoState {
  if (cached) return cached
  let parsed: DemoState | null = null
  try {
    const raw = localStorage.getItem(STATE_KEY)
    parsed = raw ? (JSON.parse(raw) as DemoState) : null
  } catch { parsed = null }
  cached = parsed ?? freshState(initialSetup(new URLSearchParams(window.location.search)))
  return cached
}

export function saveState(state: DemoState): void {
  cached = state
  try { localStorage.setItem(STATE_KEY, JSON.stringify(state)) } catch { /* storage full or blocked */ }
}

export function updateState(mutate: (s: DemoState) => void): DemoState {
  const s = loadState()
  mutate(s)
  saveState(s)
  return s
}

export function nextSeq(): number {
  return updateState((s) => { s.seq += 1 }).seq
}
