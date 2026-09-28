/**
 * Isolated capture entry for side-panel scroll memory across chat-slot
 * switches (#5701).
 *
 * WHY ISOLATED: the defect IS a mount-lifecycle effect. Panel tabs live in
 * per-slot buckets, so switching chat sessions unmounts the tab body and a
 * scrolled document remounts at `scrollTop = 0` — behaviour that only exists
 * in a real browser with real layout (happy-dom computes no scroll geometry,
 * which is why src/test/useScrollMemory.test.tsx can only pin the hook's
 * record/restore contract against faked scrollTop values).
 *
 * WHAT IS FAITHFUL: the REAL `ArtifactPanel` (embedded, exactly as SidePanel's
 * TabBody mounts it) inside a side-panel-sized frame, with the artifact API
 * stubbed at the fetch boundary. The slot switch is reproduced the way
 * SidePanel does it: the panel for slot A is REMOVED from the tree while
 * "slot B" shows, then re-created — same key, new mount.
 *
 * `?fix=off` omits `scrollMemoryKey` (the pre-fix wiring), so one harness
 * captures both arms and the before state is asserted to reproduce rather
 * than assumed.
 *
 * Query string: ?theme=dark&fix=on
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
// Initialise i18next exactly as main.tsx does — without it every label in the
// frame is blank and the screenshot misrepresents the real UI.
import { initI18n } from '../src/i18n/all'
import '../src/index.css'
import { ThemeProvider } from '../src/hooks/useTheme'
import ArtifactPanel from '../src/components/ArtifactPanel'
import { scrollMemoryKeyFor } from '../src/hooks/useScrollMemory'

initI18n()

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const fixOn = params.get('fix') !== 'off'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLUG = 'scroll-memory-demo'

/** Long markdown document — enough height that a mid-document offset is
 * unambiguous in a screenshot (section numbers double as position markers). */
const CONTENT = Array.from({ length: 80 }, (_, i) =>
  `## Section ${i + 1}\n\nParagraph for section ${i + 1}. The section number in the heading `
  + 'is the visual position marker: after the slot round-trip, the frame '
  + 'should show the same sections, not Section 1.\n',
).join('\n')

const artifact = {
  slug: SLUG,
  name: 'Scroll memory demo',
  kind: 'markdown',
  content: CONTENT,
  version: 1,
  versions: [1],
  tags: [],
  created_at: '2026-08-24T00:00:00Z',
  updated_at: '2026-08-24T00:00:00Z',
  source_path: '',
}

/** Fetch stub at the API boundary: the artifact read returns the fixture
 * document; comment reads return empty; anything else answers an empty
 * object so no code path hangs on a network that does not exist here. */
const realFetch = window.fetch.bind(window)
window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/')) return realFetch(input, init)
  const body = url.includes(`/api/artifacts/${SLUG}`) && !url.includes('comment')
    ? artifact
    : url.includes('comment') ? { comments: [] } : {}
  return Promise.resolve(new Response(JSON.stringify(body), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  }))
}

function Harness() {
  const [slot, setSlot] = useState<'a' | 'b'>('a')
  ;(window as unknown as { __switch: (s: 'a' | 'b') => void }).__switch = setSlot
  return (
    // A right-dock-sized panel frame: the artifact body's scroll range comes
    // from real layout inside this box, as it does inside SidePanel's dock.
    <div style={{ width: 420, height: '100vh', marginLeft: 'auto', borderLeft: '1px solid var(--border)', display: 'flex', flexDirection: 'column' }} className="bg-bg text-text">
      {slot === 'a' ? (
        <ArtifactPanel
          embedded
          slug={SLUG}
          kind="markdown"
          content=""
          scrollMemoryKey={fixOn ? scrollMemoryKeyFor('slot-a', 'tab-1') : undefined}
          onClose={() => {}}
        />
      ) : (
        <div className="h-full flex items-center justify-center text-muted text-[13px]">
          Session B — slot A&apos;s tab body is unmounted
        </div>
      )}
    </div>
  )
}

/** The embedded artifact body's scroll box: the deepest overflow-auto div
 * that actually overflows. Exposed as helpers so the driver never encodes
 * the component's internal class chain. */
function scrollBox(): HTMLElement | null {
  const candidates = [...document.querySelectorAll<HTMLElement>('div')]
    .filter(d => /overflow-auto/.test(d.className) && d.scrollHeight > d.clientHeight + 10)
  return candidates[candidates.length - 1] ?? null
}
;(window as unknown as { __scrollTo: (px: number) => boolean }).__scrollTo = (px: number) => {
  const el = scrollBox()
  if (!el) return false
  el.scrollTop = px
  // jsdom-free real browser: the scroll event fires natively; nothing to fake.
  return true
}
;(window as unknown as { __measure: () => unknown }).__measure = () => {
  const el = scrollBox()
  return el
    ? { found: true, scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }
    : { found: false }
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <ThemeProvider>
      <MemoryRouter>
        <Harness />
      </MemoryRouter>
    </ThemeProvider>
  </QueryClientProvider>,
)
