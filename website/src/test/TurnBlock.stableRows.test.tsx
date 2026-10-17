import { describe, it, expect, beforeEach } from 'vitest'
import { useEffect } from 'react'
import { render } from '@testing-library/react'
import TurnBlock from '../pages/chat/TurnBlock'
import type { DisplayItem, TurnItem } from '../pages/chat/types'

/**
 * #11083: the completion flip (turn.complete flips ~2.5s after a turn ends via
 * ChatPage's running latch) switches each render path from a flat
 * `items.map(renderItem)` to segment wrappers. Before the fix, an always-visible
 * row's parent element and key changed across the flip, so React unmounted and
 * remounted every visible row — harmless for text, but it re-created an inline
 * MCP App iframe and lost its in-canvas state. These tests assert every
 * always-visible row mounts EXACTLY ONCE across the flip. Each fails on the old
 * head (mount count 2).
 */

// Module-level mount counter keyed by the item's TRANSCRIPT identity (`idx`),
// not its mapped position — a position shifts when mergeTurnThinking hoists a
// later reasoning burst. The probe's useEffect with [] deps runs once per
// MOUNT, so a remount across a rerender increments it.
const mounts = new Map<string, number>()
function MountProbe({ i }: { i: string }) {
  useEffect(() => {
    mounts.set(i, (mounts.get(i) ?? 0) + 1)
  }, [i])
  return <div data-testid={`probe-${i}`} />
}

const makeTurn = (
  items: TurnItem[],
  complete: boolean,
  extra: Partial<Extract<DisplayItem, { kind: 'turn' }>> = {},
): Extract<DisplayItem, { kind: 'turn' }> => ({ kind: 'turn', items, complete, ...extra })

const identity = (it: TurnItem) => (it.kind === 'single' ? it.msg.ts ?? String(it.idx) : it.msgs[0]?.ts ?? String(it.startIdx))
const renderItem = (it: TurnItem, _i: number) => <MountProbe i={identity(it)} />

beforeEach(() => mounts.clear())

describe('TurnBlock — visible rows survive the completion flip (#11083)', () => {
  it('default mode: an MCP-app row, another visible tool row and a text row each mount once', () => {
    // idx 0: plain tool (folds); idx 1: app-bearing tool (visible-inline);
    // idx 2: another plain tool (folds); idx 3: conclusion text (visible).
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: read', ts: '1', meta: { tool_call_id: 'tc-plain' } }, idx: 0 },
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: create_view', ts: '2', meta: { tool_call_id: 'tc-app' } }, idx: 1 },
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: shell', ts: '3', meta: { tool_call_id: 'tc-plain2' } }, idx: 2 },
      { kind: 'single', msg: { role: 'assistant', content: 'Rendered the app with plenty of descriptive text to be substantive.', ts: '4' }, idx: 3 },
    ]
    const appIds = new Set(['tc-app'])
    const { rerender } = render(
      <TurnBlock turn={makeTurn(items, false)} renderItem={renderItem} appToolCallIds={appIds} />,
    )
    rerender(<TurnBlock turn={makeTurn(items, true)} renderItem={renderItem} appToolCallIds={appIds} />)
    // App row (ts '2'), conclusion text row (ts '4'): always visible in both
    // states -> one mount each.
    expect(mounts.get('2')).toBe(1)
    expect(mounts.get('4')).toBe(1)
  })

  it('collapseAll mode: the conclusion text row mounts once across the flip', () => {
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: read', ts: '1' }, idx: 0 },
      { kind: 'single', msg: { role: 'assistant', content: 'Inspecting the config before I patch it, to be sure of its shape.', ts: '2' }, idx: 1 },
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: write', ts: '3' }, idx: 2 },
      { kind: 'single', msg: { role: 'assistant', content: 'Patched the config and confirmed the build still passes cleanly.', ts: '4' }, idx: 3 },
    ]
    const { rerender } = render(
      <TurnBlock turn={makeTurn(items, false)} renderItem={renderItem} collapseAll />,
    )
    rerender(<TurnBlock turn={makeTurn(items, true)} renderItem={renderItem} collapseAll />)
    // ts '4' is the conclusion (always visible) -> one mount across the flip.
    expect(mounts.get('4')).toBe(1)
  })

  it('interim mode: a visible-inline error row mounts once across the flip', () => {
    // The interim region folds prose in both modes; an error row is
    // visible-inline (isAlwaysVisible), so it renders in place before and after.
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'assistant', content: 'Two of three agents are still in flight; here is the running summary.', ts: '1' }, idx: 0 },
      { kind: 'single', msg: { role: 'error', content: 'a spawn failed', ts: '2' }, idx: 1 },
    ]
    const { rerender } = render(
      <TurnBlock turn={makeTurn(items, false, { interim: true })} renderItem={renderItem} />,
    )
    rerender(<TurnBlock turn={makeTurn(items, true, { interim: true })} renderItem={renderItem} />)
    expect(mounts.get('2')).toBe(1)
  })

  it('a reasoning burst hoisted above the app row mid-turn does not remount it', () => {
    // mergeTurnThinking hoists reasoning to the turn top. When the burst arrives
    // AFTER the app row (idx 1 below), the next flush moves the app from mapped
    // position 0 to position 1. A wrapper keyed by position would remount the
    // app row on that flush; keyed by transcript identity (idx) it stays mounted.
    const app: TurnItem = { kind: 'single', msg: { role: 'tool', content: '🔧 Running: create_view', ts: '1', meta: { tool_call_id: 'tc-app' } }, idx: 0 }
    const burst: TurnItem = { kind: 'single', msg: { role: 'thinking', content: 'considering the result', cls: '', meta: {}, ts: '2' }, idx: 1 }
    const before: TurnItem[] = [app]
    const afterHoist: TurnItem[] = [app, burst]
    const appIds = new Set(['tc-app'])
    const { rerender } = render(
      <TurnBlock turn={makeTurn(before, false)} renderItem={renderItem} appToolCallIds={appIds} />,
    )
    rerender(<TurnBlock turn={makeTurn(afterHoist, false)} renderItem={renderItem} appToolCallIds={appIds} />)
    expect(mounts.get('1')).toBe(1)
  })

  it('a history backfill that renumbers every idx does not remount the app row', () => {
    // An older page landing prepends messages, so groupDisplayItems renumbers
    // every item's idx/startIdx while the messages themselves are unchanged. A
    // wrapper keyed by idx would remount every row below the prepend; keyed by
    // message identity (clientTs -> ts) the app row keeps its instance.
    const app = (idx: number): TurnItem =>
      ({ kind: 'single', msg: { role: 'tool', content: '🔧 Running: create_view', ts: '50', meta: { tool_call_id: 'tc-app' } }, idx })
    const text = (idx: number): TurnItem =>
      ({ kind: 'single', msg: { role: 'assistant', content: 'Rendered the app and described the result in some detail.', ts: '51' }, idx })
    const appIds = new Set(['tc-app'])
    const { rerender } = render(
      <TurnBlock turn={makeTurn([app(0), text(1)], true)} renderItem={renderItem} appToolCallIds={appIds} />,
    )
    // Same two messages, now sitting after ten older ones.
    rerender(<TurnBlock turn={makeTurn([app(10), text(11)], true)} renderItem={renderItem} appToolCallIds={appIds} />)
    expect(mounts.get('50')).toBe(1)
    expect(mounts.get('51')).toBe(1)
  })
})
