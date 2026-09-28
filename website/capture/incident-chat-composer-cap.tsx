/**
 * Isolated capture entry for the ChatEmbed `composerMaxHeight` prop (#10881).
 *
 * Mounts the REAL components against the real stylesheet, theme tokens and live
 * i18n catalog; the transcript comes from the capture script's route
 * interception (gateway-free). Two scenes, the same 420px box:
 *
 *   ?scene=before — a plain `ChatEmbed` in a fixed 420px column with no cap
 *                   passed, i.e. the shared 240px default. Shows the defect:
 *                   a long draft takes most of the box from the transcript.
 *   ?scene=after  — the real `IncidentChat`, which now passes the prop.
 *
 * The script fills the composer with a long draft and reads the textarea's
 * box height and the transcript scroller's height off `window.__measure()`.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ChatEmbed from '../src/app-sdk/ChatEmbed'
import { AppScopedApiProvider } from '../src/app-sdk/scopedApi'
import IncidentChat, { INCIDENT_CHAT_BOX_HEIGHT_PX } from '../src/apps/ops-mission-control/IncidentChat'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'after'
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

declare global {
  interface Window {
    __measure: () => { textareaHeight: number; boxHeight: number; transcriptHeight: number }
  }
}

window.__measure = () => {
  const textarea = document.querySelector('textarea') as HTMLTextAreaElement
  const box = document.querySelector('[data-capture-box]') as HTMLElement
  // The transcript scroller is the embed's `flex-1 overflow-y-auto` child.
  const transcript = box.querySelector('.overflow-y-auto:not(textarea)') as HTMLElement
  return {
    textareaHeight: textarea.getBoundingClientRect().height,
    boxHeight: box.getBoundingClientRect().height,
    transcriptHeight: transcript ? transcript.getBoundingClientRect().height : -1,
  }
}

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
})

/** The unfixed shape: the same box, the embed left at its shared default cap. */
function BeforeBox() {
  return (
    <div
      className="mt-2 border-t border-border pt-2 flex flex-col"
      style={{ height: INCIDENT_CHAT_BOX_HEIGHT_PX }}
    >
      <p className="text-[12px] text-muted mb-2 shrink-0">Live investigation — INC-42 — Checkout latency</p>
      <div className="flex-1 min-h-0">
        <AppScopedApiProvider
          appName="ops-mission-control"
          allowedApiPaths={['/api/chat*']}
          allowedEvents={[]}
          navigateFn={() => {}}
        >
          <ChatEmbed slotKey="ops-mission-control-INC-42" placeholder="Ask about INC-42…" />
        </AppScopedApiProvider>
      </div>
    </div>
  )
}

async function main() {
  await initI18n()
  createRoot(document.getElementById('root')!).render(
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <div className="min-h-screen bg-bg text-text p-4" data-capture-root>
            <div className="w-[640px] rounded-md border border-border bg-card p-3" data-capture-box>
              <div className="text-[13px] font-medium">INC-42 · Checkout latency · investigating</div>
              {scene === 'before'
                ? <BeforeBox />
                : <IncidentChat incidentId="INC-42" title="Checkout latency" />}
            </div>
          </div>
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

main()
