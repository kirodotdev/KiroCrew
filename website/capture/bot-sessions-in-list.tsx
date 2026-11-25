/**
 * Capture page: a Bot's Sessions list with the switches that keep Bot sessions in
 * the person's Sessions list (the all-Bots switch and this Bot's own).
 * `?global=1` turns the all-Bots switch on, `?own=1` this Bot's.
 * The components are the real ones; the API reads are fixtures (no gateway).
 * `?phone=1` fills the viewport instead of a 380px card.
 * `?theme=light` for the light palette, `?lang=zh-CN` for Chinese.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api, type MemberRosterRow } from '../src/api/client'
import { store } from '../src/store'
import { initI18n } from '../src/i18n/all'
import MateSessionsList from '../src/pages/members/MateSessionsList'
import BotSessionListSwitches from '../src/pages/members/BotSessionListSwitches'
import { MEMBERS_ROSTER_QUERY_KEY } from '../src/api/membersQuery'
import type { ChatSlot } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
document.documentElement.setAttribute('data-theme', params.get('theme') === 'light' ? 'light' : 'dark')
const everyone = params.get('global') === '1'
const phone = params.get('phone') === '1'

const ROOT = 'member-release-bot'
const member = {
  name: 'release-bot', slug: 'release-bot', display_name: 'Release Bot', slot_key: ROOT, running: false,
  model: 'claude-opus-5', worker_model: 'auto', max_workers: 3, show_sessions_in_list: params.get('own') === '1',
} as MemberRosterRow

const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString()
const sessions = [
  { key: 'w1', title: 'Cut the 0.13.2 release notes', running: true, last_ts: ago(2) },
  { key: 'w2', title: 'Check the Windows installer build', running: false, last_ts: ago(25) },
].map(s => ({ ...s, mate_root: ROOT, messages: 4, agent: 'kirocrew' })) as unknown as ChatSlot[]

Object.assign(api, {
  kirocrewConfig: async () => ({ dashboard: { show_bot_sessions_in_list: everyone } }),
  updateKirocrewAgent: async () => ({ ok: true }),
  updateDashboardConfig: async () => ({ ok: true }),
  crewWorkers: async () => ({ workers: [] }),
})

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
qc.setQueryData(MEMBERS_ROSTER_QUERY_KEY, [member])
initI18n(params.get('lang') === 'zh-CN' ? 'zh-CN' : 'en')

function Frame() {
  return (
    <div className={phone ? 'p-2 bg-bg text-text' : 'p-4 bg-bg text-text'} data-capture-root>
      <div style={phone ? undefined : { width: 380 }} className="border border-border rounded-xl p-3">
        <MateSessionsList root={ROOT} mateName="Release Bot" liveSlots={sessions} slotsLoaded
          empty={null} onOpen={() => {}}
          header={<BotSessionListSwitches member={member} displayName="Release Bot" />} />
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <Frame />
    </QueryClientProvider>
  </Provider>,
)
