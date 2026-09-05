/**
 * CrewAvatarBuilder — the builder dialog rendered on its own.
 *
 * `CrewRoster.test.tsx` drives the builder through the crew editor and pins
 * the Apply → Save contract; this file exercises the dialog's own branches
 * that the editor round-trip never reaches: randomize, the blush and
 * background axes, the mirror toggle, and the whole picture tier — the
 * client-side crop/downscale ladder, the size and decode failures, the
 * drag-and-drop zone, and the pick-generation guard that stops a slow decode
 * from overwriting a later pick.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import CrewAvatarBuilder from '../components/CrewAvatarBuilder'
import type { CrewAvatarOverride } from '../components/CrewAvatar'
import { ACCESSORIES, BROWS, BRAND_PURPLE, EYES, MOUTHS, PROPS, TILES } from '../lib/kiroGhostAvatar'

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'initial', 'animate', 'exit', 'transition',
    'variants', 'whileHover', 'whileTap', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children' || FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const cache = new Map<string, unknown>()
  return {
    motion: new Proxy({}, {
      get: (_t, tag: string) => {
        if (!cache.has(tag)) cache.set(tag, make(tag))
        return cache.get(tag)
      },
    }),
    AnimatePresence: ({ children }: { children?: React.ReactNode }) =>
      React.createElement(React.Fragment, null, children),
    useReducedMotion: () => false,
  }
})

type Ghost = Extract<CrewAvatarOverride, { kind: 'ghost' }>
type Picture = Extract<CrewAvatarOverride, { kind: 'image' }>

function mount(value: CrewAvatarOverride | null = null) {
  const onSave = vi.fn()
  const onCancel = vi.fn()
  const utils = render(
    <CrewAvatarBuilder open name="radar" value={value} onCancel={onCancel} onSave={onSave} />,
  )
  return { ...utils, onSave, onCancel }
}

const apply = () => fireEvent.click(screen.getByTestId('avatar-builder-save'))
const lastSaved = (onSave: ReturnType<typeof vi.fn>) => onSave.mock.calls.at(-1)?.[0] as CrewAvatarOverride | null

/** The axis strip measures 0px wide in the test DOM and collapses to a
 *  dropdown, so an axis is reached through the trigger + menu item. */
async function pickAxis(label: string) {
  // The trigger shows the ACTIVE axis label; every other axis is a menu item.
  const triggers = screen.getAllByRole('button').filter(b => b.textContent?.trim() === 'Eyes' || b.querySelector('svg.lucide-chevron-down'))
  fireEvent.click(triggers[0])
  const item = await screen.findByRole('button', { name: label })
  fireEvent.click(item)
}

async function switchToPicture() {
  fireEvent.click(screen.getByRole('button', { name: 'Picture' }))
  await screen.findByTestId('avatar-upload-pane')
}

/* ────────────── canvas + image doubles for the picture tier ────────────── */

type FakeImage = { onload: (() => void) | null; onerror: (() => void) | null; naturalWidth: number; naturalHeight: number; src: string }
let images: FakeImage[] = []
let imageSize = { w: 800, h: 600 }
/** Per-format data URI produced by toDataURL; tests override to walk the ladder. */
let dataUriFor: (canvas: { width: number }, type?: string, quality?: number) => string = (c, type = 'image/png') =>
  `data:${type};base64,${'A'.repeat(c.width)}`
let contextAvailable = true

const RealImage = globalThis.Image

function installDoubles() {
  images = []
  class ImageDouble {
    onload: (() => void) | null = null
    onerror: (() => void) | null = null
    naturalWidth = 0
    naturalHeight = 0
    private _src = ''
    constructor() { images.push(this as unknown as FakeImage) }
    set src(v: string) {
      this._src = v
      this.naturalWidth = imageSize.w
      this.naturalHeight = imageSize.h
    }
    get src() { return this._src }
  }
  ;(globalThis as unknown as { Image: unknown }).Image = ImageDouble
  if (!('createObjectURL' in URL)) {
    ;(URL as unknown as { createObjectURL: unknown }).createObjectURL = () => 'blob:stub'
    ;(URL as unknown as { revokeObjectURL: unknown }).revokeObjectURL = () => {}
  }
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(function (this: HTMLCanvasElement) {
    if (!contextAvailable) return null
    return { fillStyle: '', fillRect: vi.fn(), drawImage: vi.fn() } as unknown as CanvasRenderingContext2D
  })
  vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockImplementation(function (this: HTMLCanvasElement, type?: string, quality?: number) {
    return dataUriFor(this, type, quality)
  })
}

