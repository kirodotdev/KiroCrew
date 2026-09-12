import { Fragment, useEffect, useRef, type ReactNode } from 'react'
import { usePointerDrag } from '../hooks/usePointerDrag'
import type { GridNode, GridLeaf, GridSplit } from '../hooks/useSessionGrid'

/**
 * SessionGridLayout — recursive "terminal split" renderer.
 *
 * Walks the split tree from useSessionGrid: a leaf renders via `renderLeaf`; a
 * split tiles its children along one axis (dir 'col' → left→right with vertical
 * dividers, dir 'row' → top→bottom with horizontal dividers) using flex-grow
 * ratios from the node's `sizes`. Each split owns its dividers, so dragging one
 * resizes only that split's two adjacent children (per-node resize, tmux style).
 * Layout-only: it never knows what a leaf contains.
 */
export default function SessionGridLayout({
  node,
  renderLeaf,
  onResize,
  ownsTopRight = true,
}: {
  node: GridNode
  renderLeaf: (leaf: GridLeaf, ownsTopRight: boolean) => ReactNode
  onResize: (splitId: string, index: number, deltaFrac: number) => void
  /** Whether this subtree reaches the workspace's top-right corner. Exactly one
   *  leaf inherits it, so only that pane reserves the App-owned panel toggles. */
  ownsTopRight?: boolean
}) {
  if (node.type === 'leaf') {
    return <div className="h-full w-full min-w-0 min-h-0 overflow-hidden">{renderLeaf(node, ownsTopRight)}</div>
  }
  return <SplitContainer node={node} renderLeaf={renderLeaf} onResize={onResize} ownsTopRight={ownsTopRight} />
}

function SplitContainer({
  node,
  renderLeaf,
  onResize,
  ownsTopRight,
}: {
  node: GridSplit
  renderLeaf: (leaf: GridLeaf, ownsTopRight: boolean) => ReactNode
  onResize: (splitId: string, index: number, deltaFrac: number) => void
  ownsTopRight: boolean
}) {
  const ref = useRef<HTMLDivElement>(null)
  // Teardown for an in-progress divider drag, invoked on unmount so closing a
  // pane mid-drag still removes the overlay + window listeners (no leak).
  useEffect(() => () => { document.body.style.cursor = '' }, [])

  const horizontal = node.dir === 'col' // children flow left→right

  // Per-divider resize via Pointer Events (mouse + touch + pen). One hook
  // instance serves every divider in this split: pointerdown records which
  // divider (data-divider-index) plus the split's extent and start position;
  // onMove applies the fractional delta to that divider. setPointerCapture keeps
  // the drag glued to the handle even over iframe/canvas children, so no
  // full-screen overlay is needed.
  const dragState = useRef<{ index: number; extent: number; last: number } | null>(null)
  const gridResize = usePointerDrag({
    threshold: 0,
    onStart: (e) => {
      const el = ref.current
      if (!el) { dragState.current = null; return }
      const rect = el.getBoundingClientRect()
      const extent = horizontal ? rect.width : rect.height
      if (extent <= 0) { dragState.current = null; return }
      const index = Number((e.currentTarget as HTMLElement).dataset.dividerIndex)
      dragState.current = { index, extent, last: horizontal ? e.clientX : e.clientY }
      document.body.style.cursor = horizontal ? 'col-resize' : 'row-resize'
    },
    onMove: ({ x, y }) => {
      const st = dragState.current
      if (!st) return
      const pos = horizontal ? x : y
      const d = (pos - st.last) / st.extent
      if (d !== 0) { onResize(node.id, st.index, d); st.last = pos }
    },
    onEnd: () => {
      dragState.current = null
      document.body.style.cursor = ''
    },
  })

  return (
    <div
      ref={ref}
      className="flex h-full w-full min-w-0 min-h-0"
      style={{ flexDirection: horizontal ? 'row' : 'column' }}
    >
      {node.children.map((child, i) => (
        <Fragment key={child.id}>
          <div
            className="min-w-0 min-h-0 overflow-hidden"
            style={{ flexGrow: node.sizes[i] ?? 1, flexBasis: 0, flexShrink: 1 }}
          >
            <SessionGridLayout
              node={child}
              renderLeaf={renderLeaf}
              onResize={onResize}
              ownsTopRight={ownsTopRight && (horizontal ? i === node.children.length - 1 : i === 0)}
            />
          </div>
          {i < node.children.length - 1 && (
            <div
              {...gridResize}
              data-divider-index={i}
              onPointerDown={(e) => { e.stopPropagation(); gridResize.onPointerDown(e) }}
              className={`relative z-10 shrink-0 bg-border hover:bg-accent transition-colors ${horizontal ? 'w-px cursor-col-resize' : 'h-px cursor-row-resize'}`}
              style={{ touchAction: 'none' }}
              role="separator"
              aria-orientation={horizontal ? 'vertical' : 'horizontal'}
            >
              {/* Keep a forgiving hit target without making the divider occupy
                  more than its one visible pixel in layout. */}
              <span aria-hidden="true" className={`absolute ${horizontal ? 'inset-y-0 left-1/2 w-[7px] -translate-x-1/2' : 'inset-x-0 top-1/2 h-[7px] -translate-y-1/2'}`} />
            </div>
          )}
        </Fragment>
      ))}
    </div>
  )
}
