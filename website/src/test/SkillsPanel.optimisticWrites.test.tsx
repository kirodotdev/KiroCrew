import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
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
  // MemoryRouter: the panel links to the pending queue on Agent Capabilities →
  // Skills and forwards a `?review=` deep link there, so it needs a router.
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  )
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

/**
 * The extra-paths field is a draft, not a toggle: what the user typed is theirs until
 * a save of exactly that text lands. A refetch must not replace it, only a matching
 * success clears it, and a failed save leaves it in place.
 */
describe('SkillsPanel — extra skill paths draft', () => {
  const PATHS_KEY = 'skills.extra_paths'
  const storedWith = (extra_paths: string[]) => ({
    skills: { auto_create_from_sessions: false, approval_required: true, extra_paths },
  })

  function wrapWithClient(ui: React.ReactElement) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>{ui}</MemoryRouter>
      </QueryClientProvider>,
    )
    return qc
  }

  async function loadedField(initial: string) {
    const field = await screen.findByLabelText('Extra skill paths')
    await waitFor(() => expect(field).toHaveValue(initial))
    return field
  }

  beforeEach(() => {
    patchConfigMock.mockReset()
    patchConfigMock.mockImplementation(() => Promise.resolve({}))
    kirocrewConfigMock.mockClear()
    kirocrewConfigMock.mockImplementation(() => Promise.resolve(storedWith(['/a'])))
  })

  it('a refetch does not clobber a draft the user has not saved', async () => {
    const qc = wrapWithClient(<SkillsPanel />)
    const field = await loadedField('/a')
    fireEvent.change(field, { target: { value: '/a\n/typing' } })

    kirocrewConfigMock.mockImplementation(() => Promise.resolve(storedWith(['/from-server'])))
    await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalledTimes(2))

    expect(field).toHaveValue('/a\n/typing')
    expect(patchConfigMock).not.toHaveBeenCalled()
  })

  it('a refetch refreshes the field while nothing is being edited', async () => {
    const qc = wrapWithClient(<SkillsPanel />)
    const field = await loadedField('/a')

    kirocrewConfigMock.mockImplementation(() => Promise.resolve(storedWith(['/from-server', '/b'])))
    await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })

    await waitFor(() => expect(field).toHaveValue('/from-server\n/b'))
  })

  it('a success for the submitted list clears the draft and shows the stored list', async () => {
    const { resolve } = deferPatch()
    wrapWithClient(<SkillsPanel />)
    const field = await loadedField('/a')
    fireEvent.change(field, { target: { value: '  /new  \n\n' } })
    fireEvent.blur(field)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(PATHS_KEY, ['/new']))
    await waitFor(() => expect(field).toBeDisabled()) // locked for the round trip

    kirocrewConfigMock.mockImplementation(() => Promise.resolve(storedWith(['/new'])))
    resolve({})

    await waitFor(() => expect(field).toHaveValue('/new')) // the canonical joined value, not '  /new  '
    expect(field).not.toBeDisabled()
  })

  it('a stale success keeps text typed after the request started', async () => {
    const { resolve } = deferPatch()
    wrapWithClient(<SkillsPanel />)
    const field = await loadedField('/a')
    fireEvent.change(field, { target: { value: '/new' } })
    fireEvent.blur(field)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(PATHS_KEY, ['/new']))

    // Typing that lands while the request is in flight (the field is locked in the UI,
    // so this is the guard behind the lock, not a path a person takes).
    fireEvent.change(field, { target: { value: '/new\n/later' } })

    kirocrewConfigMock.mockImplementation(() => Promise.resolve(storedWith(['/new'])))
    resolve({})
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(field).not.toBeDisabled())

    expect(field).toHaveValue('/new\n/later')
  })

  it('a failed save keeps the draft, shows a local error, and offers no agent hand-off', async () => {
    const { reject } = deferPatch()
    wrapWithClient(<SkillsPanel />)
    const field = await loadedField('/a')
    fireEvent.change(field, { target: { value: '/a\n/b' } })
    fireEvent.blur(field)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(PATHS_KEY, ['/a', '/b']))

    reject(new Error('boom'))

    expect(await screen.findByText(/Failed to save the extra skill paths/)).toBeInTheDocument()
    expect(field).toHaveValue('/a\n/b')
    await waitFor(() => expect(field).not.toBeDisabled())
    // The toggles' banner is for the toggles; this failure is not reported there.
    expect(screen.queryByText(/Failed to save skills setting/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /ask/i })).not.toBeInTheDocument()

    // A fresh attempt supersedes the stale failure.
    patchConfigMock.mockImplementation(() => Promise.resolve({}))
    kirocrewConfigMock.mockImplementation(() => Promise.resolve(storedWith(['/a', '/b'])))
    fireEvent.blur(field)
    await waitFor(() =>
      expect(screen.queryByText(/Failed to save the extra skill paths/)).not.toBeInTheDocument(),
    )
  })

  it('saving the paths does not disturb the toggles', async () => {
    wrapWithClient(<SkillsPanel />)
    const field = await loadedField('/a')
    fireEvent.change(field, { target: { value: '/a\n/b' } })
    fireEvent.blur(field)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(PATHS_KEY, ['/a', '/b']))

    expect(autoToggle()).not.toBeChecked()
    expect(patchConfigMock).not.toHaveBeenCalledWith(AUTO_PATH, expect.anything())
  })
})
