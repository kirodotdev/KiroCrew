import { describe, it, expect, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'

import AssistantMessage from '../pages/chat/AssistantMessage'
import { setVoicePlaying, setVoicePreparing } from '../store/chatSlice'
import { createTestStore, renderWithProviders } from './helpers'

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
vi.mock('../hooks/useSmoothStream', () => ({
  useSmoothStream: (content: string) => content,
}))

const reply = 'A reply long enough to be read aloud.'

const openOverflow = () => fireEvent.pointerDown(
  screen.getByTitle('More actions'), { button: 0, ctrlKey: false, pointerType: 'mouse' },
)

function mountInStore(onSpeak = vi.fn()) {
  const store = createTestStore()
  renderWithProviders(
    <AssistantMessage content={reply} isStreaming={false} slotRunning={false} onSpeak={onSpeak} />,
    { store },
  )
  return { store, onSpeak }
}

describe('AssistantMessage Read aloud item', () => {
  it('reads "Read aloud" while nothing plays, and picking it reads this reply', () => {
    const { onSpeak } = mountInStore()
    openOverflow()
    const item = screen.getByTestId('speak-message')
    expect(item).toHaveTextContent('Read aloud')
    expect(item).toHaveAttribute('aria-description', 'Read message aloud')
    expect(item).not.toHaveAttribute('data-playing')
    fireEvent.click(item)
    expect(onSpeak).toHaveBeenCalledWith(reply)
  })

  it('reads "Stop reading" while audio plays, because picking it then stops the reading', () => {
    const { store, onSpeak } = mountInStore()
    act(() => { store.dispatch(setVoicePlaying(true)) })
    openOverflow()
    const item = screen.getByTestId('speak-message')
    expect(item).toHaveTextContent('Stop reading')
    expect(item).not.toHaveTextContent('Read aloud')
    expect(item).toHaveAttribute('data-playing', 'true')
    expect(item).not.toHaveAttribute('aria-description')
    // The pick still goes to the host's handler, which owns the stop.
    fireEvent.click(item)
    expect(onSpeak).toHaveBeenCalledWith(reply)
  })

  it('keeps "Read aloud" while audio is only being prepared, since a pick then starts this reply', () => {
    const { store } = mountInStore()
    act(() => { store.dispatch(setVoicePreparing(true)) })
    openOverflow()
    expect(screen.getByTestId('speak-message')).toHaveTextContent('Read aloud')
  })

  it('follows playback while the menu is open', () => {
    const { store } = mountInStore()
    openOverflow()
    expect(screen.getByTestId('speak-message')).toHaveTextContent('Read aloud')
    act(() => { store.dispatch(setVoicePlaying(true)) })
    expect(screen.getByTestId('speak-message')).toHaveTextContent('Stop reading')
    act(() => { store.dispatch(setVoicePlaying(false)) })
    expect(screen.getByTestId('speak-message')).toHaveTextContent('Read aloud')
  })

  it('renders without a store, as the app SDK embed does, and reads "Read aloud"', () => {
    render(<AssistantMessage content={reply} isStreaming={false} slotRunning={false} onSpeak={vi.fn()} />)
    openOverflow()
    expect(screen.getByTestId('speak-message')).toHaveTextContent('Read aloud')
  })
})
