import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { waitFor, act, fireEvent, screen } from '@testing-library/react'
import type { RefObject } from 'react'
import { renderHookWithProviders, renderWithProviders } from './helpers'
import { useFileArtifactComments } from '../components/FileArtifactComments'
import * as annotationHighlight from '../utils/annotationHighlight'
import { api } from '../api/client'
import type { ArtifactComment } from '../types'

vi.mock('../api/client')

type PreviewRef = RefObject<HTMLDivElement | null>
type ScrollRef = RefObject<HTMLElement | null>
type CommentsResponse = { comments: ArtifactComment[]; remote_sync_error?: string | null }

function refs() {
  return { previewRef: { current: null } as PreviewRef, scrollRef: { current: null } as ScrollRef }
}

function mk(id: string): ArtifactComment {
  return {
    id, origin: 'local', scope: 'private', author: 'alex', is_agent: false,
    body: 'x', thread_id: id, status: 'open', sync_state: 'local_only',
    created_at: '', updated_at: '',
  }
}

/** A rendered body the hook can anchor into: one text node, in the document. */
const BODY = 'alpha beta gamma beta delta'
const mounted: HTMLElement[] = []
function mountPreview(): HTMLDivElement {
  const el = document.createElement('div')
  el.textContent = BODY
  document.body.appendChild(el)
  mounted.push(el)
  return el
}

/**
 * Back `window.getSelection` with a real `Range` over `node` from `start` for
 * `word.length` characters — the same object shape the hook reads. The test DOM
 * has no real selection, and the hook must see the LIVE range (a composer's
 * `onOpen` is the one moment it exists), so this is what `onOpen` runs against.
 */
function selectRange(node: Node, start: number, word: string) {
  const range = document.createRange()
  range.setStart(node, start)
  range.setEnd(node, start + word.length)
  vi.spyOn(window, 'getSelection').mockReturnValue({
    isCollapsed: false,
    anchorNode: node,
    rangeCount: 1,
    getRangeAt: () => range,
    removeAllRanges: () => undefined,
    toString: () => word,
  } as unknown as Selection)
}

const noSelection = () => vi.spyOn(window, 'getSelection').mockReturnValue({
  isCollapsed: true, anchorNode: null, rangeCount: 0, getRangeAt: () => { throw new Error('no range') },
  removeAllRanges: () => undefined, toString: () => '',
} as unknown as Selection)

const postedAnchor = () => vi.mocked(api.postArtifactComment).mock.calls[0][1].anchor as Record<string, unknown> | undefined

