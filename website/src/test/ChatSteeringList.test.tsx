/**
 * "Steering in this chat", under the Context breakdown tab: one list item per
 * steering file, each saying where the file comes from, when it applies, and
 * whether it reaches this chat, as text a screen reader reads too.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { act, render, screen, cleanup, within } from '@testing-library/react'

import { ApiError } from '../api/apiError'
import type { ChatSteering } from '../api/client/chatSlotSettings'
import { ChatSteeringList, ChatSteeringListView } from '../pages/ChatSteeringList'
import { sseSlots, updateSlot } from '../store/dashboardSlice'
import { createTestStore, renderWithProviders } from './helpers'

const steeringMock = vi.fn()
const switchMock = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    chatSlotSteering: (slot: string) => steeringMock(slot),
    chatSlotSteeringSwitch: (slot: string, path: string, off: boolean) => switchMock(slot, path, off),
  },
}))

afterEach(() => {
  cleanup()
  steeringMock.mockReset()
  switchMock.mockReset()
})

const data: ChatSteering = {
  provider: 'codex',
  files: [
    { name: 'style.md', path: '~/.kiro/steering/style.md', source: 'global', applies: 'always', reaches: 'no', reason: 'harness_has_no_path' },
    { name: 'own.md', path: '~/standards/own.md', source: 'folder', applies: 'always', reaches: 'yes', reason: '' },
    { name: 'review.md', path: '~/p/.kiro/steering/review.md', source: 'project', applies: 'manual', reaches: 'harness', reason: 'harness_native' },
  ],
}

describe('ChatSteeringListView', () => {
  it('names the section and lists one item per file', () => {
    render(<ChatSteeringListView data={data} />)
    const section = screen.getByRole('region', { name: 'Steering in this chat' })
    const items = within(section).getAllByRole('listitem')
    expect(items.map(item => within(item).getByText(/\.md$/).textContent)).toEqual(['style.md', 'own.md', 'review.md'])
  })

  it('says plainly when a file does not reach the chat, and why', () => {
    render(<ChatSteeringListView data={data} />)
    const [style, own, review] = screen.getAllByRole('listitem')
    expect(style).toHaveTextContent('Global · Always applies')
    expect(style).toHaveTextContent("Not included: this chat's agent harness doesn't load global or project steering")
    expect(own).toHaveTextContent('Folder · Always applies')
    expect(own).toHaveTextContent('Included in this chat')
    expect(review).toHaveTextContent('Project · Applies when asked for')
    expect(review).toHaveTextContent('Loaded by the agent harness')
  })

  it('shows an empty state instead of an empty list', () => {
    render(<ChatSteeringListView data={{ provider: 'kas', files: [] }} />)
    expect(screen.queryByRole('list')).toBeNull()
    expect(screen.getByText('No steering files apply to this chat.')).toBeInTheDocument()
  })
})

describe('ChatSteeringList', () => {
  it('reads the list again when the chat moves to another project', async () => {
    steeringMock.mockResolvedValue({ provider: 'codex', files: [] })
    const store = createTestStore()
    store.dispatch(sseSlots([{ key: 'chat-1', messages: 0, running: false, project: '/p/one' }] as never))
    renderWithProviders(<ChatSteeringList slot="chat-1" />, {
      store,
      queryDefaults: { staleTime: Infinity },
    })
    await screen.findByText('No steering files apply to this chat.')
    expect(steeringMock).toHaveBeenCalledTimes(1)
    act(() => {
      store.dispatch(updateSlot({ key: 'chat-1', project: '/p/two' }))
    })
    await vi.waitFor(() => expect(steeringMock).toHaveBeenCalledTimes(2))
  })
})

describe('switching a file off for this chat', () => {
  const crewWritten = { name: 'style.md', path: '~/.kiro/steering/style.md', source: 'global' as const, applies: 'always' as const }

  it('switches a file Crew writes off, and the row says so', async () => {
    const off = { provider: 'claude_code', files: [{ ...crewWritten, reaches: 'no', reason: 'switched_off', switch: 'off' }] }
    steeringMock
      .mockResolvedValueOnce({ provider: 'claude_code', files: [{ ...crewWritten, reaches: 'yes', reason: '', switch: 'on' }] })
      .mockResolvedValue(off)
    switchMock.mockResolvedValue(off)
    renderWithProviders(<ChatSteeringList slot="chat-1" />)
    const toggle = await screen.findByRole('switch', { name: 'Use style.md in this chat' })
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    act(() => toggle.click())
    expect(switchMock).toHaveBeenCalledWith('chat-1', '~/.kiro/steering/style.md', true)
    await screen.findByText('Switched off for this chat')
    expect(screen.getByRole('switch', { name: 'Use style.md in this chat' })).toHaveAttribute('aria-checked', 'false')
  })

  it('reads the shown list again after a save, in case it was re-keyed meanwhile', async () => {
    const off = { provider: 'claude_code', files: [{ ...crewWritten, reaches: 'no', reason: 'switched_off', switch: 'off' }] }
    steeringMock
      .mockResolvedValueOnce({ provider: 'claude_code', files: [{ ...crewWritten, reaches: 'yes', reason: '', switch: 'on' }] })
      .mockResolvedValue(off)
    switchMock.mockResolvedValue(off)
    renderWithProviders(<ChatSteeringList slot="chat-1" />)
    const toggle = await screen.findByRole('switch', { name: 'Use style.md in this chat' })
    expect(steeringMock).toHaveBeenCalledTimes(1)
    act(() => toggle.click())
    await vi.waitFor(() => expect(switchMock).toHaveBeenCalledTimes(1))
    await vi.waitFor(() => expect(steeringMock).toHaveBeenCalledTimes(2))
  })

  it('says in plain words why a switch failed, and reads the list again when it moved', async () => {
    steeringMock.mockResolvedValue({ provider: 'claude_code', files: [{ ...crewWritten, reaches: 'yes', reason: '', switch: 'on' }] })
    const body = JSON.stringify({ error: 'raw server text', code: 'session_changed' })
    switchMock.mockRejectedValue(new ApiError(409, 'raw server text', body))
    renderWithProviders(<ChatSteeringList slot="chat-1" />)
    const toggle = await screen.findByRole('switch', { name: 'Use style.md in this chat' })
    expect(steeringMock).toHaveBeenCalledTimes(1)
    act(() => toggle.click())
    await screen.findByText(
      'This chat moved to another session while saving, so nothing changed. The list is up to date now. Try again.',
    )
    expect(screen.queryByText('raw server text')).toBeNull()
    await vi.waitFor(() => expect(steeringMock).toHaveBeenCalledTimes(2))
    expect(screen.getByRole('switch', { name: 'Use style.md in this chat' })).toHaveAttribute('aria-checked', 'true')
  })

  it('reads the list again after a failed save, which may still have applied', async () => {
    steeringMock
      .mockResolvedValueOnce({ provider: 'claude_code', files: [{ ...crewWritten, reaches: 'yes', reason: '', switch: 'on' }] })
      .mockResolvedValue({ provider: 'claude_code', files: [{ ...crewWritten, reaches: 'no', reason: 'switched_off', switch: 'off' }] })
    const body = JSON.stringify({ error: 'raw server text', code: 'write_failed' })
    switchMock.mockRejectedValue(new ApiError(500, 'raw server text', body))
    renderWithProviders(<ChatSteeringList slot="chat-1" />)
    const toggle = await screen.findByRole('switch', { name: 'Use style.md in this chat' })
    act(() => toggle.click())
    await screen.findByText('The switch may not have been saved. The list now shows what is stored.')
    await screen.findByText('Switched off for this chat')
    expect(steeringMock).toHaveBeenCalledTimes(2)
  })

  it('reads the list again when the reply is lost, since the save may have applied', async () => {
    steeringMock
      .mockResolvedValueOnce({ provider: 'claude_code', files: [{ ...crewWritten, reaches: 'yes', reason: '', switch: 'on' }] })
      .mockResolvedValue({ provider: 'claude_code', files: [{ ...crewWritten, reaches: 'no', reason: 'switched_off', switch: 'off' }] })
    switchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    renderWithProviders(<ChatSteeringList slot="chat-1" />)
    const toggle = await screen.findByRole('switch', { name: 'Use style.md in this chat' })
    act(() => toggle.click())
    await screen.findByText('Switched off for this chat')
    expect(steeringMock).toHaveBeenCalledTimes(2)
  })

  it('offers no switch where the harness loads the file, and says why', () => {
    render(<ChatSteeringListView
      data={{ provider: 'kas', files: [{ ...crewWritten, reaches: 'harness', reason: 'harness_native', switch: 'unavailable' }] }}
      onSwitch={() => {}}
    />)
    expect(screen.queryByRole('switch')).toBeNull()
    expect(screen.getByText("Can't be switched off here: the agent harness loads this file itself")).toBeInTheDocument()
  })
})
