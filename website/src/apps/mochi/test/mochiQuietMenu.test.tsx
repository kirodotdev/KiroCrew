/**
 * Pet context menu — quiet mode (DND) item.
 *
 * Pins the two states of the toggle and the wiring behind each: not quiet
 * renders "Quiet for 1 hour" and clicking it POSTs 60 minutes; quiet renders
 * the resume label carrying the expiry time (the label doubles as the
 * quiet-mode indicator) and clicking it POSTs 0. The state is pulled fresh on
 * every menu open, so natural expiry needs no live push to self-correct.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

const getQuietUntil = vi.fn<() => Promise<number>>()
const setQuiet = vi.fn<(minutes: number) => Promise<number>>()
const contextMenuAction = vi.fn()

vi.mock('../src/mochiApi', () => ({
  api: {
    get getQuietUntil() {
      return getQuietUntil
    },
    get setQuiet() {
      return setQuiet
    },
    get contextMenuAction() {
      return contextMenuAction
    },
    reportMenuHitbox: undefined,
  },
}))

import { PetContextMenu } from '../src/renderer/PetContextMenu'

beforeEach(() => {
  getQuietUntil.mockReset()
  setQuiet.mockReset().mockResolvedValue(0)
  contextMenuAction.mockReset()
})

function renderMenu() {
  return render(<PetContextMenu x={10} y={10} isHidden={false} onClose={() => {}} />)
}

describe('pet menu quiet item', () => {
  it('offers "Quiet for 1 hour" when not quiet, POSTing 60 on click', async () => {
    getQuietUntil.mockResolvedValue(0)
    renderMenu()
    const item = await screen.findByText('Quiet for 1 hour')
    await userEvent.click(item)
    expect(setQuiet).toHaveBeenCalledWith(60)
    // The quiet toggle is self-contained — it must not fall through to the
    // generic menu-action relay (an unknown id there is a silent no-op).
    expect(contextMenuAction).not.toHaveBeenCalled()
  })

  it('offers resume with the expiry time when quiet, POSTing 0 on click', async () => {
    const until = Date.now() + 30 * 60_000
    getQuietUntil.mockResolvedValue(until)
    renderMenu()
    const item = await screen.findByText(/Resume notifications/)
    // The label carries WHEN the pet wakes — the quiet-mode indicator.
    const time = new Intl.DateTimeFormat(undefined, {
      hour: '2-digit',
      minute: '2-digit',
    }).format(new Date(until))
    expect(item.textContent).toContain(time)
    await userEvent.click(item)
    expect(setQuiet).toHaveBeenCalledWith(0)
  })

  it('renders the not-quiet item while the state pull is in flight', async () => {
    let resolve!: (v: number) => void
    getQuietUntil.mockReturnValue(new Promise<number>((r) => (resolve = r)))
    renderMenu()
    expect(screen.getByText('Quiet for 1 hour')).toBeTruthy()
    resolve(Date.now() + 60_000)
    await waitFor(() => expect(screen.queryByText('Quiet for 1 hour')).toBeNull())
  })
})
