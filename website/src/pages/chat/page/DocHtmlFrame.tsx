/**
 * A doc's HTML block, drawn live (PoC): the block's HTML in the same sandboxed,
 * themed frame chat widgets use (`buildSrcdoc`: a null-origin iframe, scripts
 * allowed, nothing fetched), sized to its content and with a transparent page, so
 * it reads as part of the doc rather than a box on it.
 *
 * Mounted by `docHtml` inside the editor, where the app's theme context is out of
 * reach: the pane passes the theme in.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { Code, Trash2 } from 'lucide-react'
import { AGENT_PAGE_SANDBOX, buildSrcdoc } from '../../../lib/widgetSrcdoc'

export interface EmbedTheme { mode: 'dark' | 'light'; vars: Record<string, string> }

/** Ahead of the block's own HTML: the frame's page is see-through and edge to edge,
 *  so the block sits on the doc itself. The block can still colour its own parts. */
// The widget frame pads its body for chat bubbles; here the block lines up with the text.
const BLEND = '<style>html,body{background:transparent!important;margin:0!important;padding:0!important}body{font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--text)}</style>'

/** Heights the frame may report: from one line to a long page, never a runaway. */
const MIN_H = 24
const MAX_H = 2400

export default function DocHtmlFrame({ html, theme, onEdit, onDelete, onHeight }: {
  html: string
  theme: EmbedTheme
  onEdit: () => void
  onDelete: () => void
  /** The frame's height changed: the editor re-measures the lines below it. */
  onHeight: () => void
}) {
  const frame = useRef<HTMLIFrameElement>(null)
  const [height, setHeight] = useState(120)
  const srcdoc = useMemo(
    () => buildSrcdoc({ html: BLEND + html, themeVars: theme.vars, mode: theme.mode, includeHeightReporter: true }),
    [html, theme],
  )
  const onHeightRef = useRef(onHeight)
  onHeightRef.current = onHeight
  useEffect(() => {
    // Only this frame's own reports: every widget on the page posts the same message.
    const on = (e: MessageEvent) => {
      if (e.source !== frame.current?.contentWindow || e.data?.type !== 'mc-widget-height' || typeof e.data.height !== 'number') return
      const h = Math.min(MAX_H, Math.max(MIN_H, Math.ceil(e.data.height)))
      setHeight(prev => (prev === h ? prev : h))
    }
    window.addEventListener('message', on)
    return () => window.removeEventListener('message', on)
  }, [])
  useEffect(() => { onHeightRef.current() }, [height])

  return (
    <div data-he="root" data-testid="doc-html">
      <iframe ref={frame} title="HTML block" sandbox={AGENT_PAGE_SANDBOX} srcDoc={srcdoc} data-testid="doc-html-frame"
        style={{ display: 'block', width: '100%', height, border: 0, background: 'transparent', colorScheme: theme.mode }} />
      <div data-he="tools">
        <button type="button" data-he="tool" data-testid="doc-html-edit" onMouseDown={e => e.preventDefault()} onClick={onEdit}>
          <Code size={14} aria-hidden /> Edit HTML
        </button>
        <button type="button" data-he="tool" data-testid="doc-html-delete" aria-label="Delete HTML block" onMouseDown={e => e.preventDefault()} onClick={onDelete}>
          <Trash2 size={14} aria-hidden />
        </button>
      </div>
    </div>
  )
}
