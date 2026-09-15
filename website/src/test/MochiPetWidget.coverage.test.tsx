/**
 * Mochi PetWidget — the desktop pet overlay.
 *
 * The widget had no test at all, so every behaviour below is pinned here for the
 * first time. The whole component talks to the Electron main process through one
 * seam (`api` in `mochiApi`), so the harness mocks that module and drives the
 * widget the way the main process does: an activation frame, then IPC events
 * (state change, bubble, appearance switch, hide, drag).
 *
 * Everything is on fake timers on purpose — the widget stages its art swaps
 * behind 150ms fades, holds a state for a 2s minimum, auto-dismisses bubbles
 * after 6s, and walks on requestAnimationFrame. Fake timers make all of that
 * deterministic instead of a race.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'

import svgIdleRaw from '../apps/mochi/assets/animations/mochi_idle.svg?raw'
import svgPeekRaw from '../apps/mochi/assets/animations/mochi_peek.svg?raw'

const mocks = vi.hoisted(() => {
  type Listener = (...args: never[]) => void
  const listeners = new Map<string, Set<Listener>>()
  /** Mirrors the preload's `onX(cb) => off` subscription shape. */
  const on = (event: string) => (cb: Listener) => {
    const set = listeners.get(event) ?? new Set<Listener>()
    set.add(cb)
    listeners.set(event, set)
    return () => { set.delete(cb) }
  }
  const api = {
    // ── one-shot reads ──
    getPetState: vi.fn<() => Promise<string>>(),
    getMochiConfig: vi.fn<() => Promise<unknown>>(),
    getWindowPosition: vi.fn<() => Promise<{ x: number; y: number } | null>>(),
    presetsGetColorMap: vi.fn<(packId: string) => Promise<Record<string, string>>>(),
    galleryGetPackDetail: vi.fn<(packId: string) => Promise<unknown>>(),
    // ── reads the context menu makes ──
    getQuietUntil: vi.fn<() => Promise<number>>(),
    getConfig: vi.fn<() => Promise<unknown>>(),
    setQuiet: vi.fn<(minutes: number) => Promise<number>>(),
    contextMenuAction: vi.fn(),
    setMenuHitbox: vi.fn(),
    menuOpened: vi.fn(),
    menuClosed: vi.fn(),
    // ── commands ──
    savePosition: vi.fn(),
    walkDone: vi.fn(),
    openChat: vi.fn(),
    dismissBubble: vi.fn(),
    setPeeking: vi.fn(),
    updateHitbox: vi.fn(),
    dragStart: vi.fn(),
    dragEnd: vi.fn(),
    dragMouseup: vi.fn(),
    reportWalkDistance: vi.fn(),
    // ── subscriptions ──
    onSetActive: on('setActive'),
    onDisplaysInfo: on('displaysInfo'),
    onStateChange: on('stateChange'),
    onBubble: on('bubble'),
    onMood: on('mood'),
    onThemeChanged: on('themeChanged'),
    onConfigUpdated: on('configUpdated'),
    onColorMapChanged: on('colorMapChanged'),
    onGalleryActiveChanged: on('galleryActiveChanged'),
    onDragListenMouseup: on('dragListenMouseup'),
    onDragUpdate: on('dragUpdate'),
    onDragEnded: on('dragEnded'),
    onWalk: on('walk'),
    onWalkPath: on('walkPath'),
    onWalkCancel: on('walkCancel'),
    onWalkAppend: on('walkAppend'),
    onHide: on('hide'),
    onApprovalRequest: on('approvalRequest'),
    onApprovalResolvedExternal: on('approvalResolvedExternal'),
  }
  const emit = (event: string, ...args: unknown[]) => {
    for (const cb of [...(listeners.get(event) ?? [])]) {
      ;(cb as (...a: unknown[]) => void)(...args)
    }
  }
  return { api, emit, clearListeners: () => listeners.clear() }
})

vi.mock('../apps/mochi/src/mochiApi', () => ({ api: mocks.api }))

import { PetWidget } from '../apps/mochi/src/renderer/PetWidget'

const api = mocks.api
const SVG_PREFIX = 'data:image/svg+xml,'
/** `toDataUri` strips the XML declaration before encoding. */
const stripXmlDecl = (raw: string) => raw.replace(/<\?xml[^?]*\?>\s*/, '')

