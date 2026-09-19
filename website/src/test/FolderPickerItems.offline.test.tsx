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
function mount(connected: boolean) {
  const onPick = vi.fn()
  const store = createTestStore()
  if (connected) store.dispatch(sseConnected())
  render(
    <Provider store={store}>
      <FolderPickerItems folders={folders} onPick={onPick} Item={Item} />
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
      expect(String(row.className)).toContain('opacity-40')
    }
  })

  it('dims to the same weight as the menu rows above it, not a paler variant of its own', () => {
    mount(false)
    for (const row of rows) {
      expect(String(row.className)).toBe('opacity-40 text-muted')
    }
  })

  it('moves and lets the menu close once connected — the control', () => {
    const onPick = mount(true)
    const event = select(rows[1])
    expect(event.defaultPrevented).toBe(false)
    expect(onPick).toHaveBeenCalledWith('f1')
    expect(rows[1]['aria-disabled']).toBe(false)
  })
})