const pngFile = (name = 'pic.png', size?: number) => {
  const f = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], name, { type: 'image/png' })
  if (size !== undefined) Object.defineProperty(f, 'size', { value: size })
  return f
}

const chooseFile = (file: File) => {
  const input = screen.getByTestId('avatar-upload-input') as HTMLInputElement
  fireEvent.change(input, { target: { files: [file] } })
}

/** Decode the i-th picked image (default: the latest). */
const decode = async (index = images.length - 1) => {
  await waitFor(() => expect(images.length).toBeGreaterThan(index))
  await act(async () => { images[index].onload?.() })
}

beforeEach(() => {
  imageSize = { w: 800, h: 600 }
  contextAvailable = true
  dataUriFor = (c, type = 'image/png') => `data:${type};base64,${'A'.repeat(c.width)}`
  installDoubles()
})

afterEach(() => {
  vi.restoreAllMocks()
  ;(globalThis as unknown as { Image: unknown }).Image = RealImage
})

/* ──────────────────────────── ghost tier ──────────────────────────── */

describe('CrewAvatarBuilder — ghost tier', () => {
  it('randomize draws every axis from the shipped vocabulary and Apply hands it over', () => {
    const { onSave } = mount()
    fireEvent.click(screen.getByTestId('avatar-builder-randomize'))
    apply()
    const saved = lastSaved(onSave) as Ghost
    expect(saved.kind).toBe('ghost')
    expect(Object.keys(EYES)).toContain(saved.traits.eyes)
    expect(Object.keys(BROWS)).toContain(saved.traits.brows)
    expect(Object.keys(MOUTHS)).toContain(saved.traits.mouth)
    // Headwear and items each have an explicit "none" option beyond the art keys.
    expect([...Object.keys(ACCESSORIES), 'none']).toContain(saved.traits.accessory)
    expect([...Object.keys(PROPS), 'none']).toContain(saved.traits.prop)
    expect(typeof saved.traits.blush).toBe('boolean')
    expect(typeof saved.traits.flip).toBe('boolean')
    expect([BRAND_PURPLE, ...TILES]).toContain(saved.traits.tile)
  })

  it('the Blush axis is a two-option tab: off first, then on', async () => {
    const { onSave } = mount()
    await pickAxis('Blush')
    fireEvent.click(screen.getByTestId('avatar-opt-blush'))
    apply()
    expect((lastSaved(onSave) as Ghost).traits.blush).toBe(true)
    fireEvent.click(screen.getByTestId('avatar-opt-none'))
    apply()
    expect((lastSaved(onSave) as Ghost).traits.blush).toBe(false)
  })

  it('the Background axis pins the tile and labels swatches with color names, not hex', async () => {
    const { onSave } = mount()
    await pickAxis('Background')
    const steel = screen.getByTestId('avatar-opt-25679d')
    expect(steel).toHaveAttribute('aria-label', 'Steel blue')
    fireEvent.click(steel)
    apply()
    expect((lastSaved(onSave) as Ghost).traits.tile).toBe('#25679d')
  })

  it('the mirror toggle flips the face relative to the seeded default', () => {
    const { onSave } = mount()
    const sw = screen.getByRole('switch', { name: 'Flip direction' })
    const before = sw.getAttribute('aria-checked') === 'true'
    fireEvent.click(sw)
    expect(sw.getAttribute('aria-checked')).toBe(String(!before))
    apply()
    expect((lastSaved(onSave) as Ghost).traits.flip).toBe(!before)
  })

  it('Cancel reports back without saving', () => {
    const { onSave, onCancel } = mount()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(onSave).not.toHaveBeenCalled()
  })
})

