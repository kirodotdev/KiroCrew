/**
 * Two claims about the PROPS a destination row is handed, so the hosting menu's
 * Item is captured rather than rendered: Radix dismisses the menu on select
 * unless the handler defaults the event, so a guard that only returned closed the
 * flyout — and the offline reason row inside it — having moved nothing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type React from 'react'
import { render } from '@testing-library/react'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'
import { sseConnected } from '../store/dashboardSlice'
import { FolderPickerItems } from '../components/FolderMoveSubmenu'
import type { ChatFolder } from '../types'

const folders: ChatFolder[] = [{ id: 'f1', name: 'Alpha', order: 0 }]

const rows: Record<string, unknown>[] = []
function Item(props: Record<string, unknown>) {
  rows.push(props)
  return <div>{props.children as React.ReactNode}</div>
}

/** No `connected` prop is passed anywhere here: the component must read it itself. */
function mount(connected: boolean, offlineVerb?: string) {
  const onPick = vi.fn()
  const store = createTestStore()
  if (connected) store.dispatch(sseConnected())
  render(
    <Provider store={store}>
      <FolderPickerItems folders={folders} onPick={onPick} Item={Item} offlineVerb={offlineVerb} />
    </Provider>,
  )
  return onPick
}

function select(row: Record<string, unknown>): Event {
  const event = new Event('select', { cancelable: true })
  ;(row.onSelect as (e: Event) => void)(event)
  return event
}

beforeEach(() => {
  rows.length = 0
})

describe('a folder destination row, gateway offline', () => {
  it('refuses the pick and defaults the event, so the menu survives to explain itself', () => {
    const onPick = mount(false)
    expect(rows).toHaveLength(folders.length + 1)
    for (const row of rows) {
      const event = select(row)
      expect(event.defaultPrevented).toBe(true)
    }
    expect(onPick).not.toHaveBeenCalled()
  })

  it('announces the refusal rather than only dimming, which reads as broken', () => {
    mount(false)
    for (const row of rows) {
      expect(row['aria-disabled']).toBe(true)
      expect(String(row.title)).toMatch(/Gateway offline/)
      expect(row.offline).toBe(true)
    }
    expect(document.querySelector('[data-testid="folder-move-offline-reason"]')?.textContent).toMatch(/Gateway offline/)
  })

  it('dims to the same weight as the menu rows above it, not a paler variant of its own', () => {
    mount(false)
    for (const row of rows) {
      // Delegated to the hosting menu's Item, so the dim cannot drift from the
      // rows above it; the row contributes no dim class of its own.
      expect(row.offline).toBe(true)
      expect(row.className).toBeUndefined()
    }
  })

  it('names the caller\u2019s own object, so an artifact host does not say "move folders"', () => {
    mount(false, 'move artifacts')
    const reason = document.querySelector('[data-testid="folder-move-offline-reason"]')?.textContent ?? ''
    expect(reason).toContain('move artifacts')
    expect(reason).not.toContain('move folders')
  })

  it('moves when connected and lets the menu close — the control', () => {
    const onPick = mount(true)
    const event = select(rows[1])
    // NOT defaulted: the artifacts-library callers host no ErrorNotice and their own
    // comments expect the picker to dismiss once the destination is chosen.
    expect(event.defaultPrevented).toBe(false)
    expect(onPick).toHaveBeenCalledWith('f1')
    expect(document.querySelector('[data-testid="folder-move-offline-reason"]')).toBeNull()
    // Absent, not false: the offline affordance is applied only while offline, so a
    // connected row keeps its own disabled state with nothing contradicting it.
    expect(rows[1]['aria-disabled']).toBeUndefined()
  })
})
