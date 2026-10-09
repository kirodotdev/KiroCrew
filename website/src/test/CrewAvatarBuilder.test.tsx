/**
 * CrewAvatarBuilder — the builder dialog rendered on its own.
 *
 * The hand-built ghost "捏脸" tier and its reaction layer are no longer
 * authorable: this file pins that the ghost-face controls do not render, that a
 * new crew defaults to the Icon (SVG pose + colour) tier, and that editing a
 * crew already wearing a ghost face opens on the Icon tier and keeps the stored
 * ghost unless the user actively picks a pose. It also exercises the picture
 * tier — the client-side crop/downscale ladder, the size and decode failures,
 * the drag-and-drop zone, and the pick-generation guard.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import CrewAvatarBuilder from '../components/CrewAvatarBuilder'
import CrewAvatar from '../components/CrewAvatar'
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

type Icon = Extract<CrewAvatarOverride, { kind: 'icon' }>
type Ghost = Extract<CrewAvatarOverride, { kind: 'ghost' }>
type Picture = Extract<CrewAvatarOverride, { kind: 'image' }>

function mount(value: CrewAvatarOverride | null = null) {
  const onSave = vi.fn()
  const onCancel = vi.fn()
  // The Library pane reads the pack list through React Query; wrap so a
  // pack-tier mount has a client. No retry — nothing answers the route here.
  const queries = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={queries}>
      <CrewAvatarBuilder open name="radar" value={value} onCancel={onCancel} onSave={onSave} />
    </QueryClientProvider>,
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

/* ──────────────── the ghost-face controls are gone ──────────────── */

describe('CrewAvatarBuilder — no ghost-face authoring', () => {
  it('offers no Ghost face tab in the tier strip', () => {
    mount()
    expect(screen.queryByRole('radio', { name: 'Ghost face' })).toBeNull()
  })

  it('offers no Reactions tab — reactions were the ghost tier\u2019s alone', () => {
    mount()
    // Reactions decorated the ghost "捏脸" tier; with that tier gone there is
    // nowhere to reach them, on any starting record.
    expect(screen.queryByRole('radio', { name: 'Reactions' })).toBeNull()
  })

  it('exposes only the icon, picture and library tiers', () => {
    mount()
    expect(screen.getByRole('radio', { name: 'Icon' })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Picture' })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Library' })).toBeInTheDocument()
    // The ghost-face trait grid never mounts.
    expect(screen.queryByTestId('avatar-builder-preview')).toBeNull()
    expect(screen.queryByTestId('avatar-builder-randomize')).toBeNull()
  })

  it('shows no Ghost face tab even when editing a crew that stored a ghost face', () => {
    mount({ kind: 'ghost', traits: { eyes: 'wink', brows: 'none', mouth: 'smile', accessory: 'none', prop: 'none', blush: false, flip: false, tile: BRAND_PURPLE } })
    expect(screen.queryByRole('radio', { name: 'Ghost face' })).toBeNull()
    expect(screen.queryByRole('radio', { name: 'Reactions' })).toBeNull()
  })
})

/* ──────────────────────────── icon tier ──────────────────────────── */

