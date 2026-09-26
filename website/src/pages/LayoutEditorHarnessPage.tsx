/**
 * Standalone dev harness for the layout editor (RFC §7, PR 2 — core pass).
 *
 * Desktop-only dev scaffold, deliberately: this is NOT a shipped surface and is
 * not linked from any nav — a route developers open to exercise the editor in
 * isolation (drag palette tiles onto the grid, move/remove panes, resize via the
 * steppers). The editor's real home is a MODAL on the Crew Members page (RFC §7
 * PR 4); this whole route is throwaway scaffolding deleted when that lands, which
 * is why it does not carry the standard PageHeader/Card page shell or a
 * narrow-viewport layout. It holds a `GridSpec` in local state and renders the
 * editor over it; it depends ONLY on the merged PR 1 model and the editor, and
 * touches nothing on the render path or the Crew Members page.
 */
import { useState } from 'react'
import LayoutEditor from '../components/crew/layout/LayoutEditor'
import type { GridSpec } from '../components/crew/layout/grid'
import { PageHeader, Btn } from '../components/ui'
import { i18nT } from '../i18n/t'

/** A small starter arrangement so the harness opens with something to grab:
 *  a 2×2 grid with chat spanning the left column and a side panel top-right. */
const SEED_SPEC: GridSpec = {
  cols: 2,
  rows: 2,
  colSizes: [3, 2],
  items: [
    { id: 'seed-chat', element: 'chat', x: 0, y: 0, w: 1, h: 2 },
    { id: 'seed-side', element: 'sidePanel', x: 1, y: 0, w: 1, h: 1 },
  ],
}

export default function LayoutEditorHarnessPage() {
  const [spec, setSpec] = useState<GridSpec>(SEED_SPEC)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0, background: 'var(--bg)' }}>
      <PageHeader
        title={i18nT('pages.layoutEditorHarness.title')}
        actions={<Btn onClick={() => setSpec(SEED_SPEC)}>{i18nT('pages.layoutEditorHarness.reset')}</Btn>}
      />
      <div style={{ flex: 1, minWidth: 0, minHeight: 0 }}>
        <LayoutEditor spec={spec} onChange={setSpec} />
      </div>
    </div>
  )
}
