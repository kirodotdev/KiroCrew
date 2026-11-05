import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import { RailCustomizeModal, regroupWithShelf } from './railCustomizeModal'
import { readRailLayout, type RailGroup } from '../../lib/railLayout'

const G = (): RailGroup[] => [{ id: 'workspace', items: ['chat', 'members'] }, { id: 'apps', items: ['apps'] }]
const LABELS: Record<string, string> = { chat: 'Sessions', members: 'Mates', apps: 'Discover', schedule: 'Schedule' }
const meta = (id: string) => (LABELS[id] ? { label: LABELS[id], icon: null } : null)

afterEach(() => localStorage.clear())

describe('regroupWithShelf', () => {
  it('reads groups and the shelf off the flat order', () => {
    const flat = ['head:workspace', 'members', 'head:apps', 'apps', 'chat', 'head:__shelf', 'schedule']
    expect(regroupWithShelf(flat, G())).toEqual({
      groups: [{ id: 'workspace', items: ['members'] }, { id: 'apps', items: ['apps', 'chat'] }],
      shelf: ['schedule'],
    })
  })

  it('puts a row dropped onto the shelf in the shelf, and one above every header in the first group', () => {
    const r = regroupWithShelf(['schedule', 'head:workspace', 'chat', 'head:apps', 'head:__shelf', 'apps'], G())
    expect(r.groups[0].items).toEqual(['schedule', 'chat'])
    expect(r.shelf).toEqual(['apps'])
  })
})

function renderModal(onCommit = vi.fn()) {
  render(<RailCustomizeModal open onClose={() => {}} groups={G()} hidden={['schedule']} meta={meta} onCommit={onCommit} />)
  return onCommit
}

describe('RailCustomizeModal', () => {
  it('lists groups, rows and the shelf without per-row buttons', () => {
    renderModal()
    expect(screen.getAllByRole('textbox', { name: 'Group name' })).toHaveLength(2)
    expect(screen.getByText('Sessions')).toBeTruthy()
    expect(screen.getByText('Not on the rail')).toBeTruthy()
    expect(screen.getByText('Schedule')).toBeTruthy()
  })

  it('unpins a row with its hover x, but offers none on the shelf', () => {
    const onCommit = renderModal()
    fireEvent.click(screen.getByRole('button', { name: 'Unpin from rail: Mates' }))
    expect(onCommit).toHaveBeenCalledWith(
      [{ id: 'workspace', items: ['chat'] }, { id: 'apps', items: ['apps'] }],
      { unpin: 'members' },
    )
    expect(screen.queryByRole('button', { name: 'Unpin from rail: Schedule' })).toBeNull()
  })

  it('renames a group by typing in its title', () => {
    renderModal()
    const [first] = screen.getAllByRole('textbox', { name: 'Group name' })
    first.focus()
    fireEvent.change(first, { target: { value: 'Daily' } })
    fireEvent.keyDown(first, { key: 'Enter' })
    expect(readRailLayout().groups[0].title).toBe('Daily')
  })

  it('reverts a title with Escape', () => {
    renderModal()
    const [first] = screen.getAllByRole('textbox', { name: 'Group name' })
    first.focus()
    fireEvent.change(first, { target: { value: 'Nope' } })
    fireEvent.keyDown(first, { key: 'Escape' })
    expect((first as HTMLInputElement).value).toBe('')
    expect(localStorage.length).toBe(0)
  })

  it('removes a group', () => {
    renderModal()
    fireEvent.click(screen.getAllByRole('button', { name: 'Remove group' })[1])
    expect(readRailLayout().groups.map(g => g.id)).toEqual(['workspace'])
  })

  it('adds a new, untitled group', () => {
    renderModal()
    fireEvent.click(screen.getByRole('button', { name: /New group/ }))
    expect(readRailLayout().groups.map(g => g.id)).toEqual(['workspace', 'apps', 'group-3'])
  })
})
