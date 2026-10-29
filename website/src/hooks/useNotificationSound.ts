/**
 * Notification sound system. Built-in and custom tone sounds are synthesized
 * with the Web Audio API; uploaded sound files are played from IndexedDB.
 * Settings persist in localStorage under 'mc-notification-sound'.
 */
import { useEffect } from 'react'
import { MC_NOTIFICATION_EVENT, MC_SOUND_SETTINGS_CHANGED_EVENT, type McNotificationDetail } from './notificationEvent'
import { safeSetItem } from '../utils/safeStorage'
import { CUSTOM_FILE_LIMITS, SOUND_FILE_KEY_RE, getSoundFile } from './customSoundFiles'

export const SOUND_PRESETS = ['chime', 'ding', 'blip', 'pop', 'pulse'] as const
export type SoundPreset = typeof SOUND_PRESETS[number] | 'none'

/** Category mirrors Notification.kind values used by NotificationsPage, plus
 * the frontend-synthesized 'turn' kind (conversation ready for the user — see
 * TURN_DONE_KIND in notificationEvent.ts; sound-only, never in the feed) and
 * 'dictation' (DICTATION_STOPPED_KIND, also sound-only). */
export const SOUND_CATEGORIES = ['all', 'turn', 'dictation', 'agent', 'cron', 'approval', 'hook', 'heartbeat', 'subagent', 'taskrunner', 'skills'] as const
export type SoundCategory = typeof SOUND_CATEGORIES[number]

export interface ToneStep { freq: number; start: number; dur: number; gain: number }

/** A user-defined sound, stored by its name. Selected in `perCategory` through
 *  `customSoundId(name)`, so a custom name can never collide with a built-in id. */
export type CustomTones = Record<string, ToneStep[]>
export type CustomSoundId = `custom:${string}`

/** What is remembered about an uploaded sound file. The bytes themselves live
 *  in this browser's IndexedDB (`customSoundFiles.ts`), never in localStorage
 *  and never on the gateway; this record is what makes the sound pickable.
 *  `CustomFileMeta` is what checking a picked file yields; a saved record also
 *  carries the IndexedDB `key` its bytes were written under. */
export interface CustomFileMeta { bytes: number; seconds: number }
export interface StoredFileMeta extends CustomFileMeta { key: string }
export type CustomFiles = Record<string, StoredFileMeta>
export type FileSoundId = `file:${string}`

/** Anything a category can be set to: a built-in, 'none', a custom tone sound
 *  or an uploaded sound file. */
export type SoundChoice = SoundPreset | CustomSoundId | FileSoundId

const CUSTOM_PREFIX = 'custom:'
export const customSoundId = (name: string): CustomSoundId => `${CUSTOM_PREFIX}${name}`
export const customSoundName = (id: string): string | null =>
  id.startsWith(CUSTOM_PREFIX) ? id.slice(CUSTOM_PREFIX.length) : null

const FILE_PREFIX = 'file:'
export const fileSoundId = (name: string): FileSoundId => `${FILE_PREFIX}${name}`
export const fileSoundName = (id: string): string | null =>
  id.startsWith(FILE_PREFIX) ? id.slice(FILE_PREFIX.length) : null

/** The user-given name behind a custom tone or file choice, else null. */
export const userSoundName = (id: string): string | null => customSoundName(id) ?? fileSoundName(id)

/** Bounds a custom sound must meet. Applied when one is added AND on every load,
 *  because the stored value can be edited by hand or restored from a backup
 *  file, and whatever passes here is what reaches the speakers. */
export const CUSTOM_TONE_LIMITS = {
  maxSounds: 20,
  maxNameLength: 32,
  maxTones: 16,
  minFreq: 20,
  maxFreq: 20000,
  /** Shorter than the attack + release envelope would not sound at all. */
  minDur: 0.02,
  maxDur: 2,
  /** No step may end later than this many seconds after the sound starts. */
  maxLength: 5,
} as const

/** Bounds an uploaded sound file must meet (defined beside the file store),
 *  checked on add and on every load. They count toward the same
 *  `CUSTOM_TONE_LIMITS.maxSounds` as tone sounds. */
