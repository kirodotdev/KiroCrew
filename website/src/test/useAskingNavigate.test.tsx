/**
 * `useAskingNavigate` asks the page on screen when it runs. A guard that shows
 * a question answers with a promise, so the navigate and anything staged after
 * it (`whenCreatedChatShown`) must wait for that answer instead of treating the
 * pending promise as a yes.
 */
import React from 'react'
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, act } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { NavigationLeaveGuardProvider, useAskingNavigate, useRegisterNavigationLeaveGuard } from '../components/NavigationLeaveGuard'
import { showCreatedChat, whenCreatedChatShown } from '../lib/createdChatView'

afterEach(cleanup)

function Page({ guard, onStaged }: { guard: () => boolean | Promise<boolean>; onStaged: () => void }) {
  useRegisterNavigationLeaveGuard(guard)
  const askingNavigate = useAskingNavigate()
  return (
    <button onClick={() => {
      const view = { shown: true, from: '/notes', slot: { key: 's1' } } as unknown as Parameters<typeof showCreatedChat>[0]
      whenCreatedChatShown(showCreatedChat(view, askingNavigate), onStaged)
    }}>create</button>
  )
}

function renderAt(guard: () => boolean | Promise<boolean>) {
  const staged: string[] = []
  render(
    <MemoryRouter initialEntries={['/notes']}>
      <NavigationLeaveGuardProvider>
        <Routes>
          <Route path="/notes" element={<Page guard={guard} onStaged={() => staged.push('staged')} />} />
          <Route path="/chat" element={<div>chat page</div>} />
        </Routes>
      </NavigationLeaveGuardProvider>
    </MemoryRouter>,
  )
  return staged
}

describe('useAskingNavigate', () => {
  it('navigates and stages in the same click when nothing is at stake', () => {
    const staged = renderAt(() => true)
    fireEvent.click(screen.getByRole('button', { name: 'create' }))
    expect(screen.getByText('chat page')).toBeInTheDocument()
    expect(staged).toEqual(['staged'])
  })

  it('waits for an open question and stays put when the user keeps the draft', async () => {
    let answer: (ok: boolean) => void = () => {}
    const staged = renderAt(() => new Promise<boolean>(resolve => { answer = resolve }))
    fireEvent.click(screen.getByRole('button', { name: 'create' }))
    expect(staged, 'a pending question was read as a yes').toEqual([])
    await act(async () => { answer(false) })
    expect(screen.getByRole('button', { name: 'create' })).toBeInTheDocument()
    expect(staged).toEqual([])
  })

  it('navigates and stages once the user agrees to leave', async () => {
    let answer: (ok: boolean) => void = () => {}
    const staged = renderAt(() => new Promise<boolean>(resolve => { answer = resolve }))
    fireEvent.click(screen.getByRole('button', { name: 'create' }))
    await act(async () => { answer(true) })
    expect(screen.getByText('chat page')).toBeInTheDocument()
    expect(staged).toEqual(['staged'])
  })
})
