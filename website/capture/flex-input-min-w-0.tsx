/**
 * Isolated capture + measurement entry for issue #3789: three flex-1 inputs
 * without min-w-0 clip their row's trailing controls at narrow widths.
 *
 * WHY ISOLATED: the defect only shows under real layout at narrow container
 * widths (320-420px), which happy-dom cannot compute. Each of the three sites is
 * an unrelated surface, so each is rendered in its own constrained frame that
 * reproduces the real ancestry that matters: a flex row holding the input and
 * a trailing control, inside an overflow-hidden container.
 *
 * The faithful part is the CLASS STRINGS: each scene renders the exact classes
 * the real component uses (the shared Input is imported directly; the two raw
 * <input> sites copy their literal className), so the measurement exercises
 * the same Tailwind output production ships.
 *
 * window.__measure() reports, per scene, whether the row's scrollWidth exceeds
 * its clientWidth and whether the trailing control's right edge stays inside
 * the clip box — the two assertions the fix must hold at 320/360/420px.
 *
 * Scene + theme + width via query string: ?theme=dark&w=360
 * `fix=off` strips min-w-0 from all three scenes to reproduce the BEFORE state
 * (the shared Input gets it re-stripped via className override) so the same
 * harness captures both sides of the change.
 */
import { createRoot } from 'react-dom/client'
import { initI18n } from '../src/i18n'
import { Input } from '../src/components/ui'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const width = parseInt(params.get('w') || '360', 10)
const fixOn = params.get('fix') !== 'off'
const minW = fixOn ? 'min-w-0 ' : ''

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/** One constrained frame per site, mirroring its real row ancestry. */
function Scenes() {
  return (
    <div className="flex flex-col gap-6 p-4 bg-bg text-text" style={{ width }}>
      {/* Site 1: shared Input (ui.tsx) — imported directly, real base class. */}
      <section data-scene="ui-input" className="border border-border rounded p-2 overflow-hidden">
        <div className="text-[11px] text-muted mb-1">shared Input + trailing button @ {width}px</div>
        <div data-row className="flex gap-2 overflow-hidden">
          <Input style={fixOn ? undefined : { minWidth: 'auto' }} defaultValue="a fairly long value typed into the shared input" />
          <button data-trailing className="px-3 py-2 bg-accent text-accent-fg rounded text-[13px] shrink-0">Save</button>
        </div>
      </section>

      {/* Site 2: SessionArchive.tsx filter row — literal classes from the site. */}
      <section data-scene="session-archive" className="border border-border rounded p-2 overflow-hidden">
        <div className="text-[11px] text-muted mb-1">SessionArchive filter + Reload @ {width}px</div>
        <div data-row className="flex gap-2 overflow-hidden">
          <input
            className={`flex-1 ${minW}bg-bg-2 border border-border rounded px-2 py-1 text-[13px]`}
            placeholder="Fuzzy filter (substring match)"
            defaultValue="a long filter query that exceeds the intrinsic width"
          />
          <button data-trailing className="px-2 py-1 bg-accent text-accent-fg rounded text-[13px]">Reload</button>
        </div>
      </section>

      {/* Site 3: ChatEmbed.tsx composer row — literal classes from the site. */}
      <section data-scene="chat-embed" className="border border-border rounded p-2 overflow-hidden">
        <div className="text-[11px] text-muted mb-1">ChatEmbed composer + send @ {width}px</div>
        <div data-row className="flex items-center gap-2 px-3 py-2 overflow-hidden border-t border-border bg-bg-subtle">
          <input
            type="text"
            className={`flex-1 ${minW}px-3 py-2 text-sm bg-bg-elevated border border-border rounded-md text-text outline-none focus:border-accent transition-colors`}
            defaultValue="a message long enough to exceed the input's intrinsic minimum width"
          />
          <button data-trailing className="p-2 rounded-md bg-accent text-accent-fg shrink-0" aria-label="Send message">↑</button>
        </div>
      </section>
    </div>
  )
}

interface SceneMeasure {
  scene: string
  rowScrollWidth: number
  rowClientWidth: number
  overflows: boolean
  trailingRight: number
  clipRight: number
  trailingClipped: boolean
}

declare global {
  interface Window {
    __measure: () => SceneMeasure[]
  }
}

window.__measure = () =>
  Array.from(document.querySelectorAll<HTMLElement>('[data-scene]')).map(section => {
    const row = section.querySelector<HTMLElement>('[data-row]')!
    const trailing = section.querySelector<HTMLElement>('[data-trailing]')!
    const rowRect = row.getBoundingClientRect()
    const trailingRect = trailing.getBoundingClientRect()
    return {
      scene: section.dataset.scene!,
      rowScrollWidth: row.scrollWidth,
      rowClientWidth: row.clientWidth,
      overflows: row.scrollWidth > row.clientWidth,
      trailingRight: Math.round(trailingRect.right),
      clipRight: Math.round(rowRect.right),
      trailingClipped: trailingRect.right > rowRect.right + 0.5,
    }
  })

initI18n('en')
createRoot(document.getElementById('root')!).render(<Scenes />)