export { CUSTOM_FILE_LIMITS }

/** Letters and digits of any script, plus space, dash and underscore. */
const CUSTOM_NAME_RE = /^[\p{L}\p{N} _-]+$/u
const RESERVED_NAMES = new Set<string>(['none', 'default', ...SOUND_PRESETS])

/** Name and limit problems shared by tone sounds and sound files. `existing`
 *  holds every saved custom sound of BOTH kinds, so a name is unique across
 *  them and the limit counts them together. */
function nameProblems(name: string, existing: Record<string, unknown>, problems: Set<string>): void {
  const L = CUSTOM_TONE_LIMITS
  if (typeof name !== 'string' || name !== name.trim() || name.length === 0
      || name.length > L.maxNameLength || !CUSTOM_NAME_RE.test(name)) {
    problems.add('name')
  } else if (RESERVED_NAMES.has(name.toLowerCase())
      || Object.keys(existing).some(n => n.toLowerCase() === name.toLowerCase())) {
    problems.add('name_taken')
  }
  if (Object.keys(existing).length >= L.maxSounds) problems.add('too_many')
}

/** Problem codes a custom sound fails on: `name`, `name_taken`, `count`,
 *  `freq`, `start`, `dur`, `gain`, `too_many`. Empty means valid. */
export function validateCustomTone(name: string, tones: unknown, existing: Record<string, unknown> = {}): string[] {
  const L = CUSTOM_TONE_LIMITS
  const problems = new Set<string>()
  nameProblems(name, existing, problems)
  if (!Array.isArray(tones) || tones.length === 0 || tones.length > L.maxTones) {
    problems.add('count')
    return [...problems]
  }
  const num = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
  for (const step of tones as Array<Record<string, unknown>>) {
    if (!step || typeof step !== 'object') { problems.add('count'); continue }
    const { freq, start, dur, gain } = step
    if (!num(freq) || freq < L.minFreq || freq > L.maxFreq) problems.add('freq')
    if (!num(dur) || dur < L.minDur || dur > L.maxDur) problems.add('dur')
    if (!num(gain) || gain <= 0 || gain > 1) problems.add('gain')
    if (!num(start) || start < 0 || start + (num(dur) ? dur : 0) > L.maxLength) problems.add('start')
  }
  return [...problems]
}

/** Keep only the four tone fields, so nothing else a stored value carries is
 *  written back or played. */
const cleanTones = (tones: ToneStep[]): ToneStep[] =>
  tones.map(({ freq, start, dur, gain }) => ({ freq, start, dur, gain }))

function loadCustomTones(raw: unknown): CustomTones {
  // No prototype: a sound named `__proto__` must land as its own entry, not
  // hit Object.prototype's setter and vanish.
  const out: CustomTones = Object.create(null) as CustomTones
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return out
  for (const [name, tones] of Object.entries(raw as Record<string, unknown>)) {
    if (validateCustomTone(name, tones, out).length === 0) out[name] = cleanTones(tones as ToneStep[])
  }
  return out
}

/** Problem codes a sound file's record fails on: the name codes of
 *  `validateCustomTone`, plus `file_size` and `file_length`. Empty means valid.
 *  `existing` holds every saved custom sound of both kinds. */
export function validateCustomFile(name: string, meta: unknown, existing: Record<string, unknown> = {}): string[] {
  const F = CUSTOM_FILE_LIMITS
  const problems = new Set<string>()
  nameProblems(name, existing, problems)
  const m = (meta && typeof meta === 'object' ? meta : {}) as Record<string, unknown>
  const num = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
  if (!num(m.bytes) || m.bytes <= 0 || m.bytes > F.maxBytes) problems.add('file_size')
  if (!num(m.seconds) || m.seconds <= 0 || m.seconds > F.maxSeconds) problems.add('file_length')
  return [...problems]
}

/** Loaded after the tone sounds, against them, so the limit and the name
 *  rule hold across both kinds. A record must also carry a well-formed
 *  IndexedDB key: one without has no bytes it could play. */
