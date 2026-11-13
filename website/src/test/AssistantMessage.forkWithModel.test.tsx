import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import AssistantMessage from '../pages/chat/AssistantMessage'

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
vi.mock('../hooks/useSmoothStream', () => ({
  useSmoothStream: (content: string) => content,
}))

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { act(() => { vi.runAllTimers() }); vi.useRealTimers() })

// Radix opens the More menu on POINTERDOWN, not click.
const openOverflow = () => fireEvent.pointerDown(
  screen.getByTitle('More actions'), { button: 0, ctrlKey: false, pointerType: 'mouse' },
)

describe('AssistantMessage fork with another model entry', () => {
  const menuProps = { content: 'x'.repeat(80), isStreaming: false, slotRunning: false }
  const stable = { forkIndex: 7, forkMessageId: 'row-42' }

  it('lists it after the fork entries, says what it keeps, and hands the fork point to its own handler', async () => {
    const onFork = vi.fn()
    const onForkWithModel = vi.fn()
    render(<AssistantMessage {...menuProps} {...stable} onFork={onFork} onForkWithModel={onForkWithModel} forkDirectionChoice />)
    openOverflow()
    const order = screen.getAllByRole('menuitem').map(i => i.getAttribute('data-testid')).filter(id => id?.startsWith('fork-'))
    expect(order).toEqual(['fork-from-here', 'fork-with-history', 'fork-with-model'])
    const entry = screen.getByTestId('fork-with-model')
    expect(entry).toHaveTextContent('Fork with another model')
    expect(entry).toHaveTextContent('Starts a new chat with this message and the ones before it.')
    await act(async () => { fireEvent.click(entry) })
    expect(onForkWithModel).toHaveBeenCalledWith(7, 'row-42')
    expect(onFork).not.toHaveBeenCalled()
  })

  it('is not offered before the fork point resolves', () => {
    render(<AssistantMessage {...menuProps} onFork={vi.fn()} onForkWithModel={vi.fn()} onLoadEarlier={vi.fn()} />)
    openOverflow()
    expect(screen.getByTestId('fork-from-here')).toBeInTheDocument()
    expect(screen.queryByTestId('fork-with-model')).toBeNull()
  })
})
