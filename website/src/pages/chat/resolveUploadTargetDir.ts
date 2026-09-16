/**
 * Resolves an OS-file drop's destination directory from the actual DOM node
 * the browser reports as the drop target: drop onto a directory row writes
 * into that directory, drop onto a file row
 * writes into its parent, and a drop that lands on neither (the header, an
 * empty stretch of the tree, a row `@pierre/trees` has not rendered attributes
 * for) falls back to the tree's own root.
 *
 * `@pierre/trees` renders every row's relative path, kind, and parent path as
 * plain DOM attributes (`data-item-path` / `data-item-type` /
 * `data-item-parent-path` — see its `rowAttributes.ts`), so this walks up from
 * the native event's target with `closest()` rather than needing Pierre's own
 * model or a React prop drilled down to each row: the FULL-PANE drop listener
 * can sit on an ANCESTOR far above any specific row (the whole rail) and still
 * resolve the precise row a browser `drop` event landed on, because the
 * native event bubbles with its original `target` intact.
 *
 * Pure and DOM-only on purpose: no React, no Pierre types, so it is testable
 * with plain constructed elements and reusable from any listener that has an
 * `EventTarget` and the tree's absolute root.
 */
export function resolveUploadTargetDir(root: string, eventTarget: EventTarget | null): string {
  const rowEl = eventTarget instanceof Element ? eventTarget.closest('[data-item-path]') : null
  if (!rowEl) return root
  const relPath = rowEl.getAttribute('data-item-path') ?? ''
  const isDir = rowEl.getAttribute('data-item-type') === 'folder'
  const parentRelPath = rowEl.getAttribute('data-item-parent-path') ?? ''
  const dirRelPath = isDir ? relPath : parentRelPath
  return dirRelPath ? `${root}/${dirRelPath}` : root
}