function loadCustomFiles(raw: unknown, tones: CustomTones): CustomFiles {
  const out: CustomFiles = Object.create(null) as CustomFiles
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return out
  for (const [name, meta] of Object.entries(raw as Record<string, unknown>)) {
    const key = (meta as { key?: unknown } | null)?.key
    if (typeof key !== 'string' || !SOUND_FILE_KEY_RE.test(key)) continue
    if (validateCustomFile(name, meta, { ...tones, ...out }).length === 0) {
      const { bytes, seconds } = meta as CustomFileMeta
      out[name] = { bytes, seconds, key }
    }
  }
  return out
}

/** Every saved custom sound of both kinds, keyed by name: what a new name is
 *  checked against and what the shared limit counts. */
export const allCustomSounds = (s: Pick<SoundSettings, 'customTones' | 'customFiles'>): Record<string, unknown> =>
  Object.assign(Object.create(null) as Record<string, unknown>, s.customTones, s.customFiles)

export interface SoundSettings {
  enabled: boolean
  volume: number // 0..1
  /** Per-category sound. 'all' is the fallback; other keys override for that kind. */
  perCategory: Partial<Record<SoundCategory, SoundChoice>>
  /** User-defined sounds by name. Built-ins are never stored here. */
  customTones?: CustomTones
  /** Uploaded sound files by name: size and length only, the bytes are in
   *  IndexedDB. */
  customFiles?: CustomFiles
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
    const customTones = loadCustomTones(parsed.customTones)
    const customFiles = loadCustomFiles(parsed.customFiles, customTones)
    const perCategory: Partial<Record<SoundCategory, SoundChoice>> = { ...DEFAULTS.perCategory }
    for (const [k, v] of Object.entries(parsed.perCategory || {})) {
      if (!VALID_CATEGORIES.has(k) || typeof v !== 'string') continue
      const custom = customSoundName(v)
      const file = fileSoundName(v)
      // A custom choice counts only while its sound still exists; otherwise the
      // category falls back as if it had never been set.
      if (VALID_PRESETS.has(v) || (custom !== null && Object.hasOwn(customTones, custom))
          || (file !== null && Object.hasOwn(customFiles, file))) {
        perCategory[k as SoundCategory] = v as SoundChoice
      }
    }
    const out: SoundSettings = {
      enabled: typeof parsed.enabled === 'boolean' ? parsed.enabled : DEFAULTS.enabled,
      volume: Math.max(0, Math.min(1, typeof parsed.volume === 'number' ? parsed.volume : DEFAULTS.volume)),
      perCategory,
    }
    if (Object.keys(customTones).length > 0) out.customTones = customTones
    if (Object.keys(customFiles).length > 0) out.customFiles = customFiles
    return out
  } catch {
    return { ...DEFAULTS, perCategory: { ...DEFAULTS.perCategory } }
  }
}

