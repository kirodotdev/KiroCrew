/**
 * Isolated capture entry for the nav rail's right-edge occupants: the unread
 * badge, the sub-agent activity readout, and the hover/focus shortcut hint.
 *
 * WHY ISOLATED: the rail only renders inside the full SPA shell, which needs a
 * live gateway plus a dashboard credential — without one the shell renders the
 * Kiro CLI prerequisite gate instead of a rail. This mounts the REAL `NavItem`
 * and `NavBadge` exported from src/App.tsx against the REAL stylesheet, so the
 * class strings measured here are production's own rather than a copy that can
 * drift.
 *
 * WHY A BROWSER AT ALL: the unit suite (src/test/App.test.tsx) pins the
 * STRUCTURE — the badge is in flow and follows the chord in the same flex line.
 * happy-dom computes no layout, so only a real engine can answer whether the two
 * boxes actually intersect, which is the defect a user reported: the "1" sat on
 * top of "⌥C" and the row advertised a keystroke nobody could read.
 *
 * Query string:
 *   ?fix=off    reproduce the pre-fix positioning (badge/activity taken OUT of
 *               flow at their old `right-2` / `right-8` offsets), so the before
 *               frame is asserted to reproduce the overlap rather than assumed to
 *   ?fix=on     the shipped in-flow layout (default)
 *   ?theme=dark|light
 *
 * `window.__measure()` returns one record per right-edge element with its rect
 * and its intersection area with the chord.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import enXA from '../src/i18n/locales/en-XA.json'
import { store } from '../src/store'
import { NavItem, NavBadge } from '../src/App'
import { getBuiltinSurface } from '../src/surfaces/registry'
import { markSlotUnread } from '../src/store/dashboardSlice'
import { sseSubagentQueued } from '../src/store/chatSlice'
import '../src/surfaces/builtins'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const fix = params.get('fix') === 'off' ? 'off' : 'on'
const theme = params.get('theme') || 'dark'

document.documentElement.dataset.theme = theme
document.documentElement.classList.toggle('dark', theme === 'dark')

if (fix === 'off') {
  // The pre-fix declarations, verbatim from the classes BadgeIndicator and
  // ActivityIndicator used to carry: `absolute right-2 top-1/2 -translate-y-1/2`
  // and `absolute right-8 top-1/2 -translate-y-1/2`. Expressed as an override
  // rather than a second copy of the components, so the "before" arm cannot
  // drift from the "after" arm in anything but the one property under test.
  const css = document.createElement('style')
  css.textContent = `
    [data-capture-root] [aria-label$="unread conversations"],
    [data-capture-root] [aria-label$="approvals needed"] {
      position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
    }
    [data-capture-root] [aria-label$="subagents in flight"] {
      position: absolute; right: 32px; top: 50%; transform: translateY(-50%);
    }
  `
  document.head.appendChild(css)
}

// One unread conversation and two queued sub-agents on the Sessions row — the
// state the reporter's screenshot was taken in, plus the activity readout that
// shares the same edge.
store.dispatch(markSlotUnread({ slot: 'background', ts: '2026-01-01T00:00:05Z' }))
store.dispatch(sseSubagentQueued({ slot: 'background', queued: 2 }))

await initI18n()

const qc = new QueryClient({
  defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false } },
})

declare global {
  interface Window {
    __measure: () => Array<{
      name: string
      rect: { left: number; right: number; top: number; bottom: number }
      overlapsChord: boolean
      overlapArea: number
      chordClipped: boolean
    }>
  }
}

const rectOf = (el: Element) => {
  const r = el.getBoundingClientRect()
  return { left: r.left, right: r.right, top: r.top, bottom: r.bottom }
}

window.__measure = () => {
  const root = document.querySelector('[data-capture-root]')!
  // Per ROW, not globally: each row owns its own chord, and the squeeze row's
  // indicator must be compared against the squeeze row's hint rather than the
  // Sessions one. A global query would silently compare boxes on different lines
  // and report "clear" for an overlap that is really there.
  return [...root.querySelectorAll('[data-capture-row]')].flatMap(row => {
    const chord = row.querySelector('[data-testid^="nav-shortcut-"]')
    if (!chord) return []
    const c = chord.getBoundingClientRect()
    const which = (row as HTMLElement).dataset.captureRow
    const targets: Array<[string, Element | null]> = [
      ['unread-badge', row.querySelector('[aria-label$="unread conversations"]')],
      ['activity', row.querySelector('[aria-label$="subagents in flight"]')],
      ['wide-badge', row.querySelector('[aria-label$="approvals needed"]')],
    ]
    return targets.flatMap(([name, el]) => {
      if (!el) return []
      const r = el.getBoundingClientRect()
      const w = Math.max(0, Math.min(c.right, r.right) - Math.max(c.left, r.left))
      const h = Math.max(0, Math.min(c.bottom, r.bottom) - Math.max(c.top, r.top))
      // The chord must also still be ON the row: a `shrink-0` indicator that
      // pushed it out would score zero overlap while being strictly worse than
      // the bug. Report that separately so the runner can fail on it.
      const rowBox = row.getBoundingClientRect()
      return [{
        name: `${which}/${name}`,
        rect: rectOf(el),
        overlapsChord: w * h > 0,
        overlapArea: w * h,
        chordClipped: c.right > rowBox.right + 0.5 || c.left < rowBox.left - 0.5 || c.width === 0,
      }]
    })
  })
}

const sessions = getBuiltinSurface('chat')!
const schedule = getBuiltinSurface('schedule')!
const projects = getBuiltinSurface('projects')!

/**
 * The widest nav label the app actually ships, read from the catalog rather
 * than copied out of it: the longest `nav.*` value in the `en-XA`
 * pseudolocale, which exists precisely to expose layout expansion. Derived at
 * runtime because `en-XA.json` is GENERATED (scripts/gen-pseudolocale.mjs) — a
 * pasted copy would quietly stop being the real worst case the next time that
 * file is regenerated. Currently `nav.agent_capabilities` at 37 characters,
 * against English's 13 for the same row.
 */
