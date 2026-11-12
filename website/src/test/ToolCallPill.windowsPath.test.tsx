import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { ToolCallPill } from '../app-sdk/messageRenderers'
import type { ChatMessage } from '../types'

const pillFor = (path: string): ChatMessage => ({
  role: 'tool',
  content: '🔧 read_file',
  meta: { input_preview: JSON.stringify({ path }), output: '' },
} as unknown as ChatMessage)

// The embedded transcript's tool pill names the file it opens.
describe('ToolCallPill file button', () => {
  it('shows only the file name of a Windows path and opens the full path', () => {
    const p = 'C:\\Users\\me\\repo\\notes.md'
    const onFileOpen = vi.fn()
    render(<ToolCallPill message={pillFor(p)} running={false} onFileOpen={onFileOpen} />)
    const button = screen.getByRole('button', { name: `Open ${p}` })
    expect(button.textContent).toBe('notes.md')
    fireEvent.click(button)
    expect(onFileOpen).toHaveBeenCalledWith(p)
  })

  it('keeps a POSIX file name that contains a backslash whole', () => {
    const p = '/tmp/we\\ird.md'
    render(<ToolCallPill message={pillFor(p)} running={false} onFileOpen={() => {}} />)
    expect(screen.getByRole('button', { name: `Open ${p}` }).textContent).toBe('we\\ird.md')
  })
})
