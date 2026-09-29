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
import { overlaps } from '../components/crew/layout/grid'
import { isKnownElement } from '../components/crew/layout/layoutTree'
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

/** Dev-only seed override: `?seed=<url-encoded GridSpec JSON>` opens the harness
 *  over an arbitrary starting grid instead of SEED_SPEC. It exists so a specific
 *  editor state (e.g. a wide pane that cannot fit anywhere else, which produces
 *  the invalid "Doesn't fit" drop preview) is reproducible for screenshot
 *  capture without shipping resize grips. Throwaway with the harness (RFC §7 PR
 *  4); any seed that is malformed OR out of the editor's own bounds silently
 *  falls back to SEED_SPEC. */
const SEED_MAX_DIM = 6 // mirrors LayoutEditor's MAX_DIM — a seed cannot exceed what the editor allows

/** A positive integer in 1..max. */
function isDim(v: unknown, max: number): v is number {
  return typeof v === 'number' && Number.isInteger(v) && v >= 1 && v <= max
}
/** A non-negative integer < bound (a cell coordinate). */
function isCoord(v: unknown, bound: number): v is number {
  return typeof v === 'number' && Number.isInteger(v) && v >= 0 && v < bound
}
/** An optional track-size array: absent, or exactly `len` finite positive numbers. */
function isTrackSizes(v: unknown, len: number): boolean {
  if (v === undefined) return true
  return Array.isArray(v) && v.length === len && v.every((n) => typeof n === 'number' && Number.isFinite(n) && n > 0)
}

/** Exhaustively reject anything that would crash, render out of bounds, or reach
 *  an unchecked lookup. EVERY field a `GridSpec`/`GridItem` can carry is checked
 *  or rejected here — not just the ones that happen to crash today — so a seed
 *  that validates is one the editor can render safely:
 *   - `cols`/`rows`: positive integers within SEED_MAX_DIM;
 *   - `colSizes`/`rowSizes` (optional): finite-positive number arrays of the
 *     matching length, or absent (they feed CSS `fr` tracks);
 *   - each item's `element`: a KNOWN leaf element (guards `ELEMENT_META[…]`);
 *   - each item's `id`: a non-empty string, unique across items (a shared or
 *     empty id makes `removeItemById` delete every match when one closes);
 *   - each item's `x/y/w/h`: integers placing it fully inside the grid;
 *   - items must not overlap each other (a seed bypasses the editor's own
 *     placement guard, which only fires on interactive moves);
 *   - the container fields (`tabs`/`grid`/`activeTab`/`config`) are rejected:
 *     the harness seed is leaves only, and allowing a nested `grid`/`tabs`
 *     would reopen the same unchecked recursive lookups this guard closes. */
function isValidSeed(s: unknown): s is GridSpec {
  if (!s || typeof s !== 'object') return false
  const spec = s as Record<string, unknown>
  if (!isDim(spec.cols, SEED_MAX_DIM) || !isDim(spec.rows, SEED_MAX_DIM)) return false
  const { cols, rows } = spec as { cols: number; rows: number }
  if (!isTrackSizes(spec.colSizes, cols) || !isTrackSizes(spec.rowSizes, rows)) return false
  if (!Array.isArray(spec.items)) return false
  const rects: { x: number; y: number; w: number; h: number }[] = []
  const seenIds = new Set<string>()
  for (const it of spec.items as unknown[]) {
    if (!it || typeof it !== 'object') return false
    const item = it as Record<string, unknown>
    // `id` must be a NON-EMPTY string, UNIQUE across items: `removeItemById`
    // filters EVERY match (grid.ts), so two panes sharing an id — or two empty
    // ids, which collide — both vanish when either is closed.
    if (typeof item.id !== 'string' || item.id === '' || seenIds.has(item.id)) return false
    seenIds.add(item.id)
    // `element` must be a KNOWN leaf element — an unknown string would pass a
    // bare typeof check and then crash `ELEMENT_META[element]`.
    if (typeof item.element !== 'string' || !isKnownElement(item.element)) return false
    // Leaves only: no container/nested fields on a harness seed.
    if ('tabs' in item || 'grid' in item || 'activeTab' in item || 'config' in item) return false
    if (!isCoord(item.x, cols) || !isCoord(item.y, rows)) return false
    if (!isDim(item.w, cols) || !isDim(item.h, rows)) return false
    const rect = { x: item.x as number, y: item.y as number, w: item.w as number, h: item.h as number }
    // Fully in bounds — no span may run off the grid.
    if (rect.x + rect.w > cols || rect.y + rect.h > rows) return false
    // No two items may share a cell (the editor guards moves, not a raw seed).
    if (rects.some((r) => overlaps(r, rect))) return false
    rects.push(rect)
  }
  return true
}

function readSeedFromQuery(): GridSpec {
  try {
    const raw = new URLSearchParams(window.location.search).get('seed')
    if (!raw) return SEED_SPEC
    const parsed: unknown = JSON.parse(raw)
    if (isValidSeed(parsed)) return parsed
  } catch {
    // Malformed seed → fall back to the default arrangement.
  }
  return SEED_SPEC
}

export default function LayoutEditorHarnessPage() {
  const [spec, setSpec] = useState<GridSpec>(readSeedFromQuery)

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
