// Removing a chip in the Settings -> Browser domain and origin editors takes
// the focused remove button off the page with it; focus must land on the add
// field instead of the page body. The real page disables both editors while
// the save a remove starts is in flight, so the harness does the same: the
// list changes and `disabled` turns on, then the test lets the save settle.
import { useLayoutEffect, useState } from 'react'
import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'

import { BrowserCredentialDomains } from '../pages/settings/BrowserCredentialDomains'
import { BrowserLocalOrigins } from '../pages/settings/BrowserLocalOrigins'

let settle: () => void = () => {}

function useSavingList(initial: string[]) {
  const [items, setItems] = useState(initial)
  const [saving, setSaving] = useState(false)
  const onChange = (next: string[]) => {
    setItems(next)
    setSaving(true)
    settle = () => setSaving(false)
  }
  // A browser drops focus from a control that becomes disabled (the HTML focus
  // fixup); jsdom does not, and its blur() ignores a disabled element, so the
  // harness re-enables it for the blur on the disabling commit.
  useLayoutEffect(() => {
    const active = document.activeElement as HTMLInputElement | null
    if (!saving || !active?.disabled) return
    active.disabled = false
    active.blur()
    active.disabled = true
  }, [saving])
  return { items, saving, onChange }
}

function Domains() {
  const { items, saving, onChange } = useSavingList(['example.com', 'example.org'])
  return <BrowserCredentialDomains domains={items} provider="Acme" disabled={saving} onChange={onChange} />
}

function Origins() {
  const { items, saving, onChange } = useSavingList(['http://localhost:3000', 'http://localhost:5173'])
  return <BrowserLocalOrigins origins={items} disabled={saving} onChange={onChange} />
}

describe('browser settings chip lists', () => {
  it('moves focus to the add field once a credential domain removal has saved', () => {
    render(<Domains />)
    const remove = screen.getByRole('button', { name: /example\.com/ })
    remove.focus()
    fireEvent.click(remove)
    expect(screen.queryByText('example.com')).toBeNull()
    const field = screen.getByPlaceholderText('example.com')
    expect(field).toBeDisabled()
    act(() => settle())
    expect(field).toHaveFocus()
  })

  it('moves focus to the add field once a local origin removal has saved', () => {
    render(<Origins />)
    const remove = screen.getByRole('button', { name: /localhost:3000/ })
    remove.focus()
    fireEvent.click(remove)
    expect(screen.queryByText('http://localhost:3000')).toBeNull()
    const field = screen.getByRole('textbox')
    expect(field).toBeDisabled()
    act(() => settle())
    expect(field).toHaveFocus()
  })

  it('leaves focus where the user moved it while the save ran', () => {
    render(<><Origins /><input aria-label="elsewhere" /></>)
    const remove = screen.getByRole('button', { name: /localhost:3000/ })
    remove.focus()
    fireEvent.click(remove)
    const elsewhere = screen.getByLabelText('elsewhere')
    elsewhere.focus()
    act(() => settle())
    expect(elsewhere).toHaveFocus()
  })
})
