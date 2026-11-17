/**
 * Visual evidence for the one-click Dashboard card send (#22367), on the REAL
 * components.
 *
 * `state=refused|unknown|sent` mounts the real `CrewDynamicDashboard` (as
 * `test/CrewDynamicDashboard.test.tsx` does) over a page whose button posts
 * `kirocrew-dashboard:send`; the host's own gates (a real click tied to the frame)
 * decide whether `onSend` runs. `onSend` resolves the named outcome, the way
 * `sendCardReply` would for that gateway answer. The chat box and thread beside
 * the frame are this fixture's stand-ins for the Members page's composer and
 * transcript: they show where `onAct` filled the reply and what `onSend` sent.
 *
 * `state=profile-error` mounts the real `CrewProfileSettings` with the setting
 * write answering 500, so the script's click on the switch shows its notice.
 *
 * Network is stubbed in-page (no gateway): the dashboard read answers a page,
 * `POST /api/sandbox-doc` hands back a blob URL for the frame.
 *
 * `state=off` mounts it with no `onSend`, as the Members side panel does while the
 * crewmate's "Send replies in one click" switch is off.
 *
 *   ?theme=dark|light&state=off|refused|unknown|sent|profile-error
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import dashboardReducer, { sseConnected } from '../src/store/dashboardSlice'
import chatReducer from '../src/store/chatSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import instancesReducer from '../src/store/instancesSlice'
import { initI18n } from '../src/i18n/all'
import { ThemeProvider } from '../src/hooks/useTheme'
import { MEMBERS_ROSTER_QUERY_KEY } from '../src/api/membersQuery'
import type { MemberRosterRow } from '../src/api/client'
import CrewDynamicDashboard from '../src/pages/members/CrewDynamicDashboard'
import CrewProfileSettings from '../src/pages/members/CrewProfileSettings'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const state = params.get('state') || 'refused'
localStorage.setItem('mc-theme', theme)
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const REPLY = 'About "Ship the release notes": Approve the plan'
const fg = theme === 'light' ? '#222' : '#ddd'
const PAGE = `<!doctype html><html><body style="font:14px system-ui;margin:16px;color:${fg}">
<h3 style="margin:0 0 6px">Needs you</h3>
<p style="margin:0 0 10px">Ship the release notes &middot; waiting on your answer</p>
<button id="answer" style="font:13px system-ui;padding:6px 12px;border-radius:6px;border:1px solid #888;background:transparent;color:${fg}">Approve the plan</button>
<script>document.getElementById('answer').addEventListener('click',function(){parent.postMessage({type:'kirocrew-dashboard:send',text:${JSON.stringify(REPLY)}},'*')})</script>
</body></html>`

const READ = {
  slug: 'oncall', member: 'oncall', dashboard: 'goal-board', state: 'live',
  html: PAGE, rendered_html: PAGE, manifest: { fields: {} }, instance_version: 1,
  template: { id: 'goal-board', version: 1 }, read: { fields: {}, version: 1, generation: 'gen-a' },
}

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const json = (body: unknown, status = 200) =>
    Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } }))
  if (url.includes('/api/sandbox-doc')) {
    const { html } = JSON.parse(String(init?.body ?? '{}')) as { html: string }
    return json({ url: URL.createObjectURL(new Blob([html], { type: 'text/html' })) })
  }
  if (url.includes('/dashboard')) return json(READ)
  // The Profile switch's write: refused, so the notice under it shows.
  if (url.includes('/api/agents') && (init?.method === 'PUT' || init?.method === 'PATCH' || init?.method === 'POST')) {
    return json({ error: 'unavailable' }, 500)
  }
  if (url.includes('/api/')) return json({})
  return realFetch(input, init)
}) as typeof globalThis.fetch

await initI18n()

const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const store = configureStore({
  reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer, instances: instancesReducer },
})
store.dispatch(sseConnected())

const MEMBER = {
  name: 'oncall', slug: 'oncall', slot_key: 'member-oncall', running: false, model: '',
  approval_mode: 'trust', kiro_agent: 'kirocrew', memory_store: 'default', workspace: 'default',
  dashboard_card_send: true,
} as MemberRosterRow
client.setQueryData(MEMBERS_ROSTER_QUERY_KEY, [MEMBER])

function DashboardFixture() {
  const [box, setBox] = useState('')
  const [sent, setSent] = useState<string[]>([])
  const onSend = (text: string) => {
    if (state === 'sent') {
      setSent((s) => [...s, `[Dashboard card] ${text}`])
      return Promise.resolve('sent' as const)
    }
    return Promise.resolve(state === 'unknown' ? ('unknown' as const) : ('refused' as const))
  }
  return (
    <div className="h-screen bg-bg text-text flex flex-col" data-capture-root>
      <div className="flex-1 min-h-0 flex flex-col">
        <CrewDynamicDashboard
          target={{ kind: 'member', slug: 'oncall', member: 'oncall' }}
          displayName="On Call"
          onAct={(t) => setBox(t)}
          onActKeep={(t) => setBox((b) => (b ? `${b}\n${t}` : t))}
          // `off`: the switch is off, so the host offers no send (as the Members side
          // panel does), and the card button only fills the chat box.
          onSend={state === 'off' ? undefined : onSend}
          onOpenProfileTab={() => {}}
        />
      </div>
      <div className="border-t border-border p-3 flex flex-col gap-2" data-testid="fixture-chat">
        <div className="text-[11px] uppercase tracking-wide text-muted">Thread</div>
        <div data-testid="fixture-thread" className="text-[13px] min-h-[20px]">
          {sent.length ? sent.map((s) => <div key={s} className="rounded-md bg-bg-elevated px-2 py-1">{s}</div>) : <span className="text-muted">(nothing sent)</span>}
        </div>
        <div className="text-[11px] uppercase tracking-wide text-muted">Chat box</div>
        <textarea data-testid="fixture-chat-box" readOnly value={box} rows={2} className="w-full rounded-md border border-border bg-bg-elevated p-2 text-[13px]" placeholder="(empty)" />
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <Provider store={store}><MemoryRouter><QueryClientProvider client={client}>
    <ThemeProvider>
      {state === 'profile-error' ? (
        <div className="h-screen bg-bg-elevated text-text p-4 flex items-start" data-capture-root>
          <div className="w-96"><CrewProfileSettings member={MEMBER} slotKey="member-oncall" /></div>
        </div>
      ) : (
        <DashboardFixture />
      )}
    </ThemeProvider>
  </QueryClientProvider></MemoryRouter></Provider>,
)
