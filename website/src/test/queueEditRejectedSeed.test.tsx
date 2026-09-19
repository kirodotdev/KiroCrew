/** A rejected queue edit must come back seeded, not thrown away.
 *
 *  `commitEdit` closes the input BEFORE the request resolves, so when the failure landed the typed
 *  text was gone from the screen and the banner carried only the reason -- the user retyped work the
 *  server never took.
 */
import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import QueueStack from '../components/QueueStack'
import type { ChatMessage } from '../types'

const QID = 'q-rejected'
const CARD = 'the original queued text'
const TYPED = 'the text the user typed'

const queued = (queueId: string, content: string): ChatMessage =>
  ({ role: 'queued', content, cls: 'msg msg-queued', ts: '', meta: { queueId } }) as ChatMessage

const draw = (editRejected?: Record<string, string>) =>
  render(
    <QueueStack
      messages={[queued(QID, CARD)]}
      onEdit={vi.fn()}
      editError={editRejected ? 'Couldn\u2019t save the edit: rejected outright' : null}
      editRejected={editRejected}
    />,
  )

const box = (c: HTMLElement) => c.querySelector('textarea') as HTMLTextAreaElement | null

describe('a rejected queue edit reopens its input seeded with the typed text', () => {
  it('reopens the editor for the card the edit was rejected on', () => {
    const { container } = draw({ [QID]: TYPED })

    expect(box(container), 'the input must come back, or the text has nowhere to live').not.toBeNull()
    expect(box(container)?.value,
      'seeded with what the user typed, not the text the card was rolled back to').toBe(TYPED)
  })

  it('draws no editor when nothing was rejected', () => {
    // Positive control: an editor that opened unprompted would hijack the queue on every render.
    const { container } = draw(undefined)

    expect(box(container)).toBeNull()
  })

  it('ignores a rejection naming a card that has left the queue', () => {
    // Second positive control: the entry can drain while the failure is in flight, and seeding a
    // card that is gone would open an editor over an unrelated row.
    const { container } = render(
      <QueueStack
        messages={[queued(QID, CARD)]}
        onEdit={vi.fn()}
        editError="Couldn&#39;t save the edit: rejected outright"
        editRejected={{ 'q-drained': TYPED }}
      />,
    )

    expect(box(container)).toBeNull()
  })
})
