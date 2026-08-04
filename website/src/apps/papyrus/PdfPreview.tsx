/**
 * PdfPreview — the rendered-PDF pane.
 *
 * Uses the BROWSER's own PDF viewer (in an `<iframe>`; see below) rather than
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
 * dashboard's origin. That per-response header is the containment; it does not
 * depend on which element embeds the document.
 *
 * `<iframe>`, NOT `<object>`: the dashboard's own base CSP sets `object-src
 * 'none'`, so Chromium and Firefox refused to load the plugin document and the
 * pane rendered the "cannot display a PDF inline" fallback on the app's headline
 * feature. (It went unnoticed because WebKit does not enforce the directive for
 * this case, so it worked in Safari.) `frame-src 'self'` already permits a
 * same-origin frame, so this needs no CSP widening — the previous justification
 * for `<object>` cited an `onError` handler that was never actually wired up, so
 * nothing is lost: a browser with no PDF viewer still gets the download link
 * below the frame rather than as replaced content.
 *
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
    <div className="flex-1 min-h-0 flex flex-col bg-bg-subtle" data-testid="papyrus-pdf">
      <iframe
        key={src}
        src={src}
        className="flex-1 min-h-0 w-full border-0"
        title={i18nT('apps.papyrus.preview.pdf_preview')}
      />
      {/* An `<iframe>` has no replaced-content fallback, so the escape hatch is
          persistent chrome rather than something only a viewer-less browser sees.
          It is one thin row, and it also answers "how do I get the file out" —
          which the fallback could never do for the users who COULD see the PDF.
          The old "this browser cannot display a PDF inline" sentence is gone with
          the fallback: as always-visible chrome it would be a claim that is false
          for nearly every reader. */}
      <div className="flex items-center justify-center px-3 py-1.5 border-t border-border text-[12px]">
        <a
          href={src}
          download={downloadName}
          className="inline-flex items-center gap-1.5 text-accent hover:underline"
        >
          <FileDown className="lucide-inline" />
          {i18nT('apps.papyrus.preview.download_pdf')}
        </a>
      </div>
    </div>
  )
}
