/**
 * Isolated capture entry for the Dashboard tab's EMPTY state.
 *
 * WHY ISOLATED: the empty state is what a crewmate's Dashboard tab shows until
 * that crewmate publishes, so in a live gateway reaching it means a crewmate with
 * no panel record, a seeded roster and an open member thread. Here the REAL
 * `CrewDashboardFrame` mounts over a fetch stub that answers the panel read with
 * `{ html: null }` -- the component's own `!html` branch -- so the face, the copy
 * and the prompts in the shot are the shipped ones.
 *
 * Query string: ?theme=dark|light, ?avatar=pinned|seeded (a crewmate that pinned
 * a face in the avatar builder vs one wearing its name-derived ghost),
 * ?prompts=0 (no chat box to put a prompt in: the prompts are withheld).
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { LayoutDashboard } from 'lucide-react'

import { initI18n } from '../src/i18n/all'
import { ThemeProvider } from '../src/hooks/useTheme'
import { CrewDashboardFrame } from '../src/pages/members/CrewWebview'
import type { CrewAvatarOverride } from '../src/components/CrewAvatar'
import { i18nT } from '../src/i18n/t'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const pinned = params.get('avatar') !== 'seeded'
const prompts = params.get('prompts') !== '0'

// BOTH, in this order, as every other capture entry does: `ThemeProvider` seeds
// its own mode from `mc-theme` on mount, so the attribute alone gets overwritten.
localStorage.setItem('mc-theme', theme)
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/** A crewmate that pinned a face in the avatar builder. */
const PINNED: CrewAvatarOverride = {
  kind: 'ghost',
  traits: { eyes: 'canon', brows: 'flat', mouth: 'smile', accessory: 'phones', prop: 'mug', blush: true, flip: false, tile: '#259d85' },
}

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/')) return realFetch(input, init)
  const json = (body: unknown) =>
    Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } }))
  // The member-panel read: a crewmate that has published nothing yet.
  if (url.includes('/panel')) return json({ html: null, panel: null })
  // Every other gateway call is refused, which is what keeps the theme the one
  // the query string asked for rather than a proxied gateway's stored palette.
  return Promise.resolve(new Response('{}', { status: 404 }))
}) as typeof globalThis.fetch

await initI18n()

const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={client}>
    <ThemeProvider>
      {/* The side panel at its default width on a laptop window, with the strip
          the tab sits in, so the shot reads as the tab the user sees. */}
      <div data-capture-root className="flex flex-col h-screen w-[720px] bg-bg text-text border-l border-border">
        <div className="side-panel-strip flex items-end gap-1.5 shrink-0 px-2 pt-2 pb-0 min-h-10 rounded-tl-xl bg-bg-elevated border-b border-border">
          <div className="side-tab-active bg-bg text-accent border-x-border border-t-border border-b-transparent flex items-center gap-1 h-8 rounded-t-md rounded-b-none border px-2 shrink-0">
            <LayoutDashboard className="lucide-inline" aria-hidden="true" />
            <span className="text-[12px]">{i18nT('pages.membersPage.dashboard_tab')}</span>
          </div>
        </div>
        <div className="flex-1 min-h-0">
          <CrewDashboardFrame
            slug="radar"
            member="radar"
            displayName="Radar"
            avatar={pinned ? PINNED : undefined}
            onAct={prompts ? () => {} : undefined}
          />
        </div>
      </div>
    </ThemeProvider>
  </QueryClientProvider>,
)
