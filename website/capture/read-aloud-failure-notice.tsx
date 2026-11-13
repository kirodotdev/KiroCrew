/**
 * Isolated capture entry for the Read aloud failure notice.
 *
 * Renders the REAL VoicePlaybackNotice against the real stylesheet, theme
 * tokens and i18n catalog, driven by the same `voice-error` window event the
 * playback owner dispatches. The `code` query selects which failure the notice
 * renders, so the before (generic `failed`) and the new cause
 * (`voice_polly_rejected`) are each seen in place.
 *
 * Query: ?theme=dark|light  ?code=<voice failure code>
 */
import { useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'

import VoicePlaybackNotice from '../src/components/VoicePlaybackNotice'
import { reportVoiceFailure } from '../src/lib/voiceFailure'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const code = params.get('code') || 'failed'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLOT = 'chat-1-read-aloud'

function Scene() {
  // Fire the same event the WebSocket playback owner raises on a failed
  // synthesis, so the notice renders its real journalled copy for this code.
  useEffect(() => {
    reportVoiceFailure({ slot: SLOT, code })
  }, [])
  return (
    <div className="h-screen bg-bg text-text p-4" data-capture-page>
      <div className="w-full max-w-[760px] mx-auto" data-capture-root>
        <VoicePlaybackNotice slot={SLOT} />
      </div>
    </div>
  )
}

async function mount() {
  await initI18n()
  createRoot(document.getElementById('root')!).render(
    <MemoryRouter>
      <Scene />
    </MemoryRouter>,
  )
}

void mount()
