/**
 * Notification sound system. Synthesizes tones via Web Audio API (no audio files).
 * Settings persist in localStorage under 'mc-notification-sound'.
 */
import { useEffect } from 'react'
import { MC_NOTIFICATION_EVENT, MC_SOUND_SETTINGS_CHANGED_EVENT, type McNotificationDetail } from './notificationEvent'
import { safeSetItem } from '../utils/safeStorage'

export const SOUND_PRESETS = ['chime', 'ding', 'blip', 'pop', 'pulse'] as const
export type SoundPreset = typeof SOUND_PRESETS[number] | 'none'

/** Category mirrors Notification.kind values used by NotificationsPage, plus
 * the frontend-synthesized 'turn' kind (agent finished a turn — see
 * TURN_DONE_KIND in notificationEvent.ts; sound-only, never in the feed). */
export const SOUND_CATEGORIES = ['all', 'turn', 'cron', 'approval', 'hook', 'heartbeat', 'subagent', 'taskrunner'] as const
export type SoundCategory = typeof SOUND_CATEGORIES[number]

export interface SoundSettings {
  enabled: boolean
  volume: number // 0..1
  /** Per-category sound. 'all' is the fallback; other keys override for that kind. */
  perCategory: Partial<Record<SoundCategory, SoundPreset>>
}

const STORAGE_KEY = 'mc-notification-sound'

const DEFAULTS: SoundSettings = {
  enabled: true,
  volume: 0.35,
  perCategory: { all: 'chime' },
}

const VALID_PRESETS = new Set<string>(['none', ...SOUND_PRESETS])
const VALID_CATEGORIES = new Set<string>(SOUND_CATEGORIES)

export function loadSoundSettings(): SoundSettings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return { ...DEFAULTS, perCategory: { ...DEFAULTS.perCategory } }
    const parsed = JSON.parse(raw) as Partial<SoundSettings>
    const perCategory: Partial<Record<SoundCategory, SoundPreset>> = { ...DEFAULTS.perCategory }
    for (const [k, v] of Object.entries(parsed.perCategory || {})) {
      if (VALID_CATEGORIES.has(k) && typeof v === 'string' && VALID_PRESETS.has(v)) {
        perCategory[k as SoundCategory] = v as SoundPreset
      }
    }
    return {
      enabled: typeof parsed.enabled === 'boolean' ? parsed.enabled : DEFAULTS.enabled,
      volume: Math.max(0, Math.min(1, typeof parsed.volume === 'number' ? parsed.volume : DEFAULTS.volume)),
      perCategory,
    }
  } catch {
    return { ...DEFAULTS, perCategory: { ...DEFAULTS.perCategory } }
  }
}

export function saveSoundSettings(s: SoundSettings): void {
  safeSetItem(STORAGE_KEY, JSON.stringify(s))
  window.dispatchEvent(new CustomEvent(MC_SOUND_SETTINGS_CHANGED_EVENT))
}

let ctxSingleton: AudioContext | null = null
// Backoff counter for repeated AudioContext close-under-pressure. When the
// browser closes the context (resource pressure, backgrounded tab, etc.) we
// clear the singleton and let the next call build a fresh one. But if the
// browser keeps closing it, we'd churn unbounded on every notification. After
// MAX_CLOSED_RECOVERIES consecutive 'closed' hits we stop trying. Counter
// resets on any successful schedule.
let closedRecoveryCount = 0
const MAX_CLOSED_RECOVERIES = 3

/** Test-only helper to reset module state between tests. */
export function __resetForTests(): void {
  ctxSingleton = null
  closedRecoveryCount = 0
}

function getCtx(): AudioContext | null {
  if (typeof window === 'undefined') return null
  if (ctxSingleton) return ctxSingleton
  const AC = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
  if (!AC) return null
  try { ctxSingleton = new AC() } catch { return null }
  return ctxSingleton
}

interface ToneStep { freq: number; start: number; dur: number; gain: number }

const PRESETS: Record<Exclude<SoundPreset, 'none'>, ToneStep[]> = {
  chime: [
    { freq: 1047, start: 0,    dur: 0.30, gain: 1.0 },
    { freq: 1319, start: 0.15, dur: 0.35, gain: 1.0 },
    { freq: 1568, start: 0.30, dur: 0.40, gain: 0.85 },
  ],
  ding:  [{ freq: 1760, start: 0, dur: 0.45, gain: 1.0 }],
  blip:  [{ freq: 880,  start: 0, dur: 0.08, gain: 1.0 }],
  pop:   [{ freq: 220,  start: 0, dur: 0.12, gain: 0.9 }],
  pulse: [
    { freq: 660,  start: 0,    dur: 0.12, gain: 1.0 },
    { freq: 880,  start: 0.15, dur: 0.12, gain: 1.0 },
    { freq: 660,  start: 0.30, dur: 0.12, gain: 0.9 },
    { freq: 880,  start: 0.45, dur: 0.12, gain: 0.9 },
  ],
}

