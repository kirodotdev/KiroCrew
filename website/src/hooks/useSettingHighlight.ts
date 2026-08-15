import { useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'
import { i18nT } from '../i18n/t'

/**
 * Legacy highlight-id migrations. Registry ids are `<tab>.<kebab-label>`, so
 * they shift when a tab or label is renamed; bookmarks and palette history
 * keep the old ids. Map old → new here instead of letting the link silently
 * lose its highlight.
 *
 * - `slack.*` — the Slack tab collapsed into the Channels tab (nav regroup).
 * - `voice.aws-*` — labels gained (Transcribe)/(Polly) qualifiers, replacing
 *   the positional `-2` disambiguation suffix. The Polly pair then shifted
 *   again when the qualifier was corrected to the service's real name,
 *   Amazon Polly — so BOTH the positional id and the short-form id have to
 *   land on the current one.
 */
const LEGACY_ID_EXACT: Record<string, string> = {
  'voice.aws-profile': 'voice.aws-profile-transcribe',
  'voice.aws-region': 'voice.aws-region-transcribe',
  'voice.aws-profile-2': 'voice.aws-profile-amazon-polly',
  'voice.aws-region-2': 'voice.aws-region-amazon-polly',
  'voice.aws-profile-polly': 'voice.aws-profile-amazon-polly',
  'voice.aws-region-polly': 'voice.aws-region-amazon-polly',
  // The "Default Model" row is labeled "Fallback Model", and registry ids
  // derive from the label — without this, links saved or bookmarked against
  // the old id silently lose their highlight.
  'chat.default-model': 'chat.fallback-model',
}

/** Rewrite a legacy highlight id to its current form (identity for current ids). */
export function resolveLegacyHighlightId(id: string): string {
  if (LEGACY_ID_EXACT[id]) return LEGACY_ID_EXACT[id]
  if (id.startsWith('slack.')) return `channels.${id.slice('slack.'.length)}`
  return id
}

/**
 * Deep-link target for the "Default Model" row in Settings → Chat.
 *
 * Registry ids are derived from the setting's LABEL, so renaming that row
 * silently breaks any hard-coded link. Callers (the in-session model picker)
 * import this constant instead of inlining the string, and
 * `ModelEffortDropdown.defaultLink.test.tsx` asserts it still resolves in
 * SETTINGS_REGISTRY — so a rename fails a test instead of shipping a dead link.
 */
export const SETTINGS_DEFAULT_MODEL_ID = 'chat.fallback-model'

/**
 * useSettingHighlight — deep-link + highlight hook for Settings.
 *
 * Reads `?highlight=<id>` from the URL, resolves the id to the label rendered
 * in the active locale via SETTINGS_REGISTRY, finds the element by
 * `data-setting-label`, scrolls it into view, applies a temporary 2s ring
 * flash, then strips the param.
 *
 * Also accepts `?highlight=key:<configKey>` — first tries direct DOM lookup
 * via `data-setting-key` attribute (zero round-trip); falls back to resolving
 * the dotted config key to the registry entry's id via the configKey field,
 * then proceeds with the standard label-based DOM highlight.
 */
export function useSettingHighlight(): void {
  const [params, setParams] = useSearchParams()
  const rawHighlightId = params.get('highlight')

  // Resolve key: prefix to a registry id via configKey lookup
  let highlightId: string | null = null
  let directConfigKey: string | null = null
  if (rawHighlightId) {
    if (rawHighlightId.startsWith('key:')) {
      const configKey = rawHighlightId.slice(4)
      directConfigKey = configKey
      const entry = SETTINGS_REGISTRY.find(e => e.configKey === configKey)
      highlightId = entry ? entry.id : null
      // If no entry found for this configKey, still null — effect will strip param
      if (!highlightId) highlightId = rawHighlightId // let the effect handle the strip
    } else {
      highlightId = resolveLegacyHighlightId(rawHighlightId)
    }
  }

  useEffect(() => {
    if (!highlightId) return

    // key: path — try direct DOM lookup via data-setting-key FIRST
    if (directConfigKey) {
      const directEl = document.querySelector(`[data-setting-key="${CSS.escape(directConfigKey)}"]`) as HTMLElement | null
      if (directEl) {
        const timer = setTimeout(() => {
          directEl.scrollIntoView({ block: 'center', behavior: 'smooth' })
          directEl.style.outline = '2px solid var(--accent)'
          directEl.style.outlineOffset = '4px'
          directEl.style.borderRadius = '8px'
          directEl.style.transition = 'outline-color 0.3s ease'

          setTimeout(() => {
            directEl.style.outlineColor = 'transparent'
            setTimeout(() => {
              directEl.style.outline = ''
              directEl.style.outlineOffset = ''
              directEl.style.borderRadius = ''
              directEl.style.transition = ''
            }, 300)
          }, 2000)

          setParams(prev => {
            const next = new URLSearchParams(prev)
            next.delete('highlight')
            return next
          }, { replace: true })
        }, 100)
        return () => clearTimeout(timer)
      }
      // Fall through to legacy label-based resolution if data-setting-key not found
    }

    // Resolve id → label (legacy path)
    const entry = SETTINGS_REGISTRY.find(e => e.id === highlightId)
    if (!entry) {
      // Unknown id, strip param
      setParams(prev => {
        const next = new URLSearchParams(prev)
        next.delete('highlight')
        return next
      }, { replace: true })
      return
    }

    // Wait a tick for the panel to render
    const timer = setTimeout(() => {
      // Use querySelectorAll to handle duplicate labels within a tab.
      // entry.occurrence (1-based) identifies which DOM match to highlight.
      const renderedLabel = entry.labelKey ? i18nT(entry.labelKey) : entry.label
      const matches = document.querySelectorAll(`[data-setting-label="${CSS.escape(renderedLabel)}"]`)
      const el = matches[entry.occurrence - 1] ?? matches[0]
      if (el) {
        el.scrollIntoView({ block: 'center', behavior: 'smooth' })
        // Apply a temporary ring highlight using existing Tailwind tokens
        const htmlEl = el as HTMLElement
        htmlEl.style.outline = '2px solid var(--accent)'
        htmlEl.style.outlineOffset = '4px'
        htmlEl.style.borderRadius = '8px'
        htmlEl.style.transition = 'outline-color 0.3s ease'

        setTimeout(() => {
          htmlEl.style.outlineColor = 'transparent'
          setTimeout(() => {
            htmlEl.style.outline = ''
            htmlEl.style.outlineOffset = ''
            htmlEl.style.borderRadius = ''
            htmlEl.style.transition = ''
          }, 300)
        }, 2000)
      }

      // Strip the highlight param
      setParams(prev => {
        const next = new URLSearchParams(prev)
        next.delete('highlight')
        return next
      }, { replace: true })
    }, 100)

    return () => clearTimeout(timer)
  }, [highlightId, directConfigKey, setParams])
}
