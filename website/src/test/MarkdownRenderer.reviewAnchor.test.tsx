import { beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render } from '@testing-library/react'
import { ReviewSurfaceProvider, SlotProvider } from '../providers/SlotContext'

const hoisted = vi.hoisted(() => ({ anchors: [] as Array<string | undefined> }))

vi.mock('../components/DiffBlock', () => ({
  default: ({ reviewAnchor }: { reviewAnchor?: string }) => {
    hoisted.anchors.push(reviewAnchor)
    return <div data-testid="diff-block" />
  },
}))

import MarkdownRenderer from '../components/MarkdownRenderer'

const CONTENT = [
  'Changed file:',
  '',
  '```diff',
  '--- a/src/Thing.ts',
  '+++ b/src/Thing.ts',
  '@@ -1,1 +1,1 @@',
  '-old',
  '+new',
  '```',
].join('\n')

beforeEach(() => {
  cleanup()
  hoisted.anchors.length = 0
})

describe('MarkdownRenderer review anchor handoff', () => {
  it('separates identical fences in messages that omit the slotKey prop', async () => {
    render(
      <SlotProvider slotId="context-slot">
        <ReviewSurfaceProvider>
          <MarkdownRenderer content={CONTENT} messageTs="message-a" />
          <MarkdownRenderer content={CONTENT} messageTs="message-b" />
        </ReviewSurfaceProvider>
      </SlotProvider>,
    )

    await vi.waitFor(() => expect(hoisted.anchors.length).toBeGreaterThanOrEqual(2))
    expect(new Set(hoisted.anchors)).toEqual(new Set(['message-a:4', 'message-b:4']))
  })

  it('gives a fence no anchor when its message has an empty time', async () => {
    render(
      <SlotProvider slotId="context-slot">
        <ReviewSurfaceProvider>
          <MarkdownRenderer content={CONTENT} messageTs="" />
        </ReviewSurfaceProvider>
      </SlotProvider>,
    )

    await vi.waitFor(() => expect(hoisted.anchors.length).toBeGreaterThanOrEqual(1))
    expect(new Set(hoisted.anchors)).toEqual(new Set([undefined]))
  })
})
