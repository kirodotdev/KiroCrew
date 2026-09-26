import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { useSelectionComposerAnchor } from './useSelectionComposerAnchor'
import { composerDraftStoreFor } from '../utils/composerDraftStore'

type Anchor = { quote: string; from?: 'dom' | 'bridge' }

function setup(opts: { dom?: Anchor | null; confirmDiscard?: () => Promise<boolean>; key?: string } = {}) {
  const submit = vi.fn()
  const resolveDomAnchor = vi.fn(() => opts.dom ?? null)
  const hook = renderHook(() => useSelectionComposerAnchor<Anchor>({
    resolveDomAnchor,
    quoteOf: a => a.quote,
    quoteOnly: quote => ({ quote }),
    submit,
    draftKey: opts.key ?? 'mc-test-composer-draft:one',
    confirmDiscard: opts.confirmDiscard,
  }))
  return { hook, submit, resolveDomAnchor }
}

beforeEach(() => { window.sessionStorage.clear() })

describe('useSelectionComposerAnchor', () => {
  it('submits the anchor resolved from the live DOM selection in onOpen, then clears', () => {
    const { hook, submit, resolveDomAnchor } = setup({ dom: { quote: 'beta', from: 'dom' } })
    expect(hook.result.current.isComposerOpen()).toBe(false)
    act(() => { hook.result.current.selectionComposer.onOpen?.('beta') })
    expect(resolveDomAnchor).toHaveBeenCalledTimes(1)
    // Open from `onOpen` until the box closes or submits — the flag a host's
    // document-level Escape handler stands down on.
    expect(hook.result.current.isComposerOpen()).toBe(true)
    act(() => { hook.result.current.selectionComposer.onSubmit('note', 'beta') })
    expect(submit).toHaveBeenCalledWith('note', { quote: 'beta', from: 'dom' })
    expect(hook.result.current.isComposerOpen()).toBe(false)
    // A second submit with nothing pending posts nothing.
    act(() => { hook.result.current.selectionComposer.onSubmit('again', 'beta') })
    expect(submit).toHaveBeenCalledTimes(1)
  })

  it('a submit the host refuses keeps the pending anchor for the retry; success clears it', async () => {
    let answer: (ok: boolean) => void = () => {}
    const submit = vi.fn(() => new Promise<boolean>(resolve => { answer = resolve }))
    const resolveDomAnchor = vi.fn(() => ({ quote: 'beta', from: 'dom' as const }))
    const hook = renderHook(() => useSelectionComposerAnchor<Anchor>({
      resolveDomAnchor, quoteOf: a => a.quote, quoteOnly: quote => ({ quote }), submit, draftKey: 'mc-test-composer-draft:async',
    }))
    act(() => { hook.result.current.selectionComposer.onOpen?.('beta') })
    const first = hook.result.current.selectionComposer.onSubmit('note', 'beta') as Promise<boolean>
    answer(false)
    expect(await first).toBe(false)
    // Still open, same anchor: the retry posts against it without a new onOpen.
    expect(hook.result.current.isComposerOpen()).toBe(true)
    const second = hook.result.current.selectionComposer.onSubmit('note', 'beta') as Promise<boolean>
    expect(submit).toHaveBeenCalledTimes(2)
    expect(submit).toHaveBeenLastCalledWith('note', { quote: 'beta', from: 'dom' })
    answer(true)
    expect(await second).toBe(true)
    expect(hook.result.current.isComposerOpen()).toBe(false)
    // A rejection reads as a refusal.
    submit.mockImplementationOnce(() => Promise.reject(new Error('down')))
    act(() => { hook.result.current.selectionComposer.onOpen?.('beta') })
    expect(await (hook.result.current.selectionComposer.onSubmit('again', 'beta') as Promise<boolean>)).toBe(false)
    expect(hook.result.current.isComposerOpen()).toBe(true)
  })

  it('promotes a staged bridge anchor only when the toolbar opens for that same text', () => {
    const { hook, submit } = setup()
    act(() => { hook.result.current.stageIframeSelection({ quote: 'alpha', from: 'bridge' }, { text: 'alpha', x: 1, y: 2, start: 0 }) })
    expect(hook.result.current.iframeSelection).toEqual({ text: 'alpha', x: 1, y: 2, start: 0 })
    // The toolbar opened for a DIFFERENT text (a box already holding a draft
    // refused to re-target): the staged anchor is dropped, not submitted.
    act(() => { hook.result.current.selectionComposer.onOpen?.('gamma') })
    act(() => { hook.result.current.selectionComposer.onSubmit('note', 'gamma') })
    expect(submit).toHaveBeenCalledWith('note', { quote: 'gamma' })

    act(() => { hook.result.current.stageIframeSelection({ quote: 'alpha', from: 'bridge' }, { text: 'alpha', x: 1, y: 2 }) })
    act(() => { hook.result.current.selectionComposer.onOpen?.('alpha') })
    act(() => { hook.result.current.selectionComposer.onSubmit('second', 'alpha') })
    expect(submit).toHaveBeenLastCalledWith('second', { quote: 'alpha', from: 'bridge' })
    expect(hook.result.current.iframeSelection).toBeNull()
  })

  it('onClose and clearSelectionState drop the pending anchor and the external selection', () => {
    const { hook, submit } = setup({ dom: { quote: 'beta' } })
    act(() => { hook.result.current.stageIframeSelection({ quote: 'beta' }, { text: 'beta', x: 0, y: 0 }) })
    act(() => { hook.result.current.selectionComposer.onOpen?.('beta') })
    expect(hook.result.current.isComposerOpen()).toBe(true)
    act(() => { hook.result.current.selectionComposer.onClose?.() })
    expect(hook.result.current.isComposerOpen()).toBe(false)
    expect(hook.result.current.iframeSelection).toBeNull()
    act(() => { hook.result.current.selectionComposer.onSubmit('note', 'beta') })
    expect(submit).not.toHaveBeenCalled()

    act(() => { hook.result.current.stageIframeSelection({ quote: 'beta' }, { text: 'beta', x: 0, y: 0 }) })
    act(() => { hook.result.current.clearSelectionState() })
    expect(hook.result.current.iframeSelection).toBeNull()
  })

  it('guardCommentDraft asks only while a draft is open and clears that passage on a confirmed discard', async () => {
    const confirmDiscard = vi.fn(async () => true)
    const { hook } = setup({ confirmDiscard })
    const store = composerDraftStoreFor('mc-test-composer-draft:one')
    store.write('typed', 'beta', 6)
    const proceed = vi.fn()

    // No draft: straight through, no question, the stored draft untouched.
    await act(async () => { await hook.result.current.guardCommentDraft(proceed) })
    expect(confirmDiscard).not.toHaveBeenCalled()
    expect(proceed).toHaveBeenCalledTimes(1)
    expect(store.read('beta', 6)).toBe('typed')

    act(() => { hook.result.current.selectionComposer.onDraftChange?.(true, { anchor: 'beta', start: 6 }) })
    expect(hook.result.current.hasComposerDraft()).toBe(true)
    confirmDiscard.mockResolvedValueOnce(false)
    await act(async () => { await hook.result.current.guardCommentDraft(proceed) })
    expect(proceed).toHaveBeenCalledTimes(1)
    expect(store.read('beta', 6)).toBe('typed')

    await act(async () => { await hook.result.current.guardCommentDraft(proceed) })
    expect(proceed).toHaveBeenCalledTimes(2)
    expect(store.read('beta', 6)).toBeNull()
  })

  it('hands the toolbar a draft store keyed by the host and the same confirmDiscard', () => {
    const confirmDiscard = vi.fn(async () => true)
    const { hook } = setup({ confirmDiscard, key: 'mc-test-composer-draft:two' })
    const composer = hook.result.current.selectionComposer
    expect(composer.confirmDiscard).toBe(confirmDiscard)
    composer.draftStore?.write('kept', 'alpha', 0)
    expect(composerDraftStoreFor('mc-test-composer-draft:two').read('alpha', 0)).toBe('kept')
    expect(composerDraftStoreFor('mc-test-composer-draft:one').read('alpha', 0)).toBeNull()
    // Without confirmDiscard a guarded action drops the draft without asking.
    const bare = setup({ key: 'mc-test-composer-draft:three' })
    act(() => { bare.hook.result.current.selectionComposer.onDraftChange?.(true, { anchor: 'x', start: 0 }) })
    const proceed = vi.fn()
    return act(async () => { await bare.hook.result.current.guardCommentDraft(proceed) }).then(() => {
      expect(proceed).toHaveBeenCalledTimes(1)
    })
  })
})