export function saveSoundSettings(s: SoundSettings): boolean {
  // Persist first; only announce the change if it actually landed. A quota-
  // dropped write must NOT fire the settings-changed event, or every mounted
  // useNotificationSound would reload from localStorage and read the OLD value,
  // making the running session diverge from what the user just chose. Callers
  // (NotificationsPanel) branch on the return to update local state only on
  // success, so a failed save leaves the UI showing the persisted truth.
  const persisted = safeSetItem(STORAGE_KEY, JSON.stringify(s))
  if (persisted) {
    window.dispatchEvent(new CustomEvent(MC_SOUND_SETTINGS_CHANGED_EVENT))
  }
  return persisted
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

const ATTACK_DURATION = 0.005
const RELEASE_DURATION = 0.005
const ENVELOPE_FLOOR_RATIO = 0.01
const VOLUME_EXPONENT = 1.5
const CURVE_POINTS = 32

/** Chime partials overlap and can otherwise exceed full scale. Single-voice
 * presets retain almost all of their existing level while sharing a little
 * output headroom. */
const PRESET_OUTPUT_GAIN: Record<Exclude<SoundPreset, 'none'>, number> = {
  chime: 0.89,
  ding: 0.98,
  blip: 0.98,
  pop: 0.98,
  pulse: 0.98,
}

function smoothstepCurve(from: number, to: number): Float32Array {
  return Float32Array.from({ length: CURVE_POINTS }, (_, i) => {
    const x = i / (CURVE_POINTS - 1)
    const smooth = x * x * (3 - 2 * x)
    return from + (to - from) * smooth
  })
}

function scheduleEnvelope(gain: AudioParam, start: number, duration: number, peak: number): void {
  const floor = peak * ENVELOPE_FLOOR_RATIO
  const releaseStart = start + duration - RELEASE_DURATION
  gain.setValueCurveAtTime(smoothstepCurve(0, peak), start, ATTACK_DURATION)
  gain.exponentialRampToValueAtTime(floor, releaseStart)
  gain.setValueCurveAtTime(smoothstepCurve(floor, 0), releaseStart, RELEASE_DURATION)
}

/** The steps and output trim a choice plays, or null when it plays nothing (a
 *  custom sound that no longer exists). */
function resolveTones(preset: Exclude<SoundChoice, 'none' | FileSoundId>, customTones?: CustomTones): { steps: ToneStep[]; outputGain: number } | null {
  const custom = customSoundName(preset)
  if (custom === null) {
    const builtin = preset as Exclude<SoundPreset, 'none'>
    return { steps: PRESETS[builtin], outputGain: PRESET_OUTPUT_GAIN[builtin] }
  }
  const tones = customTones ?? loadSoundSettings().customTones ?? {}
  if (!Object.hasOwn(tones, custom)) return null
  const steps = tones[custom]
  // Overlapping steps add up; scale by the loudest overlap so a custom sound
  // can never drive the output past full scale.
  const worst = Math.max(1, ...steps.map(at => steps
    .filter(s => s.start <= at.start && at.start < s.start + s.dur)
    .reduce((sum, s) => sum + s.gain, 0)))
  return { steps, outputGain: 0.98 / worst }
}

/** `customTones` names the custom sounds a custom choice is looked up in;
 *  omitted, they are read from the stored settings. `ifFileMissing` is played
 *  instead of a file choice whose bytes are not in this browser (a settings
 *  backup restores the record without them), so a notification is never
 *  silenced by a sound that cannot play; omitted, such a choice plays nothing. */
export function playPreset(preset: SoundChoice, volume: number, customTones?: CustomTones, ifFileMissing?: SoundChoice): void {
  if (preset === 'none' || volume <= 0) return
  const file = fileSoundName(preset)
  if (file !== null) {
    // A file choice is looked up in the stored settings, like an omitted
    // `customTones`: its record names the key its bytes are under.
    const settings = loadSoundSettings()
    const meta = settings.customFiles && Object.hasOwn(settings.customFiles, file) ? settings.customFiles[file] : null
    // A fallback that is itself a file choice is not followed, so this never loops.
    const fallback = ifFileMissing !== undefined && fileSoundName(ifFileMissing) === null ? ifFileMissing : undefined
    const playFallback = () => { if (fallback !== undefined) playPreset(fallback, volume, customTones) }
    if (meta) playStoredSoundFile(meta.key, volume, playFallback)
    else playFallback()
    return
  }
  const tones = resolveTones(preset as Exclude<SoundChoice, 'none' | FileSoundId>, customTones)
  if (!tones) return
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
      if (ctx.state === 'running') scheduleTones(ctx, tones, volume)
    }).catch(() => {})
    return
  }
  scheduleTones(ctx, tones, volume)
}

