/**
 * Reasoning-effort vocabulary for the dashboard — mirrors the backend
 * `kiro_crew/effort.py` so the UI and server agree on levels and per-model
 * capability. Kept as a standalone module (not inside ChatInput) so it can be
 * imported without pulling in the component — and so test mocks of ChatInput
 * don't have to re-export it.
 */

import { i18nT } from '../i18n/t'

/**
 * Catalog KEY for each effort level's display label. '' = provider/model default.
 *
 * Keys, not strings: this table is evaluated at module load, so an `i18nT()` call
 * here would freeze the boot language and never re-resolve on a language switch.
 * The lookup happens in `effortLabel()`, which runs during render.
 *
 * Shaped as a flat `Record` of full literal keys, and indexed inline at the
 * `i18nT()` call, because that is the form `scripts/check-i18n-keys.mjs` can
 * resolve statically — a key it cannot resolve is a key it cannot verify exists.
 */
export const EFFORT_LABEL_KEY: Record<string, string> = {
  '': 'lib.effort.default',
  default: 'lib.effort.default',
  none: 'lib.effort.none',
  low: 'lib.effort.low',
  medium: 'lib.effort.medium',
  high: 'lib.effort.high',
  xhigh: 'lib.effort.xhigh',
  max: 'lib.effort.max',
}

/**
 * Localised display name for an effort level.
 *
 * A level the backend reports dynamically (via `/api/effort-levels`) that has no
 * entry above has no catalog entry either, so it is returned VERBATIM. It used to
 * be title-cased (`charAt(0).toUpperCase() + slice(1)`), which was wrong twice
 * over: it dressed a raw backend identifier up as English display copy in every
 * locale, and `toUpperCase()` is locale-insensitive, so the result was not even
 * reliably English-correct. Returning the identifier unchanged makes it legible
 * as an identifier and leaves no fabricated copy on screen.
 */
export function effortLabel(level: string): string {
  // `hasOwnProperty`, not `in`: the levels come from /api/effort-levels, so a
  // backend that reports `toString` or `constructor` would otherwise resolve to
  // an inherited Object.prototype member and hand a function to i18next.
  return Object.prototype.hasOwnProperty.call(EFFORT_LABEL_KEY, level)
    ? i18nT(EFFORT_LABEL_KEY[level])
    : level
}

/**
 * Concrete effort levels offered in the dropdown, ordered low→high, with the
 * '' default sentinel first. kiro-cli (acp) supports these on Fable/Opus/Sonnet
 * and GPT-5.x models.
 */
export const EFFORT_LEVELS = ['', 'low', 'medium', 'high', 'xhigh', 'max'] as const

/** Providers whose backend accepts a reasoning-effort level. KiroCrew is
 *  KiroACP-only, so this is just 'acp'. */
export const REASONING_EFFORT_PROVIDERS = new Set(['acp'])

/**
 * Every effort level any backend reports, ordered low→high. `none` exists only
 * on the GPT models (kiro-cli 2.21). This is the ORDER used for nearest-lower
 * fallback; which subset a given model accepts is `effortLevelsForModel`.
 */
export const EFFORT_ORDER = ['none', 'low', 'medium', 'high', 'xhigh', 'max'] as const

const LEVELS_CLAUDE: readonly string[] = ['low', 'medium', 'high', 'xhigh', 'max']
const LEVELS_GPT: readonly string[] = ['none', 'low', 'medium', 'high', 'xhigh', 'max']
const LEVELS_SONNET_46: readonly string[] = ['low', 'medium', 'high', 'max']

/**
 * The effort levels a model accepts, as kiro-cli reports them over ACP
 * `/effort` (probed per model against kiro-cli 2.21.4):
 *
 *  - Opus 5 / 4.8 / 4.7, Sonnet 5, Fable 5 / 5.1: low, medium, high, xhigh, max
 *  - Sonnet 4.6: low, medium, high, max (no xhigh)
 *  - GPT-5.6 sol / terra / luna: none, low, medium, high, xhigh, max
 *  - Opus 4.5, Sonnet 4.5, Sonnet 4, Haiku, auto, deepseek, minimax, glm, qwen,
 *    nova: effort is rejected outright
 *
 * Returns `null` for a model that rejects effort and `undefined` for a family
 * this table does not know, so the caller can fall back to a live answer.
 * kiro-cli does not expose these over the protocol (`session/new` carries no
 * `configOptions`), so the table is the only cold-start source; a live list
 * from `/api/effort-levels` still wins where the backend has one.
 */
export function effortLevelsForModel(model: string | undefined): readonly string[] | null | undefined {
  if (!model) return null
  const m = model.toLowerCase()
  if (m === 'auto' || m.includes('haiku')) return null
  if (/deepseek|minimax|glm|qwen|nova/.test(m)) return null
  if (m.includes('gpt')) return LEVELS_GPT
  if (m.includes('fable')) return LEVELS_CLAUDE
  const claude = /(sonnet|opus)-(\d+)(?:[.-](\d+))?/.exec(m)
  if (claude) {
    const family = claude[1]
    const major = Number(claude[2])
    const minor = claude[3] === undefined ? 0 : Number(claude[3])
    if (family === 'sonnet') {
      if (major === 4 && minor === 6) return LEVELS_SONNET_46
      if (major === 4) return null // 4 and 4.5 reject effort
      return LEVELS_CLAUDE
    }
    if (major === 4 && minor === 5) return null
    return LEVELS_CLAUDE
  }
  if (m.includes('sonnet') || m.includes('opus')) return LEVELS_CLAUDE
  return undefined
}

/**
 * Per-model effort capability — mirrors the backend `model_supports_effort`
 * (kiro_crew/effort.py). Answers from the per-model table first, so the
 * versions kiro rejects (Sonnet 4 / 4.5, Opus 4.5) hide the control instead of
 * offering a level the backend would refuse; an unknown family falls back to
 * the conservative family allowlist.
 */
export function modelSupportsEffort(model: string | undefined): boolean {
  const known = effortLevelsForModel(model)
  if (known !== undefined) return known !== null
  if (!model) return false
  const m = model.toLowerCase()
  return m.includes('opus') || m.includes('sonnet') || m.includes('fable') || m.includes('gpt')
}

/**
 * The level a model will actually run at when asked for `requested`.
 *
 * Same level when the model accepts it; otherwise the nearest LOWER level it
 * does accept (Sonnet 4.6 asked for `xhigh` runs `high`), never a higher one.
 * Only when nothing lower exists (`none` on a Claude model) does it step up to
 * the lowest accepted level. '' (no request) stays ''.
 */
export function nearestSupportedEffort(requested: string, levels: readonly string[]): string {
  if (!requested) return ''
  if (levels.includes(requested)) return requested
  const at = EFFORT_ORDER.indexOf(requested as (typeof EFFORT_ORDER)[number])
  // A level this vocabulary cannot rank has no "nearest lower": say nothing
  // rather than pin it to an arbitrary notch.
  if (at < 0) return ''
  for (let i = at - 1; i >= 0; i--) {
    if (levels.includes(EFFORT_ORDER[i])) return EFFORT_ORDER[i]
  }
  for (let i = at + 1; i < EFFORT_ORDER.length; i++) {
    if (levels.includes(EFFORT_ORDER[i])) return EFFORT_ORDER[i]
  }
  return ''
}
