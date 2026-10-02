/**
 * Mouse haptics. Every notification chime is also sent to the Kiro Crew plugin for
 * Logi Options+, which buzzes the mouse with the waveform the user picked for
 * that alert inside Options+. Options+ owns the mouse, so the dashboard needs no
 * device access and no macOS Input Monitoring permission.
 *
 * The Logitech mouse haptics switch in Settings > Notifications > Sound is the opt-in,
 * and it starts off: a dashboard that never turned it on sends nothing to the plugin's
 * port. Once it is on, the bridge sends no alerts until the plugin's status probe
 * answers, and it probes at most once per DETECTION_RETRY_MS so a dashboard without
 * the plugin makes almost no requests.
 *
 * The plugin listens on 127.0.0.1 and answers loopback pages only, so the bridge stays
 * dormant on any other origin. A Tailscale or LAN address would otherwise cost the
 * user a browser local-network permission prompt for a request the plugin refuses.
 */
import { useEffect } from 'react'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'
import { APPROVAL_KIND, MC_NOTIFICATION_EVENT, TURN_DONE_KIND, type McNotificationDetail } from './notificationEvent'
import { loadSoundSettings, presetForKind } from './useNotificationSound'

export const HAPTICS_PLUGIN_ORIGIN = 'http://127.0.0.1:41870'
export const HAPTICS_PLUGIN_NAME = 'KiroCrew' // brand-ok: the plugin's wire identifier in its status answer
export const DETECTION_RETRY_MS = 5 * 60_000
export const BUZZ_DEBOUNCE_MS = 300

/** localStorage key behind the Settings switch. Only a stored '1' means on: the switch is the opt-in, so absent means off. */
export const MOUSE_HAPTICS_ENABLED_KEY = 'mc-mouse-haptics'

/** Same-window signal that a bridge waiting out a failed probe should look for the plugin on the next alert. */
export const MC_MOUSE_HAPTICS_RECHECK_EVENT = 'mc-mouse-haptics-recheck' as const

const HAPTICS_PLUGIN_API_VERSION = 1
const PLUGIN_EVENT_BY_CHIME_KIND: Readonly<Record<string, string>> = {
  [TURN_DONE_KIND]: 'turn_done',
  [APPROVAL_KIND]: 'needs_input',
}
const DEFAULT_PLUGIN_EVENT = 'notification'
const LOOPBACK_HOSTNAMES = new Set(['localhost', '127.0.0.1', '[::1]'])

// Every open dashboard window hears the same alert, so they take turns under this lock to decide which one sends it.
const ONE_BUZZ_PER_ALERT_LOCK = 'kirocrew-mouse-haptics'
// When any window last sent a buzz, by the wall clock all windows share.
const LAST_BUZZ_AT_KEY = 'mc-mouse-haptics-last-buzz-at'

// A dashboard URL can carry a session token, and the plugin has no use for one.
const PLUGIN_REQUEST: RequestInit = { credentials: 'omit', referrerPolicy: 'no-referrer', cache: 'no-store' }

export function pluginEventForChimeKind(kind: string | undefined): string {
  return (kind !== undefined && PLUGIN_EVENT_BY_CHIME_KIND[kind]) || DEFAULT_PLUGIN_EVENT
}

export function isLoopbackHostname(hostname: string): boolean {
  return LOOPBACK_HOSTNAMES.has(hostname)
}

export function loadMouseHapticsEnabled(): boolean {
  return safeGetItem(MOUSE_HAPTICS_ENABLED_KEY) === '1'
}

/** Persists the switch and returns whether the write landed. Only a landed write tells the bridge to look again. */
export function saveMouseHapticsEnabled(on: boolean): boolean {
  const saved = safeSetItem(MOUSE_HAPTICS_ENABLED_KEY, on ? '1' : '0')
  if (saved) requestHapticsRecheck()
  return saved
}

export function requestHapticsRecheck(): void {
  window.dispatchEvent(new Event(MC_MOUSE_HAPTICS_RECHECK_EVENT))
}

function isKiroCrewPluginStatus(status: unknown): boolean {
  if (typeof status !== 'object' || status === null) return false
  const { plugin, api } = status as { plugin?: unknown; api?: unknown }
  return plugin === HAPTICS_PLUGIN_NAME && api === HAPTICS_PLUGIN_API_VERSION
}

/** Asks whatever listens on the plugin's port who it is. Transport, HTTP, and response-body delivery failures reject; other answers mean it is not the plugin. */
export async function identifyHapticsPlugin(fetchPlugin: typeof fetch): Promise<boolean> {
  const response = await fetchPlugin(`${HAPTICS_PLUGIN_ORIGIN}/v1/status`, PLUGIN_REQUEST)
  if (!response.ok) {
    const failure = new Error(`Haptics plugin probe failed with status ${response.status}`)
    Object.assign(failure, { status: response.status })
    throw failure
  }
  try {
    return isKiroCrewPluginStatus(await response.json())
  } catch (error) {
    if (error instanceof SyntaxError) return false
    throw error
  }
}

