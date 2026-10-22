/**
 * The image comment layer on its own: a drag draws a box, a plain click is left
 * to the image (the lightbox), a click on a box reveals its comment, and a drag
 * that starts on a box draws a new one.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useRef } from 'react'
import { render, screen, fireEvent } from '@testing-library/react'
import { ImageCommentLayer, MIN_BOX_PX, RELEASE_CLICK_MS } from '../components/ImageCommentLayer'
import { LocalOverlayRoot } from '../components/overlayRoot'
import { WHOLE_IMAGE } from '../utils/imageComments'
import type { ImageBox, ImageCommentHost } from '../utils/imageComments'

const RECT = { left: 100, top: 50, width: 400, height: 200, right: 500, bottom: 250, x: 100, y: 50, toJSON: () => ({}) } as DOMRect

function sizeImage(img: HTMLImageElement | null) {
  if (!img) return
  Object.defineProperty(img, 'offsetLeft', { configurable: true, get: () => 0 })
  Object.defineProperty(img, 'offsetTop', { configurable: true, get: () => 0 })
  Object.defineProperty(img, 'offsetWidth', { configurable: true, get: () => 400 })
  Object.defineProperty(img, 'offsetHeight', { configurable: true, get: () => 200 })
  img.getBoundingClientRect = () => RECT
}

function Harness({ host, onImageClick }: { host: ImageCommentHost; onImageClick: () => void }) {
  const imgRef = useRef<HTMLImageElement | null>(null)
  return (
    <span className="relative block">
      {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions */}
      <img
        ref={el => { sizeImage(el); imgRef.current = el }}
        alt="Shot"
        src="/api/file-raw?path=%2Fd%2Fa.png"
        onClick={onImageClick}
      />
      <ImageCommentLayer host={host} imgRef={imgRef} src="a.png" path="/d/a.png" alt="Shot" line={3} column={1} />
    </span>
  )
}

const makeHost = (boxes: ImageBox[] = []): ImageCommentHost => ({
  boxesFor: vi.fn(() => boxes),
  register: vi.fn(() => () => {}),
  draft: null,
  ownsDraft: vi.fn(() => false),
  onPlace: vi.fn(),
  onActivate: vi.fn(),
  onHover: vi.fn(),
})

const setup = (boxes: ImageBox[] = []) => {
  const host = makeHost(boxes)
  const onImageClick = vi.fn()
  render(<Harness host={host} onImageClick={onImageClick} />)
  return { host, onImageClick, img: screen.getByRole('img', { name: 'Shot' }) }
}

/** Press at `from`, move to `to`, release there, then the click the browser
 *  follows a release with, on `target`. */
const drag = (target: Element, from: [number, number], to: [number, number]) => {
  fireEvent.pointerDown(target, { clientX: from[0], clientY: from[1], pointerId: 1, button: 0 })
  fireEvent.pointerMove(window, { clientX: to[0], clientY: to[1], pointerId: 1 })
  fireEvent.pointerUp(window, { clientX: to[0], clientY: to[1], pointerId: 1 })
  fireEvent.click(target)
}

const BOX: ImageBox = { id: 'c1', number: 1, text: 'too tight', x: 0.25, y: 0.25, w: 0.5, h: 0.5 }

beforeEach(() => { vi.useRealTimers() })
afterEach(() => { vi.useRealTimers() })

