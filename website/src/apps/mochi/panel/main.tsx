/**
 * Mochi panel window — entry point (mirrors the original chatEntry.tsx).
 *
 * Renders the VENDORED ORIGINAL component from src/renderer/. The seam is the
 * typed `api` module (src/mochiApi.ts), imported by the component itself — this
 * entry only mounts it and writes the theme variables.
 */
import { createRoot } from 'react-dom/client'

import { ChatApp } from '../src/renderer/ChatApp'
import { PanelErrorBoundary } from './PanelErrorBoundary'
import { MochiSnipHost } from './MochiSnipHost'
import { applyTheme } from '../src/shared/themes'
import { MochiLocalized, initMochiI18n } from '../mochiLanguage'

applyTheme()

// Seeded before first paint so no window flashes the fallback language.
initMochiI18n()

const el = document.getElementById('root')
if (el) {
  createRoot(el).render(
    <MochiLocalized>
      <PanelErrorBoundary>
        <ChatApp />
        {/* Owns the screen-capture overlay; renders nothing until a capture starts. */}
        <MochiSnipHost />
      </PanelErrorBoundary>
    </MochiLocalized>,
  )
}
