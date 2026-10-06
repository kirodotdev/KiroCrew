/**
 * Regression for #11732: the image viewer (Lightbox) and the diagram viewer
 * (DiagramLightbox) put their controls at the top-right of a full-window
 * overlay portaled to <body>. On a frameless desktop window that corner is the
 * caption band: Windows paints its minimize/maximize/close buttons over it,
 * frameless Linux injects its own, and the band is a window drag strip, so the
 * viewers' download/close buttons could not be clicked and a click there could
 * close the whole app instead.
 *
 * Both overlays live outside the shell div that carries `.win-electron` /
 * `.linux-electron`, so the header's CSS insets never reach them; the clearance
 * comes from overlayCaptionClearancePx() and drops the control row below the
 * 42px band. These cases pin where it applies:
 *   - frameless Windows / frameless Linux        -> 42px
 *   - embedded pane under a Windows host (.embedded-win-inset on <html>) -> 42px
 *   - plain browser, macOS, framed Linux         -> no inline style
 *
 * The platform consts in src/lib/electron.ts are captured once at module load
 * from `window.kirocrew`, so each case sets it, resets the module registry and
 * re-imports the component fresh.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, act, cleanup, waitFor } from '@testing-library/react'

// The copy outcome ("Image copied" pill vs "copy failed" notice) is driven by
// the clipboard util. Mock it so each case picks the branch it pins, without a
// real clipboard. imageBlobToPng is pass-through; copyImageToClipboard resolves
// the boolean the Lightbox turns into copyState 'ok' / 'failed'.
const copyOutcome = vi.fn<[], Promise<boolean>>()
vi.mock('../utils/clipboard', () => ({
  copyImageToClipboard: () => copyOutcome(),
  imageBlobToPng: (p: Promise<Blob>) => p,
}))

type Shell = Record<string, unknown> | undefined

function setShell(kirocrew: Shell) {
  if (kirocrew === undefined) {
    delete (window as unknown as { kirocrew?: unknown }).kirocrew
  } else {
    ;(window as unknown as { kirocrew: unknown }).kirocrew = kirocrew
  }
  vi.resetModules()
}

async function lightboxToolbar(kirocrew: Shell): Promise<HTMLElement> {
  setShell(kirocrew)
  const { Lightbox } = await import('../components/markdown/Lightbox')
  const { container } = render(<Lightbox />)
  act(() => {
    window.dispatchEvent(new CustomEvent('lightbox', {
      detail: { images: [{ src: 'a.png', alt: 'a' }], index: 0 },
    }))
  })
  const el = container.querySelector<HTMLElement>('[data-testid="lightbox-toolbar"]')
  if (!el) throw new Error('lightbox toolbar did not render')
  return el
}

async function diagramHeader(kirocrew: Shell): Promise<HTMLElement> {
  setShell(kirocrew)
  const { default: DiagramLightbox } = await import('../components/DiagramLightbox')
  render(<DiagramLightbox svg="<svg></svg>" onClose={() => {}} />)
  const el = document.body.querySelector<HTMLElement>('[data-testid="diagram-lightbox-header"]')
  if (!el) throw new Error('diagram viewer header did not render')
  return el
}

/**
 * Mount the image viewer, drive a copy to `succeed`, and return the surface it
 * produces: the "Image copied" pill (`lightbox-copy-status`) on success, the
 * "copy failed" notice (`lightbox-copy-error`) on failure. The copy is fired by
 * the bare-`c` shortcut, which needs the async Clipboard API present, so a
 * stub `navigator.clipboard.write` and `ClipboardItem` are installed; the real
 * write never runs because `copyImageToClipboard` is mocked.
 */
