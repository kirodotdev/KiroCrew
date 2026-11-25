/* The canvas's entry points outside its own folder, each in its own chunk.
   The editor, the model and the JSON Canvas reader load only when a canvas is
   shown, so a page that never draws one does not pay for them in its first
   load. A chunk that fails to load shows its failure where the canvas would
   have been, with Try Again and Reload page, so the page around it stays usable.
   `canvasPending.ts` stays a static import: the page's controls call it on
   every artifact, and it imports nothing. */
import { lazyRetry } from '../lazyRetry'
import { i18nT } from '../../i18n/t'

const title = () => i18nT('components.canvas.load_failed')

// No hand-off: the canvas's unsaved edits live in the editor this replaces.
export const CanvasArtifactBody = lazyRetry(() => import('./CanvasArtifactBody'), {
  layout: 'block',
  title,
  scope: 'canvas-body',
  askAgent: false,
})

export const CanvasPanelView = lazyRetry(() => import('./CanvasPanelView'), {
  layout: 'block',
  title,
  scope: 'canvas-panel',
})

export const CanvasThumb = lazyRetry(() => import('./CanvasThumb'), {
  layout: 'inline',
  title,
  scope: 'canvas-thumb',
})

/** A new, empty canvas document, loaded with the model only when one is made. */
export async function emptyCanvasDocument(): Promise<string> {
  const { emptyCanvas, serializeCanvas } = await import('./canvasModel')
  return serializeCanvas(emptyCanvas())
}
