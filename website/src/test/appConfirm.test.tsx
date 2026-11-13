/**
 * askAppConfirm / AppConfirmHost — the app-wide in-app confirmation that the
 * session close, ⌘W and the sidebar's destructive actions ask through instead
 * of the browser-native confirm().
 */
import { describe, it, expect, vi, onTestFinished } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import AppConfirmHost from '../components/AppConfirmHost'
import { askAppConfirm, registerAppConfirmHost } from '../lib/appConfirm'

describe('askAppConfirm', () => {
  it('answers no, without a native dialog, when no host is mounted', async () => {
    const native = vi.fn(() => true)
    vi.stubGlobal('confirm', native)
    onTestFinished(() => { vi.unstubAllGlobals() })
    await expect(askAppConfirm({ title: 'Delete it?', confirmLabel: 'Delete' })).resolves.toBe(false)
    expect(native).not.toHaveBeenCalled()
  })

  it('an unregister only removes its own host', async () => {
    const first = vi.fn(() => Promise.resolve(true))
    const second = vi.fn(() => Promise.resolve(true))
    const offFirst = registerAppConfirmHost(first)
    const offSecond = registerAppConfirmHost(second)
    onTestFinished(offSecond)
    offFirst()
    await expect(askAppConfirm({ title: 'Delete it?', confirmLabel: 'Delete' })).resolves.toBe(true)
    expect(second).toHaveBeenCalledTimes(1)
    expect(first).not.toHaveBeenCalled()
  })
})

describe('AppConfirmHost', () => {
  it('draws the ask and resolves true on its confirm button', async () => {
    render(<AppConfirmHost />)
    let answer: Promise<boolean> | undefined
    act(() => { answer = askAppConfirm({ title: 'Delete this column?', confirmLabel: 'Delete column' }) })
    const dialog = await screen.findByRole('dialog', { name: 'Delete this column?' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete column' }))
    await expect(answer).resolves.toBe(true)
  })

  it('paints above a full-screen preview (the top layer), since ⌘W can ask from one', async () => {
    render(<AppConfirmHost />)
    act(() => { void askAppConfirm({ title: 'Close this session?', confirmLabel: 'Close session' }) })
    const dialog = await screen.findByRole('dialog', { name: 'Close this session?' })
    // Modal's `top` layer is the one above the z-[9999] full-screen overlays.
    expect(dialog.closest('.z-\\[10001\\]')).not.toBeNull()
  })

  it('opens after a closing menu restores focus to its trigger, keeps focus, and hands it back on close', async () => {
    render(<><button type="button">Session menu</button><AppConfirmHost /></>)
    const trigger = screen.getByRole('button', { name: 'Session menu' })
    let answer: Promise<boolean> | undefined
    act(() => {
      // Ask from a menu item's select, as the session menu's "Close session" does.
      answer = askAppConfirm({ title: 'Close this session?', confirmLabel: 'Close session' })
      // The closing Radix menu queues its trigger-focus restore in the same task.
      setTimeout(() => trigger.focus(), 0)
    })
    const dialog = await screen.findByRole('dialog', { name: 'Close this session?' })
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await expect(answer).resolves.toBe(false)
    await waitFor(() => expect(document.activeElement).toBe(trigger))
  })

  it('answers no for an ask still waiting to open when the host unmounts', async () => {
    const { unmount } = render(<AppConfirmHost />)
    const answer = askAppConfirm({ title: 'Close this session?', confirmLabel: 'Close session' })
    unmount()
    await expect(answer).resolves.toBe(false)
  })

  it('resolves false on Cancel and closes the dialog', async () => {
    render(<AppConfirmHost />)
    let answer: Promise<boolean> | undefined
    act(() => { answer = askAppConfirm({ title: 'Delete this column?', confirmLabel: 'Delete column' }) })
    const dialog = await screen.findByRole('dialog', { name: 'Delete this column?' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await expect(answer).resolves.toBe(false)
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Delete this column?' })).toBeNull())
  })

  it('answers no once it unmounts, so no caller waits forever', async () => {
    const { unmount } = render(<AppConfirmHost />)
    let answer: Promise<boolean> | undefined
    act(() => { answer = askAppConfirm({ title: 'Delete this column?', confirmLabel: 'Delete column' }) })
    await screen.findByRole('dialog', { name: 'Delete this column?' })
    unmount()
    await expect(answer).resolves.toBe(false)
    await expect(askAppConfirm({ title: 'Again?', confirmLabel: 'Delete' })).resolves.toBe(false)
  })
})
