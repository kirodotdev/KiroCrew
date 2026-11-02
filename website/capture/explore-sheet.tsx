/**
 * Isolated capture entry for the Explore panel (MilestonesSheet).
 *
 * WHY ISOLATED: Explore needs a milestones read, the intro clip's Feature
 * Videos library and the app shell's store. Here the two reads are answered
 * with fabricated data on the real `api` object, so the REAL sheet renders
 * without a gateway. `?video=1` keeps the intro clip card up; `?mobile=1` is
 * picked up from the viewport by `useIsMobile`.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { initI18n } from '../src/i18n'
import { api } from '../src/api/client'
import type { MilestoneFeatureItem, Milestones } from '../src/api/client/featureDiscovery'
import { store } from '../src/store'
import MilestonesSheet from '../src/components/milestones/MilestonesSheet'
import { openMilestonesPanel } from '../src/components/milestones/useMilestones'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const withVideo = params.get('video') !== '0'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

function feature(id: string, state: MilestoneFeatureItem['state'], send: 'send' | 'prefill' = 'prefill'): MilestoneFeatureItem {
  return {
    id, state, done_at: state === 'done' ? 1793170000 : null, source: state === 'done' ? 'event' : null, video: null,
    starter: { kind: 'prompt', send, route: '', agent: '', crew_mode: false },
    doc: 'artifacts.md',
  }
}

const DATA: Milestones = {
  enabled: true,
  features: { done: 3, total: 7 },
  streak: { current: 1, best: 1 },
  groups: [
    { id: 'features', items: [
      feature('widgets', 'done', 'send'),
      feature('scheduled-task', 'open'),
      feature('mate', 'done'),
      feature('parallel-chats', 'done', 'send'),
      feature('slack', 'skipped'),
      feature('pr-watch', 'open'),
      feature('crew-mode', 'skipped'),
    ] },
    { id: 'sessions', items: [{ id: 'chats-opened', value: 1316, tiers: [10, 100, 1000], tiers_reached_at: { 10: 1793170000, 100: 1793170000, 1000: 1793170000 } }] },
    { id: 'streak', items: [{ id: 'streak-best', value: 1, tiers: [3, 7, 30, 100], tiers_reached_at: {} }] },
  ],
  recommended: [],
  notices: [],
}

const a = api as unknown as Record<string, unknown>
a.milestones = () => Promise.resolve(DATA)
a.milestoneEvent = () => Promise.resolve({})
a.featureVideoLibrary = () => Promise.resolve({
  enabled: true,
  videos: withVideo ? [{
    id: 'kiro-crew-intro', feature: 'intro', title: 'Kiro Crew in 15 seconds', description: '',
    src: '/app-assets/feature-videos/kiro-crew-intro.mp4', poster: '/app-assets/feature-videos/kiro-crew-intro.jpg', duration_s: 15,
  }] : [],
})

initI18n('en')
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/chat']}>
        <div className="fixed inset-0 bg-bg text-muted p-6 text-[13px]" data-scene="backdrop">Chat content behind the panel</div>
        <MilestonesSheet />
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
openMilestonesPanel()
