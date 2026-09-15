import { resolveSlotOverlays, type OverlayAppRecord } from './overlaySlots'

/**
 * Unit tests for host overlay slot resolution — a pure function over the
 * `/api/apps` subset, no React and no registry mutation.
 */

function app(over: Partial<OverlayAppRecord> = {}): OverlayAppRecord {
  return {
    name: 'command-bar',
    enabled: true,
    // The shipped claimant is a builtin; provenance is what the slot check turns on.
    origin: 'builtin',
    manifest: {
      ui: { overlays: [{ id: 'command-bar', replaces: 'quick-search' }] },
    },
    ...over,
  }
}

describe('resolveSlotOverlays', () => {
  it('gives the slot to an enabled app that claims it', () => {
    expect(resolveSlotOverlays([app()])['quick-search']).toEqual({
      app: 'command-bar',
      overlayId: 'command-bar',

    })
  })

  it('leaves the slot to the host while the app is disabled', () => {
    // The app's enable state IS the user's opt-in: a disabled app must not
    // displace the host's own surface.
    expect(resolveSlotOverlays([app({ enabled: false })])['quick-search']).toBeUndefined()
    expect(resolveSlotOverlays([app({ enabled: undefined })])['quick-search']).toBeUndefined()
  })

  it('ignores a declaration with no id or no replaces', () => {
    const noId = app({
      manifest: { ui: { overlays: [{ replaces: 'quick-search' }] } },
    })
    const noSlot = app({ manifest: { ui: { overlays: [{ id: 'command-bar' }] } } })
    expect(resolveSlotOverlays([noId])['quick-search']).toBeUndefined()
    expect(resolveSlotOverlays([noSlot])['quick-search']).toBeUndefined()
  })

  it('refuses an unknown host slot without throwing', () => {
    // Manifest data is third-party input, so a bogus slot degrades to "host keeps
    // its surface" — it must never take the dashboard down (contrast
    // reportSeamCollision, which throws in dev/test for developer-authored
    // registrations).
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const bogus = app({
      manifest: {
        ui: { overlays: [{ id: 'command-bar', replaces: 'not-a-real-slot' }] },
      },
    })
    expect(() => resolveSlotOverlays([bogus])).not.toThrow()
    expect(resolveSlotOverlays([bogus])).toEqual({})
    expect(warn).toHaveBeenCalled()
    warn.mockRestore()
  })

  it('refuses an overlay id with no registered component without throwing', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const ghost = app({
      manifest: {
        ui: { overlays: [{ id: 'not-bundled', replaces: 'quick-search' }] },
      },
    })
    expect(() => resolveSlotOverlays([ghost])).not.toThrow()
    expect(resolveSlotOverlays([ghost])).toEqual({})
    warn.mockRestore()
  })

  it('resolves a contested slot by app name, not response order', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const rival = app({
      name: 'zzz-other',
      manifest: {
        ui: { overlays: [{ id: 'command-bar', replaces: 'quick-search' }] },
      },
    })
    const forward = resolveSlotOverlays([app(), rival])['quick-search']
    const reversed = resolveSlotOverlays([rival, app()])['quick-search']
    expect(forward?.app).toBe('command-bar')
    expect(reversed?.app).toBe('command-bar')
    warn.mockRestore()
  })

  it('refuses a non-builtin app that claims a host slot', () => {
    // The reachable bypass this closes: an overlay id only resolves to a component
    // in this bundle, so a third party cannot supply its own -- but it can NAME a
    // builtin's id. A self-managed app persists its own manifest through the
    // register path, so without a provenance check, declaring `command-bar` would
    // replace Cmd+K while the Command Bar app is disabled, defeating the opt-in.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    for (const origin of ['external', 'registry', 'local', undefined]) {
      const intruder = app({ name: 'other-app', origin })
      expect(resolveSlotOverlays([intruder])).toEqual({})
    }
    expect(warn).toHaveBeenCalled()
    warn.mockRestore()
  })

  it('returns no owners for an empty app list', () => {
    expect(resolveSlotOverlays([])).toEqual({})
  })

  it('tolerates an app with no manifest at all', () => {
    expect(() => resolveSlotOverlays([{ name: 'bare', enabled: true }])).not.toThrow()
  })

  it('refuses an inherited Object property as an overlay id', () => {
    // The slug grammar admits `constructor`, `toString` and friends, so a membership
    // test written with `in` would accept them and the lookup would hand the host a
    // function to render as a component.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    for (const id of ['constructor', 'toString', 'valueOf']) {
      const poisoned = app({
        manifest: { ui: { overlays: [{ id, replaces: 'quick-search' }] } },
      })
      expect(resolveSlotOverlays([poisoned])).toEqual({})
    }
    warn.mockRestore()
  })
})
