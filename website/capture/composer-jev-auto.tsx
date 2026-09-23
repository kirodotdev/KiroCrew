/**
 * Evidence for the composer model chip's three states.
 *
 * Every row mounts the REAL ChatInput, labels the chip through the REAL
 * `displayModel()`, and derives both markers from the REAL predicates the two
 * hosts use — `jevRouteOffered()` and `isUnpinnedModel()` — on one slot fixture.
 * So the only thing that differs between the rows is the fixture, not a flag
 * typed in by hand:
 *
 *   PINNED    slot.model names a model        -> the model name, bare
 *   JEV AUTO  slot.model is `''`, preview on  -> `Auto (Jev)`, no model id
 *   PLAIN     slot.model is `''`, preview off -> `<served> · default`
 *
 *   ?theme=dark|light
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ChatInput from '../src/components/ChatInput'
import { initI18n } from '../src/i18n/all'
import { isUnpinnedModel, jevRouteOffered } from '../src/lib/jevRoute'
import { displayModel } from '../src/lib/model'
import { store } from '../src/store'
import { setActiveSlot } from '../src/store/chatSlice'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n(params.get('lang') || 'en')

store.dispatch(setActiveSlot('capture-slot'))
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

/** A partition that does not serve `auto`, so an unpinned session reports a real id. */
const SERVED = ['gpt-5.6-sol', 'gpt-5.6-terra', 'deepseek-3.2', 'glm-5'].map(name => ({ name }))
const SERVED_MODEL = 'gpt-5.6-sol'

/**
 * The two reads `jevRouteOffered` takes: the fleet's answer, and the owner's
 * `permits` -- the effective one, which also holds the consented endpoint against
 * the configured one. Feeding `enabled` here would let this frame claim a routed
 * turn the gate would refuse.
 */
const PREVIEW_ON = { config: { decisions_enabled: true }, consent: { permits: true } }
const PREVIEW_OFF = { config: { decisions_enabled: true }, consent: { permits: false } }

type Episode = {
  id: string
  /** The slot's RAW model — what the routing gate reads, and what the chip keys off. */
  slotModel: string
  preview: typeof PREVIEW_ON
  caption: string
}

const EPISODES: Episode[] = [
  {
    id: 'pinned',
    slotModel: 'gpt-5.6-terra',
    preview: PREVIEW_ON,
    caption: 'PINNED — the owner named a model, so nothing routes and the chip is bare',
  },
  {
    id: 'jev-auto',
    slotModel: '',
    preview: PREVIEW_ON,
    caption: 'JEV AUTO — no model named and the preview is on, so Jev picks each turn',
  },
  {
    id: 'plain-auto',
    slotModel: '',
    preview: PREVIEW_OFF,
    caption: 'PLAIN AUTO — no model named and the preview is off, so the session inherits',
  },
]

function Label({ children }: { children: string }) {
  return (
    <div
      style={{
        fontSize: 11,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        opacity: 0.55,
        margin: '18px 0 6px',
        fontFamily: 'ui-sans-serif, system-ui, sans-serif',
      }}
    >
      {children}
    </div>
  )
}

function Row({ episode }: { episode: Episode }) {
  const [value, setValue] = useState('')
  // The two hosts' own expressions, verbatim, so this frame cannot drift from them.
  const shown = displayModel(episode.slotModel, SERVED, false, null, SERVED_MODEL)
  const pinShown = displayModel(episode.slotModel, SERVED, false, null)
  const offered = jevRouteOffered(episode.preview.config, episode.preview.consent, true)
  const routed = offered && isUnpinnedModel(episode.slotModel)
  return (
    <div data-episode={episode.id} data-routed={String(routed)} className="flex flex-col">
      <ChatInput
        value={value}
        onChange={setValue}
        onSend={() => setValue('')}
        connected
        approvalMode="normal"
        modelName={shown}
        modelIsJevRouted={routed}
        modelIsInheritedDefault={shown !== 'auto' && shown !== pinShown}
        onModelClick={() => {}}
      />
    </div>
  )
}

function Scene() {
  return (
    <div
      data-capture-root
      className="bg-bg text-text"
      style={{ maxWidth: 760, margin: '0 auto', padding: '20px 24px 28px' }}
    >
      {EPISODES.map(episode => (
        <div key={episode.id}>
          <Label>{episode.caption}</Label>
          <Row episode={episode} />
        </div>
      ))}
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <Scene />
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
