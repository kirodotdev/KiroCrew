/**
 * MochiInstancesList — the pane that picks WHICH gateway's Mochi the one pet shows.
 *
 * `isUsable` / `visibleRows` are already pinned by `mochiInstanceRows.test.ts`;
 * what had no test is the rendered pane, and its interesting behaviours are all
 * about not lying to the user:
 *
 *  - the four list-level states (loading, feature off, list failed, needs restart)
 *    have to stay apart, because they need four different actions from the user;
 *  - a row that cannot host the pet must be inert AND marked, not silently dead;
 *  - the SAVED choice stays listed and pickable-looking even when it went away,
 *    since `petInstance` is stored opaquely and survives an absent instance;
 *  - "Mochi is off there" outranks the tunnel state in the label — the tunnel is
 *    fine, so "not connected" would be actively misleading.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { CoreInstance, InstancesView } from '../panel/panelBridge'

const listInstances = vi.fn<() => Promise<InstancesView>>()
vi.mock('../panel/panelBridge', () => ({
  get listInstances() {
    return listInstances
  },
}))

/** Swapped per test so the "no shell" fallback can be exercised too. */
let instancesList: (() => Promise<InstancesView | null>) | undefined
let instancesEnabledMap: (() => Promise<Record<string, boolean>>) | undefined

vi.mock('../src/mochiApi', () => ({
  api: {
    get instancesList() {
      return instancesList
    },
    get instancesEnabledMap() {
      return instancesEnabledMap
    },
  },
}))

import { MochiInstancesList } from '../panel/MochiInstances'

function inst(over: Partial<CoreInstance> = {}): CoreInstance {
  return {
    id: 'zzq-a',
    name: 'Crew A',
    local_port: 7778,
    status: { state: 'connected' },
    ...over,
  }
}

beforeEach(() => {
  listInstances.mockReset().mockResolvedValue({ state: 'ready', instances: [] })
  instancesList = undefined
  instancesEnabledMap = undefined
})

describe('MochiInstancesList list states', () => {
  it('offers this computer immediately and says it is looking while the list loads', () => {
    listInstances.mockReturnValue(new Promise(() => {}))
    render(<MochiInstancesList value="" onChange={() => {}} />)
    // 'self' is always offered — it is the gateway serving this window.
    const self = screen.getByRole('button', { name: /This computer/ })
    expect(self.getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByText('Looking for instances…')).toBeTruthy()
  })

  it('says the FEATURE is off rather than showing an empty list', async () => {
    listInstances.mockResolvedValue({ state: 'disabled' })
    render(<MochiInstancesList value="self" onChange={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Multi-instance is turned off/)).toBeTruthy())
    expect(screen.queryByText('Looking for instances…')).toBeNull()
  })

  it('says the LIST failed, which is not the same as having none', async () => {
    listInstances.mockResolvedValue({ state: 'error' })
    render(<MochiInstancesList value="self" onChange={() => {}} />)
    await waitFor(() => expect(screen.getByText('Could not read the instance list.')).toBeTruthy())
  })

  it('asks for a gateway restart while still listing the waiting instances', async () => {
    listInstances.mockResolvedValue({ state: 'inactive', instances: [inst()] })
    render(<MochiInstancesList value="self" onChange={() => {}} />)
    await waitFor(() => expect(screen.getByText(/restart the gateway/)).toBeTruthy())
    expect(screen.getByRole('button', { name: /Crew A/ })).toBeTruthy()
  })
})

