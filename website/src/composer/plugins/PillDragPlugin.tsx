import { useEffect } from 'react'
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext'
import {
  $getNodeByKey,
  $getNearestNodeFromDOMNode,
  $getRoot,
  $isElementNode,
  $isTextNode,
  HISTORIC_TAG,
  type ElementNode,
  type LexicalNode,
} from 'lexical'

import { $createDropGapNode, $isDropGapNode, DropGapNode } from '../nodes/DropGapNode'
import { $isPasteBlockNode } from '../nodes/PasteBlockNode'

/**
 * Native HTML5 drag-and-drop reordering for paste pills, ported verbatim from
 * the spike (`~/.kiro/crew/workspace/ime-pill-spike/lexical.html`). Every bug
 * fix in that spike is load-bearing and reproduced here:
 *
 *  - `resolvePoint` does ALL DOM probing (`elementFromPoint`, `caretRangeFromPoint`)
 *    OUTSIDE `editor.read`, because those APIs can fire selection/mutation
 *    plumbing that needs an active editor; the Lexical-node mapping
 *    (`$getNearestNodeFromDOMNode`) then runs inside `editor.read`.
 *  - The insertion point is an ABSOLUTE unit index within the paragraph (text
 *    chars count 1 each, pills count 1, gaps count 0) so it is stable across
 *    text-node splits and gap presence, and doubles as the dedupe key.
 *  - The live "make room" gap is its own `DropGapNode` (Lexical owns its DOM —
 *    nothing foreign is injected into the contenteditable).
 *  - Transient gap insertion/removal updates carry `HISTORIC_TAG`, so they never
 *    become undo entries; the untagged drop update remains the user's undoable
 *    node move.
 *  - A rAF-throttled `dragover` captures the drag id when scheduling and BAILS
 *    if the drag already ended (Chrome fires the last dragover and drop within a
 *    few ms — the stranded-gap race).
 *  - `drop` resolves the point SYNCHRONOUSLY (the final gap may not exist yet)
 *    and moves the SAME node via `$insertAtUnits` — never `node.replace` on an
 *    attached decorator, which corrupts the reconciler.
 *  - Cleanup fires from the root `dragend`, document-level `dragend`/`drop`
 *    (drop outside the editor), a document-level `dragover` that decides
 *    "pointer left the editor" from the event target (NOT `dragleave.relatedTarget`,
 *    which Chrome leaves null), and a 900ms no-dragover watchdog armed on
 *    dragstart, every dragover, and every gap insertion.
 *
 * The dragged pill is marked by toggling a `dragging` class on its `.pill-host`
 * element directly — the chip's React `state` prop is React-owned, and replacing
 * the drag-source element mid-drag loses its `dragend`.
 */
/** Vertical clearance between the cursor (= the drop point) and the ghost. */
export const GHOST_CLEARANCE_PX = 22

/**
 * Private dataTransfer flavour carrying the dragged pill's Lexical node key.
 * Internal handle only (nothing in the repo reads it yet); the public
 * `text/plain` flavour carries the paste body — see `onDragStart`.
 */
export const PILL_KEY_MIME = 'application/x-kirocrew-pill-key'

/**
 * Replace the browser's default drag image — an opaque snapshot of the pill
 * glued to the cursor, which hides exactly the text you are trying to drop
 * into — with a smaller, translucent ghost that hangs BELOW the cursor. The
 * cursor tip and the live gap stay visible, so the drop point is never covered.
 *
 * The clearance is a transparent band inside the drag image (not a negative
 * setDragImage offset, which browsers clamp inconsistently).
 */
export function setGhostDragImage(dt: DataTransfer, host: HTMLElement): void {
  if (typeof dt.setDragImage !== 'function') return
  const chip = host.querySelector('[data-paste-seq]') as HTMLElement | null
  if (!chip) return
  const rect = chip.getBoundingClientRect()
  const wrap = document.createElement('div')
  wrap.setAttribute('data-pill-drag-ghost', '')
  // Parked off-screen (absolute, far left of the document) purely so the
  // browser can snapshot it for the drag image — it is never a visible
  // surface. The transparent top band is what keeps the clone below the
  // pointer.
  Object.assign(wrap.style, {
    position: 'absolute',
    left: '-10000px',
    top: '0',
    pointerEvents: 'none',
    paddingTop: `${GHOST_CLEARANCE_PX}px`,
    width: `${Math.ceil(rect.width)}px`,
  })
  const clone = chip.cloneNode(true) as HTMLElement
  clone.removeAttribute('data-paste-seq')
  Object.assign(clone.style, { opacity: '.72', transform: 'scale(.92)', transformOrigin: 'top', display: 'inline-flex' })
  wrap.appendChild(clone)
  document.body.appendChild(wrap)
  // Anchor the cursor at the top-centre of the transparent band: the visible
  // chip starts GHOST_CLEARANCE_PX below the pointer.
  dt.setDragImage(wrap, Math.round(rect.width / 2), 0)
  // The snapshot is taken synchronously by dragstart; the node can go next tick.
  setTimeout(() => wrap.remove(), 0)
}