const LONGEST_NAV_LABEL = Object.values((enXA as { nav?: Record<string, unknown> }).nav ?? {})
  .filter((v): v is string => typeof v === 'string')
  .reduce((widest, v) => (v.length > widest.length ? v : widest), '')

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/chat']}>
        {/* 220px is the rail's row track: RAIL_W_EXPANDED (236) less the rail
            container's `mx-2` inset on both sides, matching the shell's own
            `width: railWidthFor(...) - 16`. */}
        <div className="bg-bg text-text min-h-screen p-6" data-capture-root>
          <div className="w-[220px] flex flex-col gap-0.5">
            <div data-capture-row="sessions">
              <NavItem
                navId="chat"
                path="/chat"
                label="Sessions"
                icon={sessions.icon}
                active
                collapsed={false}
                badge={<NavBadge navId="chat" collapsed={false} appBadges={{}} />}
              />
            </div>
            {/* A chord-bearing row with NO badge: its hint is where the Sessions
                hint has to be legible too, and its right edge is the alignment
                the badge row is compared against. */}
            <div data-capture-row="schedule">
              <NavItem
                navId="schedule"
                path="/schedule"
                label="Schedule"
                icon={schedule.icon}
                active={false}
                collapsed={false}
                badge={<NavBadge navId="schedule" collapsed={false} appBadges={{}} />}
              />
            </div>
            {/* THE SQUEEZE. The worst reachable case for an in-flow right edge:
                the longest shipped translation of a nav label against a
                three-digit count, on a row that also renders a chord (Alt+P).
                `projects` is a stub surface — no `slotMode`, no
                `unreadSelector` — so `appBadges` drives its count through the
                REAL BadgeIndicator rather than a stand-in. Without this scene
                the harness only ever proved the English label at count 1, which
                is the one width nothing squeezes. */}
            <div data-capture-row="squeeze">
              <NavItem
                navId="projects"
                path="/projects"
                label={LONGEST_NAV_LABEL}
                icon={projects.icon}
                active={false}
                collapsed={false}
                badge={<NavBadge navId="projects" collapsed={false} appBadges={{ projects: 999 }} />}
              />
            </div>
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
