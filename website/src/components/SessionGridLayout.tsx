import { Fragment, useEffect, useRef, type ReactNode } from 'react'
import { usePointerDrag } from '../hooks/usePointerDrag'
import type { GridNode, GridLeaf, GridSplit } from '../hooks/useSessionGrid'

const DIVIDER = 6 // px — draggable separator thickness between sibling panes

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
}: {
  node: GridNode
  renderLeaf: (leaf: GridLeaf) => ReactNode
  onResize: (splitId: string, index: number, deltaFrac: number) => void
}) {
  if (node.type === 'leaf') {
    return <div className="h-full w-full min-w-0 min-h-0 overflow-hidden">{renderLeaf(node)}</div>
  }
  return <SplitContainer node={node} renderLeaf={renderLeaf} onResize={onResize} />
}

function SplitContainer({
  node,
  renderLeaf,
  onResize,
}: {
  node: GridSplit
  renderLeaf: (leaf: GridLeaf) => ReactNode
  onResize: (splitId: string, index: number, deltaFrac: number) => void
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
  // the drag glued to the handle even over iframe/canvas children, so the old
  // full-screen overlay is no longer needed.
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
            <SessionGridLayout node={child} renderLeaf={renderLeaf} onResize={onResize} />
          </div>
          {i < node.children.length - 1 && (
            <div
              {...gridResize}
              data-divider-index={i}
              onPointerDown={(e) => { e.stopPropagation(); gridResize.onPointerDown(e) }}
              className={`shrink-0 flex items-center justify-center group/div ${horizontal ? 'cursor-col-resize' : 'cursor-row-resize'}`}
              style={{ ...(horizontal ? { width: DIVIDER } : { height: DIVIDER }), touchAction: 'none' }}
              role="separator"
              aria-orientation={horizontal ? 'vertical' : 'horizontal'}
            >
              <div
                className={`bg-border group-hover/div:bg-accent transition-colors rounded-full ${horizontal ? 'w-[2px] h-full' : 'h-[2px] w-full'}`}
              />
            </div>
          )}
        </Fragment>
      ))}
    </div>
  )
}
