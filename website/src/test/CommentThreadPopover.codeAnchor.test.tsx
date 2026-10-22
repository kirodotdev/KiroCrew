/**
 * A code thread's chip lives in Pierre's shadow DOM, out of reach of the
 * popover's `[data-mc-cid]` lookup. The popover must still anchor to it
 * live (so the card follows the chip on scroll), and a sidebar open with no
 * rect must scroll the owning code block into view and land on the chip once
 * it mounts, instead of sitting at the fallback spot.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { CommentThreadPopover } from '../components/CommentThreadPopover'
import { holdCodeBlock, holdCodeChip } from '../components/codeComments'
import type { ArtifactComment } from '../types'

const root: ArtifactComment = {
  id: 'r', origin: 'local', scope: 'private', author: 'alex', is_agent: false,
  body: 'a code thread', thread_id: 'r', status: 'open', sync_state: 'local_only',
  created_at: '2026-06-10T00:00:00Z', updated_at: '2026-06-10T00:00:00Z',
}

/** An element whose viewport box the test controls. */
function anchorAt(top: number, left = 40) {
  const el = document.createElement('div')
  document.body.append(el)
  const box = { top, left }
  el.getBoundingClientRect = () => ({ top: box.top, bottom: box.top + 16, left: box.left, right: box.left + 30, width: 30, height: 16, x: box.left, y: box.top, toJSON: () => ({}) })
  el.scrollIntoView = vi.fn()
  return { el, box }
}

function openPopover() {
  render(
    <CommentThreadPopover
      comments={[root]} rootId="r"
      onClose={vi.fn()} onReply={vi.fn()} onResolve={vi.fn()} onMarkReview={vi.fn()}
      onReopen={vi.fn()} onDelete={vi.fn()}
    />,
  )
  return screen.getByRole('dialog')
}

const releases: Array<() => void> = []
afterEach(() => {
  releases.splice(0).forEach(r => r())
  document.body.replaceChildren()
})

describe('CommentThreadPopover code-thread anchoring', () => {
  it('anchors under the code chip and follows it on scroll', () => {
    const chip = anchorAt(100)
    releases.push(holdCodeChip('r', chip.el))
    const card = openPopover()
    expect(card.style.top).toBe('124px') // chip bottom 116 + 8px gap
    chip.box.top = 50
    act(() => { fireEvent.scroll(window) })
    expect(card.style.top).toBe('74px')
  })

  it('scrolls the owning block into view, then moves onto the chip once it mounts', () => {
    const block = anchorAt(200)
    releases.push(holdCodeBlock('r', block.el))
    const card = openPopover()
    expect(block.el.scrollIntoView).toHaveBeenCalled()
    expect(card.style.top).toBe('224px')
    const chip = anchorAt(300)
    act(() => { releases.push(holdCodeChip('r', chip.el)) })
    expect(card.style.top).toBe('324px')
  })
})
