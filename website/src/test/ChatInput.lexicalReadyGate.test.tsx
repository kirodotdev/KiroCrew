import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { useLayoutEffect, type MutableRefObject } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ComposerControl } from '../components/composerControl'
import ChatInput from '../components/ChatInput'
import { renderWithProviders } from './helpers'

const lexicalHarness = vi.hoisted(() => {
  let resolvePending = () => {}
  const state = {
    pending: Promise.resolve() as Promise<void>,
    control: null as ComposerControl | null,
    reset(control: ComposerControl) {
      state.control = control
      state.pending = new Promise<void>((resolve) => { resolvePending = resolve })
    },
    release() { resolvePending() },
  }
  return state
})

vi.mock('../components/LexicalComposerInput', () => ({
  default: function PendingLexicalComposer({
    controlRef,
    onReady,
  }: {
    controlRef: MutableRefObject<ComposerControl | null>
    onReady: () => void
  }) {
    if (lexicalHarness.pending) throw lexicalHarness.pending
    useLayoutEffect(() => {
      controlRef.current = lexicalHarness.control
      onReady()
      return () => { controlRef.current = null }
    }, [controlRef, onReady])
    return <div role="textbox" aria-label="Message input" data-lexical-composer="" />
  },
}))

const makeControl = (): ComposerControl => ({
  focus: vi.fn(),
  getRootElement: vi.fn(() => null),
  getSelection: vi.fn(() => null),
  replaceText: vi.fn(),
  setSelection: vi.fn(),
})

describe('ChatInput Lexical readiness gate', () => {
  beforeEach(() => {
    lexicalHarness.reset(makeControl())
  })

  it('disables Optimize and the + menu triggers until the Lexical control registers', async () => {
    renderWithProviders(
      <ChatInput
        value="draft"
        onChange={vi.fn()}
        onSend={vi.fn()}
        onUploadFiles={vi.fn()}
        onFileSelect={vi.fn()}
        connected
        lexicalComposer
      />,
    )

    expect(screen.getByRole('button', { name: 'Optimize prompt' })).toBeDisabled()
    fireEvent.click(screen.getByTitle('Add files & options'))
    expect(screen.getByTitle('Slash commands')).toBeDisabled()
    // `@` and `$` append the sigil through the same whole-value write as `/`;
    // gating only one of the three would leave the other two able to land a
    // host write under the still-loading editor.
    expect(screen.getByTitle('Reference a file')).toBeDisabled()
    expect(screen.getByTitle('Use a skill')).toBeDisabled()

    lexicalHarness.pending = null as never
    await act(async () => { lexicalHarness.release() })
    await waitFor(() => expect(screen.getByRole('textbox')).toHaveAttribute('data-lexical-composer'))
    expect(screen.getByRole('button', { name: 'Optimize prompt' })).not.toBeDisabled()
    expect(screen.getByTitle('Slash commands')).not.toBeDisabled()
    expect(screen.getByTitle('Reference a file')).not.toBeDisabled()
    expect(screen.getByTitle('Use a skill')).not.toBeDisabled()
  })
})