export default function PillDragPlugin(): null {
  const [editor] = useLexicalComposerContext()

  useEffect(() => {
    const root = editor.getRootElement()
    if (!root) return

    // --- drag state (module-instance scoped to this effect) ------------------
    let dragKey: string | null = null // Lexical key of the pill being dragged
    let gapKey: string | null = null // key of the currently-open gap node
    let lastPosKey = '' // dedupe: only move when the resolved point changes
    let rafPending = false
    let dragWatchdog: ReturnType<typeof setTimeout> | null = null
    // Pending gap-node removals keyed by node key, so cleanup can flush them.
    const gapRemovalTimers = new Map<string, ReturnType<typeof setTimeout>>()

    type Target =
      | { self: true }
      | { self?: false; parentKey: string; index: number }
      | null

    const keyFromDom = (dom: Element): string | null =>
      editor.read(() => {
        const n = $getNearestNodeFromDOMNode(dom)
        return n ? n.getKey() : null
      })

    // Absolute unit offset within `parent` up to (but not including) `stopNode`,
    // plus `extra`. Text = its length, pill = 1, gap = 0.
    const unitsBefore = (parent: ElementNode, stopNode: LexicalNode, extra: number): number => {
      let acc = 0
      for (const c of parent.getChildren()) {
        if (c.is(stopNode)) return acc + extra
        if ($isDropGapNode(c)) continue
        acc += $isTextNode(c) ? c.getTextContentSize() : 1
      }
      return acc + extra
    }

    // Resolve a viewport point to an insertion target in Lexical terms.
    const resolvePoint = (x: number, y: number): Target => {
      // DOM probing OUTSIDE editor.read (see file header).
      const probed = document.elementFromPoint(x, y)
      const overPillHost =
        probed && typeof probed.closest === 'function'
          ? (probed.closest('.pill-host') as HTMLElement | null)
          : null
      let pillAfter = false
      if (overPillHost) {
        const r = overPillHost.getBoundingClientRect()
        pillAfter = x > r.left + r.width / 2
      }

      let range: Range | null = null
      if (!overPillHost) {
        range = document.caretRangeFromPoint ? document.caretRangeFromPoint(x, y) : null
        // caretPositionFromPoint fallback (Firefox / spec name).
        const docWithCPFP = document as Document & {
          caretPositionFromPoint?: (x: number, y: number) => { offsetNode: Node; offset: number } | null
        }
        if (!range && docWithCPFP.caretPositionFromPoint) {
          const p = docWithCPFP.caretPositionFromPoint(x, y)
          if (p) {
            range = document.createRange()
            range.setStart(p.offsetNode, p.offset)
          }
        }
        if (!range) return null
      }

      const sc = range ? range.startContainer : null
      const domForNode: Node | null = range
        ? sc && sc.nodeType === 3
          ? sc.parentNode
          : sc
        : overPillHost
      const isTextDom = !!range && !!sc && sc.nodeType === 3
      const domOffset = range ? range.startOffset : 0
      if (!domForNode) return null

      return editor.read(() => {
        const node = $getNearestNodeFromDOMNode(domForNode)
        if (!node) return null

        if (overPillHost) {
          if (!$isPasteBlockNode(node)) return null
          if (node.getKey() === dragKey) return { self: true } // hovering the dragged pill: "put it back"
          const parent = node.getParent()
          if (!parent) return null
          return { parentKey: parent.getKey(), index: unitsBefore(parent, node, pillAfter ? 1 : 0) }
        }
        if ($isDropGapNode(node)) return null // hovering the gap itself: keep
        if ($isPasteBlockNode(node)) return null
        if ($isTextNode(node)) {
          const size = node.getTextContentSize()
          const off = isTextDom ? Math.min(domOffset, size) : domOffset ? size : 0
          const parent = node.getParent()
          if (!parent) return null
          return { parentKey: parent.getKey(), index: unitsBefore(parent, node, off) }
        }
        if ($isElementNode(node)) {
          // paragraph / root: use child index
          const parent = node.getType() === 'root' ? node.getLastChild() : node
          if (!parent || !$isElementNode(parent)) return null
          const kids = parent.getChildren().filter(c => !$isDropGapNode(c))
          const idx = Math.min(domOffset, kids.length)
          let acc = 0
          for (let i = 0; i < idx; i++) acc += $isTextNode(kids[i]) ? kids[i].getTextContentSize() : 1
          return { parentKey: parent.getKey(), index: acc }
        }
        return null
      })
    }

    // Insert a node at an absolute unit offset inside a paragraph (splits text).
    const $insertAtUnits = (parent: ElementNode, index: number, nodeToInsert: LexicalNode) => {
      let acc = 0
      for (const c of parent.getChildren()) {
        if ($isDropGapNode(c)) continue
        const len = $isTextNode(c) ? c.getTextContentSize() : 1
        if (index <= acc) {
          c.insertBefore(nodeToInsert)
          return
        }
        if (index < acc + len && $isTextNode(c)) {
          const [left] = c.splitText(index - acc)
          left.insertAfter(nodeToInsert)
          return
        }
        acc += len
      }
      parent.append(nodeToInsert)
    }

    // Animate a gap shut, then remove the node after the CSS transition.
    const closeGap = (key: string | null) => {
      if (!key) return
      const host = editor.getElementByKey(key)
      const el = host && (host.firstElementChild as HTMLElement | null)
      if (el) el.classList.remove('wide')
      const existing = gapRemovalTimers.get(key)
      if (existing) clearTimeout(existing)
      const t = setTimeout(() => {
        gapRemovalTimers.delete(key)
        editor.update(
          () => {
            const n = $getNodeByKey(key)
            if (n) n.remove()
          },
          { discrete: true, tag: HISTORIC_TAG },
        )
      }, 190)
      gapRemovalTimers.set(key, t)
    }

    const clearDraggingClass = () => {
      root.querySelectorAll('.pill-host.dragging').forEach(el => el.classList.remove('dragging', 'opacity-40'))
    }

    // Watchdog: native DnD fires dragover continuously. If NO dragover reaches
    // the document for 900ms the drag is over and everything that should have
    // told us got lost -> clean up so a gap is never stranded.
    const armWatchdog = () => {
      if (dragWatchdog) clearTimeout(dragWatchdog)
      dragWatchdog = setTimeout(() => {
        if (dragKey || gapKey) endDrag()
      }, 900)
    }

    // One place that ends a drag, whatever path got us here. Idempotent.
    const endDrag = () => {
      if (dragWatchdog) {
        clearTimeout(dragWatchdog)
        dragWatchdog = null
      }
      if (gapKey) {
        closeGap(gapKey)
        gapKey = null
      }
      lastPosKey = ''
      dragKey = null
      clearDraggingClass()
    }

    // Open (or move) the gap for a resolved target. Runs inside a drag only.
    const applyTarget = (target: Target) => {
      if (!target) return
      if (target.self) {
        // over the dragged pill: no gap = "stays put"
        if (gapKey) {
          closeGap(gapKey)
          gapKey = null
        }
        lastPosKey = 'self'
        return
      }
      const posKey = target.parentKey + ':' + target.index
      if (posKey === lastPosKey) return // same spot: gap stays, no churn
      lastPosKey = posKey
      const oldGap = gapKey
      gapKey = null
      closeGap(oldGap) // old shrinks…
      editor.update(
        () => {
          // …while the new one opens: text flows aside
          const parent = $getNodeByKey(target.parentKey)
          if (!parent || !$isElementNode(parent)) return
          const gap = $createDropGapNode()
          gapKey = gap.getKey()
          $insertAtUnits(parent, target.index, gap)
        },
        { discrete: true, tag: HISTORIC_TAG },
      )
      armWatchdog() // a gap exists -> the safety net is armed no matter what
    }

    // --- listeners -----------------------------------------------------------
    const onDragStart = (e: DragEvent) => {
      const target = e.target as Element | null
      const host = target && typeof target.closest === 'function'
        ? (target.closest('.pill-host') as HTMLElement | null)
        : null
      if (!host) return
      dragKey = keyFromDom(host)
      // `dragging` is the marker the plugin and tests select on; the opacity
      // utility is the visible dim (the chip's own state prop never sees a drag).
      host.classList.add('dragging', 'opacity-40')
      if (e.dataTransfer) {
        e.dataTransfer.effectAllowed = 'move'
        // Nothing in this plugin reads dataTransfer back — `onDrop` resolves the
        // target from the pointer via `resolvePoint` — so the ONLY consumer of
        // these flavours is a drop OUTSIDE this editor (another composer, a
        // search box, an external app), and those read `text/plain` alone.
        // Give them the paste BODY: that is exactly what copy/cut hand out for
        // a pill (`expandedSelectionText` in LexicalComposerInput expands each
        // PasteBlockNode to `getBlock().content`), so drag matches copy. Not
        // the `[ Paste #N · … ]` token — that marker only means something to
        // the composer that owns the sidecar. The Lexical key is an internal
        // handle: keep it on a private type so a public reader never sees it.
        // `editor.read` here is a plain read outside any update.
        const body = dragKey
          ? editor.read(() => {
              const n = $getNodeByKey(dragKey!)
              return $isPasteBlockNode(n) ? n.getContent() : null
            })
          : null
        e.dataTransfer.setData('text/plain', body ?? '')
        if (dragKey) e.dataTransfer.setData(PILL_KEY_MIME, dragKey)
        setGhostDragImage(e.dataTransfer, host)
      }
      armWatchdog()
    }

    const onDragOver = (e: DragEvent) => {
      if (!dragKey) return
      e.preventDefault()
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'move'
      if (rafPending) return
      rafPending = true
      const cx = e.clientX
      const cy = e.clientY
      const dk = dragKey
      requestAnimationFrame(() => {
        rafPending = false
        // RACE GUARD: if drop/dragend already ended THIS drag, drop this deferred
        // work — otherwise it inserts a gap into a drag that no longer exists.
        if (dragKey !== dk) return
        const target = resolvePoint(cx, cy)
        applyTarget(target)
      })
    }

    const onDrop = (e: DragEvent) => {
      e.preventDefault()
      if (!dragKey) return
      const dk = dragKey
      // Fast drop: the gap for the final position may not exist yet (its dragover
      // work is still queued in rAF). Resolve SYNCHRONOUSLY so a quick drag lands.
      const target = resolvePoint(e.clientX, e.clientY)
      const gk = gapKey
      if (dragWatchdog) {
        clearTimeout(dragWatchdog)
        dragWatchdog = null
      }
      gapKey = null
      lastPosKey = ''
      dragKey = null
      editor.update(
        () => {
          const dragged = $getNodeByKey(dk)
          const gap = gk ? $getNodeByKey(gk) : null
          if (dragged) {
            if (target && !target.self) {
              const parent = $getNodeByKey(target.parentKey)
              if (parent && $isElementNode(parent)) {
                // account for the dragged pill itself sitting before the target
                // in this paragraph
                let idx = target.index
                const draggedParent = dragged.getParent()
                if (draggedParent && draggedParent.is(parent)) {
                  let acc = 0
                  for (const c of parent.getChildren()) {
                    if (c.is(dragged)) break
                    if (!$isDropGapNode(c)) acc += $isTextNode(c) ? c.getTextContentSize() : 1
                  }
                  if (acc < target.index) idx -= 1
                }
                dragged.remove()
                $insertAtUnits(parent, idx, dragged)
              }
            } else if (gap && gap.isAttached() && !target) {
              // no resolvable point (e.g. dropped on the gap itself): use the gap
              gap.insertBefore(dragged)
            }
          }
          // sweep every gap (open or still closing); snapshot first
          const stray: DropGapNode[] = []
          for (const p of $getRoot().getChildren()) {
            if ($isElementNode(p)) {
              for (const c of p.getChildren()) if ($isDropGapNode(c)) stray.push(c)
            }
          }
          stray.forEach(c => c.remove())
        },
        { discrete: true },
      )
      // flush any pending gap-removal timers — the sweep above already removed them
      for (const t of gapRemovalTimers.values()) clearTimeout(t)
      gapRemovalTimers.clear()
      clearDraggingClass()
    }

    const onRootDragEnd = () => {
      if (dragKey || gapKey) endDrag()
    }

    // Document-level cleanup paths.
    const onDocDragEnd = () => {
      if (dragKey || gapKey) endDrag()
    }
    const onDocDrop = (e: DragEvent) => {
      const t = e.target as Node | null
      if (dragKey && (!t || !root.contains(t))) endDrag()
    }
    // Pointer left the editor: close the gap. Decided from the event target being
    // outside the editor, NOT dragleave.relatedTarget (Chrome leaves it null).
    const onDocDragOver = (e: DragEvent) => {
      if (!dragKey) return
      armWatchdog()
      const t = e.target as Node | null
      if (gapKey && (!t || !root.contains(t))) {
        closeGap(gapKey)
        gapKey = null
        lastPosKey = ''
      }
    }

    root.addEventListener('dragstart', onDragStart)
    root.addEventListener('dragover', onDragOver)
    root.addEventListener('drop', onDrop)
    root.addEventListener('dragend', onRootDragEnd)
    document.addEventListener('dragend', onDocDragEnd, true)
    document.addEventListener('drop', onDocDrop, true)
    document.addEventListener('dragover', onDocDragOver, true)

    return () => {
      root.removeEventListener('dragstart', onDragStart)
      root.removeEventListener('dragover', onDragOver)
      root.removeEventListener('drop', onDrop)
      root.removeEventListener('dragend', onRootDragEnd)
      document.removeEventListener('dragend', onDocDragEnd, true)
      document.removeEventListener('drop', onDocDrop, true)
      document.removeEventListener('dragover', onDocDragOver, true)
      if (dragWatchdog) clearTimeout(dragWatchdog)
      for (const t of gapRemovalTimers.values()) clearTimeout(t)
      gapRemovalTimers.clear()
    }
  }, [editor])

  return null
}
