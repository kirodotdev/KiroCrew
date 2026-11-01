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
import { BRAND_PURPLE } from '../lib/kiroGhostAvatar'
import { POSE_IDS, seedIconPose } from '../lib/avatarPoses'

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

async function switchToPicture() {
  fireEvent.click(screen.getByRole('radio', { name: 'Picture' }))
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

/* ───────────────── ghost-face picker removed (issue #18616) ───────────────── */

describe('CrewAvatarBuilder — ghost-face picker removed (#18616)', () => {
  // A fully-configured stored ghost, so "the record is untouched" is a visible
  // claim: every trait is pinned and would be lost if Apply overwrote it.
  const STORED_GHOST: Ghost = {
    kind: 'ghost',
    traits: { eyes: 'wink', brows: 'none', mouth: 'smile', accessory: 'none', prop: 'none', blush: false, flip: false, tile: BRAND_PURPLE },
  }

  it('create mode offers no Ghost face or Reactions tab and starts on Icon', () => {
    mount(null)
    // The builder opens on the Icon tier for a brand-new crew.
    expect(screen.getByTestId('avatar-icon-pane')).toBeInTheDocument()
    // The ghost-face selection UI is gone from the tier strip entirely.
    expect(screen.queryByRole('radio', { name: 'Ghost face' })).not.toBeInTheDocument()
    expect(screen.queryByRole('radio', { name: 'Reactions' })).not.toBeInTheDocument()
    // The authorable tiers remain.
    expect(screen.getByRole('radio', { name: 'Icon' })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Picture' })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Library' })).toBeInTheDocument()
  })

  it('a crew already wearing a ghost also sees no Ghost face or Reactions tab', () => {
    // #18616 removes the picker in BOTH flows: a ghost-config crew can no
    // longer re-enter the 捏脸 builder either.
    mount(STORED_GHOST)
    expect(screen.queryByRole('radio', { name: 'Ghost face' })).not.toBeInTheDocument()
    expect(screen.queryByRole('radio', { name: 'Reactions' })).not.toBeInTheDocument()
  })

  it('editing a stored ghost crew opens on the Icon tier', () => {
    mount(STORED_GHOST)
    expect(screen.getByTestId('avatar-icon-pane')).toBeInTheDocument()
    // The ghost-authoring pane is not what the dialog lands on.
    expect(screen.queryByTestId('avatar-builder-preview')).not.toBeInTheDocument()
  })

  it('applying without a pick keeps the stored ghost face unchanged', () => {
    // The edge case: a stored ghost opens on Icon, and Apply with no SVG
    // pose/colour pick must hand the stored ghost BACK verbatim — not overwrite
    // it with a name-seeded icon, and not reset it.
    const { onSave } = mount(STORED_GHOST)
    apply()
    expect(lastSaved(onSave)).toEqual(STORED_GHOST)
  })

  it('a stored ghost crew that DOES pick an SVG pose saves an icon override', () => {
    // Once the user actively picks a pose, the ghost is intentionally replaced.
    const { onSave } = mount(STORED_GHOST)
    fireEvent.click(screen.getByTestId('avatar-icon-pose-pose-3'))
    apply()
    expect(lastSaved(onSave)).toMatchObject({ kind: 'icon', pose: 'pose-3' })
  })

  it('the Icon pane previews the stored ghost and selects no pose, so preview matches Apply', () => {
    // Review blocker (Opus/UX): the pane must not show a seeded pose as selected
    // while Apply keeps the ghost. For a stored ghost it previews the current
    // avatar through the real renderer (so NOT the seeded-pose img) and
    // highlights no pose until the user picks.
    mount(STORED_GHOST)
    expect(screen.queryByTestId('avatar-icon-preview')).not.toBeInTheDocument()
    for (const pose of POSE_IDS) {
      expect(screen.getByTestId(`avatar-icon-pose-${pose}`)).toHaveAttribute('aria-selected', 'false')
    }
  })

  it('Reset survives a tab detour: the stored ghost is NOT resurrected on Apply', () => {
    // Review blocker (Opus): Reset → switch tiers → back to Icon → Apply must
    // commit the reset (null = name-derived default), not the ghost the reset
    // cleared, even though the tab change clears resetToDefault.
    const { onSave } = mount(STORED_GHOST)
    fireEvent.click(screen.getByTestId('avatar-builder-reset'))
    fireEvent.click(screen.getByRole('radio', { name: 'Picture' }))
    fireEvent.click(screen.getByRole('radio', { name: 'Icon' }))
    apply()
    expect(lastSaved(onSave)).toBeNull()
  })

  it('after Reset the Icon preview matches the null that Apply saves, not a pose img', () => {
    // Review blocker (Opus/GPT): post-reset, a ghost crew still previews through
    // the real renderer (what `null` draws — the name-derived face), never the
    // name-seeded pose img, which Apply would never save here.
    mount(STORED_GHOST)
    fireEvent.click(screen.getByTestId('avatar-builder-reset'))
    expect(screen.queryByTestId('avatar-icon-preview')).not.toBeInTheDocument()
    expect(screen.queryByTestId('avatar-icon-preview-current')).not.toBeInTheDocument()
  })

  it('pick-then-Reset still matches: preview is the reset face and Apply saves null', () => {
    // Review blocker (Opus/GPT): picking a pose sets iconTouched; Reset must
    // clear it, or the pane would preview the seeded pose while Apply saves null
    // (the name-derived ghost). After Reset the preview is the real renderer and
    // Apply commits null.
    const { onSave } = mount(STORED_GHOST)
    fireEvent.click(screen.getByTestId('avatar-icon-pose-pose-3'))
    fireEvent.click(screen.getByTestId('avatar-builder-reset'))
    expect(screen.queryByTestId('avatar-icon-preview')).not.toBeInTheDocument()
    apply()
    expect(lastSaved(onSave)).toBeNull()
  })

  it('Cancel reports back without saving', () => {
    const { onSave, onCancel } = mount()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(onSave).not.toHaveBeenCalled()
  })
})

