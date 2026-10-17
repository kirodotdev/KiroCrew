/**
 * Isolated capture entry for the crewmate Notes side-panel tab.
 *
 * Mounts the REAL CrewNotesTab against the real stylesheet and theme tokens,
 * at the narrow (~400px) width the panel actually renders at, so a frame
 * documents the shipped readability styling and not forced component state.
 * The briefing read (`GET /api/members/{slug}/briefing`) is answered by a
 * window.fetch stub with realistic content — headings, nested bullets, long
 * file paths / commit hashes / branch names, and a GitHub PR link chip — the
 * cases the readability pass is tuned against.
 *
 * Scenes via query string: ?theme=dark|light.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import CrewNotesTab from '../src/pages/members/CrewNotesTab'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import '../src/index.css'
import '../src/styles/crew-notes.css'

const SLUG = 'radar'
const MEMBER = 'Radar'

const BRIEFING = `# Briefing

Standing notes for the Settings restyle work. Kept short: current priorities
and pointers only, so a cold resume starts here.

## In flight

- **Chat settings subnav** — borderless cards and larger section headers
  landed; PR open at [kirodotdev/KiroCrew#11381](https://github.com/kirodotdev/KiroCrew/pull/11381).
  - Worktree \`/srv/worktrees/kc-settings-restyle\`, branch
    \`feat/chat-settings-subnav\`, base commit \`07a05a2b1c9f3e2a1b4c5d6e7f8a9b0c1d2e3f4a\`.
  - Waiting on the UX review lane to re-read the open-search screenshot.
- **Notes panel readability** — this panel: softening body colour and heading
  weight so sections group with their content.

## Decisions

- Do **not** rename Settings rail labels without usage data — View / Terminal /
  Theme stay as they are.
- Screenshots go under \`/srv/artifacts/notes-readability-shots/\`.

## Next

1. Re-run only the failed review job after attaching the open-results frame.
2. Rebase onto \`origin/main\` if the Bundle Size Gate inherits a red from base.
`

const BRIEFING_RESPONSE = {
  slug: SLUG,
  member: MEMBER,
  supported: true,
  text: BRIEFING,
  updated_ts: Math.floor(Date.now() / 1000) - 12 * 60,
  redacted: false,
  truncated: false,
}

// Answer only the briefing read; everything else falls through to the real
// fetch (there is nothing else on this isolated surface to answer).
const realFetch = window.fetch.bind(window)
window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/briefing')) {
    return Promise.resolve(
      new Response(JSON.stringify(BRIEFING_RESPONSE), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  }
  return realFetch(input as RequestInfo, init)
}) as typeof window.fetch

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'kiro-light' : 'kiro-dark'
document.documentElement.setAttribute('data-theme', theme)
document.documentElement.setAttribute('data-mode', theme === 'kiro-light' ? 'light' : 'dark')

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

/** A minimal stand-in for the shared identity row the three panel tabs carry,
 *  just enough to frame the notes body the way the real panel does. */
const Header = (
  <div className="flex items-center gap-2 mb-3 min-w-0">
    <div className="w-7 h-7 rounded-full bg-accent/30 shrink-0" aria-hidden />
    <span className="text-[13px] font-medium text-text truncate">{MEMBER}</span>
    <span className="text-[11px] text-muted ml-auto shrink-0">Notes</span>
  </div>
)

async function main() {
  await initI18n()
  createRoot(document.getElementById('root')!).render(
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <div className="h-screen bg-bg text-text flex justify-start">
          {/* The side panel renders at roughly this width; box it so the frame
              shows wrapping and density at the real column width. */}
          <div className="w-[400px] h-full border-l border-border bg-surface">
            <CrewNotesTab slug={SLUG} member={MEMBER} header={Header} visible />
          </div>
        </div>
      </QueryClientProvider>
    </Provider>,
  )
}

main()
