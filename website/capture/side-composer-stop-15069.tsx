/**
 * Isolated capture entry for issue #15069: the Side Chat composer in its BUSY
 * state, after a hung turn gains an in-panel Stop and a reachable Refresh.
 *
 * WHY ISOLATED: the reshaped controls only render under a real browser — the
 * busy composer's affordance switches on draft presence, and happy-dom computes
 * no layout, so a screenshot of the real pixels needs a served frame. Design and
 * UX review both asked for rendered evidence of this exact control set, which no
 * unit test can produce.
 *
 * FAITHFUL PART: scenes 1 and 2 mount the real ``ChatInput`` with the production
 * busy props; scene 3 renders the Side Chat Refresh row's literal classes and
 * icon. So the capture exercises the same component and class strings the panel
 * ships, not a mock of them.
 *
 * Scenes, the control set and settled states the reviews named:
 *   1. empty-busy composer  → the Stop control (the hung-turn escape, gap 1)
 *   2. busy composer + draft → the steer/queue split button (steer stays
 *      discoverable once the composer has text)
 *   3. Refresh DISABLED mid-turn — Stop is the single in-flight escape, so the
 *      unconsumed-steer loss a mid-turn Refresh risked never exists (gap 2)
 *   4. the settled transcript after Stop: the stopped question plus its
 *      "(side response stopped)" error-styled row (real ChatMessageList)
 *   5. a failed Stop surfaces the real ErrorNotice (gap 5)
 *
 * Theme via query string: ?theme=dark (default) | ?theme=light
 *
 * Usage (served by vite on a loopback port):
 *   npx vite --host 127.0.0.1 --port 6810 --strictPort   # in another shell
 *   node scripts/capture-side-composer-stop-15069.mjs http://127.0.0.1:6810 ../.github/screenshots
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RotateCcw } from 'lucide-react'
import { initI18n } from '../src/i18n'
import { i18nT } from '../src/i18n/t'
import ChatInput from '../src/components/ChatInput'
import ChatMessageList from '../src/app-sdk/ChatMessageList'
import ErrorNotice from '../src/components/ErrorNotice'
import { store } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const noop = () => {}

/** The Side Chat context-age row's Refresh button, DISABLED while a turn runs. */
function RefreshRow() {
  return (
    <div className="flex items-center justify-between gap-2 px-3 py-1.5 text-[11px] text-muted border-b border-border">
      <span className="italic">
        {i18nT('pages.chat.sideChat.context_from')} · 2 {i18nT('pages.chat.sideChat.ago')}
      </span>
      <button
        disabled
        className="flex items-center gap-1 text-[11px] font-medium text-accent hover:text-accent-hover disabled:opacity-50 bg-transparent border-none cursor-pointer disabled:cursor-not-allowed"
      >
        <RotateCcw size={11} />
        {i18nT('pages.chat.sideChat.refresh_context')}
      </button>
    </div>
  )
}

function Scenes() {
  return (
    <div className="flex flex-col gap-6 p-4 bg-bg text-text" style={{ width: 420 }}>
      <section data-scene="refresh" className="border border-border rounded overflow-hidden">
        <div className="text-[11px] text-muted px-3 pt-2">3 · Refresh disabled while a turn runs — Stop is the in-flight escape (gap 2)</div>
        <RefreshRow />
      </section>

      <section data-scene="empty-busy" className="border border-border rounded overflow-hidden">
        <div className="text-[11px] text-muted px-3 pt-2">1 · Empty busy composer → Stop control</div>
        <ChatInput
          value=""
          onChange={noop}
          onSend={noop}
          isRunning
          onStop={noop}
          canSteer
          onSteer={noop}
          placeholder={i18nT('pages.chat.sideChat.ask_a_side_question_2')}
        />
      </section>

      <section data-scene="draft-busy" className="border border-border rounded overflow-hidden">
        <div className="text-[11px] text-muted px-3 pt-2">2 · Busy composer with a draft → steer/queue split button</div>
        <ChatInput
          value="follow-up question typed mid-turn"
          onChange={noop}
          onSend={noop}
          isRunning
          onStop={noop}
          canSteer
          onSteer={noop}
          placeholder={i18nT('pages.chat.sideChat.ask_a_side_question_2')}
        />
      </section>

      <section data-scene="settled-stop" className="border border-border rounded overflow-hidden">
        <div className="text-[11px] text-muted px-3 pt-2">4 · After Stop: the settled transcript (the stopped question + its "(side response stopped)" row)</div>
        <div className="p-2">
          <ChatMessageList
            running={false}
            messages={[
              { role: 'user', content: 'Why does the loader skip the compact budget?', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
              { role: 'error', content: '(side response stopped)', ts: '2026-05-20T00:00:05Z', run_id: 'r1' },
            ] as never}
          />
        </div>
      </section>

      <section data-scene="stop-failed" className="border border-border rounded overflow-hidden">
        <div className="text-[11px] text-muted px-3 pt-2">5 · A failed Stop surfaces the error notice (gap 5)</div>
        <div className="p-2">
          <ErrorNotice variant="inline" message={i18nT('pages.chat.sideChat.stop_failed')} />
        </div>
      </section>
    </div>
  )
}

initI18n('en')
const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <Provider store={store}>
      <Scenes />
    </Provider>
  </QueryClientProvider>,
)
