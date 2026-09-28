import { describe, it, expect } from 'vitest'
import { act, render } from '@testing-library/react'
import { createComposerDraftStore, useComposerDraft, useComposerDraftSelector, type ComposerDraftStore } from './draftStore'

/* The composer text as a store (chat-core P3-f). The host writes it with the
 * `useState`-setter shape and re-renders only for the facts it selects; the
 * editor subscribes to the text itself. */

describe('createComposerDraftStore', () => {
  it('takes a value or an updater, and notifies only on a real change', () => {
    const store = createComposerDraftStore('a')
    let calls = 0
    const off = store.subscribe(() => { calls++ })
    store.set('ab')
    store.set(prev => `${prev}c`)
    expect(store.get()).toBe('abc')
    expect(calls).toBe(2)
    store.set('abc')
    store.set(prev => prev)
    expect(calls).toBe(2)
    off()
    store.set('x')
    expect(calls).toBe(2)
  })
})

function Probe({ store, onRender }: { store: ComposerDraftStore; onRender: (text: string) => void }) {
  onRender(useComposerDraft(store))
  return null
}

function FactProbe({ store, onRender }: { store: ComposerDraftStore; onRender: (blank: boolean) => void }) {
  onRender(useComposerDraftSelector(store, text => text.trim() === ''))
  return null
}

describe('draft subscriptions', () => {
  it('useComposerDraft re-renders its caller on every change', () => {
    const store = createComposerDraftStore('')
    const seen: string[] = []
    render(<Probe store={store} onRender={t => seen.push(t)} />)
    act(() => { store.set('h') })
    act(() => { store.set('hi') })
    expect(seen).toEqual(['', 'h', 'hi'])
  })

  it('useComposerDraftSelector re-renders only when the selected fact flips', () => {
    const store = createComposerDraftStore('')
    const seen: boolean[] = []
    render(<FactProbe store={store} onRender={b => seen.push(b)} />)
    for (const text of ['h', 'he', 'hel', 'hell', 'hello']) act(() => { store.set(text) })
    act(() => { store.set('   ') })
    // Mount (blank), first character (not blank), whitespace-only (blank again):
    // four more characters of typing cost this caller nothing.
    expect(seen).toEqual([true, false, true])
  })
})
