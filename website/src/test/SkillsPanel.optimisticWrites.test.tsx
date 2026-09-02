import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

/**
 * Settings → Skills: optimistic toggle writes through the per-path overlay
 * (#6890). The panel PATCHes sibling paths of the shared ['kirocrewConfig']
 * object; the whole-object onMutate snapshot it used to take could restore
 * another save's in-flight value on failure. Pinned here: the toggle flips
 * immediately without a cache write, a failure rolls back only its own path
 * and raises the banner, and a retry on the same path clears the stale
 * banner.
 */

const { patchConfigMock, kirocrewConfigMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn((_path: string, _value: unknown) => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() =>
    Promise.resolve({ skills: { auto_create_from_sessions: false, approval_required: true } }),
  ),
}))

vi.mock('../api/client', () => ({
  api: { kirocrewConfig: kirocrewConfigMock, patchConfig: patchConfigMock },
}))

import { SkillsPanel } from '../pages/settings/SkillsPanel'

const AUTO_PATH = 'skills.auto_create_from_sessions'

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

function deferPatch() {
  let resolve!: (v: unknown) => void
  let reject!: (e: unknown) => void
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  patchConfigMock.mockImplementation(() => promise as never)
  return { resolve, reject }
}

const autoToggle = () =>
  screen.getByRole('switch', { name: /Auto-generate skills from sessions/ })

describe('SkillsPanel — optimistic toggle via the per-path overlay', () => {
  beforeEach(() => {
    patchConfigMock.mockReset()
    patchConfigMock.mockImplementation(() => Promise.resolve({}))
    kirocrewConfigMock.mockClear()
    kirocrewConfigMock.mockImplementation(() =>
      Promise.resolve({ skills: { auto_create_from_sessions: false, approval_required: true } }),
    )
  })

  it('flips the toggle immediately while the PATCH is in flight, then keeps the refetched value', async () => {
    const { resolve } = deferPatch()
    wrap(<SkillsPanel />)
    const label = await screen.findByText('Auto-generate skills from sessions')
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalledTimes(1))
    fireEvent.click(label)

    // Pre-settle: the toggle already reads ON while only the initial config
    // fetch has run — the display comes from the overlay, not a cache write.
    await waitFor(() => expect(autoToggle()).toBeChecked())
    expect(patchConfigMock).toHaveBeenCalledWith(AUTO_PATH, true)
    expect(kirocrewConfigMock).toHaveBeenCalledTimes(1)

    kirocrewConfigMock.mockImplementation(() =>
      Promise.resolve({ skills: { auto_create_from_sessions: true, approval_required: true } }),
    )
    resolve({})
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalledTimes(2))
    expect(autoToggle()).toBeChecked()
  })

  it('a failed save rolls back its own toggle and shows the banner; a retry clears it', async () => {
    const { reject } = deferPatch()
    wrap(<SkillsPanel />)
    const label = await screen.findByText('Auto-generate skills from sessions')
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalledTimes(1))
    fireEvent.click(label)
    await waitFor(() => expect(autoToggle()).toBeChecked())

    reject(new Error('boom'))
    await waitFor(() => expect(autoToggle()).not.toBeChecked())
    expect(await screen.findByText(/Failed to save skills setting/)).toBeInTheDocument()

    // A fresh attempt on the SAME path supersedes its stale failure banner.
    patchConfigMock.mockImplementation(() => Promise.resolve({}))
    kirocrewConfigMock.mockImplementation(() =>
      Promise.resolve({ skills: { auto_create_from_sessions: true, approval_required: true } }),
    )
    fireEvent.click(label)
    await waitFor(() =>
      expect(screen.queryByText(/Failed to save skills setting/)).not.toBeInTheDocument(),
    )
    await waitFor(() => expect(autoToggle()).toBeChecked())
  })
})
