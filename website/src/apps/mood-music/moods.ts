/**
 * The mood chips, and how each mood colours and moves the page's backdrop.
 *
 * A mood names two hues and a drift period. The backdrop never paints a hue
 * straight: it mixes each one into the theme's own `--bg` (`MoodBackdrop`), so
 * the same mood reads soft on a light theme and deep on a dark one, and text
 * keeps the theme's `--text` contrast.
 */

export const MOOD_KEYS = ['calm', 'sad', 'happy', 'missing', 'fired'] as const
export type MoodKey = (typeof MOOD_KEYS)[number]
/** `neutral` is the resting look: no chip picked and nothing recognised. */
export type MoodLook = MoodKey | 'neutral'

export interface MoodTheme {
  /** Two hues (0-360) for the backdrop's two drifting glows. */
  hues: readonly [number, number]
  /** Seconds for one slow drift loop; a calmer mood drifts slower. */
  driftSeconds: number
  /** How much hue is mixed into `--bg`, in percent. */
  strength: number
}

const THEMES: Record<MoodLook, MoodTheme> = {
  calm: { hues: [195, 165], driftSeconds: 30, strength: 22 },
  sad: { hues: [220, 255], driftSeconds: 36, strength: 20 },
  happy: { hues: [42, 330], driftSeconds: 16, strength: 26 },
  missing: { hues: [280, 320], driftSeconds: 26, strength: 22 },
  fired: { hues: [8, 32], driftSeconds: 10, strength: 30 },
  neutral: { hues: [230, 300], driftSeconds: 24, strength: 16 },
}

export function isMoodKey(value: unknown): value is MoodKey {
  return typeof value === 'string' && (MOOD_KEYS as readonly string[]).includes(value)
}

/** The look for a chip key; anything else (an unknown chip, free text) is neutral. */
export function moodTheme(look: unknown): MoodTheme {
  return isMoodKey(look) ? THEMES[look] : THEMES.neutral
}

/** Words that place free text, or the agent's mood tags, on one of the chips. */
const WORDS: Record<MoodKey, readonly string[]> = {
  calm: ['平静', '安静', '放松', 'calm', 'peaceful', 'relaxed', 'chill'],
  sad: ['难过', '伤心', '悲伤', '失落', 'sad', 'down', 'heartbroken', 'blue'],
  happy: ['开心', '快乐', '高兴', '愉快', 'happy', 'joy', 'cheerful'],
  missing: ['想念', '思念', '怀念', 'miss', 'missing', 'nostalgic', 'longing'],
  fired: ['燃', '热血', '激动', '兴奋', 'hype', 'pumped', 'energetic'],
}

/** The chip a mood text or tag list reads as, or neutral when none fits. */
export function moodLookFor(texts: readonly string[]): MoodLook {
  const hay = texts.join(' ').toLowerCase()
  if (!hay.trim()) return 'neutral'
  for (const key of MOOD_KEYS) {
    if (WORDS[key].some(w => hay.includes(w))) return key
  }
  return 'neutral'
}