describe('useFileArtifactComments', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.postArtifactComment).mockResolvedValue({ ok: true } as Awaited<ReturnType<typeof api.postArtifactComment>>)
  })
  afterEach(() => {
    vi.restoreAllMocks()
    mounted.splice(0).forEach(el => el.remove())
  })

  it('is inert when slug is null', () => {
    const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: null, ...refs() }))
    expect(result.current.commentCount).toBe(0)
    expect(result.current.overlay).toBeNull()
    expect(result.current.sidebar).toBeNull()
    expect(result.current.popovers).toBeNull()
  })

  it('loads comments for a slug and reflects the count', async () => {
    vi.mocked(api.artifactComments).mockResolvedValue({ comments: [mk('a'), mk('b')] } as CommentsResponse)
    const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', ...refs() }))
    await waitFor(() => expect(result.current.commentCount).toBe(2))
    expect(result.current.sidebar).not.toBeNull()
    expect(result.current.overlay).not.toBeNull()
  })

  it('clicking an anchored thread in the sidebar marks it read and drives the body (native) or the bridge (iframe) to it', async () => {
    const root = { ...mk('root'), body: 'anchored note', anchor: { quote: 'beta' } }
    const reply = { ...mk('reply'), body: 'a reply', parent_id: 'root' }
    vi.mocked(api.artifactComments).mockResolvedValue({ comments: [root, reply] } as CommentsResponse)
    type Hook = ReturnType<typeof useFileArtifactComments>
    let latest: Hook | null = null
    function Probe({ usesIframe }: { usesIframe: boolean }) {
      const h = useFileArtifactComments({ slug: 'doc', usesIframe, ...refs() })
      latest = h
      return <>{h.sidebar}{h.popovers}</>
    }
    const native = renderWithProviders(<Probe usesIframe={false} />)
    await waitFor(() => expect(latest!.commentCount).toBe(2))
    // Unread roots collapse a reply onto its root.
    expect([...latest!.unreadRootIds]).toEqual(['root'])
    fireEvent.click(screen.getByRole('button', { name: /anchored note/ }))
    expect(latest!.activeCommentId).toBe('root')
    expect(latest!.unreadRootIds.size).toBe(0)
    expect(latest!.scrollNonce).toBe(1)
    expect(latest!.iframeScrollTarget).toBeNull()
    native.unmount()

    latest = null
    renderWithProviders(<Probe usesIframe />)
    await waitFor(() => expect(latest!.commentCount).toBe(2))
    fireEvent.click(screen.getByRole('button', { name: /anchored note/ }))
    expect(latest!.iframeScrollTarget?.id).toBe('root')
    expect(latest!.scrollNonce).toBe(0)
    // The bridge answers with the anchor rect: the thread popover opens there.
    act(() => latest!.onIframeOpenThread('root', { x: 1, y: 2, w: 3, h: 4 }))
    expect(latest!.activeCommentId).toBe('root')
  })

  it('toggles the sidebar open state', async () => {
    vi.mocked(api.artifactComments).mockResolvedValue({ comments: [] } as CommentsResponse)
    const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', ...refs() }))
    expect(result.current.sidebarOpen).toBe(true)
    act(() => result.current.toggleSidebar())
    expect(result.current.sidebarOpen).toBe(false)
  })

  describe('selection composer', () => {
    beforeEach(() => {
      vi.mocked(api.artifactComments).mockResolvedValue({ comments: [] } as CommentsResponse)
    })

    it('onOpen resolves the live DOM selection into an anchor that onSubmit posts, pinned to THIS occurrence', async () => {
      const preview = mountPreview()
      const previewRef: PreviewRef = { current: preview }
      const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', previewRef, scrollRef: { current: null } }))
      // The SECOND "beta": an indexOf-based anchor would land on the first.
      const second = BODY.indexOf('beta', BODY.indexOf('beta') + 1)
      selectRange(preview.firstChild as Text, second, 'beta')

      act(() => result.current.selectionComposer.onOpen?.('beta'))
      // What the toolbar does after `onOpen`: focus collapses the selection.
      noSelection()
      act(() => { void result.current.selectionComposer.onSubmit('tighten this', 'beta') })

      await waitFor(() => expect(api.postArtifactComment).toHaveBeenCalledTimes(1))
      const [slug, body] = vi.mocked(api.postArtifactComment).mock.calls[0]
      expect(slug).toBe('doc')
      expect(body).toMatchObject({ text: 'tighten this', scope: 'private' })
      expect(postedAnchor()).toEqual({
        quote: 'beta',
        prefix: BODY.slice(Math.max(0, second - 32), second),
        suffix: BODY.slice(second + 4, second + 4 + 32),
        start_offset: second,
        end_offset: second + 4,
      })
    })

    it('onOpen accepts a triple-click selection whose end point is hoisted out of the preview, like the toolbar does', async () => {
      // Browsers normalize a triple-click on the last block to end "at the start
      // of the next block" — a boundary point in the preview's PARENT. The
      // toolbar opens for it (containedSelectionRange), so the resolver must
      // anchor it too, with offsets measured inside the preview.
      const wrapper = document.createElement('div')
      const preview = document.createElement('div')
      preview.textContent = BODY
      wrapper.append(preview, document.createElement('p'))
      document.body.appendChild(wrapper)
      mounted.push(wrapper)
      const previewRef: PreviewRef = { current: preview }
      const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', previewRef, scrollRef: { current: null } }))
      const start = BODY.indexOf('gamma')
      const range = document.createRange()
      range.setStart(preview.firstChild as Text, start)
      range.setEnd(wrapper, 1)
      vi.spyOn(window, 'getSelection').mockReturnValue({
        isCollapsed: false, anchorNode: preview.firstChild, rangeCount: 1, getRangeAt: () => range,
        removeAllRanges: () => undefined, toString: () => range.toString(),
      } as unknown as Selection)

      act(() => result.current.selectionComposer.onOpen?.(range.toString().trim()))
      noSelection()
      act(() => { void result.current.selectionComposer.onSubmit('last block', range.toString().trim()) })
      await waitFor(() => expect(api.postArtifactComment).toHaveBeenCalledTimes(1))
      expect(postedAnchor()).toMatchObject({ quote: BODY.slice(start), start_offset: start, end_offset: BODY.length })
    })

    it('paints the passage on open (focus is about to collapse the selection) and clears it on close and on submit', async () => {
      const paint = vi.spyOn(annotationHighlight, 'paintAnnotationHighlight').mockImplementation(() => {})
      const clear = vi.spyOn(annotationHighlight, 'clearAnnotationHighlight').mockImplementation(() => {})
      const preview = mountPreview()
      const previewRef: PreviewRef = { current: preview }
      const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', previewRef, scrollRef: { current: null } }))
      selectRange(preview.firstChild as Text, BODY.indexOf('beta'), 'beta')
      act(() => result.current.selectionComposer.onOpen?.('beta'))
      expect(paint).toHaveBeenCalledTimes(1)
      expect(paint.mock.calls[0][1].toString()).toBe('beta')
      const owner = paint.mock.calls[0][0]
      act(() => result.current.selectionComposer.onClose?.())
      expect(clear).toHaveBeenCalledWith(owner)
      clear.mockClear()
      act(() => result.current.selectionComposer.onOpen?.('beta'))
      noSelection()
      // The paint goes once the store has the comment (the post is awaited).
      await act(async () => { await result.current.selectionComposer.onSubmit('note', 'beta') })
      expect(clear).toHaveBeenCalledWith(owner)
    })

    it('onOpen with no live selection anchors on the toolbar\'s text alone (no offsets)', async () => {
      const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', ...refs() }))
      noSelection()
      act(() => result.current.selectionComposer.onOpen?.('quoted words'))
      act(() => { void result.current.selectionComposer.onSubmit('note', 'quoted words') })
      await waitFor(() => expect(api.postArtifactComment).toHaveBeenCalledTimes(1))
      expect(postedAnchor()).toEqual({ quote: 'quoted words', prefix: undefined, suffix: undefined })
    })

    it('an in-iframe selection hands the toolbar an external selection and keeps the bridge anchor through onOpen', async () => {
      const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', ...refs(), usesIframe: true }))
      expect(result.current.iframeSelection).toBeNull()
      act(() => result.current.onIframeSelect({ x: 20, y: 40, quote: 'widget body', prefix: 'before ', suffix: ' after' }))
      expect(result.current.iframeSelection).toEqual({ text: 'widget body', x: 20, y: 40 })

      // The toolbar opens from that external selection: no DOM range to resolve,
      // so `onOpen` must NOT replace the bridge's prefix/suffix with the bare quote.
      noSelection()
      act(() => result.current.selectionComposer.onOpen?.('widget body'))
      act(() => { void result.current.selectionComposer.onSubmit('from the frame', 'widget body') })
      await waitFor(() => expect(api.postArtifactComment).toHaveBeenCalledTimes(1))
      // Offsets are omitted on purpose: the frame's text space is not the parent's.
      expect(postedAnchor()).toEqual({ quote: 'widget body', prefix: 'before ', suffix: ' after' })
      // The submit consumed the external selection.
      expect(result.current.iframeSelection).toBeNull()
    })

    it('a second in-iframe selection made while a box is open does not re-anchor that box\'s draft', async () => {
      // The toolbar refuses to re-target a box holding a typed draft — it never
      // calls `onOpen` for the second selection. The host must not move the
      // anchor underneath it either: the draft was written about passage A.
      const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', ...refs(), usesIframe: true }))
      noSelection()
      act(() => result.current.onIframeSelect({ x: 1, y: 2, quote: 'passage A', prefix: 'before A ', suffix: ' after A' }))
      act(() => result.current.selectionComposer.onOpen?.('passage A'))
      // Reading gesture inside the frame while the box (with a draft) is open.
      act(() => result.current.onIframeSelect({ x: 5, y: 6, quote: 'passage B', prefix: 'before B ', suffix: ' after B' }))
      act(() => { void result.current.selectionComposer.onSubmit('about A', 'passage A') })
      await waitFor(() => expect(api.postArtifactComment).toHaveBeenCalledTimes(1))
      expect(postedAnchor()).toEqual({ quote: 'passage A', prefix: 'before A ', suffix: ' after A' })
    })

    it('onClose discards the pending anchor and the external selection, so a later submit posts nothing', async () => {
      const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', ...refs(), usesIframe: true }))
      act(() => result.current.onIframeSelect({ x: 1, y: 2, quote: 'q', prefix: 'p', suffix: 's' }))
      act(() => result.current.selectionComposer.onClose?.())
      expect(result.current.iframeSelection).toBeNull()
      act(() => { void result.current.selectionComposer.onSubmit('orphan', 'q') })
      await act(async () => { await Promise.resolve() })
      expect(api.postArtifactComment).not.toHaveBeenCalled()
    })

    it('mirrors the composer draft flag and threads the host\'s discard prompt into the composer', () => {
      const confirmDiscardDraft = vi.fn(async () => true)
      const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', ...refs(), confirmDiscardDraft }))
      expect(result.current.hasComposerDraft()).toBe(false)
      act(() => result.current.selectionComposer.onDraftChange?.(true, { anchor: 'x', start: 0 }))
      expect(result.current.hasComposerDraft()).toBe(true)
      act(() => result.current.selectionComposer.onDraftChange?.(false, null))
      expect(result.current.hasComposerDraft()).toBe(false)
      expect(result.current.selectionComposer.confirmDiscard).toBe(confirmDiscardDraft)
    })

    it('hands the composer a per-artifact draft store, and clears the open passage\'s slot on a confirmed discard', () => {
      window.sessionStorage.clear()
      const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', ...refs() }))
      const store = result.current.selectionComposer.draftStore!
      store.write('half a thought', 'beta', 6)
      // A fresh mount over the SAME artifact (a slot switch replaced the panel) reads it back.
      const again = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', ...refs() }))
      expect(again.result.current.selectionComposer.draftStore!.read('beta', 6)).toBe('half a thought')
      // Another artifact does not.
      const other = renderHookWithProviders(() => useFileArtifactComments({ slug: 'other', ...refs() }))
      expect(other.result.current.selectionComposer.draftStore!.read('beta', 6)).toBeNull()

      act(() => result.current.selectionComposer.onDraftChange?.(true, { anchor: 'beta', start: 6 }))
      act(() => result.current.clearComposerDraftSlot())
      expect(store.read('beta', 6)).toBeNull()
    })

    it('a post the store refuses resolves false and keeps the anchor, so the box can retry against it', async () => {
      vi.mocked(api.postArtifactComment).mockRejectedValueOnce(new Error('gateway restarting'))
      const preview = mountPreview()
      const previewRef: PreviewRef = { current: preview }
      const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', previewRef, scrollRef: { current: null } }))
      selectRange(preview.firstChild as Text, BODY.indexOf('gamma'), 'gamma')
      act(() => result.current.selectionComposer.onOpen?.('gamma'))
      noSelection()
      let outcome: boolean | undefined
      await act(async () => { outcome = await (result.current.selectionComposer.onSubmit('keep me', 'gamma') as Promise<boolean>) })
      expect(outcome).toBe(false)
      expect(result.current.isComposerOpen()).toBe(true)
      // The failure is shown beside the sidebar's composer, not swallowed.
      expect(result.current.sidebar).not.toBeNull()
      // Retry: same anchor, and the store's success clears the box state.
      vi.mocked(api.postArtifactComment).mockResolvedValueOnce({ ok: true } as Awaited<ReturnType<typeof api.postArtifactComment>>)
      await act(async () => { outcome = await (result.current.selectionComposer.onSubmit('keep me', 'gamma') as Promise<boolean>) })
      expect(outcome).toBe(true)
      expect(api.postArtifactComment).toHaveBeenCalledTimes(2)
      const anchors = vi.mocked(api.postArtifactComment).mock.calls.map(c => (c[1].anchor as { quote: string; start_offset?: number }))
      expect(anchors[1]).toEqual(anchors[0])
      expect(anchors[1].start_offset).toBe(BODY.indexOf('gamma'))
      expect(result.current.isComposerOpen()).toBe(false)
    })

    it('onSubmit before any onOpen is a safe no-op', async () => {
      const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', ...refs() }))
      expect(() => act(() => result.current.selectionComposer.onSubmit('note', 'text'))).not.toThrow()
      await act(async () => { await Promise.resolve() })
      expect(api.postArtifactComment).not.toHaveBeenCalled()
    })
  })
})
