import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'

import { RailGroups } from './railGroups'
import { RAIL_LAYOUT_KEY, RAIL_PIN_MIME, insertPin, readRailLayout, writeRailLayout } from '../../lib/railLayout'

const AVAILABLE = ['chat', 'members', 'apps', 'apps-library', 'app:radar']
const LABELS: Record<string, string> = { chat: 'Sessions', members: 'Mates', apps: 'Discover', 'apps-library': 'Library', 'app:radar': 'Radar' }

function renderRail(setAppPinned = vi.fn()) {
  render(
    <RailGroups
      available={AVAILABLE}
      collapsed={false}
      defaultGroup={id => (id.startsWith('app:') ? 'apps' : 'workspace')}
      renderItem={(id, label) => <button type="button">{label ?? LABELS[id]}</button>}
      meta={id => (LABELS[id] ? { label: LABELS[id], icon: null } : null)}
      isApp={id => id.startsWith('app:')}
      setAppPinned={setAppPinned}
    />,
  )
  return setAppPinned
}

afterEach(() => localStorage.clear())

describe('RailGroups right-click', () => {
  it('renders the default groups with their titles', () => {
    renderRail()
    expect(screen.getByRole('group', { name: 'Workspace' })).toBeTruthy()
    expect(screen.getByRole('group', { name: 'Apps' })).toBeTruthy()
  })

  it('unpins a static row into the layout hidden set', async () => {
    renderRail()
    fireEvent.contextMenu(screen.getByText('Mates'))
    fireEvent.click(await screen.findByRole('menuitem', { name: /Unpin from rail/ }))
    expect(readRailLayout().hidden).toEqual(['members'])
    expect(screen.queryByText('Mates')).toBeNull()
  })

  it('unpins an app through the Library pin state, not the layout', async () => {
    const setAppPinned = renderRail()
    fireEvent.contextMenu(screen.getByText('Radar'))
    fireEvent.click(await screen.findByRole('menuitem', { name: /Unpin from rail/ }))
    expect(setAppPinned).toHaveBeenCalledWith('app:radar', false)
    expect(localStorage.getItem(RAIL_LAYOUT_KEY)).toBeNull()
  })

  it('offers New group on empty rail space and adds one', async () => {
    renderRail()
    fireEvent.contextMenu(screen.getByRole('group', { name: 'Apps' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: /New group/ }))
    expect(readRailLayout().groups.map(g => g.id)).toEqual(['workspace', 'apps', 'group-3'])
  })

  it('opens the customize modal from the menu', async () => {
    renderRail()
    fireEvent.contextMenu(screen.getByText('Sessions'))
    fireEvent.click(await screen.findByRole('menuitem', { name: /Customize rail/ }))
    expect(await screen.findByRole('dialog', { name: 'Customize rail' })).toBeTruthy()
  })
})

/** Run the rename field's focus frame. */
async function flushFrame() { await act(async () => { await new Promise(r => requestAnimationFrame(() => r(null))) }) }

describe('RailGroups rail-only names', () => {
  it('renames a row on the rail with Enter, and resets it', async () => {
    renderRail()
    fireEvent.contextMenu(screen.getByText('Sessions'))
    fireEvent.click(await screen.findByRole('menuitem', { name: /Rename on rail/ }))
    await flushFrame()
    const field = screen.getByRole('textbox', { name: 'Rename on rail' })
    fireEvent.change(field, { target: { value: '  Chats ' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    expect(readRailLayout().labels).toEqual({ chat: 'Chats' })
    expect(screen.getByText('Chats')).toBeTruthy()

    fireEvent.contextMenu(screen.getByText('Chats'))
    fireEvent.click(await screen.findByRole('menuitem', { name: /Reset name/ }))
    expect(readRailLayout().labels).toBeUndefined()
    expect(screen.getByText('Sessions')).toBeTruthy()
  })

  it('treats saving an unchanged name as no rename', async () => {
    renderRail()
    fireEvent.contextMenu(screen.getByText('Library'))
    fireEvent.click(await screen.findByRole('menuitem', { name: /Rename on rail/ }))
    await flushFrame()
    fireEvent.keyDown(screen.getByRole('textbox', { name: 'Rename on rail' }), { key: 'Enter' })
    expect(localStorage.getItem(RAIL_LAYOUT_KEY)).toBeNull()
  })

  it('cancels a rename with Escape and writes nothing', async () => {
    renderRail()
    fireEvent.contextMenu(screen.getByText('Mates'))
    fireEvent.click(await screen.findByRole('menuitem', { name: /Rename on rail/ }))
    await flushFrame()
    const field = screen.getByRole('textbox', { name: 'Rename on rail' })
    fireEvent.change(field, { target: { value: 'Nope' } })
    fireEvent.keyDown(field, { key: 'Escape' })
    expect(localStorage.getItem(RAIL_LAYOUT_KEY)).toBeNull()
    expect(screen.queryByRole('textbox')).toBeNull()
  })
})

describe('RailGroups entity pins', () => {
  it('unpins a pinned session outright instead of hiding it', async () => {
    LABELS['session:s1'] = 'Standup'
    writeRailLayout(insertPin(readRailLayout(), 'session:s1', 'workspace', null))
    render(
      <RailGroups
        available={[...AVAILABLE, 'session:s1']}
        collapsed={false}
        renderItem={(id, label) => <button type="button">{label ?? LABELS[id]}</button>}
        meta={id => (LABELS[id] ? { label: LABELS[id], icon: null } : null)}
      />,
    )
    fireEvent.contextMenu(screen.getByText('Standup'))
    fireEvent.click(await screen.findByRole('menuitem', { name: /Unpin from rail/ }))
    const stored = readRailLayout()
    expect(stored.groups.flatMap(g => g.items)).not.toContain('session:s1')
    expect(stored.hidden).toEqual([])
    delete LABELS['session:s1']
  })

  it('pins a natively dragged Mate where it is dropped', () => {
    renderRail()
    const host = document.getElementById('rail-pin-drop-host')!
    const dataTransfer = { types: [RAIL_PIN_MIME], getData: (t: string) => (t === RAIL_PIN_MIME ? 'mate:kiro' : ''), dropEffect: '' }
    fireEvent.dragOver(host, { dataTransfer })
    fireEvent.drop(host, { dataTransfer, clientY: 9999 })
    // jsdom lays nothing out, so a point below everything lands at the end of the last group.
    expect(readRailLayout().groups.at(-1)!.items.at(-1)).toBe('mate:kiro')
  })

  it('ignores a native drop that is not a rail pin', () => {
    renderRail()
    const host = document.getElementById('rail-pin-drop-host')!
    fireEvent.drop(host, { dataTransfer: { types: ['text/plain'], getData: () => 'session-key' }, clientY: 0 })
    expect(localStorage.getItem(RAIL_LAYOUT_KEY)).toBeNull()
  })
})
