/**
 * gallery.tsx — the avatar gallery's window entry.
 *
 * Thin on purpose: GalleryPanel owns the whole surface, including its own ✕ and
 * Escape handling, because the window is frameless and has no traffic lights.
 */
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { adoptDashboardTheme, watchThemeChanges } from './dashboardTheme'
import { initI18n } from '../../i18n'
import { GalleryPanel } from './GalleryPanel'

const host = document.getElementById('root')
if (host) {
  initI18n()
  // Await the theme before the first paint: the gallery is styled from Kiro Crew's
  // variables, and rendering ahead of them shows fallback colours and then snaps.
  void adoptDashboardTheme().then(() => {
    watchThemeChanges()
    createRoot(host).render(
      <StrictMode>
        <GalleryPanel />
      </StrictMode>,
    )
  })
}