function petImg(): HTMLImageElement {
  const img = document.body.querySelector('img')
  if (!img) throw new Error('pet art is not rendered')
  return img as HTMLImageElement
}

/** The pet's own draggable container — the art's direct parent. */
function petBox(): HTMLElement {
  const parent = petImg().parentElement
  if (!parent) throw new Error('pet container is not rendered')
  return parent
}

/** The SVG source actually painted, decoded back out of the data URI. */
function petArt(): string {
  const src = petImg().getAttribute('src') ?? ''
  expect(src.startsWith(SVG_PREFIX)).toBe(true)
  return decodeURIComponent(src.slice(SVG_PREFIX.length))
}

/** Let promise chains settle and staged timers fire. */
async function tick(ms = 0): Promise<void> {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
}

/**
 * Mount the widget and let the main process activate this overlay, which is the
 * only way anything renders at all.
 *
 * Activated WITHOUT coordinates, so the pet stays wherever the saved-position
 * read put it — that read is what each test controls via `getWindowPosition`.
 */
async function mountActive(): Promise<void> {
  render(<PetWidget />)
  // 300ms saved-position read (useDrag) + 500ms activation fallback (useDisplayActivation).
  await tick(600)
  await act(async () => { mocks.emit('setActive', true) })
}

const ghostPack = (over: Record<string, unknown> = {}) => ({
  meta: { id: 'kiro-ghost', name: 'Kiro Ghost', format: 'svg' },
  animations: {
    idle: '<svg id="ghost-idle"></svg>',
    walking: '<svg id="ghost-walk"></svg>',
    happy: '<svg id="ghost-happy"></svg>',
  },
  ...over,
})

beforeEach(() => {
  vi.useFakeTimers()
  mocks.clearListeners()
  vi.resetAllMocks()
  api.getPetState.mockResolvedValue('idle')
  api.getMochiConfig.mockResolvedValue({ theme: 'kirocrew' })
  api.getWindowPosition.mockResolvedValue({ x: 300, y: 400 })
  api.presetsGetColorMap.mockResolvedValue({})
  api.galleryGetPackDetail.mockResolvedValue(null)
  api.getQuietUntil.mockResolvedValue(0)
  api.getConfig.mockResolvedValue({ shortcuts: {} })
  api.setQuiet.mockResolvedValue(0)
})

afterEach(() => {
  vi.useRealTimers()
})

describe('PetWidget overlay activation', () => {
  it('renders nothing until the main process activates this display', async () => {
    const { container } = render(<PetWidget />)
    await tick(600)
    // One overlay exists per display; the pet lives in exactly one of them.
    expect(container.querySelector('img')).toBeNull()

    await act(async () => { mocks.emit('setActive', true, 300, 400, false) })
    expect(container.querySelector('img')).not.toBeNull()
  })

  it('unmounts the pet again when the overlay is deactivated', async () => {
    await mountActive()
    expect(petImg()).toBeTruthy()

    await act(async () => { mocks.emit('setActive', false) })
    expect(document.body.querySelector('img')).toBeNull()
  })

  it('places the pet at the saved position and reveals it once the read lands', async () => {
    await mountActive()
    expect(petBox().style.left).toBe('300px')
    expect(petBox().style.top).toBe('400px')
    expect(petBox().style.opacity).toBe('1')
  })

  it('adopts the coordinates it is activated with, so a cross-display drag lands in place', async () => {
    render(<PetWidget />)
    await tick(600)
    // The pet was dragged onto THIS display: the main process hands over both
    // the position and the fact that a drag is still in progress.
    await act(async () => { mocks.emit('setActive', true, 640, 120, true) })

    expect(petBox().style.left).toBe('640px')
    expect(petBox().style.top).toBe('120px')
    // Mid-drag hand-over: the drag ends here, not on the display it started on.
    await act(async () => { mocks.emit('dragEnded', 640, 120) })
    expect(api.savePosition).toHaveBeenCalledWith(640, 120)
  })

  it('falls back to the bottom-left edge when there is no saved position', async () => {
    api.getWindowPosition.mockResolvedValue(null)
    await mountActive()
    // No saved position → parked against the left edge, and peeking from it.
    expect(api.setPeeking).toHaveBeenCalledWith(true)
    expect(petArt()).toBe(stripXmlDecl(svgPeekRaw))
  })
})

