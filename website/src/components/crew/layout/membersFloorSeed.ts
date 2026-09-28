/**
 * The floor-layout seed — the Members THREAD AREA expressed as a `LayoutTree`
 * (RFC §4 integration, decision 1).
 *
 * The roster rail is PERMANENT CHROME (it is how a member is selected), so it is
 * never part of a custom layout (RFC decision 2). The layout governs only the
 * area beside it: the chat pane (reader of the selected member's slot) and the
 * session side panel (reader). A one-row `grid` of two cells, left→right.
 *
 * A factory, not a const: callers hold it in mutable editor state, so each call
 * returns a fresh object rather than a shared reference. Pure data — no React,
 * no DOM — so the round-trip test can exercise the conversion against it.
 */
import { LAYOUT_VERSION, type LayoutTree } from './layoutTree'

export function makeMembersFloorSeed(): LayoutTree {
  return {
    version: LAYOUT_VERSION,
    root: {
      kind: 'grid',
      id: 'root',
      cols: 2,
      rows: 1,
      colSizes: [3, 2], // chat wider, panel narrower
      children: [
        {
          x: 0,
          y: 0,
          w: 1,
          h: 1,
          node: {
            kind: 'cell',
            id: 'thread',
            element: 'chat',
            config: { agentLocked: true, frameless: true, followContentWidth: true, busyMode: 'steer-only' },
          },
        },
        { x: 1, y: 0, w: 1, h: 1, node: { kind: 'cell', id: 'panel', element: 'sidePanel' } },
      ],
    },
  }
}
