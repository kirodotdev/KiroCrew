import { useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'

/**
 * useSettingHighlight — deep-link + highlight hook for Settings.
 *
 * Reads `?highlight=<id>` from the URL, resolves the id to a label via
 * SETTINGS_REGISTRY, finds the element by `data-setting-label`, scrolls it
 * into view, applies a temporary 2s ring flash, then strips the param.
 */
export function useSettingHighlight(): void {
  const [params, setParams] = useSearchParams()
  const highlightId = params.get('highlight')

  useEffect(() => {
    if (!highlightId) return

    // Resolve id → label
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
      const matches = document.querySelectorAll(`[data-setting-label="${CSS.escape(entry.label)}"]`)
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
  }, [highlightId, setParams])
}
