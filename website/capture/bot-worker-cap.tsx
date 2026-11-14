/**
 * Capture page: a Bot's Settings group with the new "Workers at once" row, and
 * the picker page for it.
 *
 * The group is the real component; the API reads are fixtures (no gateway).
 * `?theme=light` for the light palette, `?lang=zh-CN` for Chinese.
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api, type MemberRosterRow } from '../src/api/client'
import { store } from '../src/store'
import { initI18n } from '../src/i18n/all'
import CrewProfileSettings from '../src/pages/members/CrewProfileSettings'
import { ProfilePagePushContext, type ProfilePagePush } from '../src/pages/members/CrewProfilePanel'
import { MEMBERS_ROSTER_QUERY_KEY } from '../src/api/membersQuery'
import '../src/index.css'

const params = new URLSearchParams(location.search)
document.documentElement.setAttribute('data-theme', params.get('theme') === 'light' ? 'light' : 'dark')

const member = {
  name: 'release-bot', slug: 'release-bot', slot_key: 'member-release-bot', running: false,
  model: 'claude-opus-5', worker_model: 'auto', max_workers: 3,
} as MemberRosterRow

Object.assign(api, {
  agentResolvedModel: async () => ({ model: 'claude-opus-5', harnesses: [] }),
  harnessModels: async () => [{ model_name: 'auto' }, { model_name: 'claude-opus-5' }],
  models: async () => [{ name: 'auto' }, { name: 'claude-opus-5' }],
  memberDashboard: async () => ({}),
  kirocrewConfig: async () => ({ dashboard: {} }),
  updateKirocrewAgent: async () => ({ ok: true }),
})

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
qc.setQueryData(MEMBERS_ROSTER_QUERY_KEY, [member])
initI18n(params.get('lang') === 'zh-CN' ? 'zh-CN' : 'en')

function Frame() {
  const [group, setGroup] = useState<HTMLDivElement | null>(null)
  const [pick, setPick] = useState<HTMLDivElement | null>(null)
  const push: ProfilePagePush = { open: () => {}, close: () => {}, host: group, hosts: [group, pick] }
  return (
    <ProfilePagePushContext.Provider value={push}>
      <div className="flex gap-4 p-4 bg-bg text-text" data-testid="capture-frame">
        <div style={{ width: 380 }} className="border border-border rounded-xl p-3">
          <CrewProfileSettings member={member} slotKey="member-release-bot" />
          <div ref={setGroup} data-testid="group-host" />
        </div>
        <div style={{ width: 380 }} className="border border-border rounded-xl p-3" ref={setPick} data-testid="picker-host" />
      </div>
    </ProfilePagePushContext.Provider>
  )
}

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <Frame />
    </QueryClientProvider>
  </Provider>,
)
