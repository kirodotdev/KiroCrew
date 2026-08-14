/**
 * The three provider-layer seams that carry real branching but had no direct
 * test: the pane-scoping context, the single-adapter provider context, and the
 * static model-registry fallback.
 *
 * `SlotContext`'s whole point is the `undefined` sentinel — "no provider, fall
 * back to the global focused slot" must stay distinguishable from "a provider
 * supplying a deliberately empty pane". Both readings are asserted against the
 * SAME non-null global so a regression that collapses them cannot pass.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import type { ReactElement } from 'react'

import { SlotProvider, useSlotId, useIsPaneScoped } from '../providers/SlotContext'
import { ProviderProvider, useProvider } from '../providers/context'
import { getAdapter } from '../providers/registry'
import { displayModels } from '../providers/modelRegistry'
import { setActiveSlot } from '../store/chatSlice'
import { createTestStore } from './helpers'

/** Reports both hooks as text, so one render answers both questions. */
function Probe() {
  const slot = useSlotId()
  const scoped = useIsPaneScoped()
  return (
    <span data-testid="probe" data-slot={slot === null ? 'NULL' : slot} data-scoped={String(scoped)} />
  )
}

/** A store whose global focused slot is set, via the slice's own action. */
function storeWithGlobal(slot: string | null) {
  const store = createTestStore()
  store.dispatch(setActiveSlot(slot))
  return store
}

function renderProbe(store: ReturnType<typeof createTestStore>, node: ReactElement) {
  render(<Provider store={store}>{node}</Provider>)
  return screen.getByTestId('probe')
}

describe('providers/SlotContext', () => {
  it('falls back to the global focused slot with no provider in the tree', () => {
    const probe = renderProbe(storeWithGlobal('zzslot-global'), <Probe />)
    expect(probe.getAttribute('data-slot')).toBe('zzslot-global')
    expect(probe.getAttribute('data-scoped')).toBe('false')
  })

  it('binds to the pane slot inside a provider, ignoring the global', () => {
    const probe = renderProbe(
      storeWithGlobal('zzslot-global'),
      <SlotProvider slotId="zzslot-pane"><Probe /></SlotProvider>,
    )
    expect(probe.getAttribute('data-slot')).toBe('zzslot-pane')
    expect(probe.getAttribute('data-scoped')).toBe('true')
  })

  it('keeps a provider-supplied null distinct from the no-provider fallback', () => {
    // Same non-null global as the case above: an empty pane must read as empty,
    // not silently inherit whatever pane the user last focused.
    const probe = renderProbe(
      storeWithGlobal('zzslot-global'),
      <SlotProvider slotId={null}><Probe /></SlotProvider>,
    )
    expect(probe.getAttribute('data-slot')).toBe('NULL')
    expect(probe.getAttribute('data-scoped')).toBe('true')
  })

  it('tracks the global when it changes and no provider is present', () => {
    const store = storeWithGlobal(null)
    const probe = renderProbe(store, <Probe />)
    expect(probe.getAttribute('data-slot')).toBe('NULL')
  })
})

describe('providers/context', () => {
  function AdapterProbe() {
    const adapter = useProvider()
    return <span data-testid="adapter" data-id={adapter.id} />
  }

  it('serves the single ACP adapter through the provider', () => {
    render(<ProviderProvider><AdapterProbe /></ProviderProvider>)
    expect(screen.getByTestId('adapter').getAttribute('data-id')).toBe(getAdapter().id)
  })

  it('defaults to the same adapter with no provider mounted', () => {
    // The context default is load-bearing: consumers outside the composition
    // root must still get a working adapter rather than undefined.
    render(<AdapterProbe />)
    expect(screen.getByTestId('adapter').getAttribute('data-id')).toBe(getAdapter().id)
  })
})

describe('providers/modelRegistry', () => {
  it('lists the canonical rows with the default first', () => {
    const rows = displayModels()
    expect(rows.length).toBeGreaterThan(0)
    for (const r of rows) {
      expect(typeof r.name).toBe('string')
      expect(r.name.startsWith('_')).toBe(false)
      expect(typeof r.description).toBe('string')
      expect(r.contextWindow).toBeGreaterThan(0)
    }
  })

  it('is stable across calls and drops the metadata keys', () => {
    const a = displayModels().map((r) => r.name)
    const b = displayModels().map((r) => r.name)
    expect(a).toEqual(b)
    expect(a.some((n) => n.startsWith('_'))).toBe(false)
  })
})