/**
 * Play an audio FILE the gateway serves — an appearance pack's own cue.
 *
 * A separate path from `playPreset` on purpose, and not a shortcoming of it: a
 * preset is synthesized from oscillators this module owns, while a pack's cue is
 * third-party bytes behind an authenticated route. Nothing in the AudioContext
 * graph helps with those, and decoding them through it would mean fetching and
 * holding every cue in memory; an `<audio>` element streams it and sends the
 * same-origin session cookie the route requires.
 *
 * Failure is SILENCE, never a substitute sound: a 404 (the pack declares a state
 * it cannot serve), an undecodable file, and a browser that refuses to play
 * without a user gesture all end here with nothing played. Substituting a preset
 * would report the pack's own cue with a sound its author never chose.
 *
 * The element is released as soon as it finishes or fails, so a long session does
 * not accumulate one per cue. Callers debounce per crew and state, so this needs
 * no queue of its own. `onRelease` runs exactly once, when the element is
 * released or when nothing could be played at all — the moment a caller can
 * free what backs `url` (a `blob:` URL).
 */
export function playSoundFile(url: string, volume: number, onRelease?: () => void): void {
  if (!url || volume <= 0 || typeof Audio === 'undefined') {
    onRelease?.()
    return
  }
  let el: HTMLAudioElement
  try {
    el = new Audio(url)
  } catch {
    onRelease?.()
    return
  }
  // The stored volume is a 0..1 setting, but it arrives from localStorage and
  // `HTMLMediaElement.volume` THROWS outside that range rather than clamping —
  // so a hand-edited setting would take the cue down with it.
  el.volume = Math.min(1, Math.max(0, volume))
  // Stopping is the whole of the release: dropping the handlers leaves nothing
  // holding the element, so it is collectable, and `pause()` ends a play that is
  // still buffering — the case a refused `play()` leaves behind. Assigning to
  // `src` would release the same resource and is a dynamic media-source
  // assignment, which is a shape worth not writing when it buys nothing.
  let released = false
  const release = () => {
    if (released) return
    released = true
    el.onended = null
    el.onerror = null
    el.pause()
    onRelease?.()
  }
  el.onended = release
  el.onerror = release
  // `play()` rejects on the autoplay policy and on a decode failure alike; both
  // are silence, and neither is an error the user can act on.
  void el.play().catch(release)
}

/**
 * Play one of the user's uploaded sound files, read from this browser's
 * IndexedDB under the record's `key` and played from a `blob:` URL. A file
 * whose bytes are not stored (another tab deleted it, storage was cleared, a
 * backup restored only its record) calls `onMissing`, which plays the
 * caller's fallback; without one it plays nothing. A file that is stored but
 * cannot be played plays nothing, like any other unplayable file.
 *
 * The URL is revoked when the element is released, and in any case shortly
 * after the longest a sound file may play: a platform pause that fires
 * neither `ended` nor `error` (audio focus lost, a media-session interruption)
 * would otherwise pin the bytes until the page unloads.
 */
export function playStoredSoundFile(key: string, volume: number, onMissing?: () => void): void {
  if (volume <= 0) return
  void getSoundFile(key).then(blob => {
    if (!blob) { onMissing?.(); return }
    if (typeof URL === 'undefined' || typeof URL.createObjectURL !== 'function') return
    const url = URL.createObjectURL(blob)
    let revoked = false
    const revoke = () => {
      if (revoked) return
      revoked = true
      clearTimeout(backstop)
      URL.revokeObjectURL(url)
    }
    const backstop = setTimeout(revoke, (CUSTOM_FILE_LIMITS.maxSeconds + 5) * 1000)
    playSoundFile(url, volume, revoke)
  })
}

/**
 * Schedule a preset's oscillators on a running context.
 * Disconnects nodes via `onended` so the audio graph doesn't leak over long
 * sessions — without this, every call leaks one osc + one gain node permanently.
 */
