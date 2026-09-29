/**
 * Route a file-tree row dropped on the composer to the host's "Add to chat"
 * entry point, the same one the tree's context menu calls. That handler owns
 * the mention itself (the `@path` / `@folder/` token the `@` picker inserts,
 * its place (the drop point, or the caret), keeping clear of existing tokens, dedupe and
 * staging), so a drop is only a second way to reach it.
 */
import { useCallback, useEffect, useRef, useState, type DragEvent as ReactDragEvent, type RefObject } from 'react'
import { classifyComposerDrop, readTreeEntry, type TreeEntryDragKind } from '../lib/treeEntryDrag'
import { isWindowsShapedPath, normalizeWindowsPath } from '../utils/fileTokens'
import type { ComposerControl, ComposerDropTarget } from './composerControl'

/** Is `path` strictly below the project root? Both sides are compared in the
 *  normalized form the tree hands out. An empty, `.` or `..` segment never
 *  qualifies (`/repo//etc` would relativize to the absolute `/etc`); on a
 *  Windows-shaped project neither does a segment made only of dots and
 *  spaces, since Win32 trims trailing dots and spaces and reads `.. ` or
 *  `...` as `..`. */
export function isInsideProject(path: string, project: string): boolean {
  const root = normalizeWindowsPath(project).replace(/\/+$/, '')
  const p = normalizeWindowsPath(path)
  if (!root || !p.startsWith(`${root}/`)) return false
  const win = isWindowsShapedPath(project)
  return p.slice(root.length + 1).split('/').every(seg =>
    seg !== '' && seg !== '.' && seg !== '..' && !(win && /^[. ]+$/.test(seg)))
}

type DragHandler = (event: ReactDragEvent) => void

export type TreeDropState = 'idle' | 'accept' | 'refuse'

/**
 * Route drags over the composer. A file-tree row is taken here and handed to
 * `onTreeEntryDrop`; a row the composer cannot mention (a folder whose path
 * has whitespace, which a folder reference cannot carry) shows the browser's
 * no-drop cursor instead; every other drag (OS files, selected text) goes to
 * the host's existing handlers untouched. `state` says what the drop
 * indicator shows while a row is over the composer: `accept` for a row the
 * drop will take, `refuse` for one it will not, `idle` otherwise.
 *
 * Focus moves into the composer on the next frame, after the host's update
 * has reached the editor, so the editor's own sync cannot overwrite it.
 */
export function useComposerTreeDrop({
  enabled,
  project,
  onTreeEntryDrop,
  clampDropOffset,
  getControl,
  containerRef,
  onDragOver,
  onDragLeave,
  onDrop,
}: {
  enabled: boolean
  project: string
  /** The host's "Add to chat" handler: absolute path, entry kind, and the
   *  text offset under the drop point (null: use the caret). */
  onTreeEntryDrop?: (absPath: string, kind: TreeEntryDragKind, at?: number | null) => void
  /** The host's clamp, so the preview caret shows the offset it will use. */
  clampDropOffset?: (text: string, at: number) => number
  getControl: () => ComposerControl | null
  containerRef: RefObject<HTMLElement | null>
  onDragOver?: DragHandler
  onDragLeave?: DragHandler
  onDrop?: DragHandler
}): {
  state: TreeDropState
  /** Viewport box of the insertion caret a release would land at, while an
   *  accepted row is over a composer that can measure it. */
  caret: ComposerDropTarget['caret'] | null
  onDragOver: DragHandler
  onDragLeave: DragHandler
  onDrop: DragHandler
} {
  const [state, setState] = useState<TreeDropState>('idle')
  const [caret, setCaret] = useState<ComposerDropTarget['caret'] | null>(null)
  // dragover fires every few ms; measure at most once a frame.
  const pendingPoint = useRef<{ x: number; y: number } | null>(null)
  const frame = useRef<number | null>(null)
  const cancelFrame = useCallback(() => {
    if (frame.current != null) cancelAnimationFrame(frame.current)
    frame.current = null
    pendingPoint.current = null
  }, [])
  useEffect(() => cancelFrame, [cancelFrame])
  useEffect(() => {
    if (state !== 'accept') { cancelFrame(); setCaret(null) }
  }, [state, cancelFrame])
  const active = state !== 'idle'
  const accepts = enabled && !!onTreeEntryDrop

  // A drag cancelled with Escape, or dropped elsewhere, sends no dragleave to
  // the composer; clear the indicator on the window-level end of the drag.
  useEffect(() => {
    if (!active) return
    const reset = () => setState('idle')
    window.addEventListener('dragend', reset, true)
    window.addEventListener('drop', reset, true)
    return () => {
      window.removeEventListener('dragend', reset, true)
      window.removeEventListener('drop', reset, true)
    }
  }, [active])

  const handleDragOver = useCallback((event: ReactDragEvent) => {
    const kind = accepts ? classifyComposerDrop(event.dataTransfer) : 'none'
    if (kind !== 'tree-entry' && kind !== 'tree-refused') {
      onDragOver?.(event)
      return
    }
    event.preventDefault()
    event.stopPropagation()
    event.dataTransfer.dropEffect = kind === 'tree-entry' ? 'copy' : 'none'
    setState(kind === 'tree-entry' ? 'accept' : 'refuse')
    if (kind !== 'tree-entry') return
    pendingPoint.current = { x: event.clientX, y: event.clientY }
    if (frame.current != null) return
    frame.current = requestAnimationFrame(() => {
      frame.current = null
      const point = pendingPoint.current
      pendingPoint.current = null
      if (!point) return
      setCaret(getControl()?.dropTargetAtPoint?.(point.x, point.y, clampDropOffset)?.caret ?? null)
    })
  }, [accepts, clampDropOffset, getControl, onDragOver])

  const handleDragLeave = useCallback((event: ReactDragEvent) => {
    const kind = accepts ? classifyComposerDrop(event.dataTransfer) : 'none'
    if (kind !== 'tree-entry' && kind !== 'tree-refused') {
      onDragLeave?.(event)
      return
    }
    event.stopPropagation()
    const next = event.relatedTarget
    if (next instanceof Node && containerRef.current?.contains(next)) return
    setState('idle')
  }, [accepts, containerRef, onDragLeave])

  const handleDrop = useCallback((event: ReactDragEvent) => {
    const kind = accepts ? classifyComposerDrop(event.dataTransfer) : 'none'
    if (kind !== 'tree-entry' && kind !== 'tree-refused') {
      onDrop?.(event)
      return
    }
    event.preventDefault()
    event.stopPropagation()
    setState('idle')
    if (kind !== 'tree-entry' || !onTreeEntryDrop) return
    const entry = readTreeEntry(event.dataTransfer)
    if (!entry) return
    // Only rows of this project's tree: a payload naming a path outside it
    // (another page can set any drag type) would stage an arbitrary file.
    if (!isInsideProject(entry.path, project)) return
    // Land the mention where the row was let go, not at the old caret. The
    // host still clamps this out of any token it falls inside.
    const at = getControl()?.dropTargetAtPoint?.(event.clientX, event.clientY, clampDropOffset)?.offset ?? null
    onTreeEntryDrop(entry.path, entry.kind, at)
    requestAnimationFrame(() => getControl()?.focus())
  }, [accepts, clampDropOffset, getControl, onDrop, onTreeEntryDrop, project])

  return { state, caret, onDragOver: handleDragOver, onDragLeave: handleDragLeave, onDrop: handleDrop }
}
