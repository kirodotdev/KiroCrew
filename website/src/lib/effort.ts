/**
 * Reasoning-effort vocabulary for the dashboard — mirrors the backend
 * `kiro_crew/effort.py` so the UI and server agree on levels and per-model
 * capability. Kept as a standalone module (not inside ChatInput) so it can be
 * imported without pulling in the component — and so test mocks of ChatInput
 * don't have to re-export it.
 */

import { isNotFoundError } from '../api/apiError'
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
 * '' default sentinel first. kiro-cli (acp) supports these on Fable/Opus/Sonnet,
 * Haiku 5.5 and later, and GPT-5.x models.
 */
export const EFFORT_LEVELS = ['', 'low', 'medium', 'high', 'xhigh', 'max'] as const

/** Providers whose backend accepts a reasoning-effort level. KiroCrew is
 *  KiroACP-only, so this is just 'acp'. */
export const REASONING_EFFORT_PROVIDERS = new Set(['acp'])

/** The first Haiku release that accepts an effort level — mirrors the backend
 *  `HAIKU_EFFORT_MIN_VERSION` (kiro_crew/effort.py). */
export const HAIKU_EFFORT_MIN_VERSION: readonly [number, number] = [5, 5]

/** `haiku-5.5`, `haiku-5-5` (Bedrock), `haiku-6`. One or two digits per part,
 *  so a Bedrock date suffix (`claude-3-5-haiku-20241022`) is never read as a
 *  version. Same pattern as the backend `_HAIKU_VERSION_RE`. */
const HAIKU_VERSION_RE = /haiku[-_.]?(\d{1,2})(?:[-_.](\d{1,2}))?(?!\d)/

/**
 * True when the Haiku id `model` is version 5.5 or later — mirrors the backend
 * `haiku_supports_effort`. A missing minor counts as 0; an id whose version
 * cannot be read answers false, so an unknown Haiku keeps the control hidden.
 */
export function haikuSupportsEffort(model: string): boolean {
  const match = HAIKU_VERSION_RE.exec(model.toLowerCase())
  if (!match) return false
  const major = Number(match[1])
  const minor = Number(match[2] ?? 0)
  const [minMajor, minMinor] = HAIKU_EFFORT_MIN_VERSION
  return major > minMajor || (major === minMajor && minor >= minMinor)
}

/**
 * Per-model effort capability — mirrors the backend `model_supports_effort`
 * (kiro_crew/effort.py): effort is available on Fable/Opus/Sonnet, Haiku 5.5
 * and later, and GPT-5.x models; older Haiku, auto/empty and the other
 * third-party models (deepseek, minimax, glm, qwen) cannot use it. Gates the
 * dropdown so a non-capable model never shows a control that would silently
 * no-op on the backend.
 *
 * Keep this in sync with the backend allowlist — it is a conservative list of
 * known-capable families, not a "non-Claude means unsupported" denylist.
 */
export function modelSupportsEffort(model: string | undefined): boolean {
  if (!model) return false
  const m = model.toLowerCase()
  if (m === 'auto') return false
  if (m.includes('haiku')) return haikuSupportsEffort(m)
  return m.includes('opus') || m.includes('sonnet') || m.includes('fable') || m.includes('gpt')
}

/**
 * Did the slot's selection-capability read actually FAIL?
 *
 * A 404 is not a failure: the gateway answers `slot_not_found` while a new chat's
 * slot has not been registered yet, which is the same "no ACP session has
 * advertised its options" state that `known: false` reports. Treating it as an
 * error flashed the "could not verify effort options" notice, and disabled the
 * effort control, on every cold-start poll (issue #14817). Only a real fault --
 * a 403, a 5xx, a transport failure -- counts, which keeps the
 * notice's "until the connection recovers" claim true.
 */
export function selectionCapabilitiesFailed(q: { isError: boolean; error?: unknown }): boolean {
  return q.isError && !isNotFoundError(q.error)
}
