/**
 * The crop window entry: capture first, show the overlay only once a frame is in
 * hand, return the cropped PNG as bare base64, close on every other exit.
 * The module body is a side effect (theme, i18n, mount into `#root`), so each
 * test mounts a fresh copy. Collaborators are stubbed; the entry's logic is real.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { Root } from 'react-dom/client'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'

const h = vi.hoisted(() => ({
  supported: true,
  capture: vi.fn(),
  themes: vi.fn(),
  init: vi.fn(),
  roots: [] as Root[],
}))

vi.mock('react-dom/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-dom/client')>()
  return {
    ...actual,
    createRoot: (el: Element, opts?: Parameters<typeof actual.createRoot>[1]) => {
      const root = actual.createRoot(el, opts)
      h.roots.push(root)
      return root
    },
  }
})
vi.mock('../../../hooks/useScreenSnip', () => ({
  isScreenSnipSupported: () => h.supported,
  captureScreen: (deps: unknown) => h.capture(deps),
}))
vi.mock('../src/shared/themes', () => ({
  applyThemeVarsOnly: (...args: unknown[]) => h.themes(...args),
}))
vi.mock('../mochiLanguage', () => ({
  initMochiI18n: () => h.init(),
  MochiLocalized: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}))
vi.mock('../../../components/SnipOverlay', () => ({
  default: (props: { onComplete: (f: File) => void; onCancel: () => void }) => (
    <div>
      <button onClick={() => props.onComplete(new File(['x'], 'crop.png', { type: 'image/png' }))}>zzq-complete</button>
      <button onClick={props.onCancel}>zzq-cancel</button>
    </div>
  ),
}))

type Shell = { snipReady: ReturnType<typeof vi.fn>; snipResult: ReturnType<typeof vi.fn>; snipClose: ReturnType<typeof vi.fn> }
let shell: Shell

// Captured at load, before any test stubs them, so afterEach can put them back.
const originalMediaDevices = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')
const originalDevicePixelRatio = Object.getOwnPropertyDescriptor(window, 'devicePixelRatio')

function restoreProperty(target: object, key: string, descriptor: PropertyDescriptor | undefined) {
  if (descriptor) Object.defineProperty(target, key, descriptor)
  else delete (target as Record<string, unknown>)[key]
}

async function mountEntry() {
  vi.resetModules()
  await act(async () => {
    await import('./main')
  })
}

beforeEach(() => {
  h.supported = true
  h.capture = vi.fn(() => new Promise(() => {}))
  h.themes = vi.fn()
  h.init = vi.fn()
  shell = { snipReady: vi.fn(), snipResult: vi.fn(), snipClose: vi.fn() }
  ;(window as unknown as { mochi: Shell }).mochi = shell
  const root = document.createElement('div')
  root.id = 'root'
  document.body.appendChild(root)
})

afterEach(async () => {
  for (const root of h.roots.splice(0)) await act(async () => root.unmount())
  document.body.replaceChildren()
  delete (window as unknown as { mochi?: Shell }).mochi
  restoreProperty(navigator, 'mediaDevices', originalMediaDevices)
  restoreProperty(window, 'devicePixelRatio', originalDevicePixelRatio)
  vi.restoreAllMocks()
})

describe('mochi snip window entry', () => {
  it('applies theme variables and seeds i18n before mounting', async () => {
    const rootsSeen: Record<string, number> = {}
    h.themes = vi.fn(() => {
      rootsSeen.themes = h.roots.length
    })
    h.init = vi.fn(() => {
      rootsSeen.init = h.roots.length
    })
    await mountEntry()
    expect(h.themes).toHaveBeenCalledTimes(1)
    expect(h.init).toHaveBeenCalledTimes(1)
    // Both ran while no React root existed yet; the root is created afterwards.
    expect(rootsSeen).toEqual({ themes: 0, init: 0 })
    expect(h.roots).toHaveLength(1)
  })

  it('closes at once and never captures when screen snip is unsupported', async () => {
    h.supported = false
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    await mountEntry()
    expect(shell.snipClose).toHaveBeenCalledTimes(1)
    expect(h.capture).not.toHaveBeenCalled()
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('getDisplayMedia is not exposed'))
  })

  it('shows nothing and signals no frame while the capture is pending', async () => {
    await mountEntry()
    // The capture was started, so the absences below are checked after real work.
    expect(h.capture).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('zzq-complete')).toBeNull()
    expect(shell.snipReady).not.toHaveBeenCalled()
    expect(shell.snipClose).not.toHaveBeenCalled()
  })

  it('closes the window when the user cancels the picker', async () => {
    h.capture = vi.fn(() => Promise.resolve(null))
    await mountEntry()
    await waitFor(() => expect(shell.snipClose).toHaveBeenCalledTimes(1))
    expect(shell.snipReady).not.toHaveBeenCalled()
  })

  it('reveals the overlay once a frame is in hand, then returns the crop as bare base64', async () => {
    h.capture = vi.fn(() => Promise.resolve(document.createElement('canvas')))
    await mountEntry()
    const complete = await screen.findByText('zzq-complete')
    expect(shell.snipReady).toHaveBeenCalledTimes(1)
    fireEvent.click(complete)
    await waitFor(() => expect(shell.snipResult).toHaveBeenCalledWith('eA=='))
    expect(shell.snipClose).toHaveBeenCalledTimes(1)
  })

  it('closes without returning a result when the overlay cancels', async () => {
    h.capture = vi.fn(() => Promise.resolve(document.createElement('canvas')))
    await mountEntry()
    fireEvent.click(await screen.findByText('zzq-cancel'))
    expect(shell.snipClose).toHaveBeenCalledTimes(1)
    expect(shell.snipResult).not.toHaveBeenCalled()
  })

  it('asks the display for a native-resolution still and builds the canvas and video seams', async () => {
    const getDisplayMedia = vi.fn(() => Promise.reject(new Error('picker dismissed')))
    Object.defineProperty(navigator, 'mediaDevices', { value: { getDisplayMedia }, configurable: true })
    Object.defineProperty(window, 'devicePixelRatio', { value: 2, configurable: true })
    // The seams are recorded here and checked after the entry returns, so a failed
    // expectation cannot be swallowed by the entry's own promise chain.
    let seams: { video: unknown; canvas: unknown } | undefined
    h.capture = vi.fn(async (deps: { getDisplayMedia: () => Promise<unknown>; createVideo: () => unknown; createCanvas: () => unknown }) => {
      await deps.getDisplayMedia().catch(() => undefined)
      seams = { video: deps.createVideo(), canvas: deps.createCanvas() }
      return null
    })
    await mountEntry()
    await waitFor(() => expect(shell.snipClose).toHaveBeenCalledTimes(1))
    expect((seams?.video as HTMLElement | undefined)?.tagName).toBe('VIDEO')
    expect((seams?.canvas as HTMLElement | undefined)?.tagName).toBe('CANVAS')
    expect(getDisplayMedia).toHaveBeenCalledWith({
      video: {
        width: { ideal: Math.round(window.screen.width * 2) },
        height: { ideal: Math.round(window.screen.height * 2) },
        frameRate: { ideal: 5 },
      },
    })
  })
})
