/**
 * What the submenu triggers hand their Radix primitive while offline.
 *
 * A native `disabled` is answered by the menu primitive with
 * `pointer-events-none` and removal from roving focus, so the row is skipped in
 * silence with its reason unreachable. These submenus must gate the way their
 * sibling rows do — aria-disabled, dimmed, and held closed — which is a claim
 * about the PROPS they pass, so the primitives are captured here rather than
 * rendered.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import React from 'react'
import { render } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { sseConnected } from '../store/dashboardSlice'

const seen = { sub: {} as Record<string, unknown>, trigger: {} as Record<string, unknown> }

function capture(prefix: string) {
  const Sub = (props: Record<string, unknown>) => {
    seen.sub = props
    return <div>{props.children as React.ReactNode}</div>
  }
  const Trigger = (props: Record<string, unknown>) => {
    seen.trigger = props
    return <div>{props.children as React.ReactNode}</div>
  }
  const Pass = ({ children }: { children?: React.ReactNode }) => <div>{children}</div>
  return {
    [`${prefix}Sub`]: Sub,
    [`${prefix}SubTrigger`]: Trigger,
    [`${prefix}SubContent`]: Pass,
    [`${prefix}Item`]: Pass,
  }
}

vi.mock('../components/ui/dropdown-menu', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...capture('DropdownMenu'),
}))
vi.mock('../components/ui/context-menu', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...capture('ContextMenu'),
}))

vi.mock('../api/client', () => ({
  api: new Proxy({} as Record<string, unknown>, {
    get: () => vi.fn().mockResolvedValue([]),
  }),
}))

import FolderMoveSubmenu from '../components/FolderMoveSubmenu'

function mount(connected: boolean) {
  const store = createTestStore()
  if (connected) store.dispatch(sseConnected())
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <Provider store={store}>
        <FolderMoveSubmenu folders={[]} onPick={() => {}} variant="dropdown" />
      </Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  seen.sub = {}
  seen.trigger = {}
})

describe('the folder-move submenu trigger, offline', () => {
  it('passes NO native disabled, and holds the flyout closed instead', () => {
    mount(false)
    expect(seen.trigger.disabled).toBeUndefined()
    expect(seen.trigger['aria-disabled']).toBe(true)
    expect(seen.trigger.title).toMatch(/Gateway offline/)
    expect(String(seen.trigger.className)).toContain('opacity-40')
    expect(seen.sub.open).toBe(false)
  })

  it('leaves the flyout uncontrolled when connected \u2014 the control', () => {
    mount(true)
    expect(seen.trigger.disabled).toBeUndefined()
    expect(seen.trigger['aria-disabled']).toBe(false)
    expect(seen.sub.open).toBeUndefined()
  })
})
