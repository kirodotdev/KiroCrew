/**
 * The three Mochi window entry points (panel / avatars / settings).
 *
 * Each is a module whose whole body is a side effect: write the theme variables,
 * seed i18n BEFORE first paint (so no window flashes the fallback language),
 * then mount into `#root`. None of that had a test, and every failure mode is
 * silent — a missing `initMochiI18n()` renders English for a moment, a mount
 * outside the error boundary turns a render throw into a black window, and a
 * missing `#root` guard throws during module evaluation, which in a real window
 * means a blank page with the error only in the shell log.
 *
 * `createRoot` is SPIED, not replaced: the real one still mounts, so the tree
 * these entries actually build is exercised (that is how the settings window's
 * effect and its close handler get run) and @testing-library keeps working.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { waitFor } from '@testing-library/react'
import type { Root } from 'react-dom/client'

const created: Root[] = []
const createRootSpy = vi.fn()

vi.mock('react-dom/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-dom/client')>()
  return {
    ...actual,
    createRoot: (el: Element, opts?: Parameters<typeof actual.createRoot>[1]) => {
      createRootSpy(el)
      const root = actual.createRoot(el, opts)
      created.push(root)
      return root
    },
  }
})

const applyTheme = vi.fn()
vi.mock('../src/shared/themes', () => ({
  get applyTheme() {
    return applyTheme
  },
}))

const initMochiI18n = vi.fn()
vi.mock('../mochiLanguage', () => ({
  get initMochiI18n() {
    return initMochiI18n
  },
  MochiLocalized: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}))

const closeSettings = vi.fn()
vi.mock('../src/mochiApi', () => ({
  api: {
    get closeSettings() {
      return closeSettings
    },
  },
}))

// The three window bodies are stubbed: what is under test is the MOUNT, and each
// real panel opens its own bridge traffic on mount.
vi.mock('../src/renderer/GalleryPanel', () => ({
  GalleryPanel: () => <div>zzq-gallery</div>,
}))
vi.mock('../src/renderer/ChatApp', () => ({
  ChatApp: () => <div>zzq-chat</div>,
}))
vi.mock('../panel/MochiSnipHost', () => ({
  MochiSnipHost: () => <div>zzq-snip</div>,
}))
vi.mock('../src/renderer/SettingsPanel', () => ({
  SettingsPanel: ({ onClose }: { onClose: () => void }) => (
    <button onClick={onClose}>zzq-close</button>
  ),
}))

function mountPoint(): HTMLElement {
  const el = document.createElement('div')
  el.id = 'root'
  document.body.appendChild(el)
  return el
}

beforeEach(() => {
  vi.resetModules()
  createRootSpy.mockClear()
  applyTheme.mockClear()
  initMochiI18n.mockClear()
  closeSettings.mockClear()
  // replaceChildren, not an innerHTML write: the frontend-security rule in
  // website/AUTOSDE.yaml is blocking and bans innerHTML writes under
  // src/**/*.tsx, tests included.
  document.body.replaceChildren()
  document.title = ''
})

afterEach(() => {
  for (const root of created.splice(0)) root.unmount()
})

describe('panel window entry', () => {
  it('themes, seeds i18n, and mounts the chat app plus the capture host inside the boundary', async () => {
    const el = mountPoint()
    await import('../panel/main')
    expect(applyTheme).toHaveBeenCalledTimes(1)
    expect(initMochiI18n).toHaveBeenCalledTimes(1)
    expect(createRootSpy).toHaveBeenCalledWith(el)
    await waitFor(() => expect(el.textContent).toContain('zzq-chat'))
    // The snip host renders nothing until a capture starts, but it must be
    // MOUNTED — it is what owns the overlay.
    expect(el.textContent).toContain('zzq-snip')
  })

  it('does not mount when the document has no #root', async () => {
    await import('../panel/main')
    // Still themed and seeded — those are unconditional — but no mount attempt,
    // which is what keeps module evaluation from throwing.
    expect(applyTheme).toHaveBeenCalledTimes(1)
    expect(createRootSpy).not.toHaveBeenCalled()
  })
})

describe('avatars window entry', () => {
  it('mounts the gallery inside the boundary', async () => {
    const el = mountPoint()
    await import('../avatar/main')
    expect(applyTheme).toHaveBeenCalledTimes(1)
    expect(initMochiI18n).toHaveBeenCalledTimes(1)
    expect(createRootSpy).toHaveBeenCalledWith(el)
    await waitFor(() => expect(el.textContent).toContain('zzq-gallery'))
  })

  it('does not mount when the document has no #root', async () => {
    await import('../avatar/main')
    expect(createRootSpy).not.toHaveBeenCalled()
  })
})

describe('settings window entry', () => {
  it('titles the window and closes the WINDOW, not just the panel', async () => {
    const el = mountPoint()
    await import('../settings/main')
    expect(initMochiI18n).toHaveBeenCalledTimes(1)
    expect(createRootSpy).toHaveBeenCalledWith(el)

    // The theme is applied from the component's effect here (not at module
    // scope), so it only lands once the tree has actually mounted.
    await waitFor(() => expect(applyTheme).toHaveBeenCalledTimes(1))
    expect(document.title).toBe('Settings')

    const btn = el.querySelector('button')!
    btn.click()
    expect(closeSettings).toHaveBeenCalledTimes(1)
  })

  it('does not mount when the document has no #root', async () => {
    await import('../settings/main')
    expect(initMochiI18n).toHaveBeenCalledTimes(1)
    expect(createRootSpy).not.toHaveBeenCalled()
    // The effect never ran, so the title stays whatever the shell set.
    expect(applyTheme).not.toHaveBeenCalled()
  })
})
