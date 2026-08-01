/**
 * PdfPreview — the rendered-PDF pane.
 *
 * Uses the BROWSER's own PDF viewer via an `<object>` element rather than
 * bundling a JS renderer. That is a deliberate dependency decision: the upstream
 * app shipped `pdfjs-dist` plus a hand-written text layer and its stylesheet
 * (~1 MB of JS and a worker chunk) to reproduce something Chrome, Firefox, Safari
 * and Edge all do natively — including text selection, find-in-page, zoom, page
 * thumbnails, rotate and print. This repository has no PDF renderer today, and
 * adding one to reimplement a built-in viewer is not a trade worth making.
 *
 * The backend serves the PDF with `Content-Disposition: inline` and a restrictive
 * per-response CSP (`sandbox; default-src 'none'`), so the document — which is
 * content the agent or a cloned repository produced — cannot script the
 * dashboard's origin.
 *
 * `<object>` (not `<iframe>`) because its `onError` fires when the browser has no
 * PDF handler at all, which lets us offer a download link instead of a blank box.
 * A keyed remount on `src` is what makes a recompile visible: the same-URL
 * document would otherwise be served from the in-page cache, which is why the URL
 * carries a version counter.
 */
import { FileDown, FileWarning } from 'lucide-react'
import { i18nT } from '../../i18n/t'

export interface PdfPreviewProps {
  /** Versioned PDF URL, or null when the project has not been compiled yet. */
  src: string | null
  /** Filename offered by the download affordance. */
  downloadName: string
}

export default function PdfPreview({ src, downloadName }: PdfPreviewProps) {
  if (!src) {
    return (
      <div
        className="flex-1 min-h-0 flex flex-col items-center justify-center gap-2 text-muted"
        data-testid="papyrus-pdf-empty"
      >
        <FileWarning className="lucide-inline opacity-50" />
        <div className="text-[13px]">{i18nT('apps.papyrus.preview.compile_to_see_the_pdf')}</div>
      </div>
    )
  }

  return (
    <div className="flex-1 min-h-0 bg-bg-subtle" data-testid="papyrus-pdf">
      <object
        key={src}
        data={src}
        type="application/pdf"
        className="w-full h-full"
        aria-label={i18nT('apps.papyrus.preview.pdf_preview')}
      >
        {/* Fallback content: rendered only when the browser cannot display a PDF
            inline. Offer the file instead of a blank pane. */}
        <div className="h-full flex flex-col items-center justify-center gap-3 text-muted px-6 text-center">
          <FileWarning className="lucide-inline opacity-50" />
          <div className="text-[13px]">
            {i18nT('apps.papyrus.preview.this_browser_cannot_display_a_pdf_inline')}
          </div>
          <a
            href={src}
            download={downloadName}
            className="inline-flex items-center gap-1.5 text-[13px] text-accent hover:underline"
          >
            <FileDown className="lucide-inline" />
            {i18nT('apps.papyrus.preview.download_pdf')}
          </a>
        </div>
      </object>
    </div>
  )
}
