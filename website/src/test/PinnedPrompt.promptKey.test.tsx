import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import PinnedPrompt from '../pages/chat/PinnedPrompt'
import { pinCandidateKey } from '../pages/chat/usePinnedPrompt'

// The host (usePinnedPrompt) resets the resting height it holds to the seed
// whenever the pin candidate's identity — `pinCandidateKey(idx, ts)` — changes,
// and relies on the card to report its collapsed height again for the new
// identity. The card is NOT remounted for that (a remount would restart the
// glide and the morph), and the identity can change while `text` stays the
// same: older history prepended shifts every index, and one image-only prompt
// handing off to another leaves the preview text identical. Nothing resizes in
// either case, so the ResizeObserver never fires; the card's collapsed-height
// measure must re-run on the `promptKey` itself. This file pins that contract.
const TEXT = 'the same one line of text at a new index'
const MEASURED_H = 48

function renderCard(over: Partial<Parameters<typeof PinnedPrompt>[0]> = {}) {
  const props = {
    text: TEXT,
    fullText: TEXT,
    images: [] as string[],
    pushUp: 0,
    bannerH: MEASURED_H,
    expanded: false,
    onToggleExpanded: () => {},
    onJump: () => {},
    onCollapsedHeight: () => {},
    ...over,
  }
  const utils = render(<PinnedPrompt {...props} />)
  const card = screen.getByTestId('pinned-prompt')
  const box = card.firstElementChild as HTMLElement
  const rerenderWith = (next: Partial<Parameters<typeof PinnedPrompt>[0]>) =>
    utils.rerender(<PinnedPrompt {...props} {...next} />)
  return { ...utils, card, box, rerenderWith }
}

describe('PinnedPrompt re-reports its collapsed height for a new prompt identity', () => {
  let rectSpy: ReturnType<typeof vi.spyOn>
  beforeEach(() => {
    // happy-dom lays nothing out, so the box would measure 0 and the host would
    // ignore the report (`h > 0`). Give every rect one settled height: this is
    // what the card reads at rest, and what the assertion below expects back.
    rectSpy = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
      x: 0, y: 0, top: 0, left: 0, right: 0, bottom: MEASURED_H, width: 0, height: MEASURED_H, toJSON() { return {} },
    } as DOMRect)
    if (!('ResizeObserver' in globalThis)) {
      (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
        observe() {}
        unobserve() {}
        disconnect() {}
      }
    }
  })
  afterEach(() => { rectSpy.mockRestore(); cleanup() })

  it('reports again, with the measured height, when promptKey changes and the text does not', () => {
    const onCollapsedHeight = vi.fn()
    const { rerenderWith } = renderCard({ onCollapsedHeight, promptKey: pinCandidateKey(3, 't-3') })
    expect(onCollapsedHeight).toHaveBeenCalledWith(MEASURED_H)
    onCollapsedHeight.mockClear()
    // Older history prepended: the same prompt now sits at a higher index.
    rerenderWith({ promptKey: pinCandidateKey(9, 't-3') })
    expect(onCollapsedHeight, 'the host reset to the seed on this change and needs the card\'s height back').toHaveBeenCalled()
    expect(onCollapsedHeight).toHaveBeenLastCalledWith(MEASURED_H)
  })

  it('does not report for a re-render that keeps the same promptKey', () => {
    // The discriminating control: it is the identity that drives the re-report,
    // not any re-render of the card (which happens on every scroll frame).
    const onCollapsedHeight = vi.fn()
    const { rerenderWith } = renderCard({ onCollapsedHeight, promptKey: pinCandidateKey(3, 't-3') })
    onCollapsedHeight.mockClear()
    rerenderWith({ promptKey: pinCandidateKey(3, 't-3'), pushUp: 12 })
    expect(onCollapsedHeight).not.toHaveBeenCalled()
  })

  it('reports across a hand-off between prompts that carry no ts and share their text', () => {
    // Two image-only prompts: identical preview text, '' for `ts`, only the
    // index tells them apart — which is why the index is part of the key.
    const onCollapsedHeight = vi.fn()
    const { rerenderWith } = renderCard({
      onCollapsedHeight, images: ['/api/file-raw?a'], promptKey: pinCandidateKey(4, undefined),
    })
    onCollapsedHeight.mockClear()
    rerenderWith({ images: ['/api/file-raw?b'], promptKey: pinCandidateKey(6, undefined) })
    expect(onCollapsedHeight).toHaveBeenLastCalledWith(MEASURED_H)
  })
})
