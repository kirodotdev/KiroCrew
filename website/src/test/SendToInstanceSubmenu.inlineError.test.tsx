/**
 * A click on a failed row's inline error must not send the copy again.
 *
 * Uses the real Radix menu item, because the re-send came from its own pointer
 * handling: a pointerup on an item that saw no pointerdown calls `click()` on the
 * item itself, which runs `onSelect`. The stubbed item in
 * SendToInstanceSubmenu.test.tsx has no such path, so it cannot catch this.
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import type { InstanceView } from '../api/client'
import { InstanceSendItems } from '../components/SendToInstanceSubmenu'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../components/ui/dropdown-menu'

const instance = {
  id: 'devdesk',
  name: 'devdesk',
  status: { instance_id: 'devdesk', state: 'connected' },
} as InstanceView

function renderFailedRow(onSend: (id: string) => void) {
  return render(
    <DropdownMenu open>
      <DropdownMenuTrigger>open</DropdownMenuTrigger>
      <DropdownMenuContent>
        <InstanceSendItems
          instances={[instance]}
          states={{ devdesk: { kind: 'error', message: 'The peer refused the copy.' } }}
          onSend={onSend}
          Item={DropdownMenuItem}
        />
      </DropdownMenuContent>
    </DropdownMenu>,
  )
}

describe('the inline error on a failed send row', () => {
  it('ignores a full press on the error text', () => {
    const onSend = vi.fn()
    renderFailedRow(onSend)
    const text = screen.getByText('The peer refused the copy.')
    fireEvent.pointerDown(text, { pointerType: 'mouse', button: 0 })
    fireEvent.pointerUp(text, { pointerType: 'mouse', button: 0 })
    fireEvent.click(text)
    expect(onSend).not.toHaveBeenCalled()
  })

  it('still sends from the row itself', () => {
    // Control: the row is live, so the silence above is the error's own guard.
    const onSend = vi.fn()
    renderFailedRow(onSend)
    fireEvent.click(screen.getByText('devdesk'))
    expect(onSend).toHaveBeenCalledWith('devdesk')
  })
})
