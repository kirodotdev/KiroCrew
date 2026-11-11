import { createRoot } from 'react-dom/client'
import { configureStore } from '@reduxjs/toolkit'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { initI18n } from '../src/i18n/all'
import { api } from '../src/api/client'
import ChatInput from '../src/components/ChatInput'
import McpToolAlwaysAllow, { McpToolGrantsNote } from '../src/pages/overview/McpToolAlwaysAllow'
import chatReducer, { setActiveSlot, sseChatMessage } from '../src/store/chatSlice'
import dashboardReducer from '../src/store/dashboardSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import instancesReducer from '../src/store/instancesSlice'
import '../src/index.css'

const state = new URLSearchParams(location.search).get('state') || 'off'
const refused = state === 'refused' || state === 'card-refused'
const snapshot = { grants: state === 'on' ? [{ ref: '@notes/LOOKUP' }] : [], server: 'notes', refusal: refused ? 'asks_every_time' : '' }
api.mcpToolGrants = async () => snapshot
api.slashCommands = async () => []
api.mcpToolGrantSet = async () => { throw new Error('Mocked write failure') }
const store = configureStore({ reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer, instances: instancesReducer } })
store.dispatch(setActiveSlot('capture-slot'))
store.dispatch(sseChatMessage({ slot: 'capture-slot', role: 'permission', content: '@notes/lookup', meta: {
  approval_id: 'capture-request', request_id: 'capture-request', mid: 'capture-row',
  tool_input: '{"query":"release notes"}', trust_grantable: '1',
  ...(refused ? { always_allow_tool_refusal: 'asks_every_time' } : { always_allow_tool_grantable: '1', always_allow_tool_ref: '@notes/lookup' }),
} }))
const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
qc.setQueryData(['mcp-tool-grants', 'notes'], snapshot)
document.documentElement.dataset.theme = 'kiro-dark'
await initI18n()

createRoot(document.getElementById('root')!).render(
  <Provider store={store}><QueryClientProvider client={qc}><MemoryRouter>
    <main className="bg-bg text-text min-h-screen p-8" data-capture-root>
      <p className="text-[13px] text-muted mb-6">Component render, not the live page</p>
      {state.startsWith('card') ? <div className="pt-64"><ChatInput value="" onChange={() => {}} onSend={() => {}} /></div> :
        <section className="border border-border rounded-xl p-5 max-w-[700px]">
          <h1 className="text-lg mb-4">MCP Servers · notes</h1>
          <McpToolGrantsNote server="notes" />
          <div className="flex items-start justify-between gap-6 pt-4">
            <div><div className="font-mono text-sm">lookup</div><p className="text-[13px] text-muted mt-1">Look up a note by its title.</p></div>
            <McpToolAlwaysAllow server="notes" tool="lookup" />
          </div>
        </section>}
    </main>
  </MemoryRouter></QueryClientProvider></Provider>,
)
