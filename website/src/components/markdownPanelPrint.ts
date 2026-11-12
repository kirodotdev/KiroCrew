import { downloadFileName } from '../utils/fileReadUrl'

/** Marks the preview being printed; `sidebar-and-side-panel.css` keys on both. */
const PRINT_ROOT_ATTR = 'data-print-root'
const PRINTING_ATTR = 'data-printing-preview'
/** How long the dialog waits on images still loading: a stalled one prints as
 *  its placeholder instead of leaving the row looking dead. */
const IMAGE_WAIT_MS = 3000

/**
 * The ⋯ menu's "Print or save as PDF": print the rendered markdown preview
 * alone through the browser's print dialog, which also saves it as a PDF.
 *
 * The live preview prints, not a copy: highlighted code blocks draw into
 * shadow roots and diagrams are already laid out, and a cloned node carries
 * neither. A partial read is named like its download, so the PDF never passes
 * for the whole file.
 */
export async function printPreview(root: HTMLElement, filePath: string, partial: boolean): Promise<void> {
  // An image the reader never scrolled to is still lazy, and would print blank.
  const images = Array.from(root.querySelectorAll('img'))
  for (const img of images) img.loading = 'eager'
  const loaded = Promise.all(images.map(img => img.decode().catch(() => {})))
  await Promise.race([loaded, new Promise(resolve => setTimeout(resolve, IMAGE_WAIT_MS))])
  // One frame for React to swap each loaded image's placeholder out.
  await new Promise(resolve => requestAnimationFrame(resolve))
  // Full screen swaps the preview out during the waits; marking a detached
  // root would print a blank page.
  if (!root.isConnected) return
  const html = document.documentElement
  const title = document.title
  // Drop the extension the way partialDownloadName splits it (a leading or
  // trailing dot is not one), then mark a partial read, so `README` stays
  // `README.partial`.
  const name = downloadFileName(filePath)
  const dot = name.lastIndexOf('.')
  const stem = dot > 0 && dot < name.length - 1 ? name.slice(0, dot) : name
  // Authored content on any surface (an SVG artifact in a hidden tab) could
  // carry the mark too; the stylesheet keeps every element that has it, so
  // only this preview may.
  for (const el of document.querySelectorAll(`[${PRINT_ROOT_ATTR}]`)) {
    if (el !== root) el.removeAttribute(PRINT_ROOT_ATTR)
  }
  root.setAttribute(PRINT_ROOT_ATTR, '')
  html.setAttribute(PRINTING_ATTR, '')
  // The dialog names the saved PDF after the page title.
  document.title = partial ? `${stem}.partial` : stem
  window.addEventListener('afterprint', () => {
    root.removeAttribute(PRINT_ROOT_ATTR)
    html.removeAttribute(PRINTING_ATTR)
    document.title = title
  }, { once: true })
  window.print()
}
