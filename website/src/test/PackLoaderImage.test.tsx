/**
 * An installed pack's loader art can animate by itself (an animated WebP/GIF, or
 * an SVG with its own animation), and nothing on the page can pause an image's
 * own frames. Under reduced motion the art is therefore painted once onto a
 * canvas, which holds still; with motion allowed it stays the plain `<img>`.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent, act, cleanup } from '@testing-library/react'
import { PackLoaderImage, paintStill } from '../pages/chat/PackLoaderImage'
import ChatFooter from '../pages/chat/ChatFooter'

const SRC = '/api/theme/reef/assets/loader/cursor.svg'

/** A live `prefers-reduced-motion` media query whose answer the test flips. */
function motionPreference(reduce: boolean) {
  const listeners = new Set<() => void>()
  const mql = {
    get matches() { return reduce },
    media: '(prefers-reduced-motion: reduce)',
    addEventListener: (_: string, cb: () => void) => { listeners.add(cb) },
    removeEventListener: (_: string, cb: () => void) => { listeners.delete(cb) },
  }
  vi.spyOn(window, 'matchMedia').mockImplementation(() => mql as unknown as MediaQueryList)
  return {
    set(next: boolean) {
      reduce = next
      act(() => { listeners.forEach(cb => cb()) })
    },
  }
}

/** A 2D context that records what was drawn, since happy-dom ships no canvas. */
function stubCanvas() {
  const drawImage = vi.fn()
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
    () => ({ drawImage }) as unknown as CanvasRenderingContext2D,
  )
  return drawImage
}

function withNaturalSize(img: HTMLImageElement, w: number, h: number) {
  Object.defineProperty(img, 'naturalWidth', { value: w, configurable: true })
  Object.defineProperty(img, 'naturalHeight', { value: h, configurable: true })
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  document.documentElement.removeAttribute('data-theme')
})

describe('PackLoaderImage', () => {
  it('is the plain animated <img> when motion is allowed', () => {
    motionPreference(false)
    const { container } = render(<PackLoaderImage className="kp" src={SRC} />)
    const img = container.querySelector('img')
    expect(img?.getAttribute('src')).toBe(SRC)
    expect(img?.style.display).toBe('')
    expect(container.querySelector('canvas')).toBeNull()
  })

  it('paints one still frame onto a canvas under reduced motion, and shows no live image', () => {
    motionPreference(true)
    const drawImage = stubCanvas()
    const { container } = render(<PackLoaderImage className="kp" src={SRC} />)
    const canvas = container.querySelector('canvas')!
    const img = container.querySelector('img')!
    expect(canvas.className).toBe('kp')
    expect(canvas.getAttribute('aria-hidden')).toBe('true')
    // The only <img> is the decode-only copy, and it is never displayed.
    expect(img.getAttribute('src')).toBe(SRC)
    expect(img.style.display).toBe('none')
    expect(drawImage).not.toHaveBeenCalled()
    fireEvent.load(img)
    expect(drawImage).toHaveBeenCalledTimes(1)
    expect(drawImage.mock.calls[0][0]).toBe(img)
  })

  it('follows a preference flipped while the page is open', () => {
    const pref = motionPreference(false)
    stubCanvas()
    const { container } = render(<PackLoaderImage src={SRC} />)
    expect(container.querySelector('canvas')).toBeNull()
    pref.set(true)
    expect(container.querySelector('canvas')).not.toBeNull()
    pref.set(false)
    expect(container.querySelector('canvas')).toBeNull()
    expect(container.querySelector('img')?.style.display).toBe('')
  })

  it('fits the art inside the box like object-fit: contain', () => {
    const drawImage = stubCanvas()
    const canvas = document.createElement('canvas')
    const img = document.createElement('img')
    withNaturalSize(img, 40, 20)
    paintStill(canvas, img)  // no layout box here, so the 14px slot edge is used
    expect([canvas.width, canvas.height]).toEqual([14, 14])
    expect(drawImage).toHaveBeenCalledWith(img, 0, 3.5, 14, 7)
  })

  it('fills the box for an image that reports no intrinsic size', () => {
    const drawImage = stubCanvas()
    const canvas = document.createElement('canvas')
    const img = document.createElement('img')
    withNaturalSize(img, 0, 0)
    paintStill(canvas, img)
    expect(drawImage).toHaveBeenCalledWith(img, 0, 0, 14, 14)
  })

  it('leaves the canvas blank instead of throwing when the image cannot be drawn', () => {
    const drawImage = stubCanvas()
    drawImage.mockImplementation(() => { throw new DOMException('broken', 'InvalidStateError') })
    const img = document.createElement('img')
    withNaturalSize(img, 10, 10)
    expect(() => paintStill(document.createElement('canvas'), img)).not.toThrow()
  })
})

