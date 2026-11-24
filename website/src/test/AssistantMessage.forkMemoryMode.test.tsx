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
const forkEntries = () => screen.getAllByRole('menuitem').map(i => i.getAttribute('data-testid')).filter(id => id?.startsWith('fork-'))

describe('AssistantMessage private fork entries', () => {
  const menuProps = { content: 'x'.repeat(80), isStreaming: false, slotRunning: false }
  const stable = { forkIndex: 7, forkMessageId: 'row-42' }

  it.each([
    ['persistent', ['fork-as-incognito', 'fork-as-temporary']],
    [undefined, ['fork-as-incognito', 'fork-as-temporary']],
    ['incognito', ['fork-as-temporary']],
    ['temporary', []],
  ])('a %s chat offers only the stricter modes, after the other fork entries', (mode, offered) => {
    render(<AssistantMessage {...menuProps} {...stable} onFork={vi.fn()} onForkWithModel={vi.fn()} onForkWithMemoryMode={vi.fn()} forkSourceMemoryMode={mode} />)
    openOverflow()
    expect(forkEntries()).toEqual(['fork-from-here', 'fork-with-model', ...offered])
  })

  it('names the mode and hands the fork point and the mode to its handler', async () => {
    const onFork = vi.fn()
    const onForkWithMemoryMode = vi.fn()
    render(<AssistantMessage {...menuProps} {...stable} onFork={onFork} onForkWithMemoryMode={onForkWithMemoryMode} forkSourceMemoryMode="persistent" />)
    openOverflow()
    expect(screen.getByTestId('fork-as-incognito')).toHaveTextContent('Fork as incognito')
    const temporary = screen.getByTestId('fork-as-temporary')
    expect(temporary).toHaveTextContent('Fork as temporary')
    await act(async () => { fireEvent.click(temporary) })
    expect(onForkWithMemoryMode).toHaveBeenCalledWith(7, 'row-42', 'temporary')
    expect(onFork).not.toHaveBeenCalled()
  })

  it('is not offered without a handler or before the fork point resolves', () => {
    const { unmount } = render(<AssistantMessage {...menuProps} {...stable} onFork={vi.fn()} forkSourceMemoryMode="persistent" />)
    openOverflow()
    expect(screen.queryByTestId('fork-as-incognito')).toBeNull()
    unmount()
    render(<AssistantMessage {...menuProps} onFork={vi.fn()} onForkWithMemoryMode={vi.fn()} onLoadEarlier={vi.fn()} forkSourceMemoryMode="persistent" />)
    openOverflow()
    expect(screen.getByTestId('fork-from-here')).toBeInTheDocument()
    expect(screen.queryByTestId('fork-as-incognito')).toBeNull()
  })
})
