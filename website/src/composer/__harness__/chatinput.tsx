// Dev-only harness: the REAL ChatInput with the Lexical composer on, plus the
// window.__ci probes the acceptance script (scripts/capture-composer-pills.mjs) reads.
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import dashboardReducer from '../../store/dashboardSlice'
import chatReducer from '../../store/chatSlice'
import notificationsReducer from '../../store/notificationsSlice'
import instancesReducer from '../../store/instancesSlice'
import { ThemeProvider } from '../../hooks/useTheme'
import ChatInput from '../../components/ChatInput'
import { expandAll, type PasteBlock } from '../../utils/pasteTokens'
import { initI18n } from '../../i18n/all'
import '../../index.css'
initI18n('en')
const store = configureStore({ reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer, instances: instancesReducer } })
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
declare global { interface Window { __ci: Record<string, unknown> } }
function Harness() {
  const [value, setValue] = useState('')
  const [blocks, setBlocks] = useState<PasteBlock[]>([])
  const [sent, setSent] = useState<string[]>([])
  const log: string[] = ((window.__ci?.logArr as string[]) ?? [])
  const push = (s: string) => { log.unshift(s); if (log.length > 30) log.pop() }
  window.__ci = { logArr: log, value: () => value, blocks: () => blocks, sent: () => sent, logs: () => log.slice(), setValue: (v: string) => setValue(v), seedSent: (m: string[]) => setSent(m) }
  return (
    <div className="p-4" style={{ paddingTop: 260 }}>
      <ChatInput
        lexicalComposer
        value={value}
        onChange={setValue}
        pasteBlocks={blocks}
        onPasteBlocksChange={setBlocks}
        onSend={() => { push('SEND ' + JSON.stringify(value)); /* like ChatPage: history holds the EXPANDED text */ setSent(s => [...s, expandAll(value, blocks)]); setValue(''); setBlocks([]) }}
        onFileSelect={p => push('file ' + p)}
        connected
        sendOnEnter="enter"
        sentMessages={sent}
      />
      <pre className="mt-3 text-[11px] text-muted whitespace-pre-wrap">{value}</pre>
    </div>
  )
}
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}><Provider store={store}><ThemeProvider><MemoryRouter><Harness /></MemoryRouter></ThemeProvider></Provider></QueryClientProvider>,
)