describe('PetWidget art and state', () => {
  it('names the pet and its displayed state in the art tooltip', async () => {
    await mountActive()
    expect(screen.getByTitle('Mochi: idle')).toBe(petImg())
  })

  it('uses the name the user configured', async () => {
    api.getMochiConfig.mockResolvedValue({ theme: 'kirocrew', petName: '  Ghosty  ' })
    await mountActive()
    expect(screen.getByTitle('Ghosty: idle')).toBeTruthy()
  })

  it('swaps art on a state change, after the fade', async () => {
    await mountActive()
    await act(async () => { mocks.emit('stateChange', 'working') })
    // The swap is staged behind a 150ms fade-out so the art never pops.
    expect(petImg().style.opacity).toBe('0')

    await tick(150)
    expect(screen.getByTitle('Mochi: working')).toBeTruthy()
    expect(petImg().style.opacity).toBe('1')
  })

  it('holds a non-idle state for its minimum display time before applying the next one', async () => {
    await mountActive()
    await act(async () => { mocks.emit('stateChange', 'working') })
    await tick(150)
    expect(screen.getByTitle('Mochi: working')).toBeTruthy()

    // Arrives inside the 2s lock — queued rather than applied, so the art
    // cannot flicker through a state nobody could read.
    await act(async () => { mocks.emit('stateChange', 'idle') })
    expect(screen.getByTitle('Mochi: working')).toBeTruthy()

    await tick(2000)
    expect(screen.getByTitle('Mochi: idle')).toBeTruthy()
  })

  it('ignores a state event that repeats the state already shown', async () => {
    await mountActive()
    await act(async () => { mocks.emit('stateChange', 'idle') })
    // Same state as the current one → no fade, no re-render churn.
    expect(petImg().style.opacity).toBe('1')
    expect(screen.getByTitle('Mochi: idle')).toBeTruthy()
  })

  it('paints the built-in art, recoloured by the saved colour map', async () => {
    api.presetsGetColorMap.mockResolvedValue({ '#E98649': '#00FF00' })
    await mountActive()
    expect(api.presetsGetColorMap).toHaveBeenCalledWith('default-mochi')
    expect(petArt()).toContain('#00FF00')
  })

  it('recolours live when the colour map changes for the built-in pack', async () => {
    await mountActive()
    expect(petArt()).toContain('#E98649')

    await act(async () => {
      mocks.emit('colorMapChanged', { packId: 'default-mochi', colorMap: { '#E98649': '#123456' } })
    })
    expect(petArt()).toContain('#123456')

    // A colour map for some OTHER pack must not repaint this one.
    await act(async () => {
      mocks.emit('colorMapChanged', { packId: 'some-other-pack', colorMap: { '#123456': '#ABCDEF' } })
    })
    expect(petArt()).toContain('#123456')
  })
})