/** Bridge-facing probe. Never throws: a failed request keeps the plugin undetected and starts the retry wait. */
export async function probeHapticsPlugin(fetchPlugin: typeof fetch): Promise<boolean> {
  try {
    return await identifyHapticsPlugin(fetchPlugin)
  } catch {
    return false
  }
}

export interface HapticsBridgeDeps {
  fetch: typeof fetch
  /** This window's monotonic clock, for its own debounce and retry windows. */
  now: () => number
  /** The wall clock every window shares, for the record of the last buzz. */
  wallClock: () => number
  locks?: LockManager
}

export interface HapticsBridge {
  onChime: (kind: string | undefined) => void
  /** Ends a retry wait early, so the next alert looks for the plugin again. */
  recheck: () => void
}

export function createHapticsBridge({ fetch: fetchPlugin, now, wallClock, locks }: HapticsBridgeDeps): HapticsBridge {
  let pluginDetected = false
  let lastProbeAt = -Infinity
  let probeInFlight: Promise<boolean> | null = null
  let lastBuzzAt = -Infinity

  // Any failure parks the bridge for a full retry window, so a missing or broken plugin costs one request per window.
  function forgetPlugin(): void {
    pluginDetected = false
    lastProbeAt = now()
  }

  async function probePlugin(): Promise<boolean> {
    lastProbeAt = now()
    pluginDetected = await probeHapticsPlugin(fetchPlugin)
    return pluginDetected
  }

  function pluginIsReady(): Promise<boolean> {
    if (pluginDetected) return Promise.resolve(true)
    if (probeInFlight) return probeInFlight
    if (now() - lastProbeAt < DETECTION_RETRY_MS) return Promise.resolve(false)
    probeInFlight = probePlugin().finally(() => {
      probeInFlight = null
    })
    return probeInFlight
  }

  async function send(pluginEvent: string): Promise<void> {
    try {
      const response = await fetchPlugin(`${HAPTICS_PLUGIN_ORIGIN}/v1/events/${pluginEvent}`, { ...PLUGIN_REQUEST, method: 'POST' })
      if (!response.ok) forgetPlugin()
    } catch {
      forgetPlugin()
    }
  }

  // Readiness comes before the claim, so a window that cannot reach the plugin never takes the buzz from one that can.
  async function buzzOncePerAlert(pluginEvent: string, alertAt: number): Promise<void> {
    if (!(await pluginIsReady())) return
    if (await claimAlert(alertAt)) await send(pluginEvent)
  }

  // Every window hears the same alert, each at its own moment. A window skips it when another recorded a buzz within
  // BUZZ_DEBOUNCE_MS of that moment, before or after; any wider gap, including a clock that jumped, is a different alert.
  function claimUnderLock(alertAt: number): boolean {
    const lastSharedBuzzAt = Number(safeGetItem(LAST_BUZZ_AT_KEY))
    if (Math.abs(alertAt - lastSharedBuzzAt) < BUZZ_DEBOUNCE_MS) return false
    // Storage that is denied or full drops the record and leaves each window to decide alone. Buzzing anyway keeps a
    // single window working; several windows then buzz once each, which beats a mouse that never buzzes.
    safeSetItem(LAST_BUZZ_AT_KEY, String(alertAt))
    return true
  }

  // The lock covers only this synchronous check, never a timer, so a throttled background window cannot hold it.
  async function claimAlert(alertAt: number): Promise<boolean> {
    if (!locks) return claimUnderLock(alertAt)
    try {
      return await locks.request(ONE_BUZZ_PER_ALERT_LOCK, () => claimUnderLock(alertAt))
    } catch {
      // A lock that cannot be requested at all must not cost the buzz.
      return claimUnderLock(alertAt)
    }
  }

  return {
    // Gated exactly like the chime's preset, but not its volume: dashboard volume 0 gives buzz-only alerts.
    onChime(kind) {
      if (!loadMouseHapticsEnabled()) return
      if (presetForKind(kind, loadSoundSettings()) === 'none') return
      const at = now()
      if (at - lastBuzzAt < BUZZ_DEBOUNCE_MS) return
      lastBuzzAt = at
      void buzzOncePerAlert(pluginEventForChimeKind(kind), wallClock())
    },
    recheck() {
      if (!pluginDetected) lastProbeAt = -Infinity
    },
  }
}

/** Installs the window listeners that forward notification chimes to the Options+ plugin. */
export function useMouseHaptics(): void {
  useEffect(() => {
    if (!isLoopbackHostname(window.location.hostname)) return undefined
    const bridge = createHapticsBridge({
      fetch: window.fetch.bind(window),
      now: () => performance.now(),
      wallClock: () => Date.now(),
      locks: navigator.locks,
    })
    const onNotification = (event: Event) => {
      bridge.onChime((event as CustomEvent<McNotificationDetail>).detail?.kind)
    }
    const onRecheck = () => bridge.recheck()
    window.addEventListener(MC_NOTIFICATION_EVENT, onNotification)
    window.addEventListener(MC_MOUSE_HAPTICS_RECHECK_EVENT, onRecheck)
    return () => {
      window.removeEventListener(MC_NOTIFICATION_EVENT, onNotification)
      window.removeEventListener(MC_MOUSE_HAPTICS_RECHECK_EVENT, onRecheck)
    }
  }, [])
}
