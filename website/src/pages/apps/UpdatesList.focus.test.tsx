// Focus on the Updates list's Update all and per-row Update while they run.
//
// Both stay on screen while their update runs: Update all shows the batch's
// progress in its own label, and a row whose update fails is still updatable.
// A native `disabled` on the button a reader just pressed blurs it to <body>,
// so the pressed button is held with aria-disabled instead and refuses a second
// press. The rows nobody pressed stay natively disabled while anything runs.
import { useState } from 'react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, cleanup, act } from '@testing-library/react'
import { renderWithProviders as render } from '../../test/helpers'
import userEvent from '@testing-library/user-event'
import UpdatesList from './UpdatesList'
import type { LibraryApp } from './useAppsData'

afterEach(cleanup)

const app = (name: string): LibraryApp => ({
  name, version: '1.0.0', _newVersion: '1.1.0', updateAvailable: true,
} as unknown as LibraryApp)

const ROWS = [app('alpha'), app('beta')]

/** The page's role, reduced: each press starts work the test then ends by hand. */
function Harness({ onUpdate, onUpdateAll }: { onUpdate: (n: string) => void; onUpdateAll: () => void }) {
  const [updatingAll, setUpdatingAll] = useState<{ done: number; total: number } | null>(null)
  const [updatePending, setUpdatePending] = useState<string | null>(null)
  return (
    <>
      <UpdatesList
        rows={ROWS}
        updatingAll={updatingAll}
        updatePending={updatePending}
        onUpdate={(n) => { onUpdate(n); setUpdatePending(n) }}
        onUpdateAll={() => { onUpdateAll(); setUpdatingAll({ done: 0, total: 2 }) }}
      />
      <button type="button" onClick={() => { setUpdatePending(null); setUpdatingAll(null) }}>settle</button>
    </>
  )
}

describe('UpdatesList focus while an update runs', () => {
  it('keeps focus on Update all through the batch, and refuses a second press', async () => {
    const onUpdateAll = vi.fn()
    render(<Harness onUpdate={vi.fn()} onUpdateAll={onUpdateAll} />)
    const all = screen.getByRole('button', { name: 'Update All' })

    all.focus()
    await userEvent.keyboard('{Enter}')
    expect(onUpdateAll).toHaveBeenCalledTimes(1)
    // The same element now carries the progress label.
    expect(screen.getByRole('button', { name: /Updating/ })).toBe(all)
    expect(all.getAttribute('aria-disabled')).toBe('true')
    expect(all.hasAttribute('disabled')).toBe(false)
    expect(document.activeElement).toBe(all)

    await userEvent.keyboard('{Enter}')
    expect(onUpdateAll).toHaveBeenCalledTimes(1)
  })

  it('keeps focus on the pressed row while its update runs, and after it fails', async () => {
    const onUpdate = vi.fn()
    render(<Harness onUpdate={onUpdate} onUpdateAll={vi.fn()} />)
    const [alpha, beta] = screen.getAllByTitle('Update to v1.1.0')

    alpha.focus()
    await userEvent.keyboard('{Enter}')
    expect(onUpdate).toHaveBeenCalledWith('alpha')
    expect(alpha.getAttribute('aria-disabled')).toBe('true')
    expect(alpha.hasAttribute('disabled')).toBe(false)
    expect(document.activeElement).toBe(alpha)
    // The row nobody pressed is still out of reach while one update runs.
    expect(beta.hasAttribute('disabled')).toBe(true)

    await userEvent.keyboard('{Enter}')
    expect(onUpdate).toHaveBeenCalledTimes(1)
    // Nor can a batch start over the row's update, which would post it twice.
    expect(screen.getByRole('button', { name: 'Update All' })).toBeDisabled()

    // A failed update leaves the row on the list, still updatable.
    // A programmatic click: it settles the update without moving focus.
    act(() => { screen.getByRole('button', { name: 'settle' }).click() })
    expect(alpha.hasAttribute('aria-disabled')).toBe(false)
    expect(document.activeElement).toBe(alpha)
  })
})
