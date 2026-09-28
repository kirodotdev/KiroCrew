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

  it('guardCommentDraft proceeds without asking while a post is in flight, and never touches the slot', async () => {
    // The flight owns the slot: a confirmed discard now would clear it under a
    // post that may be refused, and waiting for the answer would stall the
    // host on a hung POST. So: no question, no clear, straight through.
    const confirmDiscard = vi.fn(async () => true)
    let answer: (ok: boolean) => void = () => {}
    const submit = vi.fn(() => new Promise<boolean>(resolve => { answer = resolve }))
    const store = composerDraftStoreFor('mc-test-composer-draft:flight')
    const hook = renderHook(() => useSelectionComposerAnchor<Anchor>({
      resolveDomAnchor: () => ({ quote: 'beta' }), quoteOf: a => a.quote, quoteOnly: quote => ({ quote }), submit,
      draftKey: 'mc-test-composer-draft:flight', confirmDiscard,
    }))
    act(() => { hook.result.current.selectionComposer.onOpen?.('beta') })
    act(() => { hook.result.current.selectionComposer.onDraftChange?.(true, { anchor: 'beta', start: 6 }) })
    store.write('typed', 'beta', 6)
    const posting = hook.result.current.selectionComposer.onSubmit('typed', 'beta') as Promise<boolean>
    const proceed = vi.fn()
    await act(async () => { await hook.result.current.guardCommentDraft(proceed) })
    expect(proceed).toHaveBeenCalledTimes(1)
    expect(confirmDiscard).not.toHaveBeenCalled()
    expect(store.read('beta', 6)).toBe('typed')
    await act(async () => { answer(false); await posting })
    expect(store.read('beta', 6)).toBe('typed')
    // Flight over and refused: the guard is an ordinary guard again.
    await act(async () => { await hook.result.current.guardCommentDraft(proceed) })
    expect(confirmDiscard).toHaveBeenCalledTimes(1)
    expect(store.read('beta', 6)).toBeNull()
  })

  it('the first of two outstanding posts settling does not make the second one\'s draft discardable', async () => {
    // Box A posts and is closed mid-flight (the post stays outstanding); a new
    // box B posts. A settles first: the host still has B in flight, so a guard
    // run now must still proceed without asking and leave B's slot alone.
    const confirmDiscard = vi.fn(async () => true)
    const answers: Array<(ok: boolean) => void> = []
    const submit = vi.fn(() => new Promise<boolean>(resolve => { answers.push(resolve) }))
    const store = composerDraftStoreFor('mc-test-composer-draft:two-flights')
    const hook = renderHook(() => useSelectionComposerAnchor<Anchor>({
      resolveDomAnchor: () => ({ quote: 'beta' }), quoteOf: a => a.quote, quoteOnly: quote => ({ quote }), submit,
      draftKey: 'mc-test-composer-draft:two-flights', confirmDiscard,
    }))
    act(() => { hook.result.current.selectionComposer.onOpen?.('beta') })
    const postingA = hook.result.current.selectionComposer.onSubmit('A', 'beta') as Promise<boolean>
    act(() => { hook.result.current.selectionComposer.onClose?.() })
    act(() => { hook.result.current.selectionComposer.onOpen?.('beta') })
    act(() => { hook.result.current.selectionComposer.onDraftChange?.(true, { anchor: 'beta', start: 6 }) })
    store.write('B typed', 'beta', 6)
    const postingB = hook.result.current.selectionComposer.onSubmit('B typed', 'beta') as Promise<boolean>
    await act(async () => { answers[0](true); await postingA })
    const proceed = vi.fn()
    await act(async () => { await hook.result.current.guardCommentDraft(proceed) })
    expect(proceed).toHaveBeenCalledTimes(1)
    expect(confirmDiscard).not.toHaveBeenCalled()
    expect(store.read('beta', 6)).toBe('B typed')
    await act(async () => { answers[1](false); await postingB })
    // B refused: its text is still there for the next open.
    expect(store.read('beta', 6)).toBe('B typed')
  })

  it('reports a refusal to the host only when the box that posted is gone', async () => {
    const onRefusedAfterClose = vi.fn()
    let answer: (ok: boolean) => void = () => {}
    const submit = vi.fn(() => new Promise<boolean>(resolve => { answer = resolve }))
    const hook = renderHook(() => useSelectionComposerAnchor<Anchor>({
      resolveDomAnchor: () => ({ quote: 'beta' }), quoteOf: a => a.quote, quoteOnly: quote => ({ quote }), submit,
      draftKey: 'mc-test-composer-draft:orphan', onRefusedAfterClose,
    }))
    // Refused while the box is still open: the box shows it, the host stays quiet.
    act(() => { hook.result.current.selectionComposer.onOpen?.('beta') })
    let p = hook.result.current.selectionComposer.onSubmit('x', 'beta') as Promise<boolean>
    await act(async () => { answer(false); await p })
    expect(onRefusedAfterClose).not.toHaveBeenCalled()
    // Refused after Escape closed the box (onClose ran): the host is told.
    p = hook.result.current.selectionComposer.onSubmit('x', 'beta') as Promise<boolean>
    act(() => { hook.result.current.selectionComposer.onClose?.() })
    await act(async () => { answer(false); await p })
    expect(onRefusedAfterClose).toHaveBeenCalledTimes(1)
    // ...and told WHICH passage, so its notice can name the text to select again.
    expect(onRefusedAfterClose).toHaveBeenCalledWith('beta')
  })

  it('clips a long, multi-line passage before handing it to the refusal notice', async () => {
    const onRefusedAfterClose = vi.fn()
    let answer: (ok: boolean) => void = () => {}
    const submit = vi.fn(() => new Promise<boolean>(resolve => { answer = resolve }))
    const long = 'alpha\n  beta  gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi'
    const hook = renderHook(() => useSelectionComposerAnchor<Anchor>({
      resolveDomAnchor: () => ({ quote: long }), quoteOf: a => a.quote, quoteOnly: quote => ({ quote }), submit,
      draftKey: 'mc-test-composer-draft:orphan-long', onRefusedAfterClose,
    }))
    act(() => { hook.result.current.selectionComposer.onOpen?.(long) })
    const p = hook.result.current.selectionComposer.onSubmit('x', long) as Promise<boolean>
    act(() => { hook.result.current.selectionComposer.onClose?.() })
    await act(async () => { answer(false); await p })
    const [quote] = onRefusedAfterClose.mock.calls[0] as [string]
    // One line, whitespace folded, at most 48 characters ending in an ellipsis.
    expect(quote).not.toMatch(/\n|  /)
    expect(quote.length).toBeLessThanOrEqual(48)
    expect(quote.endsWith('\u2026')).toBe(true)
    expect(quote.startsWith('alpha beta gamma')).toBe(true)
  })

  it('a post that settles after the box moved to another passage does not clear that passage\'s anchor', async () => {
    // The host (a page reused across artifacts, or one passage after another)
    // keeps one hook; a slow post on A must not null the anchor of B's open box.
    let answer: (ok: boolean) => void = () => {}
    const submit = vi.fn((_c: string, a: Anchor) => a.quote === 'A' ? new Promise<boolean>(resolve => { answer = resolve }) : Promise.resolve(true))
    let dom: Anchor = { quote: 'A' }
    const hook = renderHook(() => useSelectionComposerAnchor<Anchor>({
      resolveDomAnchor: () => dom, quoteOf: a => a.quote, quoteOnly: quote => ({ quote }), submit, draftKey: 'mc-test-composer-draft:gen',
    }))
    act(() => { hook.result.current.selectionComposer.onOpen?.('A') })
    const postingA = hook.result.current.selectionComposer.onSubmit('about A', 'A') as Promise<boolean>
    dom = { quote: 'B' }
    act(() => { hook.result.current.selectionComposer.onOpen?.('B') })
    await act(async () => { answer(true); await postingA })
    // B's anchor survived A's late success: B's own submit posts B.
    expect(hook.result.current.isComposerOpen()).toBe(true)
    await act(async () => { await hook.result.current.selectionComposer.onSubmit('about B', 'B') })
    expect(submit).toHaveBeenLastCalledWith('about B', { quote: 'B' })
    expect(hook.result.current.isComposerOpen()).toBe(false)
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