describe('ImageCommentLayer', () => {
  it('draws a box with a drag and hands it to the host, with no lightbox', () => {
    const { host, onImageClick, img } = setup()
    drag(img, [200, 100], [300, 150])
    expect(host.onPlace).toHaveBeenCalledWith({
      src: 'a.png', path: '/d/a.png', alt: 'Shot', line: 3, column: 1,
      x: 0.25, y: 0.25, w: 0.25, h: 0.25, clientX: 300, clientY: 150,
    })
    expect(onImageClick).not.toHaveBeenCalled()
  })

  it('leaves a plain click to the image, which opens the lightbox', () => {
    const { host, onImageClick, img } = setup()
    fireEvent.pointerDown(img, { clientX: 200, clientY: 100, pointerId: 1, button: 0 })
    fireEvent.pointerMove(window, { clientX: 202, clientY: 101, pointerId: 1 })
    fireEvent.pointerUp(window, { clientX: 202, clientY: 101, pointerId: 1 })
    fireEvent.click(img)
    expect(host.onPlace).not.toHaveBeenCalled()
    expect(onImageClick).toHaveBeenCalledTimes(1)
  })

  it('drops a sliver narrower than the smallest box, and still opens no lightbox', () => {
    const { host, onImageClick, img } = setup()
    drag(img, [200, 100], [300, 100 + MIN_BOX_PX - 1])
    expect(host.onPlace).not.toHaveBeenCalled()
    expect(onImageClick).not.toHaveBeenCalled()
  })

  it('shows the box while it is being drawn', () => {
    const { img } = setup()
    fireEvent.pointerDown(img, { clientX: 200, clientY: 100, pointerId: 1, button: 0 })
    fireEvent.pointerMove(window, { clientX: 300, clientY: 150, pointerId: 1 })
    expect(document.querySelector('[data-image-box-live]')).toHaveStyle({ left: '25%', top: '25%', width: '25%', height: '25%' })
    fireEvent.pointerUp(window, { clientX: 300, clientY: 150, pointerId: 1 })
    expect(document.querySelector('[data-image-box-live]')).toBeNull()
  })

  it('never draws from a touch, which keeps its scroll', () => {
    const { host, img } = setup()
    fireEvent.pointerDown(img, { clientX: 200, clientY: 100, pointerId: 1, button: 0, pointerType: 'touch' })
    fireEvent.pointerMove(window, { clientX: 300, clientY: 150, pointerId: 1, pointerType: 'touch' })
    fireEvent.pointerUp(window, { clientX: 300, clientY: 150, pointerId: 1, pointerType: 'touch' })
    expect(host.onPlace).not.toHaveBeenCalled()
  })

  it('keeps the click cue until a press becomes a drag, then shows a crosshair until it ends', () => {
    const { img } = setup([BOX])
    const box = screen.getByRole('button', { name: /Box 1/ })
    expect(img.style.cursor).toBe('')
    expect(box.className).toContain('cursor-pointer')
    fireEvent.pointerDown(img, { clientX: 200, clientY: 100, pointerId: 1, button: 0 })
    fireEvent.pointerMove(window, { clientX: 202, clientY: 101, pointerId: 1 })
    expect(img.style.cursor).toBe('')
    fireEvent.pointerMove(window, { clientX: 300, clientY: 150, pointerId: 1 })
    expect(img.style.cursor).toBe('crosshair')
    expect(document.body.style.cursor).toBe('crosshair')
    expect(box.className).toContain('cursor-crosshair')
    fireEvent.pointerUp(window, { clientX: 300, clientY: 150, pointerId: 1 })
    expect(img.style.cursor).toBe('')
    expect(document.body.style.cursor).toBe('')
    expect(box.className).toContain('cursor-pointer')
  })

  it('leaves no crosshair on the page when it unmounts mid-drag', () => {
    const host = makeHost()
    const { unmount } = render(<Harness host={host} onImageClick={vi.fn()} />)
    const img = screen.getByRole('img', { name: 'Shot' })
    fireEvent.pointerDown(img, { clientX: 200, clientY: 100, pointerId: 1, button: 0 })
    fireEvent.pointerMove(window, { clientX: 300, clientY: 150, pointerId: 1 })
    expect(document.body.style.cursor).toBe('crosshair')
    unmount()
    expect(document.body.style.cursor).toBe('')
  })

  it('enlarges the image from a click on a whole-image box, which covers all of it', () => {
    const onLightbox = vi.fn()
    window.addEventListener('lightbox', onLightbox)
    const { host } = setup([{ id: 'w', number: 1, text: 'whole', ...WHOLE_IMAGE }])
    const box = screen.getByRole('button', { name: /Box 1/ })
    fireEvent.click(box, { detail: 1 })
    expect(onLightbox).toHaveBeenCalledTimes(1)
    expect(host.onActivate).not.toHaveBeenCalled()
    // From the keyboard it reveals its comment, as every box does.
    fireEvent.click(box, { detail: 0 })
    window.removeEventListener('lightbox', onLightbox)
    expect(onLightbox).toHaveBeenCalledTimes(1)
    expect(host.onActivate).toHaveBeenCalledWith('w')
  })

  it('reveals the comment of a partial box on click, and leaves the lightbox shut', () => {
    const onLightbox = vi.fn()
    window.addEventListener('lightbox', onLightbox)
    const { host } = setup([BOX])
    fireEvent.click(screen.getByRole('button', { name: /Box 1/ }), { detail: 1 })
    window.removeEventListener('lightbox', onLightbox)
    expect(onLightbox).not.toHaveBeenCalled()
    expect(host.onActivate).toHaveBeenCalledWith('c1')
  })

  it('drops a drag when its Crew is hidden, and leaves Escape to the Crew in front', () => {
    const host = makeHost()
    const onImageClick = vi.fn()
    const { rerender } = render(
      <LocalOverlayRoot hidden={false}><Harness host={host} onImageClick={onImageClick} /></LocalOverlayRoot>,
    )
    const img = screen.getByRole('img', { name: 'Shot' })
    fireEvent.pointerDown(img, { clientX: 200, clientY: 100, pointerId: 1, button: 0 })
    fireEvent.pointerMove(window, { clientX: 300, clientY: 150, pointerId: 1 })
    expect(document.body.style.cursor).toBe('crosshair')
    rerender(<LocalOverlayRoot hidden><Harness host={host} onImageClick={onImageClick} /></LocalOverlayRoot>)
    expect(document.querySelector('[data-image-box-live]')).toBeNull()
    expect(document.body.style.cursor).toBe('')
    const frontEscape = vi.fn()
    document.addEventListener('keydown', frontEscape)
    fireEvent.keyDown(document.body, { key: 'Escape' })
    document.removeEventListener('keydown', frontEscape)
    expect(frontEscape).toHaveBeenCalledTimes(1)
    fireEvent.pointerUp(window, { clientX: 300, clientY: 150, pointerId: 1 })
    expect(host.onPlace).not.toHaveBeenCalled()
  })

  it('drops the box on Escape, before the panel sees the key', () => {
    const { host, img } = setup()
    fireEvent.pointerDown(img, { clientX: 200, clientY: 100, pointerId: 1, button: 0 })
    fireEvent.pointerMove(window, { clientX: 300, clientY: 150, pointerId: 1 })
    const panelEscape = vi.fn()
    document.addEventListener('keydown', panelEscape)
    fireEvent.keyDown(document.body, { key: 'Escape' })
    document.removeEventListener('keydown', panelEscape)
    fireEvent.pointerUp(window, { clientX: 300, clientY: 150, pointerId: 1 })
    expect(host.onPlace).not.toHaveBeenCalled()
    expect(panelEscape).not.toHaveBeenCalled()
  })

  it('opens no lightbox from the release of a drag dropped by Escape, however late', () => {
    vi.useFakeTimers()
    const { host, img, onImageClick } = setup()
    fireEvent.pointerDown(img, { clientX: 200, clientY: 100, pointerId: 1, button: 0 })
    fireEvent.pointerMove(window, { clientX: 300, clientY: 150, pointerId: 1 })
    fireEvent.keyDown(window, { key: 'Escape' })
    // The button is still held past the timer that ends a release's swallow.
    vi.advanceTimersByTime(RELEASE_CLICK_MS + 1)
    fireEvent.pointerUp(window, { clientX: 300, clientY: 150, pointerId: 1 })
    fireEvent.click(img)
    expect(onImageClick).not.toHaveBeenCalled()
    expect(host.onPlace).not.toHaveBeenCalled()
    // Only that click: the next one is the reader's own.
    vi.advanceTimersByTime(RELEASE_CLICK_MS + 1)
    fireEvent.click(img)
    expect(onImageClick).toHaveBeenCalledTimes(1)
  })

  it('refuses the press default, so no text selection or image drag-out starts', () => {
    const { img } = setup()
    expect(fireEvent.mouseDown(img, { button: 0 })).toBe(false)
    expect(fireEvent.dragStart(img)).toBe(false)
  })

  it('reveals a box\'s comment on a click, and opens no lightbox', () => {
    const { host, onImageClick } = setup([BOX])
    const box = screen.getByRole('button', { name: 'Box 1 at 25%, 25%' })
    expect(box).toHaveStyle({ left: '25%', top: '25%', width: '50%', height: '50%' })
    fireEvent.pointerDown(box, { clientX: 300, clientY: 150, pointerId: 1, button: 0 })
    fireEvent.pointerUp(window, { clientX: 300, clientY: 150, pointerId: 1 })
    fireEvent.click(box)
    expect(host.onActivate).toHaveBeenCalledWith('c1')
    expect(host.onPlace).not.toHaveBeenCalled()
    expect(onImageClick).not.toHaveBeenCalled()
  })

  it('draws a new box from a drag that starts on an existing one', () => {
    const { host } = setup([BOX])
    const box = screen.getByRole('button', { name: 'Box 1 at 25%, 25%' })
    drag(box, [300, 150], [400, 200])
    expect(host.onPlace).toHaveBeenCalledWith(expect.objectContaining({ x: 0.5, y: 0.5, w: 0.25, h: 0.25 }))
    expect(host.onActivate).not.toHaveBeenCalled()
  })

  it('shows a box\'s comment while the pointer is over it, and hides it on leave', () => {
    const { host } = setup([BOX])
    const box = screen.getByRole('button', { name: 'Box 1 at 25%, 25%' })
    expect(box).toHaveAccessibleDescription('too tight')
    box.getBoundingClientRect = () => ({ left: 200, top: 100, width: 200, height: 100, right: 400, bottom: 200, x: 200, y: 100, toJSON: () => ({}) })
    fireEvent.pointerMove(box, { clientX: 300, clientY: 150, pointerId: 1 })
    // The box's own rect goes along, so the comment can sit beside it.
    expect(host.onHover).toHaveBeenLastCalledWith('too tight', 300, 150, { left: 200, top: 100, width: 200, height: 100 })
    fireEvent.pointerLeave(box, { clientX: 520, clientY: 150, pointerId: 1 })
    expect(host.onHover).toHaveBeenLastCalledWith(null, 520, 150)
  })

  it('hides the comment when a drag starts on its box, and keeps it hidden while drawing', () => {
    const { host } = setup([BOX])
    const box = screen.getByRole('button', { name: 'Box 1 at 25%, 25%' })
    fireEvent.pointerMove(box, { clientX: 300, clientY: 150, pointerId: 1 })
    fireEvent.pointerDown(box, { clientX: 300, clientY: 150, pointerId: 1, button: 0 })
    expect(host.onHover).toHaveBeenLastCalledWith(null, 300, 150)
    vi.mocked(host.onHover).mockClear()
    fireEvent.pointerMove(box, { clientX: 340, clientY: 170, pointerId: 1 })
    expect(host.onHover).not.toHaveBeenCalledWith('too tight', expect.anything(), expect.anything(), expect.anything())
    fireEvent.pointerUp(window, { clientX: 340, clientY: 170, pointerId: 1 })
  })

  it('shows a box\'s comment beside it on keyboard focus, and hides it on blur', () => {
    const { host } = setup([BOX])
    const box = screen.getByRole('button', { name: 'Box 1 at 25%, 25%' })
    box.getBoundingClientRect = () => ({ left: 200, top: 100, width: 200, height: 100, right: 400, bottom: 200, x: 200, y: 100, toJSON: () => ({}) })
    box.matches = (sel: string) => sel === ':focus-visible' || Element.prototype.matches.call(box, sel)
    fireEvent.focus(box)
    expect(host.onHover).toHaveBeenLastCalledWith('too tight', 300, 150, { left: 200, top: 100, width: 200, height: 100 })
    fireEvent.blur(box)
    expect(host.onHover).toHaveBeenLastCalledWith(null, 300, 150)
  })

  it('moves a focused box\'s comment with it on a scroll, and stops once it loses focus', () => {
    const { host } = setup([BOX])
    const box = screen.getByRole('button', { name: 'Box 1 at 25%, 25%' })
    let top = 100
    box.getBoundingClientRect = () => ({ left: 200, top, width: 200, height: 100, right: 400, bottom: top + 100, x: 200, y: top, toJSON: () => ({}) })
    box.matches = (sel: string) => sel === ':focus-visible' || Element.prototype.matches.call(box, sel)
    fireEvent.focus(box)
    top = 40
    fireEvent.scroll(document.body)
    expect(host.onHover).toHaveBeenLastCalledWith('too tight', 300, 90, { left: 200, top: 40, width: 200, height: 100 })
    fireEvent.blur(box)
    vi.mocked(host.onHover).mockClear()
    top = 0
    fireEvent.scroll(document.body)
    fireEvent(window, new Event('resize'))
    expect(host.onHover).not.toHaveBeenCalled()
  })

  it('keeps a focused box\'s comment hidden through a scroll while a drag draws over it', () => {
    const { host } = setup([BOX])
    const box = screen.getByRole('button', { name: 'Box 1 at 25%, 25%' })
    box.matches = (sel: string) => sel === ':focus-visible' || Element.prototype.matches.call(box, sel)
    fireEvent.focus(box)
    fireEvent.pointerDown(box, { clientX: 300, clientY: 150, pointerId: 1, button: 0 })
    fireEvent.pointerMove(window, { clientX: 360, clientY: 190, pointerId: 1 })
    vi.mocked(host.onHover).mockClear()
    fireEvent.scroll(document.body)
    expect(host.onHover).not.toHaveBeenCalledWith('too tight', expect.anything(), expect.anything(), expect.anything())
    fireEvent.pointerUp(window, { clientX: 360, clientY: 190, pointerId: 1 })
  })

  it('shows no comment on a focus from a click, which the pointer already covers', () => {
    const { host } = setup([BOX])
    const box = screen.getByRole('button', { name: 'Box 1 at 25%, 25%' })
    box.matches = (sel: string) => sel !== ':focus-visible' && Element.prototype.matches.call(box, sel)
    fireEvent.focus(box)
    expect(host.onHover).not.toHaveBeenCalled()
  })

  it('shows no comment for a touch', () => {
    const { host } = setup([BOX])
    fireEvent.pointerMove(screen.getByRole('button', { name: 'Box 1 at 25%, 25%' }), { clientX: 300, clientY: 150, pointerId: 1, pointerType: 'touch' })
    expect(host.onHover).not.toHaveBeenCalled()
  })

  it('comments on the whole image from the keyboard control, below the image', () => {
    const { host } = setup()
    fireEvent.click(screen.getByRole('button', { name: 'Comment on this image' }))
    // Anchored at the image's bottom edge, so the composer hangs below the
    // picture instead of covering the middle of it.
    expect(host.onPlace).toHaveBeenCalledWith(expect.objectContaining({ x: 0, y: 0, w: 1, h: 1, clientX: 300, clientY: 250 }))
  })

  it('keeps the whole-image anchor on screen when the image runs past the viewport', () => {
    const innerHeight = window.innerHeight
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 200 })
    try {
      const { host } = setup()
      fireEvent.click(screen.getByRole('button', { name: 'Comment on this image' }))
      expect(host.onPlace).toHaveBeenCalledWith(expect.objectContaining({ clientX: 300, clientY: 200 }))
    } finally {
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: innerHeight })
    }
  })

  // A draft brought back after a teardown opens where the keyboard control
  // opens one: below its box, at the box's middle, kept on screen.
  const restoring = (box: { x: number; y: number; w: number; h: number }) => {
    const host = {
      ...makeHost(),
      draft: { src: 'a.png', line: 3, column: 1, ...box, unplaced: true },
      ownsDraft: vi.fn(() => true),
      placeDraft: vi.fn(),
    }
    render(<Harness host={host} onImageClick={vi.fn()} />)
    return host
  }

  it('opens a restored whole-image draft below the image, as the keyboard control does', () => {
    expect(restoring({ x: 0, y: 0, w: 1, h: 1 }).placeDraft).toHaveBeenCalledWith(300, 250)
  })

  it('opens a restored drawn box below the box, at its middle', () => {
    expect(restoring(BOX).placeDraft).toHaveBeenCalledWith(300, 200)
  })

  it('keeps a restored box on screen when its bottom is past the viewport', () => {
    const innerHeight = window.innerHeight
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 180 })
    try {
      expect(restoring(BOX).placeDraft).toHaveBeenCalledWith(300, 180)
    } finally {
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: innerHeight })
    }
  })

  it('shows the keyboard control\'s name as words, drawn by CSS rather than as text', () => {
    setup()
    const button = screen.getByRole('button', { name: 'Comment on this image' })
    // The words come from this attribute through CSS generated content, so the
    // control is not an unlabelled icon, and the layer still adds no text.
    expect(button.dataset.label).toBe('Comment on this image')
    expect(button.className).toContain('after:content-[attr(data-label)]')
    expect(button.textContent).toBe('')
  })

  it('adds no text to the document, so find and selection offsets stay put', () => {
    setup([BOX])
    expect(document.querySelector('[data-image-comment-layer]')?.textContent).toBe('')
  })

  it('measures a letterboxed picture, not the bands object-contain leaves around it', () => {
    // A square SVG held at a fixed width under a height cap: the 400x200
    // element draws the picture 200x200 in its middle, from x 200 to 400.
    const host = makeHost()
    const { container } = render(<Harness host={host} onImageClick={vi.fn()} />)
    const img = screen.getByRole('img', { name: 'Shot' }) as HTMLImageElement
    Object.defineProperty(img, 'naturalWidth', { configurable: true, get: () => 100 })
    Object.defineProperty(img, 'naturalHeight', { configurable: true, get: () => 100 })
    img.style.objectFit = 'contain'
    fireEvent.load(img)
    const layer = container.querySelector('[data-image-comment-layer]') as HTMLElement
    expect(layer.style.left).toBe('100px')
    expect(layer.style.width).toBe('200px')
    drag(img, [250, 100], [350, 200])
    expect(host.onPlace).toHaveBeenCalledWith(expect.objectContaining({ x: 0.25, y: 0.25, w: 0.5, h: 0.5 }))
  })
})