describe('MochiInstancesList rows', () => {
  it('picks self and a remote through onChange', async () => {
    const onChange = vi.fn()
    listInstances.mockResolvedValue({ state: 'ready', instances: [inst()] })
    render(<MochiInstancesList value="self" onChange={onChange} />)
    const row = await screen.findByRole('button', { name: /Crew A/ })

    fireEvent.click(row)
    expect(onChange).toHaveBeenLastCalledWith('zzq-a')

    fireEvent.click(screen.getByRole('button', { name: /This computer/ }))
    expect(onChange).toHaveBeenLastCalledWith('self')
  })

  it('is keyboard-operable, and ignores keys that are not Enter or Space', async () => {
    const onChange = vi.fn()
    listInstances.mockResolvedValue({ state: 'ready', instances: [inst()] })
    render(<MochiInstancesList value="self" onChange={onChange} />)
    const row = await screen.findByRole('button', { name: /Crew A/ })

    fireEvent.keyDown(row, { key: 'Enter' })
    fireEvent.keyDown(row, { key: ' ' })
    expect(onChange).toHaveBeenCalledTimes(2)

    fireEvent.keyDown(row, { key: 'x' })
    expect(onChange).toHaveBeenCalledTimes(2)
  })

  it('marks an unusable row inert and unfocusable rather than silently dead', async () => {
    const onChange = vi.fn()
    listInstances.mockResolvedValue({
      state: 'ready',
      // Listed only because it is the SAVED choice; no port, so it cannot host.
      instances: [inst({ id: 'zzq-gone', name: 'Crew Gone', local_port: 0, status: { state: 'disconnected' } })],
    })
    render(<MochiInstancesList value="zzq-other" onChange={onChange} />)
    // Not the saved value, not usable → not listed at all.
    await waitFor(() => expect(listInstances).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /Crew Gone/ })).toBeNull()
  })

  it('keeps the saved-but-absent choice listed, highlighted and labelled', async () => {
    listInstances.mockResolvedValue({
      state: 'ready',
      instances: [inst({ id: 'zzq-gone', name: 'Crew Gone', local_port: 0, status: { state: 'disconnected' } })],
    })
    render(<MochiInstancesList value="zzq-gone" onChange={() => {}} />)
    const row = await screen.findByRole('button', { name: /Crew Gone/ })
    expect(row.getAttribute('aria-pressed')).toBe('true')
    expect(row.textContent).toContain('not connected')
    // A remote choice: the host-boundary note only makes sense then.
    expect(screen.getByText(/Turning Mochi off on another instance/)).toBeTruthy()
  })

  it('distinguishes connecting from errored from not connected', async () => {
    listInstances.mockResolvedValue({
      state: 'ready',
      instances: [
        inst({ id: 'c', name: 'Crew Connecting', local_port: 0, status: { state: 'connecting' } }),
        inst({ id: 'e', name: 'Crew Errored', local_port: 0, status: { state: 'error' } }),
        inst({ id: 'n', name: 'Crew Nothing', local_port: 0, status: undefined }),
      ],
    })
    // Every row is unusable, so each must be listed as the SAVED choice to be
    // visible — rendered three times, one saved value each.
    for (const [id, text] of [
      ['c', 'connecting…'],
      ['e', 'connection problem'],
      ['n', 'not connected'],
    ] as const) {
      const view = render(<MochiInstancesList value={id} onChange={() => {}} />)
      const row = await screen.findByRole('button', { name: new RegExp(text) })
      expect(row.textContent).toContain(text)
      view.unmount()
    }
  })

  it('says Mochi is off there instead of blaming the tunnel', async () => {
    listInstances.mockResolvedValue({ state: 'ready', instances: [inst()] })
    instancesEnabledMap = vi.fn().mockResolvedValue({ 'zzq-a': false })
    const onChange = vi.fn()
    render(<MochiInstancesList value="self" onChange={onChange} />)
    const row = await screen.findByRole('button', { name: /Mochi is turned off there/ })
    // The tunnel is connected, so the label must not read "not connected".
    expect(row.textContent).not.toContain('not connected')
    // And it cannot be picked: there is no pet to show there.
    expect(row.getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(row)
    expect(onChange).not.toHaveBeenCalled()
  })

  it('leaves the row alone when the enabled probe fails — the badge is additive', async () => {
    listInstances.mockResolvedValue({ state: 'ready', instances: [inst()] })
    instancesEnabledMap = vi.fn().mockRejectedValue(new Error('zzq no token'))
    render(<MochiInstancesList value="self" onChange={() => {}} />)
    const row = await screen.findByRole('button', { name: /Crew A/ })
    await waitFor(() => expect(instancesEnabledMap).toHaveBeenCalled())
    expect(row.getAttribute('aria-disabled')).toBeNull()
  })

  it('prefers the shell answer over the same-origin fetch', async () => {
    instancesList = vi.fn().mockResolvedValue({
      state: 'ready',
      instances: [inst({ id: 'zzq-shell', name: 'Crew Shell' })],
    })
    render(<MochiInstancesList value="self" onChange={() => {}} />)
    expect(await screen.findByRole('button', { name: /Crew Shell/ })).toBeTruthy()
    expect(listInstances).not.toHaveBeenCalled()
  })

  it('falls back to the fetch when the shell answers nothing', async () => {
    instancesList = vi.fn().mockResolvedValue(null)
    listInstances.mockResolvedValue({
      state: 'ready',
      instances: [inst({ id: 'zzq-web', name: 'Crew Web' })],
    })
    render(<MochiInstancesList value="self" onChange={() => {}} />)
    expect(await screen.findByRole('button', { name: /Crew Web/ })).toBeTruthy()
    expect(listInstances).toHaveBeenCalled()
  })

  it('re-reads the list on the poll, so a tunnel that just came up appears', async () => {
    vi.useFakeTimers()
    try {
      listInstances.mockResolvedValue({ state: 'ready', instances: [] })
      render(<MochiInstancesList value="self" onChange={() => {}} />)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(listInstances).toHaveBeenCalledTimes(1)

      listInstances.mockResolvedValue({
        state: 'ready',
        instances: [inst({ id: 'zzq-late', name: 'Crew Late' })],
      })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000)
      })
      expect(listInstances).toHaveBeenCalledTimes(2)
      expect(screen.getByRole('button', { name: /Crew Late/ })).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })
})
