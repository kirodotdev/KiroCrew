/**
 * A streamed turn block must not keep its past renders alive. Each row's
 * RowActivityContext reporter is cached for the life of the block, so a
 * reporter created inside a render closure holds that render's whole scope
 * (its `turn`, its `items`, its row elements) through V8's shared closure
 * context. A turn streamed one row per render would then keep one past render
 * per row. Mounted from history it renders once, so it keeps one.
 */
import { describe, it, expect } from 'vitest'
import v8 from 'node:v8'
import vm from 'node:vm'
import { render } from '@testing-library/react'
import type { DisplayItem, TurnItem } from '../pages/chat/types'
import TurnBlock from '../pages/chat/TurnBlock'

v8.setFlagsFromString('--expose-gc')
const gc = vm.runInNewContext('gc') as () => void

const tool = (n: number): TurnItem => ({ kind: 'single', msg: { role: 'tool', content: `✅ step${n}`, ts: `t${n}` }, idx: n })
const turnOf = (items: TurnItem[]): Extract<DisplayItem, { kind: 'turn' }> => ({ kind: 'turn', items, complete: false })
const renderItem = (it: TurnItem, i: number) => <div data-testid={`item-${i}`}>{it.kind === 'single' ? it.msg.content : 'group'}</div>

describe('streamed turn block memory', () => {
  it.each([100, 300])('a turn streamed one row per render, %i rows, keeps no past render alive', async (rows) => {
    const past: WeakRef<TurnItem[]>[] = []
    let items = [tool(0)]
    past.push(new WeakRef(items))
    const { rerender, unmount } = render(<TurnBlock turn={turnOf(items)} renderItem={renderItem} />)
    // One new row per render, the way a live turn streams.
    for (let n = 1; n < rows; n++) {
      items = [...items, tool(n)]
      past.push(new WeakRef(items))
      rerender(<TurnBlock turn={turnOf(items)} renderItem={renderItem} />)
    }
    items = []
    // A WeakRef target stays alive until the job that read it ends.
    await new Promise(r => setTimeout(r, 0))
    gc()
    await new Promise(r => setTimeout(r, 0))
    gc()
    const alive = past.filter(ref => ref.deref() !== undefined).length
    unmount()
    // React keeps the current and the previous props on the fiber pair, so a
    // couple of the newest renders may still be reachable; none beyond that.
    expect(alive).toBeLessThanOrEqual(3)
  }, 60000)
})
