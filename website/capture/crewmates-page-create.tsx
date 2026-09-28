/**
 * Isolated capture entry for the Crewmates page's create flow: the REAL
 * MembersPage (empty hero, header "+", NewCrewmateDialog, the opened chat)
 * against the real stylesheet, theme tokens and live i18n catalog. Every
 * `/api/*` call is answered by the capture script's route interception
 * (`scripts/capture-crewmates-page-create.mjs`); nothing here is mocked in
 * the component tree.
 *
 * Scenes via query string: ?theme=dark|light  ?scene=empty|done
 *   empty  zero crewmates — the hero in the chat column; the driver clicks the
 *          hero's real button for the dialog frame and fills the real form.
 *   done   Radar is the only row and its chat is open (`?member=Radar`), with
 *          the seeded greeting turn and the crewmate's reply hydrated into the
 *          slot the thread endpoint answers.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import MembersPage from '../src/pages/members/MembersPage'
import { initI18n } from '../src/i18n/all'
import { ThemeProvider } from '../src/hooks/useTheme'
import { store } from '../src/store'
import { sseSlots } from '../src/store/dashboardSlice'
import { hydrateSlotMessages } from '../src/store/chatSlice'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('scene') || 'empty'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
// ThemeProvider (the drawer's CrewWebview needs it) reads its cache from
// storage; first-run flags keep onboarding dialogs off the frame.
localStorage.setItem('mc-theme', theme)
localStorage.setItem('mc-color-theme', 'kiro')
localStorage.setItem('mc-onboarded', '1')
localStorage.setItem('mc-import-onboarded', '1')
localStorage.setItem('mc-privacy-acked', '1')

export const SLOT = 'member-radar'
const now = Date.now()

if (scene === 'done') {
  // Presence for the one crewmate: idle, bound chat, two messages.
  store.dispatch(
    sseSlots([{ key: SLOT, title: 'Radar', messages: 2, running: false, mode: 'member', agent: 'Radar' }] as never),
  )
  // The transcript the page opens into: the seeded first turn (what the page
  // sends on the user's behalf after a create) and Radar's greeting.
  store.dispatch(
    hydrateSlotMessages({
      slot: SLOT,
      messages: [
        {
          role: 'user',
          content: 'Hi Radar, welcome to the team. Your job: Triage new GitHub issues every morning. Say hello in a couple of lines and tell me how you plan to start.',
          ts: new Date(now - 40_000).toISOString(),
        },
        {
          role: 'assistant',
          content: "Hi! I'm Radar. Each morning I'll read the new issues, sort them by what they need, and bring you only the ones that need a decision. I'll start with tomorrow's batch.",
          ts: new Date(now - 30_000).toISOString(),
        },
      ],
      hasMore: false,
      total: 2,
      running: false,
    } as never),
  )
}

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

async function main() {
  await initI18n()
  const route = scene === 'done' ? '/members?member=Radar' : '/members'
  createRoot(document.getElementById('root')!).render(
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <ThemeProvider>
          <MemoryRouter initialEntries={[route]}>
            <div className="h-screen flex flex-col bg-bg text-text" data-capture-root data-scene={scene}>
              <div className="flex-1 min-h-0">
                <MembersPage />
              </div>
            </div>
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
}

main()
