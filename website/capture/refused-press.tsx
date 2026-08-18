/**
 * Evidence for the refused-press notice (regenerate / switch-variant).
 *
 * THE BUG: the server can refuse a regenerate or switch-variant press under the
 * slot lock (a turn already running, a stop in progress, a pending approval, a
 * readiness probe that timed out) and the refusal went to `console.warn`, so
 * the pressed control flicked to disabled and straight back with nothing on
 * screen.
 *
 * The scene composes the REAL surface out of `src/`: the notice wrapper
 * ChatPage renders above the composer and the composer itself. The titles come
 * from the live catalog through `i18nT`, so a frame also proves the two new
 * keys resolve — nothing here re-implements the components, their classes, or
 * their strings.
 *
 *   ?scene=before|regenerate|switch_variant   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import ChatInput from '../src/components/ChatInput'
import ErrorNotice from '../src/components/ErrorNotice'
import { initI18n } from '../src/i18n'
import { i18nT } from '../src/i18n/t'
import { store } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const rawScene = params.get('scene')
const scene = rawScene === 'regenerate' || rawScene === 'switch_variant' ? rawScene : 'before'
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/')) {
    // The composer's slash-command menu maps over its response, so an object
    // stub crashes it and the harness screenshots an unmounted tree. Arrays for
    // list endpoints, `{}` for the rest.
    const body = /commands|models|agents|skills|slots|projects/.test(url) ? '[]' : '{}'
    return Promise.resolve(new Response(body, { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

/** The reason a real refusal carries -- the server's own prose, verbatim from
 * the wired endpoints' refusal bodies, so a frame documents the shipped render
 * rather than an idealized one. */
const REFUSALS = {
  regenerate: 'slot is running',
  switch_variant: 'no variants',
} as const

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <div data-capture-root className="bg-bg text-text flex flex-col gap-2 py-5 w-[900px]">
          {scene !== 'before' && (
            <div
              className="px-4 mb-1.5 mx-auto w-full"
              style={{ maxWidth: 'var(--mc-content-width, 900px)' }}
              data-testid="refused-press-error"
            >
              <ErrorNotice
                title={i18nT(scene === 'regenerate' ? 'pages.chatPage.could_not_regenerate' : 'pages.chatPage.could_not_switch_variant')}
                message={REFUSALS[scene]}
                onDismiss={() => {}}
              />
            </div>
          )}
          {/* The composer's real container in ChatPage — the notice above mirrors
              it, so the frame shows the true alignment rather than a harness one. */}
          <div className="px-4 pb-2 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
            <ChatInput
              value=""
              onChange={() => {}}
              onSend={() => {}}
              connected
            />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
