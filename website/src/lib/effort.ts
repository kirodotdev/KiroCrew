/**
 * Reasoning-effort vocabulary for the dashboard — mirrors the backend
 * `kiro_crew/effort.py` so the UI and server agree on levels and per-model
 * capability. Kept as a standalone module (not inside ChatInput) so it can be
 * imported without pulling in the component — and so test mocks of ChatInput
 * don't have to re-export it.
 */

/** Display labels for each effort level. '' = provider/model default. */
export const EFFORT_DISPLAY: Record<string, { label: string }> = {
  '': { label: 'Default' },
  default: { label: 'Default' },
  low: { label: 'Low' },
  medium: { label: 'Medium' },
  high: { label: 'High' },
  xhigh: { label: 'Extra High' },
  max: { label: 'Max' },
}

/**
 * Display name for an effort level. Falls back to a capitalized form of the
 * raw value so levels the backend reports dynamically (via /api/effort-levels)
 * that aren't in EFFORT_DISPLAY still render sensibly.
 */
export function effortLabel(level: string): string {
  return EFFORT_DISPLAY[level]?.label || level.charAt(0).toUpperCase() + level.slice(1)
}

/**
 * Concrete effort levels offered in the dropdown, ordered low→high, with the
 * '' default sentinel first. kiro-cli (acp) supports these on Fable/Opus/Sonnet
 * models.
 */
export const EFFORT_LEVELS = ['', 'low', 'medium', 'high', 'xhigh', 'max'] as const

/** Providers whose backend accepts a reasoning-effort level. KiroCrew is
 *  KiroACP-only, so this is just 'acp'. */
export const REASONING_EFFORT_PROVIDERS = new Set(['acp'])

/**
 * Per-model effort capability — mirrors the backend `model_supports_effort`
 * (kiro_crew/effort.py): effort is Fable/Opus/Sonnet-only; Haiku/auto/empty
 * and third-party models cannot use it. Gates the dropdown so a non-capable
 * model never shows a control that would silently no-op on the backend.
 */
export function modelSupportsEffort(model: string | undefined): boolean {
  if (!model) return false
  const m = model.toLowerCase()
  if (m === 'auto' || m.includes('haiku')) return false
  return m.includes('opus') || m.includes('sonnet') || m.includes('fable')
}
