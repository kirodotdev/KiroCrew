import { useEffect } from 'react'
import { useLocation, useSearchParams } from 'react-router-dom'
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
 * - `chat.fallback-model` — the row was relabeled from "Fallback Model" to
 *   "Default Model", the tier it actually is.
 */
const LEGACY_ID_EXACT: Record<string, string> = {
  'voice.aws-profile': 'voice.aws-profile-transcribe',
  'voice.aws-region': 'voice.aws-region-transcribe',
  'voice.aws-profile-2': 'voice.aws-profile-amazon-polly',
  'voice.aws-region-2': 'voice.aws-region-amazon-polly',
  'voice.aws-profile-polly': 'voice.aws-profile-amazon-polly',
  'voice.aws-region-polly': 'voice.aws-region-amazon-polly',
  // The "Default Model" row was labeled "Fallback Model", and registry ids
  // derive from the label — without this, links saved or bookmarked against
  // the old id silently lose their highlight.
  'chat.fallback-model': 'chat.default-model',
  // The pin toggle's label moved from "prompt" to "turn" vocabulary, shifting
  // the derived id with it.
  'chat.pin-the-latest-prompt': 'chat.pin-the-latest-turn',
}

/** Current registry ids, for fail-safe legacy rewrites below. */
const REGISTRY_IDS = new Set(SETTINGS_REGISTRY.map(e => e.id))

/** Rewrite a legacy highlight id to its current form (identity for current ids). */
export function resolveLegacyHighlightId(id: string): string {
  if (LEGACY_ID_EXACT[id]) return LEGACY_ID_EXACT[id]
  if (id.startsWith('slack.')) id = `channels.${id.slice('slack.'.length)}`
  // Per-channel rows gained a "(<Channel>)" label suffix so their ids are
  // channel-qualified and order-stable. Every pre-suffix `channels.*` id in a
  // bookmark was a SlackPanel row (the only channels panel the extractor
  // mapped before the fan-out), so retarget those to the `-slack` form —
  // fail-safe: only when the bare id no longer resolves and the slack form does.
  if (id.startsWith('channels.') && !REGISTRY_IDS.has(id) && REGISTRY_IDS.has(`${id}-slack`)) {
    return `${id}-slack`
  }
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
export const SETTINGS_DEFAULT_MODEL_ID = 'chat.default-model'

/**
 * useSettingHighlight — deep-link + highlight hook for Settings.
 *
 * Reads `?highlight=<id>` from the URL, resolves the id to the label rendered
 * in the active locale via SETTINGS_REGISTRY, finds the element by
 * `data-setting-label`, scrolls it into view, applies a temporary 2s ring
 * flash, then strips the param.
 * Entries with an explicit settingId instead wait for that data-setting-id
 * row, so a cold panel cannot highlight a different same-label control.
 *
 * Also accepts `?highlight=key:<configKey>` — first tries direct DOM lookup
 * via `data-setting-key` attribute (zero round-trip); falls back to resolving
 * the dotted config key to the registry entry's id via the configKey field,
 * then proceeds with the standard label-based DOM highlight.
 */
export function useSettingHighlight(): void {
  const [params, setParams] = useSearchParams()
  const location = useLocation()
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

    const entry = SETTINGS_REGISTRY.find(e => e.id === highlightId)
    const settingId = entry?.settingId
    // Explicit UI identities and schema keys both survive an async panel load.
    if (settingId || directConfigKey) {
      const findDirectTarget = (): HTMLElement | null => {
        if (settingId) return document.querySelector<HTMLElement>(`[data-setting-id="${CSS.escape(settingId)}"]`)
        if (directConfigKey) return document.querySelector<HTMLElement>(`[data-setting-key="${CSS.escape(directConfigKey)}"]`)
        return null
      }
      const findTarget = (): HTMLElement | null => {
        const direct = findDirectTarget()
        // A declared UI identity is authoritative even before it mounts.
        if (direct || settingId) return direct
        if (!entry) return null
        // Keep legacy unidentified controls reachable, but another setting's
        // schema key or UI identity must never satisfy a same-label request.
        const label = entry.labelKey ? i18nT(entry.labelKey) : entry.label
        const matches = document.querySelectorAll<HTMLElement>(`[data-setting-label="${CSS.escape(label)}"]`)
        const candidate = matches[entry.occurrence - 1] ?? matches[0]
        return candidate && !candidate.hasAttribute('data-setting-key') && !candidate.hasAttribute('data-setting-id') ? candidate : null
      }
      if (entry || findDirectTarget()) {
        let observer: MutationObserver | null = null
        const highlightTarget = (): boolean => {
          const el = findTarget()
          if (!el) return false
          observer?.disconnect()
          el.scrollIntoView({ block: 'center', behavior: 'smooth' })
          el.style.outline = '2px solid var(--accent)'
          el.style.outlineOffset = '4px'
          el.style.borderRadius = '8px'
          el.style.transition = 'outline-color 0.3s ease'

          setTimeout(() => {
            el.style.outlineColor = 'transparent'
            setTimeout(() => {
              el.style.outline = ''
              el.style.outlineOffset = ''
              el.style.borderRadius = ''
              el.style.transition = ''
            }, 300)
          }, 2000)

          setParams(prev => {
            const next = new URLSearchParams(prev)
            next.delete('highlight')
            return next
          }, { replace: true })
          return true
        }
        const timer = setTimeout(() => {
          if (highlightTarget()) return
          // A cold settings query may outlive the initial render tick. Wait
          // only while this known target is pending, rather than guessing latency.
          observer = new MutationObserver(() => { highlightTarget() })
          observer.observe(document.body, { childList: true, subtree: true })
        }, 100)
        return () => {
          clearTimeout(timer)
          observer?.disconnect()
        }
      }
      // Unknown keys retain the legacy parameter-cleanup behavior below.
    }

    // Resolve id → label (legacy path)
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
    // location.key: every navigation re-arms the probe. Without it, the
    // legacy-URL translation (SettingsPage replace-navigates ?tab=X onto the
    // path form, mounting the target panel one commit LATER) would race this
    // effect's 100ms timer, which strips the param even when no element was
    // found — the re-run today only happens because react-router's
    // setParams identity churns with the search string, an implementation
    // detail nothing pins.
  }, [highlightId, directConfigKey, setParams, location.key])
}