describe('ChatFooter — pack loader art under reduced motion', () => {
  const running = { running: true, stopping: false, state: '', lastRole: 'user' }

  it('holds a single pack loader image still', () => {
    motionPreference(true)
    stubCanvas()
    document.documentElement.setAttribute('data-theme', 'custom-reef-dark')
    const { container } = render(<FooterWithPack images={['loader/cursor.svg']} {...running} />)
    expect(container.querySelector('canvas')).not.toBeNull()
    const visible = Array.from(container.querySelectorAll('img')).filter(i => i.style.display !== 'none')
    expect(visible).toEqual([])
  })

  it('drops the art when the image fails to load, as it did before', () => {
    motionPreference(true)
    stubCanvas()
    document.documentElement.setAttribute('data-theme', 'custom-reef-dark')
    const { container, getByRole } = render(<FooterWithPack images={['loader/cursor.svg']} {...running} />)
    fireEvent.error(container.querySelector('img')!)
    expect(container.querySelector('canvas')).toBeNull()
    expect(getByRole('status')).toBeInTheDocument()
  })

  it('holds every image of a cycled set still', () => {
    motionPreference(true)
    stubCanvas()
    document.documentElement.setAttribute('data-theme', 'custom-reef-dark')
    const images = ['loader/a.gif', 'loader/b.gif', 'loader/c.gif', 'loader/d.gif']
    const { container } = render(<FooterWithPack images={images} {...running} />)
    expect(container.querySelectorAll('.csb4 canvas').length).toBe(8)  // 4 slots x 2 layers
    const visible = Array.from(container.querySelectorAll('img')).filter(i => i.style.display !== 'none')
    expect(visible).toEqual([])
  })

  // The footer resolves its loader on every render. A new component type per
  // render would remount each slot's image and blank its still frame until the
  // next paint, which reads as a flicker exactly when motion was asked to stop.
  it('keeps each cycled image mounted across footer re-renders', () => {
    motionPreference(true)
    const drawImage = stubCanvas()
    document.documentElement.setAttribute('data-theme', 'custom-reef-dark')
    const images = ['loader/a.gif', 'loader/b.gif', 'loader/c.gif', 'loader/d.gif']
    const { container, rerender } = render(<FooterWithPack images={images} {...running} streamTick={1} />)
    container.querySelectorAll('img').forEach(img => fireEvent.load(img))
    expect(drawImage).toHaveBeenCalledTimes(8)
    const canvases = Array.from(container.querySelectorAll('canvas'))
    rerender(<FooterWithPack images={images} {...running} streamTick={2} />)
    expect(Array.from(container.querySelectorAll('canvas'))).toEqual(canvases)
    expect(drawImage).toHaveBeenCalledTimes(8)
  })
})

// ChatFooter reads installed-pack assets from the optional theme context. Mock
// just that read so the footer resolves the pack's loader images.
const packAssets = vi.hoisted(() => ({ loaderImages: [] as string[] }))
vi.mock('../hooks/useTheme', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../hooks/useTheme')>()
  return {
    ...actual,
    useOptionalTheme: () => ({
      customThemeDataMap: new Map([['reef', { slug: 'reef', assets: packAssets }]]),
    }),
  }
})

function FooterWithPack({ images, ...props }: { images: string[]; running: boolean; stopping: boolean; state: string; lastRole: string; streamTick?: number }) {
  packAssets.loaderImages = images
  return <ChatFooter {...props} />
}