/* ──────────────────────────── icon tier ──────────────────────────── */

describe('CrewAvatarBuilder — icon tier', () => {
  it('is the default pane for a crew with no override', () => {
    mount()
    expect(screen.getByTestId('avatar-icon-pane')).toBeInTheDocument()
    expect(screen.getByTestId('avatar-icon-preview')).toBeInTheDocument()
  })

  it('a stored ghost crew now opens on the Icon pane (#18616)', () => {
    mount({ kind: 'ghost', traits: { eyes: 'wink', brows: 'none', mouth: 'smile', accessory: 'none', prop: 'none', blush: false, flip: false, tile: BRAND_PURPLE } })
    expect(screen.getByTestId('avatar-icon-pane')).toBeInTheDocument()
    expect(screen.queryByTestId('avatar-builder-preview')).not.toBeInTheDocument()
  })

  it('offers no Ghost face tab for a non-ghost crew (new ghosts are not authorable)', () => {
    mount(null)
    expect(screen.queryByRole('radio', { name: 'Ghost face' })).not.toBeInTheDocument()
    // Icon, Picture and Library remain the authorable tiers.
    expect(screen.getByRole('radio', { name: 'Icon' })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Picture' })).toBeInTheDocument()
  })

  it('offers no Ghost face tab even for a crew that already wears one (#18616)', () => {
    mount({ kind: 'ghost', traits: {} })
    expect(screen.queryByRole('radio', { name: 'Ghost face' })).not.toBeInTheDocument()
  })

  it('picking a pose and Apply hands over an icon override', () => {
    const { onSave } = mount()
    fireEvent.click(screen.getByTestId('avatar-icon-pose-pose-3'))
    apply()
    const saved = lastSaved(onSave)
    expect(saved?.kind).toBe('icon')
    expect(saved).toMatchObject({ kind: 'icon', pose: 'pose-3' })
    expect((saved as { bg: string }).bg).toMatch(/^#[0-9a-f]{6}$/)
  })

  it('a fresh draft applies the name-seeded default pose without any pick', () => {
    const { onSave } = mount()
    apply()
    // The builder mounts with name="radar"; a fresh icon draft seeds its pose
    // from the name (parity with the ghost's name-seeded default), so it is the
    // seeded pose rather than the static first one.
    expect(lastSaved(onSave)).toMatchObject({ kind: 'icon', pose: seedIconPose('radar') })
  })

  it('every shipped pose is offered as a swatch', () => {
    mount()
    for (const pose of POSE_IDS) {
      expect(screen.getByTestId(`avatar-icon-pose-${pose}`)).toBeInTheDocument()
    }
  })

  it('a background swatch pins the colour the override carries', () => {
    const { onSave } = mount()
    fireEvent.click(screen.getByTestId('avatar-icon-bg-25679d'))
    apply()
    expect(lastSaved(onSave)).toMatchObject({ kind: 'icon', bg: '#25679d' })
  })

  it('a stored icon crew reopens on its pose and colour', () => {
    mount({ kind: 'icon', pose: 'pose-5', bg: '#25679d' })
    expect(screen.getByTestId('avatar-icon-pane')).toBeInTheDocument()
    expect(screen.getByTestId('avatar-icon-pose-pose-5')).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByTestId('avatar-icon-bg-25679d')).toHaveAttribute('aria-selected', 'true')
  })

  it('the icon tier offers no Reactions tab — a pose is static', () => {
    mount()
    expect(screen.getByTestId('avatar-icon-pane')).toBeInTheDocument()
    expect(screen.queryByRole('radio', { name: 'Reactions' })).not.toBeInTheDocument()
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
    // Icon pane (the default): nothing intercepts, the browser default stands.
    expect(dropOnBody()).toBe(false)
    await switchToPicture()
    // Picture tab: a stray drop must not navigate the SPA to the file.
    expect(dropOnBody()).toBe(true)
    const over = new Event('dragover', { bubbles: true, cancelable: true })
    document.body.dispatchEvent(over)
    expect(over.defaultPrevented).toBe(true)
    // Back on the Icon pane the listeners are gone again.
    fireEvent.click(screen.getByRole('radio', { name: 'Icon' }))
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

  it('Reset drops the pending picture and returns a non-ghost crew to the icon tier', async () => {
    const { onSave } = mount()
    await switchToPicture()
    chooseFile(pngFile())
    await decode()
    await screen.findByTestId('avatar-upload-preview')
    fireEvent.click(screen.getByTestId('avatar-builder-reset'))
    // A crew that wears no ghost returns to the icon tier, not the ghost
    // builder — Reset is not a path to author a new ghost (#18616).
    expect(await screen.findByTestId('avatar-icon-pane')).toBeInTheDocument()
    apply()
    // Reset on a crew that does NOT wear a ghost commits the name-seeded icon
    // default it previews — NOT `null`. `null` renders the name's ghost, and a
    // crew that never chose a ghost must never reset INTO one (#18616).
    expect(lastSaved(onSave)).toMatchObject({ kind: 'icon' })
  })

  it('returning to the Picture tier after Reset keeps the stored image, not null', async () => {
    // Regression: Reset sets the undress sentinel, but navigating back to a
    // tier is the user building again, so the sentinel must clear — otherwise
    // Apply would discard the stored image the user came back to keep.
    const { onSave } = mount({ kind: 'image', v: 7 })
    fireEvent.click(screen.getByTestId('avatar-builder-reset'))
    // Reset lands a non-ghost crew on the icon tier; switch back to Picture.
    await screen.findByTestId('avatar-icon-pane')
    await switchToPicture()
    apply()
    // The sentinel cleared on the tier switch, so Apply falls back to the
    // stored image rather than committing the undress null.
    expect(lastSaved(onSave)).toMatchObject({ kind: 'image', v: 7 })
  })
})
