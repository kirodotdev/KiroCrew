import { memo, useEffect, useRef, useState, type ReactNode } from 'react'
import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import ErrorNotice from '../ErrorNotice'
import { dispatchLightbox } from './Lightbox'

type Render = { svg: string | null; failed: boolean }

/** Diagrams kept per page; the oldest is dropped past this. */
const CACHE_LIMIT = 64
const renders = new Map<string, Promise<Render | null>>()
/** How many mounted blocks show each source right now. */
const shown = new Map<string, number>()
let queue: Promise<unknown> = Promise.resolve()

/** Every render is a JVM on the gateway host, so they run one at a time and each
 *  source is asked for once. A queued source no block shows any more (a fence
 *  still streaming, a preview being typed into) is skipped, and neither a skip
 *  (`null`) nor a failed request is kept. */
function renderPlantUml(code: string): Promise<Render | null> {
  let pending = renders.get(code)
  if (!pending) {
    pending = queue
      .then(() => (shown.has(code) ? api.plantUmlSvg(code) : null))
      .catch((): Render => { renders.delete(code); return { svg: null, failed: true } })
      .then(result => { if (result === null) renders.delete(code); return result })
    queue = pending
    renders.set(code, pending)
    if (renders.size > CACHE_LIMIT) renders.delete(renders.keys().next().value as string)
  }
  return pending
}

/** Exported for tests, which need a clean cache between cases. */
export function __resetPlantUmlRenders(): void {
  renders.clear()
  shown.clear()
  queue = Promise.resolve()
}

/**
 * A ```plantuml fence drawn by the gateway host's own PlantUML.
 *
 * `children` is the code block the fence renders as without this: it shows while
 * the picture is on its way, and stays when there is none (no `plantuml` on the
 * host, a viewer who is not the owner, a diagram PlantUML cannot draw). The
 * picture is an `<img>` of the SVG, never inline markup, so nothing in the diagram
 * can script the page. It opens the shared image viewer, and the source stays one
 * disclosure below it.
 *
 * Not React Query: `MarkdownRenderer` also renders outside any
 * `QueryClientProvider` (see `usePathKind.ts`).
 */
export const PlantUmlBlock = memo(function PlantUmlBlock({ code, children }: { code: string; children: ReactNode }) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const [render, setRender] = useState<Render | null>(null)
  const imgRef = useRef<HTMLImageElement>(null)
  const sourceRef = useRef<HTMLDetailsElement>(null)
  // A picture that lands while the reader works in the code block (its scratch
  // editor, its buttons) leaves the source open rather than folding it shut.
  const [keepOpen, setKeepOpen] = useState(false)

  useEffect(() => {
    let live = true
    setRender(null)
    setKeepOpen(false)
    // A reader may have closed it by hand; with no picture the summary is hidden.
    if (sourceRef.current) sourceRef.current.open = true
    shown.set(code, (shown.get(code) ?? 0) + 1)
    void renderPlantUml(code).then(result => {
      if (!live || !result) return
      setKeepOpen(!!sourceRef.current?.contains(document.activeElement))
      setRender(result)
    })
    return () => {
      live = false
      const left = (shown.get(code) ?? 1) - 1
      if (left > 0) shown.set(code, left)
      else shown.delete(code)
    }
  }, [code])

  const svg = render?.svg
  // `children` keeps one place in the tree in every state: moving it would
  // remount the code block and drop an edit the reader started before the
  // picture arrived. Without a picture the disclosure is open and its summary
  // hidden, so the block reads as the plain code block it is.
  return (
    <div className={svg ? 'my-3' : undefined}>
      {/* No hand-off: this renderer is embedded in hosts that hold unsaved
          drafts and cannot tell which (the file panel's editor buffer, the
          composer's markdown preview), so the navigation could discard what
          the user typed. */}
      {render?.failed && (
        <ErrorNotice
          variant="inline"
          className="mb-2"
          message={i18nT('components.markdownRenderer.mermaid_render_failed')}
          testId="plantuml-render-error"
        />
      )}
      {svg && (
        <button
          type="button"
          data-testid="plantuml-enlarge"
          aria-label={i18nT('components.diagramLightbox.enlarge_diagram')}
          className="block max-w-full cursor-zoom-in rounded-md"
          onClick={() => { if (imgRef.current) dispatchLightbox(imgRef.current) }}
        >
          <img
            ref={imgRef}
            src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`}
            alt=""
            data-lightbox-image=""
            data-lightbox-filename="plantuml-diagram.svg"
            className="max-w-full"
          />
        </button>
      )}
      <details ref={sourceRef} open={!svg || keepOpen} className={svg ? 'mt-1' : undefined}>
        <summary hidden={!svg} className="text-[13px] text-muted cursor-pointer">{i18nT('components.markdownRenderer.diagram_source')}</summary>
        {children}
      </details>
    </div>
  )
})
