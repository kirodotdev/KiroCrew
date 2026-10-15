import { afterEach, describe, expect, it, vi } from 'vitest'
import { handleDashboardFields, subscribeDashboardFields } from './dashboardFields'

/** Every test disposes what it subscribed; the registry is module state. */
const disposers: Array<() => void> = []
const listen = (slug: string, fn: (read: { [k: string]: unknown }) => void) => {
  const off = subscribeDashboardFields(slug, fn)
  disposers.push(off)
  return off
}
afterEach(() => {
  while (disposers.length) disposers.pop()?.()
})

const frame = (slug: string, fields: { [k: string]: unknown }, version = 1) => ({
  slug,
  read: { fields, agentic: [], seq: 1, stale: false, missing: [], patch: true, version },
})

describe('handleDashboardFields: which dashboard a field frame reaches', () => {
  it('delivers a frame to the dashboard showing that mate', () => {
    const got = vi.fn()
    listen('atlas', got)
    handleDashboardFields(frame('atlas', { boards: 3 }))
    expect(got).toHaveBeenCalledTimes(1)
    expect((got.mock.calls[0][0] as { fields: unknown }).fields).toEqual({ boards: 3 })
  })

  it('does not deliver one mate frame to another mate dashboard', () => {
    // Two tabs can show two crewmates at once, and a frame belongs to exactly one:
    // delivering it to both would put one crewmate's work under the other's name.
    const atlas = vi.fn()
    const nyx = vi.fn()
    listen('atlas', atlas)
    listen('nyx', nyx)
    handleDashboardFields(frame('atlas', { boards: 3 }))
    expect(atlas).toHaveBeenCalledTimes(1)
    expect(nyx).not.toHaveBeenCalled()
  })

  it('drops a frame for a mate nothing is showing', () => {
    // The ordinary case: the socket is per connection, not per tab.
    expect(() => handleDashboardFields(frame('nobody', { boards: 3 }))).not.toThrow()
  })

  it('refuses a malformed frame rather than delivering a broken read', () => {
    const got = vi.fn()
    listen('atlas', got)
    for (const bad of [
      null,
      undefined,
      {},
      { slug: 'atlas' },
      { slug: 'atlas', read: null },
      { slug: 'atlas', read: 'not an object' },
      { slug: 42, read: { fields: {} } },
      { read: { fields: {} } },
    ]) {
      handleDashboardFields(bad)
    }
    expect(got).not.toHaveBeenCalled()
  })

  it('stops delivering once the dashboard unmounts', () => {
    const got = vi.fn()
    const off = listen('atlas', got)
    off()
    handleDashboardFields(frame('atlas', { boards: 3 }))
    expect(got).not.toHaveBeenCalled()
  })

  it('keeps delivering to a second dashboard when the first unmounts', () => {
    const first = vi.fn()
    const second = vi.fn()
    const off = listen('atlas', first)
    listen('atlas', second)
    off()
    handleDashboardFields(frame('atlas', { boards: 3 }))
    expect(first).not.toHaveBeenCalled()
    expect(second).toHaveBeenCalledTimes(1)
  })

  it('survives a listener that unsubscribes during delivery', () => {
    // A copy of the set is iterated, so a listener removing itself mid-delivery
    // cannot make the iteration skip its neighbour.
    const second = vi.fn()
    const off = listen('atlas', () => off())
    listen('atlas', second)
    handleDashboardFields(frame('atlas', { boards: 3 }))
    expect(second).toHaveBeenCalledTimes(1)
  })

  it('does not let one dashboard throw cost another its refill', () => {
    const second = vi.fn()
    listen('atlas', () => {
      throw new Error('render failed')
    })
    listen('atlas', second)
    expect(() => handleDashboardFields(frame('atlas', { boards: 3 }))).not.toThrow()
    expect(second).toHaveBeenCalledTimes(1)
  })

  it('takes no listener for an empty slug and its disposer is safe to call', () => {
    const got = vi.fn()
    const off = subscribeDashboardFields('', got)
    handleDashboardFields(frame('', { boards: 3 }))
    expect(got).not.toHaveBeenCalled()
    expect(() => off()).not.toThrow()
  })

  it('passes the patch through whole, so the frame can merge and version it', () => {
    // The merge and the gap check are the PAGE's, inside the frame. This layer must
    // not summarise a patch down to its fields, or the version would never arrive
    // and every patch would read as the first one.
    const got = vi.fn()
    listen('atlas', got)
    handleDashboardFields(frame('atlas', { boards: 3 }, 7))
    const read = got.mock.calls[0][0] as { patch: unknown; version: unknown }
    expect(read.patch).toBe(true)
    expect(read.version).toBe(7)
  })
})


describe('a frame names the package it belongs to', () => {
  const packaged = (slug: string, dashboard: string, boards: number) => ({
    slug,
    dashboard,
    read: { fields: { boards }, agentic: [], seq: 1, stale: false, missing: [], patch: true, version: 2 },
  })

  it('hands the package id to the listener so a tab can drop a foreign frame', () => {
    // One crewmate can be shown through two packages at once -- two tabs, or a
    // template page beside a package page -- and the socket delivers every frame to
    // every tab. Without the id on the frame, each tab applies the other's patches and
    // its values come from a layout it is not rendering.
    const got = vi.fn()
    listen('atlas', got)
    handleDashboardFields(packaged('atlas', 'atlas-tree', 3))
    expect(got).toHaveBeenCalledWith(expect.objectContaining({ fields: { boards: 3 } }), 'atlas-tree')
  })

  it('passes an empty package id when the frame carries none', () => {
    // A frame from a host that names no package is delivered rather than dropped: the
    // listener decides, and a tab showing no package has nothing to compare against.
    const got = vi.fn()
    listen('atlas', got)
    handleDashboardFields(frame('atlas', { boards: 1 }))
    expect(got.mock.calls[0][1]).toBe('')
  })

  it('refuses a non-string package id rather than passing it on', () => {
    const got = vi.fn()
    listen('atlas', got)
    handleDashboardFields({ slug: 'atlas', dashboard: 42, read: { fields: { boards: 1 } } })
    expect(got.mock.calls[0][1]).toBe('')
  })
})
