/**
 * Evidence for the startup feature-intro video modal.
 *
 * Mounts the REAL `StartupVideoModal` through the REAL api client and the REAL
 * placeholder asset, so the frame photographs shipped markup, shipped strings and
 * the actual poster rather than a mock-up. Only the transport is stubbed: one
 * `fetch` shim answers `GET /api/feature-videos/next` with a fixture and swallows
 * the feedback POST, because there is no gateway behind a capture page.
 *
 * Scenes, selected with `?scene=`:
 *
 *   ?scene=share-off — governance says `social_share_enabled: false` (and the
 *     absent-prop case renders identically, since the prop defaults to false).
 *     This is the frame that shows there is no share control in ANY state — not a
 *     greyed one — which is the fail-closed claim the PR makes.
 *
 *   ?scene=share-on — governance granted. The share entry appears beside the
 *     acknowledgement; nothing that reaches an intent URL exists until it is
 *     pressed.
 *
 * `?motion=reduce` forces the reduce-motion branch, so the entrance frame can be
 * shown to be static rather than mid-transition.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'

import StartupVideoModal from '../src/components/StartupVideoModal'
import { store } from '../src/store'
import { initI18n } from '../src/i18n'
import { applyFallbackTheme } from '../src/apps/mochi/src/shared/themes'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const shareEnabled = params.get('scene') === 'share-on'

document.documentElement.setAttribute('data-theme', 'kiro-dark')
applyFallbackTheme()
initI18n('en')

/** Realistic copy, so the frame reads like a shipped clip rather than lorem ipsum. */
const FIXTURE = {
  video: {
    id: 'startup-videos-1',
    feature: 'startup-videos',
    title: 'Feature videos',
    description:
      'A short clip introduces each new feature the first time you launch. Watch it '
      + 'once and it never comes back.',
    // The fixture clip lives beside this page, NOT under `public/`: `public/` is
    // copied into every shipped `dist/`, and nothing in production names this
    // file. Vite's dev server serves it from here for the capture run only.
    src: '/capture/assets/placeholder.mp4',
    poster: '/capture/assets/placeholder.jpg',
    duration_s: 5,
    // The real contract shape: `feature_videos.CATALOG` stores a bare docs
    // filename, and no resolved `doc_link` ships beside it. Never rendered.
    doc: 'feature-tips.md',
  },
  enabled: true,
}

// Transport-only stub. The component still goes through `api.featureVideoNext`,
// its react-query wiring and its error handling — this just gives that request
// something to resolve against.
const realFetch = window.fetch.bind(window)
window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/feature-videos/next')) {
    return Promise.resolve(new Response(JSON.stringify(FIXTURE), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }))
  }
  if (url.includes('/api/feature-videos/feedback')) {
    return Promise.resolve(new Response(JSON.stringify({ ok: true }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }))
  }
  // The modal's pre-open HEAD probe on the clip. Vite's dev server would answer
  // this 200 on its own; answering here keeps the capture independent of how the
  // dev server treats HEAD, so the dialog opens for the screenshot the same way
  // regardless.
  if (init?.method === 'HEAD' && url.includes('/capture/assets/')) {
    return Promise.resolve(new Response(null, { status: 200 }))
  }
  return realFetch(input as RequestInfo, init)
}) as typeof window.fetch

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

// The redux Provider is REQUIRED, not decoration: the modal reads the active slot
// key off the store (`s.chat.activeSlot`) so the verdict write can carry a session
// identity. Without it `useAppSelector` throws and the dialog never mounts -- which
// is exactly how this harness broke once already, silently, on the round that added
// that read.
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
  <QueryClientProvider client={queryClient}>
    <StartupVideoModal shareEnabled={shareEnabled} onClose={() => {}} />
  </QueryClientProvider>
  </Provider>,
)
