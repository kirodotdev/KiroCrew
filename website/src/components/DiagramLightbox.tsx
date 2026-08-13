import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { motion, useReducedMotion } from 'framer-motion'
import { X } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { useDialogFocusTrap } from '../hooks/useDialogFocusTrap'

/**
 * Full-viewport viewer for an inline-rendered SVG diagram (mermaid).
 *
 * Why not the existing image `Lightbox` (MarkdownRenderer): that viewer is
 * `<img src>`-based, and mermaid emits SVG whose text labels live in
 * `<foreignObject>` HTML — browsers refuse to paint foreignObject content in an
 * image context, so serializing the diagram to a data URI would blank every
 * label. The SVG has to stay live DOM. Why not `Modal`: it is card chrome
 * (max-width, padded body, header bar) that fights the fit-to-viewport goal and
 * carries no focus trap. This component composes the shared dialog primitives
 * instead: `useDialogFocusTrap` (focus in on open, restore on close, Escape,
 * Tab cycling) + portal + `role="dialog"`.
 *
 * Scaling: mermaid pins its SVG to the source column via an inline
 * `max-width` and gives it a `viewBox`, so clearing the pin and setting
 * width/height to 100% lets `preserveAspectRatio` (default `xMidYMid meet`)
 * scale the vector output to fit the viewport without distortion or raster
 * blur. An SVG without a viewBox cannot fit-scale, so it keeps its natural
 * size and the host scrolls — the content is never cropped either way.
 *
 * The markup is inserted with `createContextualFragment`, the same path (and
 * therefore the same sanitization posture) as the inline `MermaidBlock`
 * rendering the identical string: mermaid output under
 * `securityLevel: 'strict'`, never raw user HTML.
 */
export default function DiagramLightbox({ svg, onClose }: { svg: string; onClose: () => void }) {
  const { t } = useTranslation()
  const dialogRef = useRef<HTMLDivElement>(null)
  const hostRef = useRef<HTMLDivElement>(null)
  const reduceMotion = useReducedMotion()
  useDialogFocusTrap(dialogRef, onClose)

  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    const range = document.createRange()
    range.selectNodeContents(host)
    range.deleteContents()
    host.appendChild(range.createContextualFragment(svg))
    const el = host.querySelector('svg')
    if (el && el.getAttribute('viewBox')) {
      // Clear mermaid's column-width pin and fill the host; viewBox +
      // preserveAspectRatio do the aspect-correct fitting.
      el.style.maxWidth = 'none'
      el.style.maxHeight = 'none'
      el.style.width = '100%'
      el.style.height = '100%'
    }
  }, [svg])

  // While the viewer is open, claim Escape so an enclosing <Modal> (e.g. the
  // skill/MCP browsers, which guard on !e.defaultPrevented) does not also
  // dismiss itself on the same keypress. useDialogFocusTrap still receives the
  // event (both listeners are capture-phase) and closes this viewer.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') e.preventDefault()
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [])

  return createPortal(
    <motion.div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-label={t('components.diagramLightbox.diagram_viewer')}
      className="fixed inset-0 z-[9999] bg-bg/95 backdrop-blur-sm flex flex-col"
      initial={reduceMotion ? false : { opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.15 }}
      // Click-out dismissal: any click that is not on the diagram itself (or on
      // the close button, which handles itself) closes the viewer. Escape is
      // handled by useDialogFocusTrap plus the preventDefault claim above.
      onClick={e => {
        const el = e.target as HTMLElement
        if (!el.closest('svg') && !el.closest('button')) onClose()
      }}
    >
      <div className="flex items-center justify-end px-4 h-12 shrink-0">
        <button
          aria-label={t('components.diagramLightbox.close')}
          title={t('components.diagramLightbox.close')}
          className="p-1.5 rounded-md text-muted hover:text-text hover:bg-bg-hover transition-colors cursor-pointer"
          onClick={onClose}
        >
          <X className="lucide-inline" aria-hidden="true" />
        </button>
      </div>
      {/* min-h-0 lets the flex child shrink to the viewport; overflow-auto is
          the escape hatch for a no-viewBox SVG kept at natural size. */}
      <div className="flex-1 min-h-0 overflow-auto px-6 pb-6">
        <div ref={hostRef} className="w-full h-full flex items-center justify-center" />
      </div>
    </motion.div>,
    document.body
  )
}
