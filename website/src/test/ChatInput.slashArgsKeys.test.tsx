import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { useState } from 'react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

/* A `/command` followed by its arguments ("/btw note", "/plan the fix") shows
   no slash menu: the menu only matches a bare command name. The composer's
   picker gate must agree, or a menu nobody can see keeps owning Escape and
   Tab, and Escape-to-stop does nothing at all. */

function Host(props: Partial<React.ComponentProps<typeof ChatInput>>) {
  const [value, setValue] = useState('')
  return <ChatInput value={value} onChange={setValue} onSend={vi.fn()} connected={true} {...props} />
}

const textarea = () => screen.getByRole('textbox') as HTMLTextAreaElement
const typeInto = (value: string) => fireEvent.change(textarea(), { target: { value } })

describe('ChatInput: slash text with arguments does not hold the picker gate', () => {
  it('Escape stops the running turn while the draft is a command with arguments', () => {
    const onStop = vi.fn()
    renderWithProviders(<Host isRunning onStop={onStop} />)
    typeInto('/btw remember the flag')
    fireEvent.keyDown(textarea(), { key: 'Escape', code: 'Escape' })
    expect(onStop).toHaveBeenCalledTimes(1)
  })

  it('Escape stops the turn for a multi-line slash draft', () => {
    const onStop = vi.fn()
    renderWithProviders(<Host isRunning onStop={onStop} />)
    typeInto('/plan\n- first step')
    fireEvent.keyDown(textarea(), { key: 'Escape', code: 'Escape' })
    expect(onStop).toHaveBeenCalledTimes(1)
  })

  it('Escape stops the turn for a slash draft the menu does not match (a capital)', () => {
    const onStop = vi.fn()
    renderWithProviders(<Host isRunning onStop={onStop} />)
    typeInto('/Help')
    fireEvent.keyDown(textarea(), { key: 'Escape', code: 'Escape' })
    expect(onStop).toHaveBeenCalledTimes(1)
  })

  it('NEGATIVE CONTROL: a bare command name still owns Escape (the menu closes, the turn runs on)', () => {
    const onStop = vi.fn()
    renderWithProviders(<Host isRunning onStop={onStop} />)
    typeInto('/he')
    fireEvent.keyDown(textarea(), { key: 'Escape', code: 'Escape' })
    expect(onStop).not.toHaveBeenCalled()
  })

  it('NEGATIVE CONTROL: a hyphenated command name still owns Escape', () => {
    const onStop = vi.fn()
    renderWithProviders(<Host isRunning onStop={onStop} />)
    typeInto('/security-')
    fireEvent.keyDown(textarea(), { key: 'Escape', code: 'Escape' })
    expect(onStop).not.toHaveBeenCalled()
  })

  it('NEGATIVE CONTROL: a lone slash still owns Escape', () => {
    const onStop = vi.fn()
    renderWithProviders(<Host isRunning onStop={onStop} />)
    typeInto('/')
    fireEvent.keyDown(textarea(), { key: 'Escape', code: 'Escape' })
    expect(onStop).not.toHaveBeenCalled()
  })
})
