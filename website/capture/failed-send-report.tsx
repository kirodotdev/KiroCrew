/**
 * Failure surfaces for the two `api.sendChat` callers fixed in #4198.
 *
 *   ?site=feature — the feature-request flow's transcript after a refused
 *     send: the optimistic user bubble with the error row under it, rendered
 *     through the REAL row registry (`createTranscriptRenderers` over
 *     `ChatMessageList`), so the frame shows exactly what a reader sees.
 *     Before the fix this surface showed the bubble alone, next to a slot
 *     stuck `running`.
 *   ?site=scene — the scene popover composer, driven END-TO-END through the
 *     real `useSceneInteraction` hook: the runner clicks the agent, types,
 *     and sends; the stubbed `/api/chat` answers 409 `{ok:false}` — which
 *     RESOLVES, the exact shape the old code reported as 'sent'. The frame
 *     shows the red Retry state with the payload handed back to the draft.
 *
 * No Tailwind classes are hand-written here beyond ones already used under
 * `src/` (capture/ is outside the Tailwind content glob, so novel classes
 * would silently not compile — see transcript-row-style.tsx for the full
 * argument).
 *
 *   ?theme=dark|light
 */
import { useRef } from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import { i18nT } from '../src/i18n/t'
import { store } from '../src/store'
import ChatMessageList from '../src/app-sdk/ChatMessageList'
import { createTranscriptRenderers } from '../src/pages/chat/transcriptRenderers'
import { useSceneInteraction, type SceneAgent } from '../src/hooks/useSceneInteraction'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const site = params.get('site') || 'feature'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// The scene site drives the REAL send path, so the network layer is the one
// thing stubbed: `/api/chat` answers a refused send the way a live gateway
// does (an HTTP error RESOLVES — the failure the receipt check exists for),
// and the popover's history poll gets an empty thread.
const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.startsWith('/api/chat?')) {
    return Promise.resolve(new Response(JSON.stringify({ ok: false, error: 'slot agent mismatch' }), {
      status: 409, headers: { 'Content-Type': 'application/json' },
    }))
  }
  if (url.startsWith('/api/chat/slots/')) return Promise.resolve(Response.json({ messages: [] }))
  if (url.startsWith('/api/file-read')) {
    return Promise.resolve(new Response(null, { status: 200, headers: { 'X-Path-Kind': 'file' } }))
  }
  if (url.startsWith('/api/link-meta')) return Promise.resolve(Response.json({}))
  return realFetch(input as RequestInfo, init)
}) as typeof fetch

initI18n('en')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

// ── Site: feature-request transcript after a refused send ────────────────────

let seq = 0
const msg = (role: string, content: string): ChatMessage => ({
  role,
  content,
  cls: '',
  ts: `2026-08-17T00:00:${String(seq++).padStart(2, '0')}.000Z`,
})

function FeatureSite() {
  const renderers = createTranscriptRenderers({
    slot: 'main',
    onFileOpen: () => {},
    onFolderOpen: () => {},
    onOpenSubagentPanel: () => {},
    onToolDisclosureChange: () => {},
    toolDisclosure: {},
    appInPanel: false,
    onOpenApp: () => {},
  })
  // Exactly what the fixed flow leaves on screen: the optimistic bubble the
  // user's click produced, and the error row `reportFailedSend` appends with
  // the server's own reason, framed by the existing catalog entry.
  // (`setSlotRunning(false)` has no visual row — its effect is the ABSENCE of
  // the running footer here.)
  const messages: ChatMessage[] = [
    msg('user', "I'd like to request a feature"),
    msg('error', i18nT('apps.designTweak.status.send_failed', { error: 'slot agent mismatch' })),
  ]
  return (
    <div data-capture-root className="bg-bg text-text py-4" style={{ width: 760, ['--mc-content-width' as string]: '680px' }}>
      <ChatMessageList messages={messages} running={false} contentWidth="680px" renderers={renderers} />
    </div>
  )
}

// ── Site: scene popover composer, real hook, real send path ─────────────────

const W = 600
const H = 400
const AGENT: SceneAgent = {
  id: 'slot-a', name: 'Alpha', x: 150, y: 150, running: false, detail: 'idle', kind: 'slot', color: '#8cf',
}

function SceneSite() {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const agentsRef = useRef<SceneAgent[]>([AGENT])
  const { canvasProps, tooltipEl } = useSceneInteraction(
    canvasRef, agentsRef, W, H,
    { active: 'Working', idle: 'Idle' }, 12, undefined, [],
  )
  return (
    <div data-capture-root className="bg-bg text-text" style={{ position: 'relative', width: W, height: H }}>
      <canvas data-testid="scene" ref={canvasRef} width={W} height={H} {...canvasProps} />
      {tooltipEl}
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        {site === 'scene' ? <SceneSite /> : <FeatureSite />}
      </Provider>
    </QueryClientProvider>
  </MemoryRouter>,
)