async function copySurface(
  kirocrew: Shell,
  succeed: boolean,
  testId: 'lightbox-copy-status' | 'lightbox-copy-error',
): Promise<HTMLElement> {
  copyOutcome.mockResolvedValue(succeed)
  const nav = navigator as unknown as { clipboard?: unknown }
  const priorDesc = Object.getOwnPropertyDescriptor(nav, 'clipboard')
  Object.defineProperty(nav, 'clipboard', { value: { write: vi.fn() }, configurable: true, writable: true })
  const hadClipboardItem = 'ClipboardItem' in globalThis
  ;(globalThis as unknown as { ClipboardItem: unknown }).ClipboardItem = function () {}
  try {
    setShell(kirocrew)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, blob: () => Promise.resolve(new Blob()) }))
    const { Lightbox } = await import('../components/markdown/Lightbox')
    const { container } = render(<Lightbox />)
    act(() => {
      window.dispatchEvent(new CustomEvent('lightbox', {
        detail: { images: [{ src: 'a.png', alt: 'a' }], index: 0 },
      }))
    })
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'c', bubbles: true }))
      // The copy runs fetch -> png -> clipboard, so the state flip to 'ok' /
      // 'failed' lands a macrotask later, not on the microtask queue alone.
      await new Promise(resolve => setTimeout(resolve, 0))
    })
    let el: HTMLElement | null = null
    await waitFor(() => {
      el = container.querySelector<HTMLElement>(`[data-testid="${testId}"]`)
      if (!el) throw new Error(`${testId} did not render`)
    })
    // The pill carries the inline style on the testId element itself; the error
    // notice is an <ErrorNotice> whose testId sits on its own root, wrapped by
    // the div that carries the clearance style — read that wrapper for it.
    const styled = testId === 'lightbox-copy-error'
      ? (el as unknown as HTMLElement).closest<HTMLElement>('div.fixed') ?? (el as unknown as HTMLElement).parentElement
      : (el as unknown as HTMLElement)
    if (!styled) throw new Error(`${testId} wrapper not found`)
    return styled
  } finally {
    vi.unstubAllGlobals()
    if (priorDesc) Object.defineProperty(nav, 'clipboard', priorDesc)
    else delete nav.clipboard
    if (!hadClipboardItem) delete (globalThis as unknown as { ClipboardItem?: unknown }).ClipboardItem
  }
}

afterEach(() => {
  cleanup()
  copyOutcome.mockReset()
  delete (window as unknown as { kirocrew?: unknown }).kirocrew
  document.documentElement.classList.remove('embedded-win-inset')
})

const CLEARED: Array<[string, Shell, boolean]> = [
  ['frameless Windows', { isElectron: true, platform: 'win32' }, false],
  ['frameless Linux', { isElectron: true, platform: 'linux', linuxFrameless: true }, false],
  ['an embedded pane under a Windows host', undefined, true],
]
const UNTOUCHED: Array<[string, Shell]> = [
  ['a plain browser tab', undefined],
  ['macOS (caption lives top-left)', { isElectron: true, platform: 'darwin' }],
  ['framed Linux (native title bar)', { isElectron: true, platform: 'linux', linuxFrameless: false }],
]

describe('viewer controls clear the caption band (#11732)', () => {
  for (const [name, shell, embedded] of CLEARED) {
    it(`drops the image viewer toolbar below the caption band on ${name}`, async () => {
      if (embedded) document.documentElement.classList.add('embedded-win-inset')
      expect((await lightboxToolbar(shell)).style.marginTop).toBe('42px')
    })
    it(`drops the diagram viewer header below the caption band on ${name}`, async () => {
      if (embedded) document.documentElement.classList.add('embedded-win-inset')
      expect((await diagramHeader(shell)).style.marginTop).toBe('42px')
    })
  }
  for (const [name, shell] of UNTOUCHED) {
    it(`leaves the image viewer toolbar alone on ${name}`, async () => {
      expect((await lightboxToolbar(shell)).style.marginTop).toBe('')
    })
    it(`leaves the diagram viewer header alone on ${name}`, async () => {
      expect((await diagramHeader(shell)).style.marginTop).toBe('')
    })
  }

  // The two copy surfaces sit a row BELOW the toolbar (top-safe-offset-16), so on
  // a frameless window the cleared toolbar drops onto their row and the notice,
  // later in the DOM, paints over the toolbar's left end. Both carry the same
  // clearance so they stay below the band too.
  const WINDOWS: Shell = { isElectron: true, platform: 'win32' }

  it('drops the "copy failed" notice below the caption band on frameless Windows', async () => {
    // Blocking review item: without the clearance this notice overlapped the
    // toolbar's zoom-out button on 550-690px frameless windows (#17328 review).
    expect((await copySurface(WINDOWS, false, 'lightbox-copy-error')).style.marginTop).toBe('42px')
  })
  it('leaves the "copy failed" notice alone in a plain browser tab', async () => {
    expect((await copySurface(undefined, false, 'lightbox-copy-error')).style.marginTop).toBe('')
  })

  it('drops the "Image copied" pill below the caption band on frameless Windows', async () => {
    // Pins the copyState==='ok' pill clearance that no case covered before — a
    // regression that removing it would otherwise leave green (#17328 review).
    expect((await copySurface(WINDOWS, true, 'lightbox-copy-status')).style.marginTop).toBe('42px')
  })
  it('leaves the "Image copied" pill alone in a plain browser tab', async () => {
    expect((await copySurface(undefined, true, 'lightbox-copy-status')).style.marginTop).toBe('')
  })
})
