/**
 * Isolated capture entry for ChatPage's transcript SCROLL SHELL golden frames.
 *
 * Mounts the REAL ChatPage (not a mock of it) with a preloaded store and a
 * deterministic fixture transcript; the capture script answers its API calls
 * via page.route. Golden frames are the visual half of the characterization
 * net: the migration re-captures the same scenes and compares pixels, so
 * everything nondeterministic is pinned here — animations off, fixed
 * timestamps, fixed viewport, DSF 1.
 */
import { initI18n } from '../src/i18n'
import { mountChatPage } from './chatpage-mount'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('scene') || 'long' // long | short | paging | busy
// `busy`: the active slot has a turn running, so the composer shows its
// mid-turn affordance (the Steer/Queue split button). Evidence that the main
// chat's composer is untouched by pane-only busy-mode changes.
const busyScene = scene === 'busy'
// ThemeProvider derives the root theme from its OWN preference store
// (localStorage 'mc-theme', falling back to 'system') and rewrites the
// data-theme attribute on mount — so setting the attribute alone is
// overwritten and every frame silently renders in the system theme.
// Seed the preference the provider actually reads; the attribute below
// only covers the pre-mount paint.
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme)

// Deterministic pixels: no entrance eases, no pulsing dots, no smooth scroll.
const style = document.createElement('style')
style.textContent = '*, *::before, *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; }'
document.head.appendChild(style)

type Msg = { role: string; content: string; cls: string; ts: string }
const pad = (n: number) => String(n).padStart(2, '0')
const mkLong = (): Msg[] => Array.from({ length: 16 }, (_, i) => ([
  { role: 'user', content: `Status check #${i + 1}: anything new in the queue?`, cls: '', ts: `2026-08-27T01:${pad(10 + i)}:00Z` },
  { role: 'assistant', content: `Sweep ${i + 1} done.\n\n- two issues triaged as duplicates\n- one PR moved to review-ready\n- CI green on the retry`, cls: '', ts: `2026-08-27T01:${pad(10 + i)}:30Z` },
])).flat()
const mkShort = (): Msg[] => mkLong().slice(0, 2)

const messages = scene === 'short' || busyScene ? mkShort() : mkLong()
;(window as unknown as { __CAPTURE_MESSAGES__: Msg[] }).__CAPTURE_MESSAGES__ = messages

mountChatPage({ messages, title: 'scroll-shell fixture', busy: busyScene, paging: scene === 'paging' })
