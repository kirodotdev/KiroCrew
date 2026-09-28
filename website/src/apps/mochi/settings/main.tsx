/**
 * Mochi Settings window — entry point (mirrors the original settingsEntry.tsx).
 *
 * Renders the VENDORED ORIGINAL component from src/renderer/. The seam is the
 * typed `api` module (src/mochiApi.ts), imported by the component itself — this
 * entry only mounts it and writes the theme variables.
 */
import { useEffect } from 'react'
import { createRoot } from 'react-dom/client'

import { SettingsPanel } from '../src/renderer/SettingsPanel'
import { PanelErrorBoundary } from '../panel/PanelErrorBoundary'
import { api } from '../src/mochiApi'
import { applyTheme } from '../src/shared/themes'
import { MochiLocalized, initMochiI18n } from '../mochiLanguage'
import { i18nT } from '../../../i18n/t'

function SettingsWindow() {
  useEffect(() => {
    applyTheme()
    document.title = i18nT('apps.mochi.settingsPanel.title')
  }, [])

  // Closing the WINDOW is the close affordance; the panel's own x calls this.
  return <SettingsPanel onClose={() => void api.closeSettings?.()} />
}

// Seeded before first paint so no window flashes the fallback language.
initMochiI18n()

const el = document.getElementById('root')
if (el) {
  createRoot(el).render(
    <MochiLocalized>
      <PanelErrorBoundary>
        <SettingsWindow />
      </PanelErrorBoundary>
    </MochiLocalized>,
  )
}
