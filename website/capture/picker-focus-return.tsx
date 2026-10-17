/**
 * Isolated capture entry for #18313: focus after a composer-picker PICK.
 *
 * Mounts the REAL ChatInput in two scenes (`?scene=`):
 *   approval  idle composer with the approval-mode picker in the control row
 *   busy      a running, steer-capable turn, so the steer/queue split button
 *             (BusySendButton) renders in the send slot
 *
 * In both, the harness logs every send/steer into a visible line above the
 * composer, so a recording can show "pick a mode, press Enter, the message
 * leaves" rather than the menu re-opening. The driver
 * (`scripts/capture-picker-focus-return.mjs`) asserts `document.activeElement`
 * and the log before it keeps a frame.
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ChatInput from '../src/components/ChatInput'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { setActiveSlot } from '../src/store/chatSlice'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('scene') === 'busy' ? 'busy' : 'approval'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
store.dispatch(setActiveSlot('capture-slot'))

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Harness() {
  const [value, setValue] = useState('')
  const [log, setLog] = useState<string[]>([])
  const record = (verb: string) => {
    setLog(l => [...l, `${verb}: ${value}`])
    setValue('')
  }
  return (
    <div className="flex flex-col justify-end h-screen bg-bg text-text" data-capture-root data-scene={scene}>
      <div className="flex flex-col gap-2 px-3 pb-3 overflow-hidden">
        <div className="self-end max-w-[80%] rounded-xl bg-accent-subtle px-3 py-2 text-[13px]">
          Build the project and run the tests.
        </div>
        <div className="self-start max-w-[80%] rounded-xl bg-card text-card-fg px-3 py-2 text-[13px]">
          {scene === 'busy' ? 'Running the build…' : 'Done. 42 tests passed.'}
        </div>
        {log.map((line, i) => (
          <div key={i} data-capture-log className="self-end max-w-[80%] rounded-xl bg-accent-subtle px-3 py-2 text-[13px]">
            {line}
          </div>
        ))}
      </div>
      <ChatInput
        value={value}
        onChange={setValue}
        onSend={() => record('sent')}
        connected
        slotId="capture-slot"
        approvalMode="normal"
        sendOnEnter="enter"
        {...(scene === 'busy'
          ? { isRunning: true, stopState: 'idle' as const, canSteer: true, onSteer: () => record('steered') }
          : {})}
      />
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <Harness />
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
