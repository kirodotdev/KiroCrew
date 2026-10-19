/**
 * Evidence capture for the transcript section-marker row.
 *
 * Mounts the REAL `SectionMarkerRow` — the component ChatPage and the SDK
 * message registry both dispatch to — between plain transcript text lines, so a
 * frame of this page shows what the new row draws and nothing else.
 *
 * Three states because they are the three a reviewer must judge: a normal
 * label, an unlabelled break, and a label at the 120-char schema cap. The cap
 * case repeats in a narrow column, where wrapping is load-bearing: a label span
 * that cannot shrink sizes to max-content and overflows instead of wrapping.
 *
 * Query params: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'
import { formatTsPrecise } from '../src/app-sdk/messageRenderers'
import SectionMarkerRow from '../src/pages/chat/SectionMarkerRow'

initI18n('en')

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
// BOTH attributes, matching the fork scene: 12 shipped rules key on `data-mode`, and the
// theme rules key on the `kiro-` prefixed value, so a bare `data-theme` frames a mis-themed row.
document.documentElement.dataset.mode = theme
document.documentElement.dataset.theme = theme === 'light' ? 'kiro-light' : 'kiro-dark'

// Through the SHIPPED formatter, never a hand-typed string: it draws seconds and gains a
// date off-day, so a literal photographs a stamp format the product never renders.
const at = (h: number, m: number, s: number): string => {
  const d = new Date()
  d.setHours(h, m, s, 0)
  return formatTsPrecise(d.toISOString()) ?? ''
}

// Exactly 120 characters, the schema cap, so the frame captioned as the cap shows the
// worst case rather than a shorter string that wraps less.
const LONG_LABEL =
  'reviewed the pagination rollout plan, reconciled every open follow-up item, ' +
  'and handed all the rest to the next reviewer'

const Line = ({ children }: { children: string }) => (
  <div className="text-[14px] leading-6 text-text">{children}</div>
)

const Caption = ({ children }: { children: string }) => (
  <div className="text-[11px] uppercase tracking-wide text-muted mt-6 mb-1">{children}</div>
)

function Page() {
  return (
    <div className="min-h-screen bg-bg p-8 text-text" style={{ fontFamily: 'var(--sans)' }}>
      <div data-capture-root className="flex flex-col" style={{ width: 820 }}>
        <Caption>labelled break between two units of work</Caption>
        <Line>Renamed the resolver and updated both call sites.</Line>
        <SectionMarkerRow label="item-42" time={at(14, 18, 32)} />
        <Line>Starting on the next item now.</Line>

        <Caption>unlabelled break</Caption>
        <Line>That closes the migration.</Line>
        <SectionMarkerRow label="" fallback="— End of section —" time={at(14, 32, 9)} />
        <Line>New topic.</Line>

        <Caption>label at the 120-char cap</Caption>
        <SectionMarkerRow label={LONG_LABEL} time={at(14, 41, 55)} />

        <Caption>same label in a narrow column — wraps between the rules</Caption>
        <div style={{ width: 360 }}>
          <SectionMarkerRow label={LONG_LABEL} time={at(15, 7, 3)} />
        </div>
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<Page />)
