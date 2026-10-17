import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, cleanup } from '@testing-library/react'
import { api } from '../api/client'
import { open } from './folderConfigModal.testHelpers'

// Split out of FolderConfigModal.test.tsx (file-line-cap) — see
// folderConfigModal.testHelpers.tsx for the shared render/open/fixtures this
// file and its siblings all use.
//
// vi.mock factories are hoisted above all imports, so the factory body is
// inlined here rather than imported from the helper module.
vi.mock('../api/client', () => ({
  api: {
    recentProjects: vi.fn().mockResolvedValue({ dirs: [] }),
    favoriteProjects: vi.fn().mockResolvedValue({ dirs: [] }),
    browseDirs: vi.fn().mockResolvedValue({ path: '/', parent: '', dirs: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
  },
}))

beforeEach(() => {
  vi.clearAllMocks()
  // A test that sets a persistent roster (mockResolvedValue / mockImplementation
  // on kirocrewAgents) would otherwise leak it into the next test. Re-assert
  // the default empty roster each time; this overrides a leaked implementation
  // without mockReset()'ing the other api.* factory mocks the suite relies on.
  ;(api.kirocrewAgents as unknown as ReturnType<typeof vi.fn>)
    .mockResolvedValue({ agents: [], default_agent: '' })
})

describe('footer hint dims beside a disabled Save (UX Review)', () => {
  // The "Enter to submit" hint renders unconditionally regardless of
  // canSubmit — pressing Enter while Save is disabled does nothing, so a
  // full-strength hint keeps promising a shortcut that is dead.
  //
  // Prominence is asserted as a relationship, not a class name: swapping
  // `text-muted-strong` for `text-muted` passed a class assertion while
  // BRIGHTENING the hint on dark themes, because that token pair has no
  // fixed lightness order across themes. jsdom resolves neither Tailwind nor
  // CSS variables, so the test pins the two facts that make "disabled is less
  // prominent" theme-independent: the colour source is identical in both
  // states, and the disabled state's computed opacity is strictly lower.
  // Same colour at lower opacity blends toward the background and so has
  // lower contrast against it in any theme.
  function hint() {
    return screen.getByText('Enter to submit')
  }

  function sample(name: string) {
    open()
    if (name) fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: name } })
    const el = hint()
    return {
      disabled: (screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled,
      className: el.className,
      opacity: Number(getComputedStyle(el).opacity || '1'),
      inDocument: el.isConnected,
    }
  }

  it('is less prominent while Save is disabled than while it is enabled, in place', () => {
    const disabled = sample('')
    cleanup()
    const enabled = sample('Payments')

    expect(disabled.disabled).toBe(true)
    expect(enabled.disabled).toBe(false)
    // Never hidden: the element stays rendered with the mr-auto spacer.
    expect(disabled.inDocument).toBe(true)
    expect(disabled.className).toContain('mr-auto')
    // Same colour source in both states, so the only difference is opacity.
    expect(disabled.className).toBe(enabled.className)
    expect(enabled.opacity).toBe(1)
    expect(disabled.opacity).toBeGreaterThan(0)
    expect(disabled.opacity).toBeLessThan(enabled.opacity)
  })
})
