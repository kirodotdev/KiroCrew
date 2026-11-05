import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { SlotProvider } from '../providers/SlotContext'

// The running reply's project comes from the server (`turn_project` on the
// slot) once the turn has claimed its session. A reload or reconnect mounts the
// composer with `project` already on a mid-reply pick, so a snapshot taken from
// that prop would name the pick instead of the directory the reply runs in.

const A = '/home/u/work/RepoA'
const B = '/home/u/work/RepoB'

const props = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
  onProjectClick: vi.fn(),
  onStop: vi.fn(),
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

const chip = () => screen.getByRole('button', { name: /Project: |Select project/ })
const title = () => chip().getAttribute('title') || ''

describe('ChatInput running-reply project from the server', () => {
  it('names the turn project after a remount onto a mid-reply pick', () => {
    // Start in A, pick B, reload: the composer mounts with project=B.
    renderWithProviders(<ChatInput {...props} project={B} turnProject={A} isRunning />)
    expect(chip()).toHaveTextContent('RepoB')
    expect(title()).toContain(`Project: ${B}`)
    expect(title()).toContain('This reply finishes in RepoA.')
    expect(chip().getAttribute('aria-label')).toBe(title())
    // The staged pick still reads as pending: the reply runs in A, not B.
    expect(screen.getByTestId('composer-project-pending')).toBeInTheDocument()
  })

  it('takes the turn project when it arrives after mount, as on a reconnect', () => {
    const { rerender } = renderWithProviders(<ChatInput {...props} project={B} isRunning />)
    rerender(<ChatInput {...props} project={B} turnProject={A} isRunning />)
    expect(title()).toContain('This reply finishes in RepoA.')
  })

  it('reports B for a fresh turn started in B', () => {
    renderWithProviders(<ChatInput {...props} project={B} turnProject={B} isRunning />)
    expect(title()).toContain('This reply finishes in RepoB.')
    expect(screen.queryByTestId('composer-project-pending')).toBeNull()
  })

  it('names each running chat its own turn project when the composer moves between them', () => {
    const inSlot = (slot: string, project: string, turnProject: string) => (
      <SlotProvider slotId={slot}>
        <ChatInput {...props} project={project} turnProject={turnProject} isRunning />
      </SlotProvider>
    )
    const { rerender } = renderWithProviders(inSlot('chat-a', B, A))
    expect(title()).toContain('This reply finishes in RepoA.')
    // Chat b also has a staged pick (A), but its process runs in B.
    rerender(inSlot('chat-b', A, B))
    expect(title()).toContain('This reply finishes in RepoB.')
  })

  it('keeps the start-of-response snapshot while the server has no reading', () => {
    const { rerender } = renderWithProviders(<ChatInput {...props} project={A} turnProject={null} isRunning />)
    rerender(<ChatInput {...props} project={B} turnProject={null} isRunning />)
    expect(title()).toContain('This reply finishes in RepoA.')
  })
})