describe('PetWidget appearance packs', () => {
  it('paints art from the active pack instead of the built-in cat', async () => {
    api.getMochiConfig.mockResolvedValue({ theme: 'kirocrew', activeAppearance: 'kiro-ghost' })
    api.galleryGetPackDetail.mockResolvedValue(ghostPack())
    await mountActive()

    expect(api.galleryGetPackDetail).toHaveBeenCalledWith('kiro-ghost')
    // A non-built-in pack is not recolourable, so no colour map is read for it.
    expect(api.presetsGetColorMap).not.toHaveBeenCalled()
    expect(petArt()).toContain('ghost-idle')
  })

  it('mirrors a pack that declares its art faces the other way', async () => {
    api.getMochiConfig.mockResolvedValue({ theme: 'kirocrew', activeAppearance: 'kiro-ghost' })
    api.galleryGetPackDetail.mockResolvedValue(ghostPack({ flipX: true }))
    await mountActive()
    expect(petBox().style.transform).toContain('scaleX(-1)')
  })

  it('mirrors the pet when it sits on the right half of the screen', async () => {
    const rightSide = Math.floor(window.innerWidth * 0.6)
    api.getWindowPosition.mockResolvedValue({ x: rightSide, y: 400 })
    await mountActive()
    expect(petBox().style.transform).toContain('scaleX(-1)')
  })

  it('keeps the built-in art and says why when a pack ships no idle drawing', async () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    api.getMochiConfig.mockResolvedValue({ theme: 'kirocrew', activeAppearance: 'kiro-ghost' })
    api.galleryGetPackDetail.mockResolvedValue({
      meta: { id: 'kiro-ghost', format: 'svg' },
      animations: { walking: '<svg id="ghost-walk"></svg>' },
    })
    await mountActive()

    expect(err).toHaveBeenCalledWith(
      '[mochi] pack cannot drive the pet — no idle art',
      expect.objectContaining({ packId: 'kiro-ghost' }),
    )
    expect(petArt()).toBe(stripXmlDecl(svgIdleRaw))
    err.mockRestore()
  })

  it('reports a pack whose detail carries no art at all', async () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    api.getMochiConfig.mockResolvedValue({ theme: 'kirocrew', activeAppearance: 'kiro-ghost' })
    api.galleryGetPackDetail.mockResolvedValue({ meta: { id: 'kiro-ghost' } })
    await mountActive()

    expect(err).toHaveBeenCalledWith(
      '[mochi] active pack returned no usable detail',
      expect.objectContaining({ packId: 'kiro-ghost', hasAnimations: false }),
    )
    expect(petArt()).toBe(stripXmlDecl(svgIdleRaw))
    err.mockRestore()
  })

  it('reports a pack that could not be loaded, and still draws a pet', async () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    api.getMochiConfig.mockResolvedValue({ theme: 'kirocrew', activeAppearance: 'kiro-ghost' })
    api.galleryGetPackDetail.mockRejectedValue(new Error('pack file is gone'))
    await mountActive()

    expect(err).toHaveBeenCalledWith(
      '[mochi] could not load the active pack',
      'kiro-ghost',
      expect.any(Error),
    )
    expect(petArt()).toBe(stripXmlDecl(svgIdleRaw))
    err.mockRestore()
  })

  it('switches art live when the user applies another pack, and back again', async () => {
    await mountActive()
    expect(petArt()).toContain('#E98649')

    await act(async () => {
      mocks.emit('galleryActiveChanged', {
        packId: 'kiro-ghost',
        meta: { id: 'kiro-ghost', format: 'svg' },
        animations: { idle: '<svg id="live-ghost"></svg>' },
      })
    })
    await tick(150)
    expect(petArt()).toContain('live-ghost')

    // Back to the built-in pack: its saved colour map is restored with it.
    api.presetsGetColorMap.mockResolvedValue({ '#E98649': '#00FF00' })
    await act(async () => { mocks.emit('galleryActiveChanged', { packId: 'default-mochi' }) })
    await tick(150)
    expect(petArt()).toContain('#00FF00')
  })

  it('reports a live switch that carried no usable art, and keeps the current art', async () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    await mountActive()

    await act(async () => { mocks.emit('galleryActiveChanged', { packId: 'kiro-ghost' }) })
    await tick(150)

    expect(err).toHaveBeenCalledWith(
      '[mochi] live appearance switch carried no usable art',
      expect.objectContaining({ packId: 'kiro-ghost' }),
    )
    expect(petArt()).toBe(stripXmlDecl(svgIdleRaw))
    err.mockRestore()
  })

  it('slides a pack with no peek drawing partly off-screen while it peeks', async () => {
    api.getMochiConfig.mockResolvedValue({ theme: 'kirocrew', activeAppearance: 'kiro-ghost' })
    api.galleryGetPackDetail.mockResolvedValue(ghostPack())
    api.getWindowPosition.mockResolvedValue({ x: 0, y: 400 })
    await mountActive()

    // The pack falls back to `idle` for the peek pose, so the POSITION has to
    // carry the meaning the missing drawing would have.
    expect(petBox().style.transform).toContain('translateX(-60px)')
  })

  it('draws the BUILT-IN peek art even when a pack is active (current behaviour — see comment)', async () => {
    api.getMochiConfig.mockResolvedValue({ theme: 'kirocrew', activeAppearance: 'kiro-ghost' })
    api.galleryGetPackDetail.mockResolvedValue(ghostPack())
    api.getWindowPosition.mockResolvedValue({ x: 0, y: 400 })
    await mountActive()

    // DEFECT, pinned rather than endorsed: the render path computes the pack's
    // own source (`currentSource`) and then throws it away for the SVG case,
    // calling `resolveSvg` instead — whose first branch returns the compiled-in
    // cat peek whenever the pet is peeking, with no resolver check. So a user on
    // another pack sees the CAT's peek drawing at the screen edge (and, via
    // peekNudgeFor, sees it nudged off-screen at the same time). The comment on
    // that branch claims it is "only reached when no resolver/pack", which is
    // exactly the assumption that does not hold.
    expect(petArt()).toBe(stripXmlDecl(svgPeekRaw))
    expect(petArt()).not.toContain('ghost-idle')
  })
})

