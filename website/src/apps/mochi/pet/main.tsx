/**
 * Mochi pet window — entry point (mirrors the original pet.tsx).
 *
 * Renders the VENDORED ORIGINAL component from src/renderer/. The seam is the
 * typed `api` module (src/mochiApi.ts), imported by the component itself — this
 * entry only mounts it and writes the theme variables.
 */
import React from 'react'
import { createRoot } from 'react-dom/client'

import { PetWidget } from '../src/renderer/PetWidget'
import { applyThemeVarsOnly } from '../src/shared/themes'
import { MochiLocalized, initMochiI18n } from '../mochiLanguage'

// The pet draws free-floating shapes over a transparent window, so its CSS
// custom properties must exist before the first paint.
applyThemeVarsOnly()

/**
 * A render error in this window has the worst possible failure shape: React
 * unmounts the tree, the transparent overlay shows NOTHING, and there is no
 * user-visible signal at all — the pet just ceases to exist until a restart.
 * (A stale theme id crashing the bubble overlay did exactly this.) The
 * boundary logs the error and retries the render after a short delay; the
 * fallback stays empty on purpose — an error card floating over the desktop
 * would be worse than a briefly absent pet.
 */
const RETRY_RENDER_MS = 10_000

class PetErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { failed: boolean }
> {
  state = { failed: false }

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true }
  }

  componentDidCatch(error: unknown): void {
    console.error('[mochi-pet] render crashed; retrying shortly', error)
    setTimeout(() => this.setState({ failed: false }), RETRY_RENDER_MS)
  }

  render(): React.ReactNode {
    return this.state.failed ? null : this.props.children
  }
}

// Seeded before first paint so no window flashes the fallback language.
initMochiI18n()

const el = document.getElementById('root')
if (el) {
  createRoot(el).render(
    <MochiLocalized remount={false}>
      <PetErrorBoundary>
        <PetWidget />
      </PetErrorBoundary>
    </MochiLocalized>,
  )
}

// Presence heartbeat, every 30s for as long as this renderer exists. The beat
// ARRIVING tells the backend the Electron shell is running (gates autonomous
// work — a web-only dashboard session cannot open Mochi, so background agents
// would burn tokens for surfaces that cannot exist); `visible` tells it the
// pet is actually on screen (gates companion time). hideAll hides the window
// WITHOUT destroying it, so a hidden pet keeps beating with visible=false —
// polling continues (the original polled while hidden too), only the
// companionship clock stops. Quitting the shell stops the beat entirely.
const PRESENCE_BEAT_MS = 30_000
const beat = () => {
  void fetch('/api/apps/mochi/presence', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ visible: document.visibilityState === 'visible' }),
  }).catch(() => {})
}
beat()
setInterval(beat, PRESENCE_BEAT_MS)
document.addEventListener('visibilitychange', beat)
