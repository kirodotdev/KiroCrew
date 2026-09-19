/**
 * The send-to-instance submenu is a gateway write in the same menu whose other
 * write rows dim offline, so it gates the way FolderMoveSubmenu does: announced
 * and dimmed, flyout still openable, send refused at the sink.
 *
 * jsdom cannot drive a real Radix submenu open (no PointerEvent), so the
 * primitives are captured and the Item stub invokes onSelect itself.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import React from 'react'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { sseConnected } from '../store/dashboardSlice'

/** Long enough for a react-query mutation to reach its mutationFn. */
const settle = () => act(async () => { await new Promise(r => setTimeout(r, 0)) })

const seen = { trigger: {} as Record<string, unknown> }

const mocks = vi.hoisted(() => ({
  listInstances: vi.fn(),
  sendSessionToInstance: vi.fn(),
}))
vi.mock('../api/client', () => ({
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
}))

function capture(prefix: string) {
  const Trigger = (props: Record<string, unknown>) => {
    seen.trigger = props
    return <div>{props.children as React.ReactNode}</div>
  }
  const Pass = ({ children }: { children?: React.ReactNode }) => <div>{children}</div>
  // A real button so a click reaches onSelect, which is where the guard sits.
  const Item = ({ children, onSelect, disabled, title, className, 'aria-disabled': ariaDisabled }: {
    children?: React.ReactNode
    onSelect?: (e: Event) => void
    disabled?: boolean
    title?: string
    className?: string
    'aria-disabled'?: boolean | 'true' | 'false'
  }) => (
    <button
      type="button"
      disabled={disabled}
      title={title}
      className={className}
      aria-disabled={ariaDisabled}
      onClick={() => onSelect?.(new Event('select'))}
    >
      {children}
    </button>
  )
  return {
    [`${prefix}Sub`]: Pass,
    [`${prefix}SubTrigger`]: Trigger,
    [`${prefix}SubContent`]: Pass,
    [`${prefix}Item`]: Item,
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

import SendToInstanceSubmenu from '../components/SendToInstanceSubmenu'

function mount(connected: boolean) {
  const store = createTestStore()
  if (connected) store.dispatch(sseConnected())
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <Provider store={store}>
        <SendToInstanceSubmenu slotKey="chat-1-100" variant="dropdown" />
      </Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  seen.trigger = {}
  mocks.listInstances.mockResolvedValue({
    instances: [{ id: 'peer-1', name: 'Laptop', status: { state: 'connected' } }],
  })
  mocks.sendSessionToInstance.mockResolvedValue({ resume_mode: 'session_load' })
})

describe('the send-to-instance submenu, offline', () => {
  it('announces and dims the trigger without withholding the flyout', async () => {
    mount(false)
    await screen.findByText('Laptop')
    expect(seen.trigger.disabled).toBeUndefined()
    expect(seen.trigger['aria-disabled']).toBe(true)
    expect(seen.trigger.title).toMatch(/Gateway offline/)
    expect(String(seen.trigger.className)).toContain('opacity-40')
  })

  it('carries the reason inside the flyout, where the rows are', async () => {
    const { container } = mount(false)
    await screen.findByText('Laptop')
    const reason = container.querySelector('[data-testid="send-instance-offline-reason"]')
    expect(reason).not.toBeNull()
    expect(reason!.textContent).toContain('send to instances')
    expect(reason!.textContent).not.toContain('session changes')
  })

  it('refuses the send at the sink, so an opened flyout cannot drop a transfer', async () => {
    mount(false)
    fireEvent.click(await screen.findByRole('button', { name: /Laptop/ }))
    // Without this the assertion would pass on timing alone.
    await settle()
    expect(mocks.sendSessionToInstance).not.toHaveBeenCalled()
  })

  it('dims and announces each destination row, not only the trigger', async () => {
    mount(false)
    const row = await screen.findByRole('button', { name: /Laptop/ })
    expect(row.getAttribute('aria-disabled')).toBe('true')
    expect(row.className).toContain('opacity-40')
    expect(row.title).toMatch(/Gateway offline/)
    // Native disabled would drop the row from tab order and make the tooltip
    // unreachable — the idiom this change deliberately avoids.
    expect(row.hasAttribute('disabled')).toBe(false)
  })

  it('leaves a reachable peer row at full weight when connected — the control', async () => {
    mount(true)
    const row = await screen.findByRole('button', { name: /Laptop/ })
    expect(row.getAttribute('aria-disabled')).toBe('false')
    expect(row.className || '').not.toContain('opacity-40')
  })

  it('sends when connected — the control for the refusal above', async () => {
    mount(true)
    // No trigger exists until the instances query resolves.
    fireEvent.click(await screen.findByRole('button', { name: /Laptop/ }))
    expect(seen.trigger['aria-disabled']).toBe(false)
    await settle()
    expect(mocks.sendSessionToInstance).toHaveBeenCalledWith('peer-1', 'chat-1-100')
  })
})