describe('PetWidget speech bubble', () => {
  it('shows a bubble and dismisses it on click', async () => {
    await mountActive()
    await act(async () => { mocks.emit('bubble', 'Build finished', false) })
    expect(screen.getByText('Build finished')).toBeTruthy()

    const dismiss = screen.getByRole('button', { name: 'Dismiss message' })
    await act(async () => { fireEvent.click(dismiss) })
    expect(api.dismissBubble).toHaveBeenCalled()

    await tick(300) // fade-out
    expect(screen.queryByText('Build finished')).toBeNull()
  })

  it('auto-dismisses a non-sticky bubble', async () => {
    await mountActive()
    await act(async () => { mocks.emit('bubble', 'Walking to the corner', false) })

    await tick(5000)
    expect(screen.getByText('Walking to the corner')).toBeTruthy()

    await tick(1000 + 300) // auto-dismiss, then the fade
    expect(screen.queryByText('Walking to the corner')).toBeNull()
  })

  it('survives a retired theme id from stored settings', async () => {
    // Theme ids were collapsed to one; a pre-consolidation value read from disk
    // used to take the whole pet tree down on the first bubble render.
    api.getMochiConfig.mockResolvedValue({ theme: 'mocha' })
    await mountActive()
    await act(async () => { mocks.emit('bubble', 'still here', false) })

    expect(screen.getByText('still here')).toBeTruthy()
    expect(petImg()).toBeTruthy()
  })

  it('keeps rendering across live theme and config updates', async () => {
    await mountActive()
    await act(async () => {
      mocks.emit('themeChanged', 'kirocrew')
      mocks.emit('configUpdated', { theme: 'kirocrew' })
    })
    await act(async () => { mocks.emit('bubble', 'after the theme change', false) })
    expect(screen.getByText('after the theme change')).toBeTruthy()
  })

  it('raises a sticky bubble for a pending approval and retracts it when answered elsewhere', async () => {
    await mountActive()
    await act(async () => { mocks.emit('approvalRequest', { purpose: 'reading the repo' }) })
    expect(screen.getByText('Mochi needs your approval for reading the repo')).toBeTruthy()

    // Sticky: an approval blocks the agent, so it must not time out unanswered.
    await tick(30_000)
    expect(screen.getByText('Mochi needs your approval for reading the repo')).toBeTruthy()

    await act(async () => { mocks.emit('approvalResolvedExternal') })
    await tick(300)
    expect(screen.queryByText(/needs your approval/)).toBeNull()
  })
})

describe('PetWidget interaction', () => {
  it('opens the chat on double-click', async () => {
    await mountActive()
    await act(async () => { fireEvent.dblClick(petBox()) })
    expect(api.openChat).toHaveBeenCalled()
  })

  it('opens the pet menu on right-click and closes it again', async () => {
    await mountActive()
    await act(async () => { fireEvent.contextMenu(petBox(), { clientX: 40, clientY: 60 }) })
    await tick()

    const quiet = screen.getByText('Quiet for 1 hour')
    expect(quiet).toBeTruthy()

    await act(async () => { fireEvent.click(quiet) })
    expect(api.setQuiet).toHaveBeenCalledWith(60)
  })

  it('moves the pet with the mouse and persists where it was dropped', async () => {
    await mountActive()
    await act(async () => { fireEvent.mouseDown(petBox(), { clientX: 350, clientY: 400 }) })
    // Grab offset is 50px into the body, so the pet trails the cursor by that.
    await act(async () => { fireEvent.mouseMove(window, { clientX: 500, clientY: 420 }) })

    expect(api.dragStart).toHaveBeenCalledWith(50, 0)
    expect(petBox().style.left).toBe('450px')

    await act(async () => { fireEvent.mouseUp(window) })
    expect(api.dragEnd).toHaveBeenCalled()
    expect(api.savePosition).toHaveBeenCalledWith(450, 420)
  })

  it('snaps to the edge and starts peeking when the drag ends near one', async () => {
    await mountActive()
    await act(async () => { mocks.emit('dragEnded', 10, 200) })

    expect(petBox().style.left).toBe('0px')
    expect(api.savePosition).toHaveBeenCalledWith(0, 200)
    expect(api.setPeeking).toHaveBeenLastCalledWith(true)
  })

  it('listens for a cross-display drag mouseup exactly once', async () => {
    await mountActive()
    await act(async () => { mocks.emit('dragListenMouseup') })

    await act(async () => { fireEvent.mouseUp(window) })
    expect(api.dragMouseup).toHaveBeenCalledTimes(1)

    // The one-shot handler removes itself, so a later mouseup is not reported.
    await act(async () => { fireEvent.mouseUp(window) })
    expect(api.dragMouseup).toHaveBeenCalledTimes(1)
  })
})

