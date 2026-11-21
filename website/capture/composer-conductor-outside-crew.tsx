/** The composer of a new, empty chat on the conductor, through the REAL code.
 *  The REAL `useCrewModeSwitch` and `showsAsCrewMode` decide what it shows, with
 *  the same derivations ChatPage makes; the REAL `MemoryModeChip`, `CrewModeChip`
 *  and `ChatInput` draw it. Only the slot row is mocked.
 *  `?scene=outside`: another session opened it with `session_create`, so the
 *  gateway stamps it `crew_mode_root: false` and it is in no crew.
 *  `?scene=root`: a chat the person opened on the conductor, a Crew Mode root.
 *  Query: ?scene=outside|root&theme=dark|light */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { store } from '../src/store'
import { setActiveSlot } from '../src/store/chatSlice'
import { setCrewModeCatalog, showsAsCrewMode } from '../src/lib/crewMode'
import { useCrewModeSwitch } from '../src/pages/chat/page/crewModeSwitch'
import { MemoryModeChip } from '../src/components/MemoryModeChip'
import { CrewModeChip } from '../src/components/CrewModeChip'
import ChatInput from '../src/components/ChatInput'
import type { ChatSlot } from '../src/types'
import { initI18n } from '../src/i18n/all'
import { noteFeaturePreviewLockKnown } from '../src/utils/editionLocks'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const scene = params.get('scene') === 'root' ? 'root' : 'outside'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n('en')
// An oss gateway: the config schema answered and locks no Feature Preview.
noteFeaturePreviewLockKnown()
setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor'])

const KEY = 'capture-slot'
store.dispatch(setActiveSlot(KEY))
const row = {
  key: KEY, title: 'New chat', messages: 0, running: false, memory_mode: 'persistent', origin: 'user', mode: '',
  agent: 'kirocrew-conductor', agent_kind: 'template',
  ...(scene === 'outside' ? { created_by: 'slot-parent', crew_mode_root: false } : { crew_mode_root: true }),
} as unknown as ChatSlot

function Composer() {
  const [value, setValue] = useState('')
  const { crewModeEligible, crewModeChosen, crewModeLeaving, crewModeSwitching } = useCrewModeSwitch({
    currentSlot: row, defaultAgent: 'kirocrew', hasUserMessage: false,
    switchAgent: async () => true, openView: () => {}, autoOpenBoard: false,
  })
  // ChatPage's own derivations for an empty chat on its welcome state.
  const crewChipPressed = crewModeEligible && crewModeChosen
  const agentPickerHidden = !crewModeLeaving && (crewModeSwitching === 'on' || showsAsCrewMode(row))
  return (
    <div data-capture-root data-pressed={String(crewChipPressed)} data-picker={String(!agentPickerHidden)}
      className="bg-bg text-text" style={{ maxWidth: 820, margin: '0 auto', padding: '24px' }}>
      <div className="mx-auto w-fit max-w-full px-4 pt-2 pb-2 flex flex-wrap items-center justify-center gap-x-2 gap-y-1">
        <MemoryModeChip memoryMode="persistent" onSwitchMode={() => {}} switching={false} disabled={false} />
        {crewChipPressed && <CrewModeChip onTurnOn={() => {}} pressed onTurnOff={() => {}} />}
      </div>
      <ChatInput
        value={value}
        onChange={setValue}
        onSend={() => setValue('')}
        connected
        approvalMode="normal"
        modelName="auto"
        onModelClick={() => {}}
        agentName="kirocrew-conductor"
        onAgentClick={agentPickerHidden ? undefined : () => {}}
      />
    </div>
  )
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Composer />
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
