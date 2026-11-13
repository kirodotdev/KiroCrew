/**
 * ZoomLightbox — the full-size view of a critiqued screen.
 *
 * It is an aria-modal dialog, so it takes keyboard focus when it opens, keeps
 * Tab inside, and gives focus back to the Enlarge control when it closes. The
 * Close button sits INSIDE the dialog node: a trap that held focus in a dialog
 * without its own Close would leave keyboard users nowhere to go but Escape.
 *
 * Escape stays on the page's own window listener (DesignCritiquePage), so the
 * trap is wired with Escape off and a stable no-op in its place.
 */
import { useRef, type ReactNode } from 'react'
import { X } from 'lucide-react'
import { S } from './styles'
import { i18nT } from '../../i18n/t'
import { useDialogFocusTrap } from '../../hooks/useDialogFocusTrap'

const NO_ESCAPE = () => {}

export default function ZoomLightbox({ src, onClose, children }: {
  src: string
  onClose: () => void
  /** Overlaid on the image: the finding markers. */
  children?: ReactNode
}) {
  const dialogRef = useRef<HTMLDivElement>(null)
  useDialogFocusTrap(dialogRef, NO_ESCAPE, { handleEscape: false })
  return (
    <div
      style={S.lbOverlay}
      role="presentation"
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div ref={dialogRef} style={S.lbInner} role="dialog" aria-modal="true" aria-label={i18nT('apps.designCritique.designCritiquePage.full_size_view')}>
        <img src={src} style={S.lbImg} alt={i18nT('apps.designCritique.designCritiquePage.full_size')} />
        {children}
        {/* The wrapper is `fixed` to the viewport, so moving Close inside the
            dialog leaves it in the same corner. */}
        <div style={S.lbCloseWrap}>
          <button
            style={S.lbClose}
            onClick={onClose}
            title={i18nT('apps.designCritique.designCritiquePage.close_esc')}
            aria-label={i18nT('apps.designCritique.askLayer.close')}
          ><X size={18} /></button>
        </div>
      </div>
    </div>
  )
}
