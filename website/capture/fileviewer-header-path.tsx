/**
 * Isolated capture entry for the file-viewer header path a11y fix (#7900).
 *
 * Mounts the REAL FileHeaderBreadcrumb (imported unmodified) inside a mimic of
 * the MarkdownPanel header bar. The path is a deeply nested Brazil-style tree so
 * the last-three-segment truncation is visible and the full path is only in the
 * hover/focus affordance.
 *
 * `?focus=1` moves keyboard focus onto the breadcrumb so the capture documents
 * the NEW route: the focus ring that appears for a keyboard user, which did not
 * exist before this change. `theme` comes from the query string: ?theme=light.
 */
import { createRoot } from 'react-dom/client'
import { useEffect, useRef } from 'react'
import { FileHeaderBreadcrumb } from '../src/components/MarkdownPanel'
import { FileText } from 'lucide-react'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const wantFocus = params.get('focus') === '1'
const diffMode = params.get('diff') === '1'
// Panel width: model the real side panel, which clips its overflow. 320 is
// SIDE_PANEL_MIN_W, the narrowest the viewer allows.
const panelW = Number(params.get('panelW') || '460')
document.documentElement.setAttribute('data-theme', theme)

const NESTED = '/home/user/workplace/MyWorkspace/src/PackageName/src/PackageName/index.ts'

function Bar() {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (wantFocus) {
      const el = ref.current?.querySelector('[role="group"]') as HTMLElement | null
      el?.focus()
    }
  }, [])
  return (
    // overflow-hidden mimics the real DetailPanel frame, so a readout that ran
    // past the panel edge would be CLIPPED here exactly as in the product.
    <div ref={ref} style={{ width: panelW, overflow: 'hidden', background: 'var(--bg)' }} className="border border-border rounded-md">
      <div className="shrink-0 border-b border-border">
        {/* Same structure as MarkdownPanel's header bar: relative (readout
            anchor), breadcrumb, diff-stats badge beside the filename, spacer,
            action button. This mirrors the real order so the badge's position
            is faithfully shown. */}
        <div className="relative flex items-center gap-2 h-[38px] px-3">
          <FileText size={14} className="text-muted shrink-0" />
          <FileHeaderBreadcrumb filePath={NESTED} />
          {diffMode && (
            <span className="text-[11px] font-mono font-semibold shrink-0">
              <span className="text-ok">+12</span>
              <span className="text-danger ml-1.5">-3</span>
            </span>
          )}
          <span className="flex-1 min-w-[8px]" />
          <span className="w-[26px] h-[26px] rounded-md bg-bg-hover shrink-0" />
        </div>
      </div>
      {/* spacer so the absolutely-positioned readout below the header is visible */}
      <div style={{ height: 90 }} />
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <div
    data-capture-root
    style={{ padding: 20, minHeight: '100vh', width: '100%', background: 'var(--bg)', color: 'var(--text)', font: '13px system-ui, -apple-system, sans-serif' }}
  >
    <div style={{ fontSize: 10, letterSpacing: 1, textTransform: 'uppercase', color: 'var(--text-muted)', marginBottom: 6 }}>
      File viewer header {wantFocus ? '(keyboard focus)' : '(default)'}{diffMode ? ' (diff mode)' : ''} - panel {panelW}px
    </div>
    <Bar />
  </div>,
)
