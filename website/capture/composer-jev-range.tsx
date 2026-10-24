/**
 * Evidence for the Jev range controls inside the composer's model picker.
 *
 * Mounts the REAL ModelEffortDropdown over a slot in the REAL store, with the
 * list built by the REAL `withJevRoute()`. Only the slot fixture and the fetch
 * stub differ between states:
 *
 *   jev-no-range      Auto (Jev) picked, no range: every model ticked, both bounds on "Use my level"
 *   models-unchecked  the range names two models, the rest unticked
 *   effort-span       the range bounds effort to low..high, both switches off
 *   no-effort         a backend without effort support: checks, no sliders
 *   refused           the save answers 400; the driver ticks an unticked row
 *   preview-off       the preview is off, so no Jev row and no range controls
 *
 *   ?state=<one of the above>&theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { createRef } from 'react'

import ModelEffortDropdown from '../src/components/ModelEffortDropdown'
import { initI18n } from '../src/i18n/all'
import { i18nT } from '../src/i18n/t'
import { JEV_ROUTE_MODEL, withJevRoute } from '../src/lib/jevRoute'
import { store } from '../src/store'
import { addSlotOptimistic } from '../src/store/dashboardSlice'
import type { ChatSlot, JevRange } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n(params.get('lang') || 'en')

const SLOT = 'capture-slot'
// glm-5 second, so state 2's two ticked rows both sit above the list's fold.
const SERVED = ['gpt-5.6-sol', 'glm-5', 'gpt-5.6-terra', 'deepseek-3.2'].map(name => ({ name }))
const LEVELS = ['low', 'medium', 'high', 'xhigh', 'max']
const NO_RANGE: JevRange = { models: [], effort_min: '', effort_max: '' }

type State = { range: JevRange; hasEffort: boolean; preview: boolean; refuse: boolean }
const STATES: Record<string, State> = {
  'jev-no-range': { range: NO_RANGE, hasEffort: true, preview: true, refuse: false },
  'models-unchecked': { range: { ...NO_RANGE, models: ['gpt-5.6-sol', 'glm-5'] }, hasEffort: true, preview: true, refuse: false },
  'effort-span': { range: { ...NO_RANGE, effort_min: 'low', effort_max: 'high' }, hasEffort: true, preview: true, refuse: false },
  'no-effort': { range: NO_RANGE, hasEffort: false, preview: true, refuse: false },
  refused: { range: { ...NO_RANGE, models: ['gpt-5.6-sol', 'gpt-5.6-terra'] }, hasEffort: true, preview: true, refuse: true },
  'preview-off': { range: { ...NO_RANGE, models: ['gpt-5.6-sol'] }, hasEffort: true, preview: false, refuse: false },
}
const state = STATES[params.get('state') || ''] ?? STATES['jev-no-range']

const realFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/')) return realFetch(input, init)
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
  if (url.includes('/api/effort-levels')) return json(LEVELS)
  if (url.includes('/jev-range')) {
    return state.refuse
      ? json({ error: 'models not offered by this session: glm-5', code: 'jev_range_invalid' }, 400)
      : json({ ok: true, jev_range: JSON.parse(String(init?.body)) })
  }
  return json({})
}

store.dispatch(addSlotOptimistic({
  key: SLOT, model: 'auto', jev_route: state.preview, jev_range: state.range,
} as ChatSlot))
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const models = withJevRoute(
  [{ name: 'auto' }, ...SERVED],
  state.preview,
  i18nT('pages.chatPage.model_auto_jev_description'),
)

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <div data-capture-root className="bg-bg text-text" style={{ minHeight: '100vh' }}>
          <ModelEffortDropdown
            anchorRect={{ right: 372, top: window.innerHeight - 16 } as DOMRect}
            dropdownRef={createRef<HTMLDivElement>()}
            inputRef={createRef<HTMLInputElement>()}
            models={models}
            activeModel={state.preview ? JEV_ROUTE_MODEL : 'auto'}
            onSelectModel={() => {}}
            filter=""
            setFilter={() => {}}
            onClose={() => {}}
            hasEffort={state.hasEffort}
            slot={SLOT}
            currentEffort=""
            onListKeyDown={() => {}}
          />
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