describe('CrewAvatarBuilder — icon tier is the default', () => {
  it('is the default pane for a crew with no override', () => {
    mount()
    expect(screen.getByTestId('avatar-icon-pane')).toBeInTheDocument()
    expect(screen.getByTestId('avatar-icon-preview')).toBeInTheDocument()
  })

  it('a fresh draft applies the name-seeded SVG pose, with a colour, without any pick', () => {
    const { onSave } = mount()
    // No pick: a new crew defaults to the Icon (SVG + colour) tier. The pose is
    // seeded from the name (parity with the old ghost's name-seeded default),
    // and the override carries a hex background.
    apply()
    const saved = lastSaved(onSave) as Icon
    expect(saved.kind).toBe('icon')
    expect(saved.pose).toBe(seedIconPose('radar'))
    expect(saved.bg).toMatch(/^#[0-9a-f]{6}$/)
  })

  it('picking a pose and Apply hands over an icon override', () => {
    const { onSave } = mount()
    fireEvent.click(screen.getByTestId('avatar-icon-pose-pose-3'))
    apply()
    expect(lastSaved(onSave)).toMatchObject({ kind: 'icon', pose: 'pose-3' })
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

  it('the swatch labels are colour names, not raw hex', () => {
    mount()
    expect(screen.getByTestId('avatar-icon-bg-25679d')).toHaveAttribute('aria-label', 'Steel blue')
  })

  it('a stored icon crew reopens on its pose and colour', () => {
    mount({ kind: 'icon', pose: 'pose-5', bg: '#25679d' })
    expect(screen.getByTestId('avatar-icon-pane')).toBeInTheDocument()
    expect(screen.getByTestId('avatar-icon-pose-pose-5')).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByTestId('avatar-icon-bg-25679d')).toHaveAttribute('aria-selected', 'true')
  })
})

/* ───────────────── editing a stored ghost-face crew ───────────────── */

describe('CrewAvatarBuilder — editing a stored ghost crew', () => {
  const STORED_GHOST: CrewAvatarOverride = {
    kind: 'ghost',
    traits: { eyes: 'wink', brows: 'none', mouth: 'smile', accessory: 'halo', prop: 'none', blush: true, flip: false, tile: '#21a5de' },
    motions: { done: 'nod' },
  }

  it('opens on the Icon tier, not on any ghost pane', () => {
    mount(STORED_GHOST)
    expect(screen.getByTestId('avatar-icon-pane')).toBeInTheDocument()
    expect(screen.queryByTestId('avatar-builder-preview')).toBeNull()
  })

  it('previews the STORED ghost (not a name-seeded pose) with a kept-until-you-pick note', () => {
    // The preview must match what an untouched Apply saves — the stored ghost —
    // so the user is never shown a pose the save would not produce, and a note
    // says the current face is kept until a pick replaces it.
    mount(STORED_GHOST)
    expect(screen.queryByTestId('avatar-icon-preview')).toBeNull()
    const note = screen.getByTestId('avatar-icon-kept-ghost-note')
    expect(note).toHaveTextContent('Current face is kept until you pick a pose or colour.')
  })

  it('switches the preview to the chosen pose (note gone) once the user picks', () => {
    mount(STORED_GHOST)
    fireEvent.click(screen.getByTestId('avatar-icon-pose-pose-2'))
    expect(screen.getByTestId('avatar-icon-preview')).toBeInTheDocument()
    expect(screen.queryByTestId('avatar-icon-kept-ghost-note')).toBeNull()
  })

  it('shows the name-derived default preview after Reset — not a seeded icon pose', () => {
    // After Reset, Apply returns null (the name-derived default, which the roster
    // draws as the name-seeded ghost). The preview must match that, so it renders
    // CrewAvatar with no override rather than an icon-pose img the save would not
    // produce. The kept-ghost note is gone too.
    const { onSave } = mount(STORED_GHOST)
    fireEvent.click(screen.getByTestId('avatar-builder-reset'))
    expect(screen.queryByTestId('avatar-icon-preview')).toBeNull()
    expect(screen.queryByTestId('avatar-icon-kept-ghost-note')).toBeNull()
    apply()
    expect(lastSaved(onSave)).toBeNull()
  })

  it('keeps the stored ghost face verbatim when Apply is pressed without picking a pose', () => {
    // The edge case: editing a ghost-face crew must not silently rewrite its
    // face. An untouched Apply hands the stored record straight back — traits
    // and reactions intact — so only an active pick changes it.
    const { onSave } = mount(STORED_GHOST)
    apply()
    expect(lastSaved(onSave)).toBe(STORED_GHOST)
  })

  it('replaces the ghost with an icon only once the user actively picks a pose', () => {
    const { onSave } = mount(STORED_GHOST)
    fireEvent.click(screen.getByTestId('avatar-icon-pose-pose-2'))
    apply()
    expect(lastSaved(onSave)).toMatchObject({ kind: 'icon', pose: 'pose-2' })
  })

  it('replaces the ghost with an icon when the user picks a background colour', () => {
    const { onSave } = mount(STORED_GHOST)
    fireEvent.click(screen.getByTestId('avatar-icon-bg-25679d'))
    apply()
    expect((lastSaved(onSave) as Icon).kind).toBe('icon')
    expect((lastSaved(onSave) as Icon).bg).toBe('#25679d')
  })

  it('Reset clears the stored ghost to the name-derived default', () => {
    const { onSave } = mount(STORED_GHOST)
    fireEvent.click(screen.getByTestId('avatar-builder-reset'))
    apply()
    expect(lastSaved(onSave)).toBeNull()
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
    // Icon tab: nothing intercepts, the browser default stands.
    expect(dropOnBody()).toBe(false)
    await switchToPicture()
    // Picture tab: a stray drop must not navigate the SPA to the file.
    expect(dropOnBody()).toBe(true)
    const over = new Event('dragover', { bubbles: true, cancelable: true })
    document.body.dispatchEvent(over)
    expect(over.defaultPrevented).toBe(true)
    // Back on the icon tab the listeners are gone again.
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

  it('Reset drops the pending picture and returns to the icon default', async () => {
    const { onSave } = mount()
    await switchToPicture()
    chooseFile(pngFile())
    await decode()
    await screen.findByTestId('avatar-upload-preview')
    fireEvent.click(screen.getByTestId('avatar-builder-reset'))
    expect(screen.getByTestId('avatar-icon-pane')).toBeInTheDocument()
    apply()
    expect(lastSaved(onSave)).toBeNull()
  })
})

/* ──────────────────────────── misc ──────────────────────────── */

describe('CrewAvatarBuilder — dialog', () => {
  it('Cancel reports back without saving', () => {
    const { onSave, onCancel } = mount()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(onSave).not.toHaveBeenCalled()
  })

  it('a stored ghost still renders its ghost icon via the shared renderer', () => {
    // Guards requirement 3: removing the authoring UI must not touch rendering.
    // The roster's own renderer still draws a ghost record's silhouette.
    const ghost: Ghost = {
      kind: 'ghost',
      traits: { eyes: 'wink', brows: 'none', mouth: 'smile', accessory: 'halo', prop: 'none', blush: true, flip: false, tile: '#21a5de' },
    }
    const { container } = render(<CrewAvatar seed="oncall" avatar={ghost} size={38} />)
    const src = container.querySelector('img')?.getAttribute('src') ?? ''
    expect(src.startsWith('data:image/svg+xml')).toBe(true)
  })
})
