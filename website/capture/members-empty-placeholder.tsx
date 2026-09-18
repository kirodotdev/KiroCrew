/**
 * Isolated capture entry that reproduces the REAL Crew Members page with one
 * member selected and an EMPTY DM thread, so the "Session ready. Type a message
 * to start." placeholder renders inside the page's true column layout (roster +
 * thread + side panel), which is what governs its alignment.
 *
 * Gateway-free: the capture script answers GET /api/members and
 * POST /api/members/{slug}/thread. The confirmed slot is seeded EMPTY in the
 * store so ChatPane's aboveRows renders the placeholder branch. The selected
 * member comes from the `?member=` search param the page reads (MEMBER_PARAM).
 *
 * Scene via query string: ?theme=dark|light  (viewport set by the driver).
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import MembersPage from '../src/pages/members/MembersPage'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { sseSlots } from '../src/store/dashboardSlice'
import { hydrateSlotMessages } from '../src/store/chatSlice'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const MEMBER = 'Oncall'
const SLOT = 'member-oncall-slot'
const RAIL_W = 236 // app shell nav rail expanded (useRailWidth RAIL_W_EXPANDED)

// Seed the confirmed, EMPTY thread slot in the store. The page's POST-thread
// mutation (answered by the driver with this same slot_key) writes the outcome
// cache; this makes the slot's transcript empty so the placeholder shows.
store.dispatch(
  sseSlots([
    { key: SLOT, title: MEMBER, messages: 0, running: false, mode: 'member', agent: 'oncall' },
  ] as never),
)
store.dispatch(
  hydrateSlotMessages({ slot: SLOT, messages: [], hasMore: false, total: 0, running: false } as never),
)

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

async function main() {
  await initI18n()
  createRoot(document.getElementById('root')!).render(
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        {/* Start already on the member so the thread opens on mount. */}
        <MemoryRouter initialEntries={[`/members?member=${encodeURIComponent(MEMBER)}`]}>
          {/* The app shell's left nav rail is a peer of the page and consumes
              width the Members page never sees. At 768 the rail (236) + roster
              (264) leave the DM thread ~252px, the width at which the empty
              placeholder's alignment matters. Omitting it (as an isolated page
              mount does) gives the thread a false ~488px, so the harness
              reserves it. */}
          <div className="h-screen w-screen flex bg-bg text-text" data-capture-root>
            <div className="shrink-0 h-full border-r border-border" style={{ width: RAIL_W }} data-nav-rail />
            <div className="flex-1 min-w-0 h-full" data-app-main>
              <MembersPage />
            </div>
          </div>
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

main()
