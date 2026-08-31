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
import SectionMarkerRow from '../src/pages/chat/SectionMarkerRow'

initI18n('en')

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme)

const LONG_LABEL =
  'reviewed the pagination rollout plan and reconciled every open follow-up item'

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
        <SectionMarkerRow label="item-42" />
        <Line>Starting on the next item now.</Line>

        <Caption>unlabelled break</Caption>
        <Line>That closes the migration.</Line>
        <SectionMarkerRow label="" fallback="— End of section —" />
        <Line>New topic.</Line>

        <Caption>label at the 120-char cap</Caption>
        <SectionMarkerRow label={LONG_LABEL} />

        <Caption>same label in a narrow column — wraps between the rules</Caption>
        <div style={{ width: 360 }}>
          <SectionMarkerRow label={LONG_LABEL} />
        </div>
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<Page />)
