import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import ChatDropOverlay, { useChatFileDrop } from '../components/ChatDropOverlay'

function fileTransfer(): DataTransfer {
  const file = new File(['hello'], 'hello.txt', { type: 'text/plain' })
  return {
    types: ['Files'],
    items: [{ kind: 'file', type: file.type, getAsFile: () => file }],
    files: [file],
    dropEffect: 'none',
  } as unknown as DataTransfer
}

function internalTransfer(): DataTransfer {
  return {
    types: ['application/x-kiro-session'],
    items: [],
    files: [],
    dropEffect: 'none',
  } as unknown as DataTransfer
}

function explorerDragTransfer(): DataTransfer {
  return {
    types: ['Files'],
    items: [],
    files: [],
    dropEffect: 'none',
  } as unknown as DataTransfer
}

function Harness({ onDrop }: { onDrop: (dataTransfer: DataTransfer) => void }) {
  const { active, dropTargetProps } = useChatFileDrop(onDrop)
  return (
    <div data-testid="drop-target" {...dropTargetProps}>
      <div data-testid="nested-child" />
      <ChatDropOverlay active={active} />
    </div>
  )
}

describe('ChatDropOverlay', () => {
  it('covers the pane for file drags without flickering across nested children', async () => {
    render(<Harness onDrop={vi.fn()} />)
    const target = screen.getByTestId('drop-target')
    const child = screen.getByTestId('nested-child')
    const dataTransfer = fileTransfer()

    fireEvent.dragEnter(target, { dataTransfer })
    expect(screen.getByTestId('chat-drop-overlay')).toHaveClass('pointer-events-none')
    expect(screen.getByText('Drop to attach')).toBeInTheDocument()

    fireEvent.dragEnter(child, { dataTransfer })
    fireEvent.dragLeave(child, { dataTransfer })
    expect(screen.getByTestId('chat-drop-overlay')).toBeInTheDocument()

    fireEvent.dragLeave(target, { dataTransfer })
    await waitFor(() => expect(screen.queryByTestId('chat-drop-overlay')).not.toBeInTheDocument())
  })

  it('dispatches one drop through the shared handler', () => {
    const onDrop = vi.fn()
    render(<Harness onDrop={onDrop} />)
    const target = screen.getByTestId('drop-target')
    const dataTransfer = fileTransfer()

    fireEvent.dragEnter(target, { dataTransfer })
    fireEvent.dragOver(target, { dataTransfer })
    fireEvent.drop(target, { dataTransfer })

    expect(onDrop).toHaveBeenCalledTimes(1)
    expect(onDrop.mock.calls[0][0].items[0].getAsFile()?.name).toBe('hello.txt')
  })

  it('clears a drag whose nested target disappears before dragleave', async () => {
    render(<Harness onDrop={vi.fn()} />)
    const dataTransfer = fileTransfer()

    fireEvent.dragEnter(screen.getByTestId('nested-child'), { dataTransfer })
    expect(screen.getByTestId('chat-drop-overlay')).toBeInTheDocument()

    fireEvent.dragEnd(window, { dataTransfer })
    await waitFor(() => expect(screen.queryByTestId('chat-drop-overlay')).not.toBeInTheDocument())
  })

  it('activates for a Windows Explorer drag before file items are exposed', () => {
    render(<Harness onDrop={vi.fn()} />)
    fireEvent.dragEnter(screen.getByTestId('drop-target'), {
      dataTransfer: explorerDragTransfer(),
    })

    expect(screen.getByTestId('chat-drop-overlay')).toBeInTheDocument()
  })

  it('ignores internal drags used by the session grid', () => {
    const onDrop = vi.fn()
    render(<Harness onDrop={onDrop} />)
    const target = screen.getByTestId('drop-target')
    const dataTransfer = internalTransfer()

    fireEvent.dragEnter(target, { dataTransfer })
    fireEvent.dragOver(target, { dataTransfer })
    fireEvent.drop(target, { dataTransfer })

    expect(screen.queryByTestId('chat-drop-overlay')).not.toBeInTheDocument()
    expect(onDrop).not.toHaveBeenCalled()
  })

  it('lets Escape cancel the current drag until it leaves or drops', async () => {
    const onDrop = vi.fn()
    render(<Harness onDrop={onDrop} />)
    const target = screen.getByTestId('drop-target')
    const dataTransfer = fileTransfer()

    fireEvent.dragEnter(target, { dataTransfer })
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('chat-drop-overlay')).not.toBeInTheDocument())

    fireEvent.dragOver(target, { dataTransfer })
    expect(screen.queryByTestId('chat-drop-overlay')).not.toBeInTheDocument()
    expect(dataTransfer.dropEffect).toBe('none')
    fireEvent.drop(target, { dataTransfer })
    expect(onDrop).not.toHaveBeenCalled()
  })
})