describe('PetWidget walking and hiding', () => {
  it('walks to a target, then persists the position and reports the distance', async () => {
    await mountActive()
    await act(async () => { mocks.emit('walk', 500, 400) })
    // 200px at 6ms/px → 1200ms of rAF steps.
    await tick(1500)

    expect(petBox().style.left).toBe('500px')
    expect(api.savePosition).toHaveBeenCalledWith(500, 400)
    expect(api.walkDone).toHaveBeenCalled()
    expect(api.reportWalkDistance).toHaveBeenCalled()
    // Landed mid-screen, so it is not peeking from an edge.
    expect(api.setPeeking).toHaveBeenLastCalledWith(false)
  })

  it('walks to the edge and peeks from it when told to hide', async () => {
    await mountActive()
    await act(async () => { mocks.emit('hide', 'left') })
    await tick(3000)

    expect(petBox().style.left).toBe('0px')
    expect(api.savePosition).toHaveBeenCalledWith(0, 400)
    expect(api.setPeeking).toHaveBeenLastCalledWith(true)
    // At the edge with the built-in pack, the peek POSE is what gets drawn.
    expect(petArt()).toBe(stripXmlDecl(svgPeekRaw))
  })

  it('draws the thinking peek pose when it is busy at the edge', async () => {
    api.getWindowPosition.mockResolvedValue({ x: 0, y: 400 })
    await mountActive()
    await act(async () => { mocks.emit('stateChange', 'thinking') })
    await tick(150)

    expect(screen.getByTitle('Mochi: thinking')).toBeTruthy()
    // Peek art is compiled in rather than part of any pack, so a busy pet at
    // the edge gets the dedicated thinking-peek drawing.
    expect(petArt()).not.toBe(stripXmlDecl(svgPeekRaw))
    expect(petArt()).toContain('<svg')
  })

  it('cancels a walk as soon as the user grabs the pet', async () => {
    await mountActive()
    await act(async () => { mocks.emit('walk', 900, 400) })
    await tick(300) // mid-walk

    const midX = parseFloat(petBox().style.left)
    expect(midX).toBeGreaterThan(300)
    expect(midX).toBeLessThan(900)

    await act(async () => { fireEvent.mouseDown(petBox(), { clientX: midX, clientY: 400 }) })
    await tick(2000)
    // The walk is abandoned where the grab happened — it does not resume.
    expect(parseFloat(petBox().style.left)).toBeCloseTo(midX, 0)
  })

  it('reflects the pet mood in the art it draws', async () => {
    await mountActive()
    const idleArt = petArt()

    await act(async () => { mocks.emit('mood', 'happy', 1) })
    await act(async () => { mocks.emit('stateChange', 'working') })
    await tick(150)

    // A mood outranks the state art for the built-in pack.
    expect(petArt()).not.toBe(idleArt)
  })

  it('draws the walking art from the active pack while it moves', async () => {
    api.getMochiConfig.mockResolvedValue({ theme: 'kirocrew', activeAppearance: 'kiro-ghost' })
    api.galleryGetPackDetail.mockResolvedValue(ghostPack())
    await mountActive()

    await act(async () => { mocks.emit('walk', 500, 400) })
    await tick(300) // mid-walk
    expect(petArt()).toContain('ghost-walk')

    await tick(1500) // arrives
    expect(petArt()).toContain('ghost-idle')
  })

  it('tilts the pet on a diagonal walk', async () => {
    await mountActive()
    await act(async () => { mocks.emit('walk', 500, 600) })
    await tick(300)
    // 45° of travel reads as a diagonal, which leans the body into the move.
    expect(petBox().style.transform).toMatch(/rotate\([\d.]+deg\)/)
  })
})

