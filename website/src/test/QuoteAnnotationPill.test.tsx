import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import QuoteAnnotationPill from '../components/QuoteAnnotationPill'
import type { QuoteRef } from '../utils/quoteRefs'

const quote = (key: string, text: string = `text from ${key}`): QuoteRef => ({
  key,
  role: 'Assistant',
  time: '10:22',
  text,
  mid: `mid-${key}`,
  ts: `ts-${key}`,
})

describe('QuoteAnnotationPill', () => {
  it('renders nothing when no quote is staged', () => {
    const { container } = render(<QuoteAnnotationPill quotes={[]} />)
    expect(container.firstChild).toBeNull()
  })

  it('removes the selected row by its stable quote key', async () => {
    const user = userEvent.setup()
    const onRemove = vi.fn()
    render(
      <QuoteAnnotationPill
        quotes={[quote('first'), quote('second')]}
        onRemove={onRemove}
      />,
    )

    await user.click(screen.getByTestId('quote-annotation-pill'))
    await user.click(screen.getAllByTestId('quote-annotation-row-remove')[1])

    expect(onRemove).toHaveBeenCalledTimes(1)
    expect(onRemove).toHaveBeenCalledWith('second')
  })

  it('clears the complete collection from the resting pill', async () => {
    const user = userEvent.setup()
    const onClearAll = vi.fn()
    render(
      <QuoteAnnotationPill
        quotes={[quote('first'), quote('second')]}
        onClearAll={onClearAll}
      />,
    )

    await user.click(screen.getByTestId('quote-annotation-clear'))
    expect(onClearAll).toHaveBeenCalledTimes(1)
  })

  it('jumps using the quote belonging to the activated attribution', async () => {
    const user = userEvent.setup()
    const first = quote('first')
    const second = quote('second')
    const onJumpToSource = vi.fn()
    render(
      <QuoteAnnotationPill
        quotes={[first, second]}
        onJumpToSource={onJumpToSource}
      />,
    )

    await user.click(screen.getByTestId('quote-annotation-pill'))
    await user.click(screen.getAllByTestId('quote-annotation-jump')[1])

    expect(onJumpToSource).toHaveBeenCalledTimes(1)
    expect(onJumpToSource).toHaveBeenCalledWith(second)
  })

  it('Escape dismisses a pinned disclosure even while the trigger is hovered', async () => {
    const user = userEvent.setup()
    render(<QuoteAnnotationPill quotes={[quote('first')]} />)
    const pill = screen.getByTestId('quote-annotation-pill')

    await user.hover(pill)
    await user.click(pill)
    expect(screen.getByTestId('quote-annotation-popover')).toBeInTheDocument()

    await user.keyboard('{Escape}')
    expect(screen.queryByTestId('quote-annotation-popover')).not.toBeInTheDocument()
    expect(pill).toHaveFocus()
  })

  it('a second activation explicitly dismisses the disclosure while hovered', async () => {
    const user = userEvent.setup()
    render(<QuoteAnnotationPill quotes={[quote('first')]} />)
    const pill = screen.getByTestId('quote-annotation-pill')

    await user.hover(pill)
    await user.click(pill)
    expect(screen.getByTestId('quote-annotation-popover')).toBeInTheDocument()

    await user.click(pill)
    expect(screen.queryByTestId('quote-annotation-popover')).not.toBeInTheDocument()
    expect(pill).toHaveAttribute('aria-expanded', 'false')
  })

  it('does not retain pinned-open state after the quote collection empties', async () => {
    const user = userEvent.setup()
    const { rerender } = render(<QuoteAnnotationPill quotes={[quote('first')]} />)

    await user.click(screen.getByTestId('quote-annotation-pill'))
    expect(screen.getByTestId('quote-annotation-popover')).toBeInTheDocument()

    rerender(<QuoteAnnotationPill quotes={[]} />)
    expect(screen.queryByTestId('quote-annotation-pill')).not.toBeInTheDocument()

    rerender(<QuoteAnnotationPill quotes={[quote('replacement')]} />)
    expect(screen.queryByTestId('quote-annotation-popover')).not.toBeInTheDocument()
    expect(screen.getByTestId('quote-annotation-pill')).toHaveAttribute('aria-expanded', 'false')
  })
})
