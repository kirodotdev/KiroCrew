import { memo, useEffect, useRef, useState } from 'react'
import { TriangleAlert } from 'lucide-react'
import { useTranslation } from 'react-i18next'

/**
 * Inline Excalidraw diagram.
 *
 * Renders an Excalidraw scene (`.excalidraw` JSON) as inline SVG — the same
 * treatment `MermaidBlock` gives a ```mermaid fence, and deliberately NOT the
 * iframe used for `<mcwidget>` HTML. Inline SVG reflows with the chat column and
 * stays selectable; an iframe would box the diagram off from both.
 *
 * The renderer (and rough.js behind it) is pulled in with a dynamic `import()`
 * so it stays out of the entry chunk. This module sits on the critical path —
 * every chat message renders through `MarkdownRenderer` — while an Excalidraw
 * fence is rare, so a static import would tax every user for a feature most
 * never hit. The promise is cached at module scope so N diagrams on a page share
 * one load; `import()` is idempotent regardless.
 *
 * The diagram is rendered against its own painted canvas rather than composited
 * onto the dashboard, so it does NOT depend on the active theme. That is
 * deliberate: a scene carries the author's explicit colours (typically near-black
 * ink), and restyling it per theme would either distort those colours or leave
 * the diagram invisible on a dark surface. It also means there is no theme state
 * for this effect to track and go stale against.
 *
 * On any failure (malformed JSON, truncated stream, empty scene) it explains
 * itself and falls back to the source. A diagram should never cost the user the
 * content — if we can't draw it, they still get the text.
 */
type Renderer = typeof import('../lib/excalidrawScene')

let rendererLoad: Promise<Renderer> | null = null

function loadRenderer(): Promise<Renderer> {
  if (!rendererLoad) rendererLoad = import('../lib/excalidrawScene')
  return rendererLoad
}

/** Exported for tests, which need to clear the module-scope cache between cases. */
export function __resetRendererCache(): void {
  rendererLoad = null
}

export const ExcalidrawBlock = memo(function ExcalidrawBlock({
  code,
  className = 'my-3 flex justify-center overflow-x-auto min-h-[60px]',
}: {
  code: string
  className?: string
}) {
  const { t } = useTranslation()
  const ref = useRef<HTMLDivElement>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    // Guards against a late resolve landing after unmount or after `code`
    // changed — without it a slow first load could paint a stale diagram over
    // a newer one.
    let live = true
    setFailed(false)
    loadRenderer()
      .then(({ renderExcalidrawSource }) => {
        if (!live || !ref.current) return
        const svg = renderExcalidrawSource(code)
        if (!live || !ref.current) return
        ref.current.replaceChildren(svg)
      })
      .catch(() => {
        if (!live) return
        setFailed(true)
      })
    return () => { live = false }
  }, [code])

  if (failed) {
    // Muted and height-capped, not a red wall. Scene JSON is hundreds of lines
    // of machine data, so dumping it full-bleed in danger red reads as a crash
    // rather than a fallback — and says nothing about what happened. The header
    // names the situation; the source stays available but stops dominating.
    return (
      <div className={className}>
        <div className="w-full rounded-md border border-border bg-bg-elevated p-3">
          <div className="flex items-center gap-1.5 mb-2 text-muted text-[12px]">
            <TriangleAlert className="lucide-inline" />
            {t('components.excalidrawBlock.render_failed')}
          </div>
          <pre className="m-0 max-h-64 overflow-auto text-muted text-[12px] font-mono whitespace-pre-wrap break-all">{code}</pre>
        </div>
      </div>
    )
  }

  return <div ref={ref} className={className} />
})

export default ExcalidrawBlock
