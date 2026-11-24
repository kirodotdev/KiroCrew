/**
 * Finalize-on-steer around an open construct.
 *
 * A steer bubble normally freezes the streaming message so the text after it
 * opens a fresh one below the bubble. A streaming message that ends inside an
 * open `<mcwidget>`, code fence or options marker stays live instead: the
 * gateway holds its cut until the construct closes, so the rest of the widget
 * keeps growing in the message above the bubble rather than landing below it
 * as raw markup.
 */
import { describe, it, expect } from 'vitest'
import reducer, { appendMessage, appendSlotMessage, sseChatMessage } from './chatSlice'
import type { ChatMessage } from '../types'

const SLOT = 'slot-a'
const WIDGET_HEAD = 'Here:\n<mcwidget title="t">\n<div>row 1</div>\n'
const WIDGET_TAIL = '<div>row 2</div>\n</mcwidget>\n'

let seq = 0
const chunk = (content: string) => sseChatMessage({ slot: SLOT, role: 'chunk', content, seq: ++seq } as never)
const segment = () => sseChatMessage({ slot: SLOT, role: '_segment' } as never)

function streaming(text: string) {
  seq = 0
  let state = reducer(undefined, { type: '@@INIT' })
  state = { ...state, activeSlot: SLOT }
  return reducer(state, chunk(text))
}

const steer = (sendId: string) => appendMessage({
  role: 'user', content: 'steer message', cls: 'msg msg-u',
  ts: '2026-01-01T00:00:00.000Z',
  meta: { steer: true, optimistic: true, sendId },
})

const shape = (msgs: ChatMessage[]) =>
  msgs.map(m => (m.role === 'user' ? 'steer' : `${m.role}:${m.content}`))

describe('finalize-on-steer with an open construct', () => {
  it('keeps an open widget streaming above the steer bubble until the gateway cuts', () => {
    let state = streaming(WIDGET_HEAD)
    state = reducer(state, steer('s1'))
    state = reducer(state, chunk(WIDGET_TAIL))
    expect(shape(state.messages)).toEqual(['streaming:' + WIDGET_HEAD + WIDGET_TAIL, 'steer'])

    state = reducer(state, segment())
    state = reducer(state, chunk('After the widget.'))
    expect(shape(state.messages)).toEqual([
      'assistant:' + WIDGET_HEAD + WIDGET_TAIL,
      'steer',
      'streaming:After the widget.',
    ])
  })

  it('keeps an open code fence streaming above the steer bubble', () => {
    let state = streaming('here:\n```python\nx = 1\n')
    state = reducer(state, steer('s1'))
    expect(shape(state.messages)).toEqual(['streaming:here:\n```python\nx = 1\n', 'steer'])
  })

  it('still freezes plain prose at the steer bubble', () => {
    let state = streaming('Before the steer.')
    state = reducer(state, steer('s1'))
    state = reducer(state, chunk('After the steer.'))
    expect(shape(state.messages)).toEqual([
      'assistant:Before the steer.',
      'steer',
      'streaming:After the steer.',
    ])
  })

  it('keeps an open widget live for a steer echoed from another tab', () => {
    let state = streaming(WIDGET_HEAD)
    state = reducer(state, appendSlotMessage({
      slot: SLOT,
      message: { role: 'user', content: 'steer message', cls: 'msg msg-u', meta: { steer: true, sendId: 'other' } },
    }))
    expect(shape(state.messages)).toEqual(['streaming:' + WIDGET_HEAD, 'steer'])
  })
})