function scheduleTones(ctx: AudioContext, tones: { steps: ToneStep[]; outputGain: number }, volume: number): void {
  // Reset backoff counter on successful schedule — a single good run wipes out
  // accumulated closed-state hits. Prevents permanent disable after 3 transient
  // close events over the page lifetime.
  closedRecoveryCount = 0
  const now = ctx.currentTime
  const perceptualVolume = Math.min(1, Math.max(0, volume)) ** VOLUME_EXPONENT
  for (const step of tones.steps) {
    const osc = ctx.createOscillator()
    const g = ctx.createGain()
    osc.type = 'sine'
    osc.frequency.value = step.freq
    const peak = Math.max(0.001, perceptualVolume * step.gain * tones.outputGain)
    scheduleEnvelope(g.gain, now + step.start, step.dur, peak)
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
const BUILTIN_CATEGORY_DEFAULTS: Partial<Record<SoundCategory, SoundChoice>> = {
  approval: 'pulse',
}

export function presetForKind(kind: string | undefined, settings: SoundSettings): SoundChoice {
  if (!settings.enabled) return 'none'
  const cat = kind && VALID_CATEGORIES.has(kind) ? (kind as SoundCategory) : undefined
  const specific = cat ? settings.perCategory[cat] : undefined
  if (specific) return specific
  // A global 'all' = 'none' is an explicit "silence everything" and must win
  // over a built-in category default. Otherwise setting all=none would still
  // let approval chime its built-in 'pulse', which reads as the setting being
  // ignored. An explicit per-category override (handled above) still wins over
  // this — only the UNSET category falls through to the global silence.
  const fallback = settings.perCategory.all ?? 'chime'
  if (fallback === 'none') return 'none'
  // Built-in category default (not persisted — survives "Use default" reset).
  // Reached only when the global fallback is audible.
  if (cat && BUILTIN_CATEGORY_DEFAULTS[cat]) return BUILTIN_CATEGORY_DEFAULTS[cat]!
  return fallback
}

/**
 * What `kind` plays when its file choices cannot: the same resolution with
 * every file choice treated as unset, the rule a load applies to a choice
 * whose sound is gone. File choices only, so it never resolves to a file.
 */
export function missingFileFallback(kind: string | undefined, settings: SoundSettings): SoundChoice {
  const perCategory = Object.fromEntries(
    Object.entries(settings.perCategory).filter(([, v]) => v === undefined || fileSoundName(v) === null),
  ) as SoundSettings['perCategory']
  return presetForKind(kind, { ...settings, perCategory })
}

/** Installs a window listener that plays sounds on notification SSE events. */
export function useNotificationSound(): void {
  useEffect(() => {
    let current = loadSoundSettings()
    let lastPlayedAt = 0
    const onSettingsChanged = () => { current = loadSoundSettings() }
    // Cross-tab sync: a settings write in ANOTHER tab fires a DOM `storage`
    // event here (the same-window MC_SOUND_SETTINGS_CHANGED_EVENT never crosses
    // tabs). Filter by key and storageArea so an unrelated key or a
    // sessionStorage write in a same-origin iframe does not force a reload.
    // Reload from localStorage rather than parsing e.newValue so we reuse
    // loadSoundSettings' validation/clamping (and correctly adopt DEFAULTS on a
    // cross-tab key removal, where e.newValue is null).
    const onStorage = (e: StorageEvent) => {
      try {
        if (e.storageArea && e.storageArea !== localStorage) return
      } catch {
        /* locked-down storage: fall through to the key filter alone */
      }
      if (e.key !== null && e.key !== STORAGE_KEY) return
      current = loadSoundSettings()
    }
    const onNotification = (e: Event) => {
      const now = performance.now()
      if (now - lastPlayedAt < 300) return
      const kind = (e as CustomEvent<McNotificationDetail>).detail?.kind
      // Primary switch: enabled=false yields 'none' from presetForKind, so
      // WebAudio never plays. Kept as the single gate rather than a second
      // check here.
      const preset = presetForKind(kind, current)
      if (preset === 'none' || current.volume <= 0) return
      lastPlayedAt = now
      playPreset(preset, current.volume, current.customTones ?? {}, missingFileFallback(kind, current))
    }
    window.addEventListener(MC_SOUND_SETTINGS_CHANGED_EVENT, onSettingsChanged)
    window.addEventListener('storage', onStorage)
    window.addEventListener(MC_NOTIFICATION_EVENT, onNotification as EventListener)
    return () => {
      window.removeEventListener(MC_SOUND_SETTINGS_CHANGED_EVENT, onSettingsChanged)
      window.removeEventListener('storage', onStorage)
      window.removeEventListener(MC_NOTIFICATION_EVENT, onNotification as EventListener)
    }
  }, [])
}