export function playPreset(preset: SoundPreset, volume: number): void {
  if (preset === 'none' || volume <= 0) return
  // Backoff guard: once MAX_CLOSED_RECOVERIES consecutive 'closed' hits occur,
  // stop trying entirely. Without this, getCtx() keeps allocating fresh
  // AudioContexts that the browser closes again — unbounded churn per notification.
  if (closedRecoveryCount >= MAX_CLOSED_RECOVERIES) return
  const ctx = getCtx()
  if (!ctx) return
  // If the context is closed (browser may close under resource pressure or
  // when a tab is backgrounded), clear the singleton so the next call creates
  // a fresh context. Otherwise getCtx() keeps returning the dead one forever
  // and createOscillator() throws InvalidStateError every time.
  if (ctx.state === 'closed') {
    ctxSingleton = null
    if (++closedRecoveryCount >= MAX_CLOSED_RECOVERIES) {
      // Intentional diagnostic: warns once when sound is disabled after repeated
      // AudioContext closures so the user can correlate silence with resource pressure.
      // eslint-disable-next-line no-console
      console.warn(`AudioContext closed ${closedRecoveryCount} times consecutively; disabling sound until page reload`)
    }
    return
  }
  // Auto-resume on first gesture if suspended (common in Chrome). If resume()
  // succeeds, schedule the tones from the post-resume callback so the current
  // notification plays instead of being silently dropped. The state === 'running'
  // guard prevents an infinite retry loop if resume() resolves without actually
  // transitioning to running.
  if (ctx.state === 'suspended') {
    ctx.resume().then(() => {
      if (ctx.state === 'running') scheduleTones(ctx, preset, volume)
    }).catch(() => {})
    return
  }
  scheduleTones(ctx, preset, volume)
}

/**
 * Schedule a preset's oscillators on a running context.
 * Disconnects nodes via `onended` so the audio graph doesn't leak over long
 * sessions — without this, every call leaks one osc + one gain node permanently.
 */
function scheduleTones(ctx: AudioContext, preset: Exclude<SoundPreset, 'none'>, volume: number): void {
  // Reset backoff counter on successful schedule — a single good run wipes out
  // accumulated closed-state hits. Prevents permanent disable after 3 transient
  // close events over the page lifetime.
  closedRecoveryCount = 0
  const now = ctx.currentTime
  for (const step of PRESETS[preset]) {
    const osc = ctx.createOscillator()
    const g = ctx.createGain()
    osc.type = 'sine'
    osc.frequency.value = step.freq
    const peak = Math.max(0.001, volume * step.gain)
    g.gain.setValueAtTime(peak, now + step.start)
    g.gain.exponentialRampToValueAtTime(0.01, now + step.start + step.dur)
    osc.connect(g)
    g.connect(ctx.destination)
    osc.onended = () => { osc.disconnect(); g.disconnect() }
    osc.start(now + step.start)
    osc.stop(now + step.start + step.dur)
  }
}

/** Picks preset for a given notification kind using current settings. */
/** Built-in preset defaults for specific categories. Unlike DEFAULTS.perCategory,
 * these are NOT persisted to localStorage and therefore cannot be clobbered by
 * a "Use default" reset. They apply only when the user has never explicitly
 * chosen a preset for the category. */
const BUILTIN_CATEGORY_DEFAULTS: Partial<Record<SoundCategory, SoundPreset>> = {
  approval: 'pulse',
}

export function presetForKind(kind: string | undefined, settings: SoundSettings): SoundPreset {
  if (!settings.enabled) return 'none'
  const cat = kind && VALID_CATEGORIES.has(kind) ? (kind as SoundCategory) : undefined
  const specific = cat ? settings.perCategory[cat] : undefined
  if (specific) return specific
  // Built-in category default (not persisted — survives "Use default" reset)
  if (cat && BUILTIN_CATEGORY_DEFAULTS[cat]) return BUILTIN_CATEGORY_DEFAULTS[cat]!
  return settings.perCategory.all ?? 'chime'
}

/** Installs a window listener that plays sounds on notification SSE events. */
export function useNotificationSound(): void {
  useEffect(() => {
    let current = loadSoundSettings()
    let lastPlayedAt = 0
    const onSettingsChanged = () => { current = loadSoundSettings() }
    const onNotification = (e: Event) => {
      const now = performance.now()
      if (now - lastPlayedAt < 300) return
      const kind = (e as CustomEvent<McNotificationDetail>).detail?.kind
      const preset = presetForKind(kind, current)
      if (preset === 'none' || current.volume <= 0) return
      lastPlayedAt = now
      playPreset(preset, current.volume)
    }
    window.addEventListener(MC_SOUND_SETTINGS_CHANGED_EVENT, onSettingsChanged)
    window.addEventListener(MC_NOTIFICATION_EVENT, onNotification as EventListener)
    return () => {
      window.removeEventListener(MC_SOUND_SETTINGS_CHANGED_EVENT, onSettingsChanged)
      window.removeEventListener(MC_NOTIFICATION_EVENT, onNotification as EventListener)
    }
  }, [])
}
