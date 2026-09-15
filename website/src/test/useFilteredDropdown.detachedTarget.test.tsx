import { act, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { useFilteredDropdown } from '../hooks/useFilteredDropdown'

function Harness() {
  const menu = useFilteredDropdown([{ name: 'one' }])
  const [showReset, setShowReset] = useState(true)
  return (
    <>
      <button type="button" onClick={() => menu.setOpen(true)}>open</button>
      {menu.open && (
        <div ref={menu.dropdownRef} data-testid="menu">
          {showReset && <button type="button" onClick={() => setShowReset(false)}>reset</button>}
        </div>
      )}
    </>
  )
}

describe('useFilteredDropdown — inside clicks that change the DOM', () => {
  it('does not close when the clicked menu button unmounts itself', async () => {
    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'open' }))
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
    fireEvent.click(screen.getByRole('button', { name: 'reset' }))
    expect(screen.getByTestId('menu')).toBeInTheDocument()
  })
})
