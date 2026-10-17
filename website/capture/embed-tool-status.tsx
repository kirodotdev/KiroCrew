/**
 * Isolated capture entry for two ChatEmbed defects: a finished tool call kept
 * the loader icon, and a single pending approval previewed only 150 chars.
 *
 * Mounts the REAL ChatEmbed against the real stylesheet, theme tokens and live
 * i18n catalog; the transcript comes from the capture script's route
 * interception (gateway-free). The script fulfils the slot read with a running
 * turn holding one finished call (its result on `meta.output`, as the backend
 * persists it) and one call parked on an approval whose command is long.
 * With `?surface=crew` it instead mounts the crew chat window's list
 * (ChatMessageList + crew-window renderers + onApprove, as CrewChatWindow
 * does), the other surface that shared the 150-char cut.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ChatEmbed from '../src/app-sdk/ChatEmbed'
import ChatMessageList from '../src/app-sdk/ChatMessageList'
import { createCrewWindowRenderers } from '../src/pages/chat/crew-window/crewWindowRenderers'
import type { ChatMessage } from '../src/types'
import { useMemo } from 'react'
import { AppScopedApiProvider } from '../src/app-sdk/scopedApi'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
})

// `?surface=crew` mounts the crew chat window's transcript list the way
// CrewChatWindow does: ChatMessageList with the crew-window renderers and an
// `onApprove`. The capture script passes the fake transcript on `window`.
const surface = params.get('surface') || 'embed'

function CrewList() {
  const renderers = useMemo(() => createCrewWindowRenderers({
    instanceId: 'capture-peer', key: 'capture-session', name: 'Peer crew',
    canRewind: () => false, onRewind: () => false, regenerateRow: null, onRegenerate: () => {},
  }), [])
  const data = (window as unknown as { __captureTranscript: { messages: ChatMessage[] } }).__captureTranscript
  return (
    <ChatMessageList messages={data.messages} running renderers={renderers} onApprove={async () => {}} />
  )
}

async function main() {
  await initI18n()
  const body = surface === 'crew'
    ? <div className="w-[720px] h-[620px] flex flex-col" data-capture-box><CrewList /></div>
    : (
      <div className="w-[720px] h-[620px]" data-capture-box>
        <AppScopedApiProvider
          appName="capture-app"
          allowedApiPaths={['/api/chat*']}
          allowedEvents={[]}
          navigateFn={() => {}}
        >
          <ChatEmbed slotKey="capture-app-embed" startAtBottom />
        </AppScopedApiProvider>
      </div>
    )
  createRoot(document.getElementById('root')!).render(
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <div className="min-h-screen bg-bg text-text p-4" data-capture-root>
            {body}
          </div>
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

main()