/* ─────────────────────────── picture tier ─────────────────────────── */

describe('CrewAvatarBuilder — picture tier', () => {
  it('Apply is disabled until a picture exists, then hands over the cropped data URI', async () => {
    const { onSave } = mount()
    await switchToPicture()
    expect(screen.getByTestId('avatar-builder-save')).toBeDisabled()
    chooseFile(pngFile())
    await decode()
    await screen.findByTestId('avatar-upload-preview')
    expect(screen.getByTestId('avatar-builder-save')).not.toBeDisabled()
    apply()
    const saved = lastSaved(onSave) as Picture
    // 800x600 source → 512px square PNG (the first rung of the ladder).
    expect(saved).toEqual({ kind: 'image', pendingData: `data:image/png;base64,${'A'.repeat(512)}` })
  })

  it('a source smaller than the output edge is not upscaled', async () => {
    imageSize = { w: 300, h: 900 }
    const { onSave } = mount()
    await switchToPicture()
    chooseFile(pngFile())
    await decode()
    await screen.findByTestId('avatar-upload-preview')
    apply()
    expect((lastSaved(onSave) as Picture).pendingData).toBe(`data:image/png;base64,${'A'.repeat(300)}`)
  })

  it('walks the size ladder: PNG over budget → JPEG on a white ground → smaller JPEG', async () => {
    const big = 'B'.repeat(1_300_000) // > 900 KB of base64 payload
    dataUriFor = (c, type = 'image/png', quality) => {
      if (type === 'image/png') return `data:image/png;base64,${big}`
      if (quality === 0.85) return `data:image/jpeg;base64,${big}`
      return `data:image/jpeg;base64,q${quality}-${c.width}`
    }
    const { onSave } = mount()
    await switchToPicture()
    chooseFile(pngFile())
    await decode()
    await screen.findByTestId('avatar-upload-preview')
    apply()
    expect((lastSaved(onSave) as Picture).pendingData).toBe('data:image/jpeg;base64,q0.8-384')
  })

  it('stops at the JPEG rung when that one fits', async () => {
    const big = 'B'.repeat(1_300_000)
    dataUriFor = (c, type = 'image/png', quality) =>
      type === 'image/png' ? `data:image/png;base64,${big}` : `data:image/jpeg;base64,q${quality}-${c.width}`
    const { onSave } = mount()
    await switchToPicture()
    chooseFile(pngFile())
    await decode()
    await screen.findByTestId('avatar-upload-preview')
    apply()
    expect((lastSaved(onSave) as Picture).pendingData).toBe('data:image/jpeg;base64,q0.85-512')
  })

  it('refuses an oversized source before decoding it, through ErrorNotice, and the notice dismisses', async () => {
    mount()
    await switchToPicture()
    chooseFile(pngFile('huge.png', 21 * 1024 * 1024))
    const notice = await screen.findByTestId('avatar-upload-error')
    expect(notice).toHaveTextContent('That file is too large (20 MB max).')
    expect(images).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByTestId('avatar-upload-error')).toBeNull()
  })

  it('reports an undecodable file and keeps Apply disabled', async () => {
    mount()
    await switchToPicture()
    chooseFile(pngFile('junk.png'))
    await waitFor(() => expect(images).toHaveLength(1))
    await act(async () => { images[0].onerror?.() })
    expect(await screen.findByTestId('avatar-upload-error')).toHaveTextContent('That file could not be read as an image.')
    expect(screen.getByTestId('avatar-builder-save')).toBeDisabled()
  })

  it('treats a zero-sized decode and a missing canvas context as bad images', async () => {
    imageSize = { w: 0, h: 0 }
    mount()
    await switchToPicture()
    chooseFile(pngFile())
    await decode()
    expect(await screen.findByTestId('avatar-upload-error')).toHaveTextContent('could not be read as an image')

    imageSize = { w: 64, h: 64 }
    contextAvailable = false
    chooseFile(pngFile('again.png'))
    await decode()
    expect(await screen.findByTestId('avatar-upload-error')).toHaveTextContent('could not be read as an image')
  })

  it('only the LATEST pick may land: a slow earlier decode cannot overwrite it', async () => {
    const { onSave } = mount()
    await switchToPicture()
    imageSize = { w: 100, h: 100 }
    chooseFile(pngFile('a.png'))
    await waitFor(() => expect(images).toHaveLength(1))
    imageSize = { w: 200, h: 200 }
    chooseFile(pngFile('b.png'))
    await waitFor(() => expect(images).toHaveLength(2))
    // B finishes first, then the stale A decode completes.
    await act(async () => { images[1].onload?.() })
    await act(async () => { images[0].onload?.() })
    apply()
    expect((lastSaved(onSave) as Picture).pendingData).toBe(`data:image/png;base64,${'A'.repeat(200)}`)
  })

  it('the drop zone highlights on drag-over, clears on leave, and accepts a dropped file', async () => {
    const { onSave } = mount()
    await switchToPicture()
    const zone = screen.getByTestId('avatar-upload-dropzone')
    fireEvent.dragOver(zone)
    expect(zone.className).toContain('border-ring')
    fireEvent.dragLeave(zone)
    expect(zone.className).not.toContain('border-ring')
    fireEvent.drop(zone, { dataTransfer: { files: [pngFile('dropped.png')] } })
    await decode()
    await screen.findByTestId('avatar-upload-preview')
    apply()
    expect((lastSaved(onSave) as Picture).kind).toBe('image')
  })

  it('"Choose a picture…" forwards to the hidden file input', async () => {
    mount()
    await switchToPicture()
    const click = vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(() => {})
    fireEvent.click(screen.getByTestId('avatar-upload-choose'))
    expect(click).toHaveBeenCalledTimes(1)
  })

  it('a file dropped OUTSIDE the drop zone is swallowed while the Picture pane is open, and not otherwise', async () => {
    const dropOnBody = () => {
      const ev = new Event('drop', { bubbles: true, cancelable: true })
      document.body.dispatchEvent(ev)
      return ev.defaultPrevented
    }
    const { unmount } = mount()
    // Ghost face tab: nothing intercepts, the browser default stands.
    expect(dropOnBody()).toBe(false)
    await switchToPicture()
    // Picture tab: a stray drop must not navigate the SPA to the file.
    expect(dropOnBody()).toBe(true)
    const over = new Event('dragover', { bubbles: true, cancelable: true })
    document.body.dispatchEvent(over)
    expect(over.defaultPrevented).toBe(true)
    // Back on the face tab the listeners are gone again.
    fireEvent.click(screen.getByRole('button', { name: 'Ghost face' }))
    await waitFor(() => expect(dropOnBody()).toBe(false))
    unmount()
    expect(dropOnBody()).toBe(false)
  })

  it('reopening over a saved picture with no new pick keeps the stored value verbatim', async () => {
    const stored: Picture = { kind: 'image', v: 42 }
    const { onSave } = mount(stored)
    // Opened straight onto the Picture tab, Apply enabled without a pick.
    expect(await screen.findByTestId('avatar-upload-pane')).toBeInTheDocument()
    expect(screen.getByTestId('avatar-builder-save')).not.toBeDisabled()
    apply()
    expect(lastSaved(onSave)).toBe(stored)
  })

  it('Reset drops both the ghost draft and the pending picture and returns to the face tab', async () => {
    const { onSave } = mount()
    await switchToPicture()
    chooseFile(pngFile())
    await decode()
    await screen.findByTestId('avatar-upload-preview')
    fireEvent.click(screen.getByTestId('avatar-builder-reset'))
    expect(screen.getByTestId('avatar-builder-preview')).toBeInTheDocument()
    apply()
    expect(lastSaved(onSave)).toBeNull()
  })
})