describe('PetWidget non-SVG pack formats', () => {
  it('renders a sprite-sheet pack on a canvas, honouring the sheet geometry', async () => {
    api.getMochiConfig.mockResolvedValue({ theme: 'kirocrew', activeAppearance: 'boba-sprite' })
    api.galleryGetPackDetail.mockResolvedValue({
      meta: { id: 'boba-sprite', name: 'Boba', format: 'sprite' },
      animations: { idle: 'iVBORw0KGgo=' },
      sprite: { frameWidth: 64, frameHeight: 64, fps: 12 },
      flipX: true,
    })
    await mountActive()

    const canvas = document.body.querySelector('canvas')
    expect(canvas).not.toBeNull()
    // A sprite pack draws frames, so there is no <img> art element at all.
    expect(document.body.querySelector('img')).toBeNull()

    const box = canvas!.parentElement!.parentElement!
    // The pack declares its art is mirrored, so the container un-mirrors it.
    expect(box.style.transform).toContain('scaleX(-1)')
  })

  it("prefers the pack's own mood art over its state art", async () => {
    api.getMochiConfig.mockResolvedValue({ theme: 'kirocrew', activeAppearance: 'kiro-ghost' })
    api.galleryGetPackDetail.mockResolvedValue(ghostPack())
    await mountActive()
    expect(petArt()).toContain('ghost-idle')

    await act(async () => { mocks.emit('mood', 'happy', 1) })
    await act(async () => { mocks.emit('stateChange', 'working') })
    await tick(150)
    // The pack ships a `happy` drawing, which outranks the state slot.
    expect(petArt()).toContain('ghost-happy')
  })

  it('warns once when the active pack resolves to no art at all', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    api.getMochiConfig.mockResolvedValue({ theme: 'kirocrew', activeAppearance: 'ghost-lottie' })
    api.galleryGetPackDetail.mockResolvedValue({
      meta: { id: 'ghost-lottie', format: 'lottie' },
      animations: { idle: { content: '', format: 'lottie' } },
    })
    await mountActive()

    // Empty art, a pack that never loaded, and a slot with no drawing all look
    // identical on screen — this is the one that is a genuine fault, so it is
    // the one that gets a line in the console.
    expect(warn).toHaveBeenCalledWith('[mochi-pet] pack resolved NO art for', 'idle')
    warn.mockRestore()
  })

  it('hands a lottie-format pack to the lottie renderer, and reports a clip it cannot parse', async () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    api.getMochiConfig.mockResolvedValue({ theme: 'kirocrew', activeAppearance: 'ghost-lottie' })
    api.galleryGetPackDetail.mockResolvedValue({
      meta: { id: 'ghost-lottie', name: 'Ghost', format: 'lottie' },
      // Per-slot format, the shape the gallery sends for a mixed pack.
      animations: { idle: { content: 'not-json-at-all', format: 'lottie' } },
    })
    await mountActive()

    // Lottie art is built into a container, never an <img>.
    expect(document.body.querySelector('img')).toBeNull()
    // A clip that cannot be parsed used to render as a silent empty box.
    expect(err).toHaveBeenCalledWith(
      '[mochi] lottie JSON parse failed',
      expect.objectContaining({ head: 'not-json-at-all' }),
      expect.anything(),
    )
    err.mockRestore()
  })

  it('recolours the built-in pack through an active resolver', async () => {
    await mountActive()
    // Switch to a pack so a resolver exists, then send a built-in colour map:
    // the resolver has to be told, or its cached art keeps the old colours.
    await act(async () => {
      mocks.emit('galleryActiveChanged', {
        packId: 'kiro-ghost',
        meta: { id: 'kiro-ghost', format: 'svg' },
        animations: { idle: '<svg fill="#E98649" id="ghost-idle"></svg>' },
      })
    })
    await tick(150)
    expect(petArt()).toContain('#E98649')

    await act(async () => {
      mocks.emit('colorMapChanged', { packId: 'default-mochi', colorMap: { '#E98649': '#00FF00' } })
    })
    expect(petArt()).toContain('#00FF00')
  })
})
