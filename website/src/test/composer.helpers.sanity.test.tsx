import { describe, it, expect, vi } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { useState } from 'react'
import ChatInput from '../components/ChatInput'
import { renderWithProviders, composerValue, setComposerValue, typeIntoComposer, pressInComposer, composerPlaceholder, pasteIntoComposer, composerSelection } from './helpers'

function Host({ onSend }: { onSend?: (v: string) => void }) {
  const [v, setV] = useState('')
  return <ChatInput lexicalComposer value={v} onChange={setV} onSend={() => onSend?.(v)} pasteBlocks={[]} onPasteBlocksChange={() => {}} connected placeholder="Type here…" />
}

describe('composer test drivers', () => {
  it('set / type / read / send / placeholder work through the real editor', async () => {
    const onSend = vi.fn()
    renderWithProviders(<Host onSend={onSend} />)
    await waitFor(() => expect(document.querySelector('[data-composer-input]')).not.toBeNull())
    await act(async () => {})
    expect(composerPlaceholder()).toBe('Type here…')
    await setComposerValue('hello')
    expect(composerValue()).toBe('hello')
    expect(composerPlaceholder()).toBeNull()
    await typeIntoComposer(' world')
    expect(composerValue()).toBe('hello world')
    expect(composerSelection()).toEqual({ start: 11, end: 11 })
    await setComposerValue('replaced')
    expect(composerValue()).toBe('replaced')
    pressInComposer('Enter')
    await act(async () => {})
    expect(onSend).toHaveBeenCalledWith('replaced')
    await pasteIntoComposer('a\nb\nc\nd\ne\nf')
    expect(composerValue()).toMatch(/\[ Paste #1 · 6 lines \]/)
  })
})
