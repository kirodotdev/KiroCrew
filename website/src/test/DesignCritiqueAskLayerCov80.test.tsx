import { createRef } from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import AskLayer from '../apps/design-critique/AskLayer'
import type { Ask, Sel } from '../apps/design-critique/types'

/**
 * "Ask about this": a floating chip over a text selection, then a popover that
 * holds one annotation's thread. The pending guard matters — turns share one
 * agent slot, so a second question sent while one is in flight was silently
 * dropped; the input and the send control must both refuse instead.
 */
const SEL: Sel = { quote: 'zzz quoted text', top: 200, left: 400 }

function ask(overrides: Partial<Ask> = {}): Ask {
  return {
    id: 'zzz-ask-1',
    quote: 'zzz anchored quote',
    turns: [],
    ...overrides,
  }
}

function mount(overrides: Partial<Parameters<typeof AskLayer>[0]> = {}) {
  const props = {
    sel: null as Sel | null,
    asks: [] as Ask[],
    openAskId: null as string | null,
    askDraft: '',
    reduceMotion: true,
    threadRef: createRef<HTMLDivElement>(),
    setOpenAskId: vi.fn(),
    setSel: vi.fn(),
    setAskDraft: vi.fn(),
    askAbout: vi.fn(),
    askFollowUp: vi.fn(),
    pending: false,
    removeAsk: vi.fn(),
    ...overrides,
  }
  return { props, ...render(<AskLayer {...props} />) }
}

describe('AskLayer chip', () => {
  it('renders nothing at all with no selection and no open annotation', () => {
    const { container } = mount()
    expect(container).toBeEmptyDOMElement()
  })

  it('offers the chip over the selection and opens the composer on click', async () => {
    const { props } = mount({ sel: SEL })
    const chip = screen.getByRole('button', { name: /Ask about this/i })
    expect(chip).toHaveStyle({ top: '200px', left: '400px' })

    await userEvent.click(chip)
    expect(props.setOpenAskId).toHaveBeenCalledWith('new')
    expect(props.setAskDraft).toHaveBeenCalledWith('')
  })

  it('does not preventDefault away the selection on mousedown', () => {
    mount({ sel: SEL })
    const chip = screen.getByRole('button', { name: /Ask about this/i })
    // The handler exists precisely so the browser keeps the selection alive.
    expect(fireEvent.mouseDown(chip)).toBe(false)
  })
})

describe('AskLayer composing a new question', () => {
  it('quotes the selection and sends the draft on Enter', () => {
    const { props } = mount({ sel: SEL, openAskId: 'new', askDraft: 'zzz why is this' })
    expect(screen.getByText(/zzz quoted text/)).toBeInTheDocument()
    // No thread yet, so no delete affordance for a saved annotation.
    expect(screen.queryByRole('button', { name: /Remove this annotation/i })).not.toBeInTheDocument()

    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' })
    expect(props.askAbout).toHaveBeenCalledWith('zzz quoted text', 'zzz why is this')
  })

  it('sends the draft from the send control too', async () => {
    const { props } = mount({ sel: SEL, openAskId: 'new', askDraft: 'zzz question' })
    await userEvent.click(screen.getByRole('button', { name: /^Ask$/i }))
    expect(props.askAbout).toHaveBeenCalledWith('zzz quoted text', 'zzz question')
  })

  it('closes on Escape and on the close control, dropping the selection', () => {
    const { props } = mount({ sel: SEL, openAskId: 'new' })
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Escape' })
    expect(props.setOpenAskId).toHaveBeenCalledWith(null)
    expect(props.setSel).toHaveBeenCalledWith(null)

    props.setOpenAskId.mockClear()
    fireEvent.click(screen.getByRole('button', { name: /Close/i }))
    expect(props.setOpenAskId).toHaveBeenCalledWith(null)
  })

  it('reports typing to the owner', async () => {
    const { props } = mount({ sel: SEL, openAskId: 'new' })
    await userEvent.type(screen.getByRole('textbox'), 'z')
    expect(props.setAskDraft).toHaveBeenCalled()
  })
})

describe('AskLayer an existing annotation', () => {
  const answered = ask({
    turns: [
      { t: 1, q: 'zzz first question', a: 'zzz first answer', pending: false },
      { t: 2, q: 'zzz second question', a: '', pending: true },
    ],
  })

  it('renders the thread, its quote, and the pending turn', () => {
    mount({ asks: [answered], openAskId: 'zzz-ask-1' })
    expect(screen.getByText(/zzz anchored quote/)).toBeInTheDocument()
    expect(screen.getByText('zzz first question')).toBeInTheDocument()
    expect(screen.getByText('zzz first answer')).toBeInTheDocument()
    expect(screen.getByText(/Thinking/i)).toBeInTheDocument()
  })

  it('renders a failed turn as an answer rather than swallowing it', () => {
    const failed = ask({
      turns: [{ t: 1, q: 'zzz q', a: 'zzz it broke', pending: false, failed: true }],
    })
    mount({ asks: [failed], openAskId: 'zzz-ask-1' })
    // The failure text is the answer: a failed turn must still be readable in
    // the thread, not replaced by a spinner or dropped.
    expect(screen.getByText('zzz it broke')).toBeInTheDocument()
    expect(screen.queryByText(/Thinking/i)).not.toBeInTheDocument()
  })

  it('sends a follow-up rather than a new annotation', async () => {
    const { props } = mount({
      asks: [ask({ turns: [{ t: 1, q: 'zzz q', a: 'zzz a', pending: false }] })],
      openAskId: 'zzz-ask-1',
      askDraft: 'zzz follow up',
    })
    await userEvent.click(screen.getByRole('button', { name: /^Ask$/i }))
    expect(props.askFollowUp).toHaveBeenCalledWith('zzz-ask-1', 'zzz follow up')
    expect(props.askAbout).not.toHaveBeenCalled()
  })

  it('removes the annotation on request', async () => {
    const { props } = mount({ asks: [answered], openAskId: 'zzz-ask-1' })
    await userEvent.click(screen.getByRole('button', { name: /Remove this annotation/i }))
    expect(props.removeAsk).toHaveBeenCalledWith('zzz-ask-1')
  })

  it('renders an annotation with no turns yet without a thread block', () => {
    mount({ asks: [ask()], openAskId: 'zzz-ask-1' })
    expect(screen.getByRole('textbox')).toBeInTheDocument()
    expect(screen.queryByText(/Thinking/i)).not.toBeInTheDocument()
  })
})

describe('AskLayer pending guard', () => {
  it('refuses a second question while one is in flight', async () => {
    const { props } = mount({
      asks: [ask({ turns: [{ t: 1, q: 'zzz q', a: '', pending: true }] })],
      openAskId: 'zzz-ask-1',
      askDraft: 'zzz second',
      pending: true,
    })
    const input = screen.getByRole('textbox')
    expect(input).toBeDisabled()

    fireEvent.keyDown(input, { key: 'Enter' })
    await userEvent.click(screen.getByRole('button', { name: /^Ask$/i }))
    expect(props.askFollowUp).not.toHaveBeenCalled()
    expect(props.askAbout).not.toHaveBeenCalled()
  })
})
