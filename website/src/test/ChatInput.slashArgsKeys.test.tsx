import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { useState } from 'react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

/* A `/command` followed by its arguments ("/btw note", "/plan the fix") shows
   no slash menu: the menu only matches a bare command name. The composer's
   picker gate must agree, or a menu nobody can see keeps owning Tab and
   Escape. Tab shows the gate: an open picker takes it (the keydown is
   prevented), a closed one leaves Tab to move focus. A bare Escape never
   stops a running turn either way; the Stop button does. */

function Host(props: Partial<React.ComponentProps<typeof ChatInput>>) {
  const [value, setValue] = useState('')
  return <ChatInput value={value} onChange={setValue} onSend={vi.fn()} connected={true} {...props} />
}

const textarea = () => screen.getByRole('textbox') as HTMLTextAreaElement
const typeInto = (value: string) => fireEvent.change(textarea(), { target: { value } })
/** True when the composer let Tab through (no picker took it). */
const tabPasses = () => fireEvent.keyDown(textarea(), { key: 'Tab', code: 'Tab' })

describe('ChatInput: slash text with arguments does not hold the picker gate', () => {
  it.each([
    ['a command with arguments', '/btw remember the flag'],
    // The second line is not a list line, so Tab there would not indent it.
    ['a multi-line slash draft', '/plan\nthe first step'],
    ['a slash draft the menu does not match (a capital)', '/Help'],
  ])('%s leaves Tab to move focus, and Escape does not stop the running turn', (_name, draft) => {
    const onStop = vi.fn()
    renderWithProviders(<Host isRunning onStop={onStop} />)
    typeInto(draft)
    expect(tabPasses()).toBe(true)
    fireEvent.keyDown(textarea(), { key: 'Escape', code: 'Escape' })
    expect(onStop).not.toHaveBeenCalled()
  })

  it.each([
    ['a bare command name', '/he'],
    ['a hyphenated command name', '/security-'],
    ['a lone slash', '/'],
  ])('NEGATIVE CONTROL: %s still holds the picker gate, and Escape does not stop the turn', (_name, draft) => {
    const onStop = vi.fn()
    renderWithProviders(<Host isRunning onStop={onStop} />)
    typeInto(draft)
    expect(tabPasses()).toBe(false)
    fireEvent.keyDown(textarea(), { key: 'Escape', code: 'Escape' })
    expect(onStop).not.toHaveBeenCalled()
  })
})
