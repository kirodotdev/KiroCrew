/**
 * The detail page's agent tools card: one switch per approved or revoked third-party
 * tool, and none for a tool an update holds back (the approval notice offers that).
 * Off sends the revoke on the owner-only enable route; on asks first, then approves
 * only the tool the card showed.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import AppDetailAgentTools from '../pages/AppDetailAgentTools'
import { confirmText, stubAppConfirm } from './appConfirmStub'

const NOTE = { name: 'tunes_note', description: 'Post one note.', entry: 'tunes_note@aaaaaaaaaaaa' }
const LIST = { name: 'tunes_list', description: 'List the notes.', entry: 'tunes_list@bbbbbbbbbbbb' }
const TAG = { name: 'tunes_tag', description: 'Tag a note.', entry: 'tunes_tag@cccccccccccc' }
const GRANT = { approved: [NOTE], staged: [TAG], revoked: [LIST] }

let posts: { path: string; body: unknown }[]

beforeEach(() => {
  posts = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    posts.push({ path: new URL(String(input), 'http://localhost').pathname, body: JSON.parse(String(init?.body ?? 'null')) })
    return new Response(JSON.stringify({ ok: true, name: 'tunes' }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

const mount = (enabled = true, onChanged = vi.fn()) => {
  render(<AppDetailAgentTools appName="tunes" enabled={enabled} grant={GRANT} onChanged={onChanged} />)
  return onChanged
}

describe('AppDetailAgentTools', () => {
  it('renders a switch for the approved and revoked tools only', () => {
    mount()
    expect(screen.getByRole('switch', { name: 'tunes_note' }).getAttribute('aria-checked')).toBe('true')
    expect(screen.getByRole('switch', { name: 'tunes_list' }).getAttribute('aria-checked')).toBe('false')
    // Held back by an update: the approval notice offers it, not a switch.
    expect(screen.queryByRole('switch', { name: 'tunes_tag' })).toBeNull()
    expect(screen.queryByText('tunes_tag')).toBeNull()
  })

  it('renders nothing without a declared tool', () => {
    const { container } = render(<AppDetailAgentTools appName="tunes" enabled grant={undefined} />)
    expect(container.innerHTML).toBe('')
  })

  it('turning a tool off revokes only that entry', async () => {
    const ask = stubAppConfirm(true)
    const changed = mount()
    fireEvent.click(screen.getByRole('switch', { name: 'tunes_note' }))
    await waitFor(() => expect(changed).toHaveBeenCalled())
    expect(ask).not.toHaveBeenCalled()
    expect(posts).toEqual([{ path: '/api/apps/tunes/enable', body: { agentToolsRevoke: [NOTE.entry] } }])
  })

  it('turning a tool on asks first, then approves only that entry', async () => {
    const ask = stubAppConfirm(true)
    const changed = mount()
    fireEvent.click(screen.getByRole('switch', { name: 'tunes_list' }))
    await waitFor(() => expect(changed).toHaveBeenCalled())
    expect(ask).toHaveBeenCalledTimes(1)
    expect(confirmText(ask.mock.calls[0][0])).toContain('tunes_list')
    expect(confirmText(ask.mock.calls[0][0])).toContain('List the notes.')
    expect(posts).toHaveLength(1)
    const body = posts[0].body as { grantsConsent: Record<string, string[]> }
    expect(posts[0].path).toBe('/api/apps/tunes/enable')
    expect(body.grantsConsent.agentTools).toEqual([LIST.entry])
    expect(Object.values(body.grantsConsent).flat()).toEqual([LIST.entry])
  })

  it('a declined approval sends nothing', async () => {
    const ask = stubAppConfirm(false)
    const changed = mount()
    fireEvent.click(screen.getByRole('switch', { name: 'tunes_list' }))
    await waitFor(() => expect(ask).toHaveBeenCalled())
    expect(posts).toEqual([])
    expect(changed).not.toHaveBeenCalled()
  })

  it('a row stays in place when its switch turns off', async () => {
    const names = () => screen.getAllByRole('switch').map(s => s.getAttribute('aria-label'))
    const grant = { approved: [NOTE, TAG], staged: [], revoked: [] as typeof NOTE[] }
    const changed = vi.fn()
    const { rerender } = render(<AppDetailAgentTools appName="tunes" enabled grant={grant} onChanged={changed} />)
    const before = names()
    fireEvent.click(screen.getByRole('switch', { name: before[0]! }))
    await waitFor(() => expect(changed).toHaveBeenCalled())
    // The refetch the page runs after a revoke moves the first tool to `revoked`.
    const off = grant.approved.find(t => t.name === before[0])!
    rerender(<AppDetailAgentTools appName="tunes" enabled grant={{ approved: grant.approved.filter(t => t !== off), staged: [], revoked: [off] }} onChanged={changed} />)
    expect(names()).toEqual(before)
    expect(screen.getByRole('switch', { name: before[0]! }).getAttribute('aria-checked')).toBe('false')
  })

  it('on a disabled app a tool can be turned off but not on', () => {
    mount(false)
    const off = screen.getByRole('switch', { name: 'tunes_list' })
    expect(off.getAttribute('aria-disabled')).toBe('true')
    expect(screen.getByRole('switch', { name: 'tunes_note' }).getAttribute('aria-disabled')).toBeNull()
    // The greyed-out switch says why, and the reason is its accessible description.
    const reason = screen.getByText('Enable the app to turn these on.')
    expect(off.getAttribute('aria-describedby')).toBe(reason.id)
  })

  it('shows no enable-first reason when no switch is held by it', () => {
    mount(true)
    expect(screen.queryByText('Enable the app to turn these on.')).toBeNull()
    render(<AppDetailAgentTools appName="tunes" enabled={false} grant={{ approved: [NOTE], staged: [] }} />)
    expect(screen.queryByText('Enable the app to turn these on.')).toBeNull()
  })
})
