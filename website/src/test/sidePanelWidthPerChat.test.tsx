/**
 * Per-chat side panel size, end to end through the real strip.
 *
 * The panel remembers one width (and one bottom-dock height) per CHAT, keyed by
 * its `slot` prop, instead of one for the whole panel. Four contracts:
 *
 * 1. The rendered size follows the SLOT and never the tab: switching tabs inside
 *    one chat leaves the panel where it is, whatever the tab kinds, and never
 *    animates. Switching chats reads the other chat's size and eases the move.
 * 2. A drag persists under the chat's own key AND the bare base key, so a chat
 *    opened for the first time starts at the size last dragged anywhere, and an
 *    install upgrading from one size for the whole panel starts every chat at
 *    the size it already had.
 * 3. An empty slot (a host whose thread is not confirmed yet) reads and writes
 *    the bare key alone, and never mints a per-slot key.
 * 4. A gesture belongs to the chat it started on: a re-key mid-drag redirects
 *    neither the write nor the size on screen. The one exception is an empty
 *    slot confirmed mid-drag, which is the same chat receiving its key, so the
 *    release saves under that key.
 *
 * Bodies are stubbed as in sidePanelLeadingTab.test.tsx; only the strip, the
 * root's inline size and localStorage are driven.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { forwardRef } from 'react'
import { render, screen, act, cleanup, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'

vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
// forwardRef because the file tab passes a ref through; a plain function stub
// renders but warns, and the in-chat tab-switch case here does open a file tab.
vi.mock('../components/MarkdownPanel', () => ({ default: forwardRef(() => null) }))
vi.mock('../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../components/WebPreviewPanel', () => ({ default: () => <div data-testid="web-preview-body" /> }))
vi.mock('../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../components/CliPanel', () => ({
  default: () => null,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => true,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../hooks/useDevMode', () => ({ useDevMode: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel from '../pages/chat/SidePanel'
import { usePanelTabs, __resetPanelTabs } from '../hooks/usePanelTabs'
import { setSidePanelDock } from '../hooks/useSidePanelDock'
import { SIDE_PANEL_HEIGHT_KEY, SIDE_PANEL_WIDTH_KEY, sidePanelDimKey } from '../pages/chat/sidePanelWidth'
import { SIDE_PANEL_MOTION_MS, sidePanelDimTransition } from '../pages/chat/sidePanelMount'

/** Default width and height when nothing is stored (SidePanel's own). */
const DEFAULT_W = 460
const DEFAULT_H = 360

const A = 'chat-a'
const B = 'chat-b'

const widthKey = (slot: string) => sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, slot)
const heightKey = (slot: string) => sidePanelDimKey(SIDE_PANEL_HEIGHT_KEY, slot)

let ctl: ReturnType<typeof usePanelTabs> | null = null

type PanelProps = { slot: string; slotOwner?: string; persistSlot?: string; tabsSlot?: string; expanded?: boolean; fillWidth?: number; canDockBottom?: boolean; extraReserveW?: number }

function Harness({ slot, slotOwner, persistSlot, tabsSlot, expanded, fillWidth, canDockBottom = false, extraReserveW }: PanelProps) {
  // The strip is per slot too, so a slot switch swaps the tabs the way the chat
  // page does; the panel itself is NOT remounted, exactly as its stable-keyed
  // host wrappers keep it.
  // `tabsSlot` lets a test hand the strip a different key than the panel, so a
  // panel-only behavior can be exercised in isolation.
  const tabsCtl = usePanelTabs((tabsSlot ?? slot) || null)
  ctl = tabsCtl
  return (
    <SidePanel
      tabsCtl={tabsCtl}
      slot={slot}
      slotOwner={slotOwner}
      persistSlot={persistSlot}
      onFileSave={async () => {}}
      onClose={() => {}}
      canDockBottom={canDockBottom}
      expanded={expanded}
      fillWidth={fillWidth}
      extraReserveW={extraReserveW}
    />
  )
}

function renderPanel(props: Partial<PanelProps> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const store = createTestStore()
  const base: PanelProps = { slot: A, ...props }
  // One client and one store across rerenders on purpose: a fresh pair would
  // remount the panel, and a remount resets the very per-session memory and
  // motion state under test.
  const tree = (p: PanelProps) => (
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <Harness {...p} />
      </Provider>
    </QueryClientProvider>
  )
  const result = render(tree(base))
  let current = base
  return {
    ...result,
    /** Re-render with `next` merged over the props already in effect. */
    setProps: (next: Partial<PanelProps>) => {
      current = { ...current, ...next }
      result.rerender(tree(current))
    },
    /** Switch the panel to another chat, as the host does on a session switch. */
    switchSlot: (slot: string) => {
      current = { ...current, slot }
      act(() => { result.rerender(tree(current)) })
    },
  }
}

/** The panel root's rendered width / height in px. */
const renderedWidth = () => screen.getByTestId('side-panel-root').style.width
const renderedHeight = () => screen.getByTestId('side-panel-root').style.height

/** The panel root's inline `transition`, empty string when it carries none. */
const transitionStyle = () => screen.getByTestId('side-panel-root').style.transition

const handle = (orientation: 'vertical' | 'horizontal') =>
  screen.getAllByRole('separator').find(el => el.getAttribute('aria-orientation') === orientation)!

/** Drag the left-edge resize handle by `dx` (negative widens).
 *
 *  The move and the release are separate `act` batches on purpose: the panel
 *  persists the width its last render committed, and a browser delivers
 *  pointermove and pointerup as separate tasks with a render between them. One
 *  batch would leave the release reading the pre-drag width, a harness artifact
 *  rather than the behavior under test. */
function dragHandle(dx: number) {
  startDrag(dx)
  releaseDrag(dx)
}

/** Press the handle and move it WITHOUT releasing, so the panel is observed
 *  mid-drag. The release is the caller's job (or the test's cleanup). */
function startDrag(dx: number) {
  const el = handle('vertical')
  act(() => {
    fireEvent.pointerDown(el, { pointerId: 1, pointerType: 'mouse', button: 0, clientX: 1000, clientY: 300 })
    fireEvent.pointerMove(el, { pointerId: 1, pointerType: 'mouse', clientX: 1000 + dx, clientY: 300 })
  })
}

/** Release a gesture opened by {@link startDrag}, at the same offset. */
function releaseDrag(dx: number) {
  const el = handle('vertical')
  act(() => {
    fireEvent.pointerUp(el, { pointerId: 1, pointerType: 'mouse', clientX: 1000 + dx, clientY: 300 })
  })
}

/** Drag the bottom dock's top-edge handle by `dy` (negative grows). */
function dragHandleV(dy: number) {
  startDragV(dy)
  releaseDragV(dy)
}

/** Press the bottom dock's handle and move it WITHOUT releasing. */
function startDragV(dy: number) {
  const el = handle('horizontal')
  act(() => {
    fireEvent.pointerDown(el, { pointerId: 1, pointerType: 'mouse', button: 0, clientX: 500, clientY: 600 })
    fireEvent.pointerMove(el, { pointerId: 1, pointerType: 'mouse', clientX: 500, clientY: 600 + dy })
  })
}

/** Release a gesture opened by {@link startDragV}, at the same offset. */
function releaseDragV(dy: number) {
  const el = handle('horizontal')
  act(() => {
    fireEvent.pointerUp(el, { pointerId: 1, pointerType: 'mouse', clientX: 500, clientY: 600 + dy })
  })
}

/** Wait out a slot switch's tween so `dimAnimating` has cleared and the panel
 *  carries no transition. Without this the gate reads as open for the whole
 *  synchronous test and a "stays instant" assertion cannot fail. */
async function settleMotion() {
  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, SIDE_PANEL_MOTION_MS + 20))
  })
}

/** Every per-slot key of `base` currently in storage. */
function slotKeys(base: string): string[] {
  const keys: string[] = []
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i)
    if (k && k.startsWith(`${base}:`)) keys.push(k)
  }
  return keys.sort()
}

describe('SidePanel size per chat', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetPanelTabs()
    setSidePanelDock('right')
    ctl = null
    // Wide and tall enough that the responsive clamps never bite at the sizes
    // under test.
    window.innerWidth = 1600
    window.innerHeight = 1000
  })

  afterEach(() => { cleanup(); setSidePanelDock('right') })

  it('renders the default when nothing is stored', () => {
    renderPanel()
    act(() => { ctl!.openView('git') })
    expect(renderedWidth()).toBe(`${DEFAULT_W}px`)
  })

  it('holds one width per chat: a drag in one chat leaves the other where it was', () => {
    localStorage.setItem(widthKey(B), '900')
    const { switchSlot } = renderPanel({ slot: A })
    act(() => { ctl!.openView('git') })
    dragHandle(-40)
    expect(renderedWidth()).toBe(`${DEFAULT_W + 40}px`)

    switchSlot(B)
    act(() => { ctl!.openView('git') })
    expect(renderedWidth()).toBe('900px')
    expect(localStorage.getItem(widthKey(B))).toBe('900')

    // And back: the switch reads the chat's size, it does not overwrite it.
    switchSlot(A)
    expect(renderedWidth()).toBe(`${DEFAULT_W + 40}px`)
    expect(localStorage.getItem(widthKey(A))).toBe(String(DEFAULT_W + 40))
  })

  it('never moves on a tab switch inside one chat, whatever the tab kinds', async () => {
    renderPanel()
    act(() => { ctl!.openView('git') })
    dragHandle(-40)
    const dragged = `${DEFAULT_W + 40}px`
    expect(renderedWidth()).toBe(dragged)
    // Right after the drag, across a view, the browser and a document reader.
    act(() => { ctl!.openView('browser') })
    expect(renderedWidth()).toBe(dragged)
    expect(transitionStyle()).toBe('')
    act(() => { ctl!.openFile('/repo/notes.md', '# notes') })
    expect(renderedWidth()).toBe(dragged)
    expect(transitionStyle()).toBe('')
    act(() => { ctl!.openDiff('/repo/other.ts', 'a', 'b') })
    expect(renderedWidth()).toBe(dragged)
    act(() => { ctl!.setActive('git') })
    expect(renderedWidth()).toBe(dragged)
    expect(transitionStyle()).toBe('')
    // Nothing animated, so nothing is left animating either.
    await settleMotion()
    expect(transitionStyle()).toBe('')
    // One key for the chat, none for any tab kind.
    expect(slotKeys(SIDE_PANEL_WIDTH_KEY)).toEqual([widthKey(A)])
  })

  it('opens a new chat at the width last dragged in any chat', () => {
    const { switchSlot } = renderPanel({ slot: A })
    act(() => { ctl!.openView('git') })
    dragHandle(-40)
    // The drag end wrote the bare key too, which is what the new chat seeds from.
    expect(localStorage.getItem(SIDE_PANEL_WIDTH_KEY)).toBe(String(DEFAULT_W + 40))

    switchSlot('chat-new')
    act(() => { ctl!.openView('browser') })
    expect(renderedWidth()).toBe(`${DEFAULT_W + 40}px`)
    // Seeded, not copied: the new chat has no key of its own until it is dragged.
    expect(localStorage.getItem(widthKey('chat-new'))).toBeNull()
  })

  it('starts every chat at the pre-existing width on upgrade (bare key only)', () => {
    localStorage.setItem(SIDE_PANEL_WIDTH_KEY, '600')
    const { switchSlot } = renderPanel({ slot: A })
    act(() => { ctl!.openView('git') })
    expect(renderedWidth()).toBe('600px')
    switchSlot(B)
    act(() => { ctl!.openView('browser') })
    expect(renderedWidth()).toBe('600px')
  })

  it('reads and writes the bare key alone for an empty slot, and mints no per-slot key', () => {
    localStorage.setItem(SIDE_PANEL_WIDTH_KEY, '600')
    // Another chat's key must not leak into the unconfirmed host.
    localStorage.setItem(widthKey(A), '900')
    renderPanel({ slot: '' })
    act(() => { ctl!.openView('git') })
    expect(renderedWidth()).toBe('600px')

    dragHandle(-40)
    expect(localStorage.getItem(SIDE_PANEL_WIDTH_KEY)).toBe('640')
    expect(slotKeys(SIDE_PANEL_WIDTH_KEY)).toEqual([widthKey(A)])
    expect(localStorage.getItem(widthKey(A))).toBe('900')
  })

  it('keeps a drag on the chat it started in when the host re-keys mid-drag', () => {
    // The Members page confirms its thread's slot while the handle can be held.
    // The gesture belongs to the chat the user grabbed: the release lands there,
    // the newcomer keeps its own width, and the size ON SCREEN stays the dragged
    // one for the whole gesture rather than jumping to the newcomer's.
    localStorage.setItem(widthKey(B), '900')
    const { switchSlot } = renderPanel({ slot: A })
    act(() => { ctl!.openView('git') })

    startDrag(-40)
    expect(renderedWidth()).toBe(`${DEFAULT_W + 40}px`)
    switchSlot(B)
    expect(renderedWidth()).toBe(`${DEFAULT_W + 40}px`)
    expect(transitionStyle()).toBe('')
    releaseDrag(-40)

    expect(localStorage.getItem(widthKey(A))).toBe(String(DEFAULT_W + 40))
    expect(localStorage.getItem(widthKey(B))).toBe('900')
    // Released, the panel is the new chat's, and eases there.
    expect(renderedWidth()).toBe('900px')
    expect(transitionStyle()).toBe(sidePanelDimTransition('width').transition)
  })

  it('saves a drag under the key the host confirms mid-drag when it started on an empty slot', () => {
    // The Members page renders the panel with slot '' until its thread POST
    // answers. Confirming the key while the handle is held is the same chat
    // getting its key, so the release belongs under that key; on the bare key
    // alone, the next drag in any other chat would overwrite it.
    const { switchSlot } = renderPanel({ slot: B, slotOwner: 'member-b' })
    act(() => { ctl!.openView('git') })
    // An earlier drag leaves B's size in the panel's in-memory map, which the
    // release must replace rather than be shadowed by.
    dragHandle(-40)
    switchSlot('')
    act(() => { ctl!.openView('git') })
    expect(renderedWidth()).toBe(`${DEFAULT_W + 40}px`)

    startDrag(-40)
    switchSlot(B)
    releaseDrag(-40)

    const dragged = String(DEFAULT_W + 80)
    expect(localStorage.getItem(widthKey(B))).toBe(dragged)
    expect(localStorage.getItem(SIDE_PANEL_WIDTH_KEY)).toBe(dragged)
    expect(renderedWidth()).toBe(`${dragged}px`)
  })

  it('keeps an empty-start drag off a different chat the host switches to mid-drag', () => {
    // A Ctrl+digit switch while the handle is held re-keys the panel from ''
    // onto an EXISTING chat. Nothing says that chat is the one being dragged,
    // so its own size must survive and the drag lands on the shared default.
    localStorage.setItem(widthKey(B), '900')
    const { switchSlot } = renderPanel({ slot: '' })
    act(() => { ctl!.openView('git') })

    startDrag(-40)
    switchSlot(B)
    releaseDrag(-40)

    expect(localStorage.getItem(widthKey(B))).toBe('900')
    expect(localStorage.getItem(SIDE_PANEL_WIDTH_KEY)).toBe(String(DEFAULT_W + 40))
    expect(renderedWidth()).toBe('900px')
  })

  it('keeps an empty-start drag off the new key when the owner changes mid-drag', () => {
    // Same as the confirm case, except the host now reports a different chat:
    // the key is not the dragged chat's, so it must not receive the size.
    localStorage.setItem(widthKey(B), '900')
    const { setProps } = renderPanel({ slot: '', slotOwner: 'member-a' })
    act(() => { ctl!.openView('git') })

    startDrag(-40)
    act(() => { setProps({ slot: B, slotOwner: 'member-b' }) })
    releaseDrag(-40)

    expect(localStorage.getItem(widthKey(B))).toBe('900')
    expect(localStorage.getItem(SIDE_PANEL_WIDTH_KEY)).toBe(String(DEFAULT_W + 40))
  })

  it('keeps a drag off a stale key the host has not confirmed, without snapping back', () => {
    // The Members page keeps its last good key as `slot` through a 409, and
    // that key now belongs to another session. Its remembered size must
    // survive a drag here; the drag goes to the shared default and stays on
    // screen for the rest of the visit.
    localStorage.setItem(widthKey(B), '900')
    renderPanel({ slot: B, persistSlot: '' })
    act(() => { ctl!.openView('git') })
    expect(renderedWidth()).toBe('900px')

    dragHandle(-40)

    expect(localStorage.getItem(widthKey(B))).toBe('900')
    expect(localStorage.getItem(SIDE_PANEL_WIDTH_KEY)).toBe('940')
    expect(renderedWidth()).toBe('940px')
  })

  it('keeps a drag off the chat key when the host refuses it mid-drag', () => {
    // Confirmed when the drag starts, then a reconnect re-POSTs and the endpoint
    // refuses (409): the key now belongs to another session, so the release
    // must not write it even though it was good at the start.
    localStorage.setItem(widthKey(B), '900')
    const { setProps } = renderPanel({ slot: B, persistSlot: B })
    act(() => { ctl!.openView('git') })

    startDrag(-40)
    act(() => { setProps({ persistSlot: '' }) })
    releaseDrag(-40)

    expect(localStorage.getItem(widthKey(B))).toBe('900')
    expect(localStorage.getItem(SIDE_PANEL_WIDTH_KEY)).toBe('940')
    expect(renderedWidth()).toBe('940px')
  })

  it('saves under the chat key when the host confirms it by the release', () => {
    const { setProps } = renderPanel({ slot: B, persistSlot: '' })
    act(() => { ctl!.openView('git') })

    startDrag(-40)
    act(() => { setProps({ persistSlot: B }) })
    releaseDrag(-40)

    expect(localStorage.getItem(widthKey(B))).toBe(String(DEFAULT_W + 40))
  })

  describe('bottom dock height', () => {
    beforeEach(() => { setSidePanelDock('bottom') })

    it('holds one height per chat and never moves on an in-chat tab switch', () => {
      localStorage.setItem(heightKey(B), '500')
      const { switchSlot } = renderPanel({ slot: A, canDockBottom: true })
      act(() => { ctl!.openView('git') })
      expect(renderedHeight()).toBe(`${DEFAULT_H}px`)
      dragHandleV(-40)
      const dragged = `${DEFAULT_H + 40}px`
      expect(renderedHeight()).toBe(dragged)
      expect(localStorage.getItem(heightKey(A))).toBe(String(DEFAULT_H + 40))
      expect(localStorage.getItem(SIDE_PANEL_HEIGHT_KEY)).toBe(String(DEFAULT_H + 40))

      act(() => { ctl!.openView('browser') })
      expect(renderedHeight()).toBe(dragged)
      act(() => { ctl!.openFile('/repo/notes.md', '# notes') })
      expect(renderedHeight()).toBe(dragged)
      expect(transitionStyle()).toBe('')

      switchSlot(B)
      act(() => { ctl!.openView('git') })
      expect(renderedHeight()).toBe('500px')
      expect(transitionStyle()).toBe(sidePanelDimTransition('height').transition)
      switchSlot(A)
      expect(renderedHeight()).toBe(dragged)
      // Width keys are untouched by a height drag.
      expect(slotKeys(SIDE_PANEL_WIDTH_KEY)).toEqual([])
    })

    it('saves a drag under the key the host confirms mid-drag when it started on an empty slot', () => {
      const { switchSlot } = renderPanel({ slot: B, slotOwner: 'member-b', canDockBottom: true })
      act(() => { ctl!.openView('git') })
      dragHandleV(-40)
      switchSlot('')
      act(() => { ctl!.openView('git') })
      expect(renderedHeight()).toBe(`${DEFAULT_H + 40}px`)

      startDragV(-40)
      switchSlot(B)
      releaseDragV(-40)

      const dragged = String(DEFAULT_H + 80)
      expect(localStorage.getItem(heightKey(B))).toBe(dragged)
      expect(localStorage.getItem(SIDE_PANEL_HEIGHT_KEY)).toBe(dragged)
      expect(renderedHeight()).toBe(`${dragged}px`)
    })

    it('keeps an empty-start drag off a different chat the host switches to mid-drag', () => {
      localStorage.setItem(heightKey(B), '500')
      const { switchSlot } = renderPanel({ slot: '', canDockBottom: true })
      act(() => { ctl!.openView('git') })

      startDragV(-40)
      switchSlot(B)
      releaseDragV(-40)

      expect(localStorage.getItem(heightKey(B))).toBe('500')
      expect(localStorage.getItem(SIDE_PANEL_HEIGHT_KEY)).toBe(String(DEFAULT_H + 40))
      expect(renderedHeight()).toBe('500px')
    })

    it('keeps a drag off a stale key the host has not confirmed, without snapping back', () => {
      localStorage.setItem(heightKey(B), '500')
      renderPanel({ slot: B, persistSlot: '', canDockBottom: true })
      act(() => { ctl!.openView('git') })
      expect(renderedHeight()).toBe('500px')

      dragHandleV(-40)

      expect(localStorage.getItem(heightKey(B))).toBe('500')
      expect(localStorage.getItem(SIDE_PANEL_HEIGHT_KEY)).toBe('540')
      expect(renderedHeight()).toBe('540px')
    })

    it('keeps a drag off the chat key when the host refuses it mid-drag', () => {
      localStorage.setItem(heightKey(B), '500')
      const { setProps } = renderPanel({ slot: B, persistSlot: B, canDockBottom: true })
      act(() => { ctl!.openView('git') })

      startDragV(-40)
      act(() => { setProps({ persistSlot: '' }) })
      releaseDragV(-40)

      expect(localStorage.getItem(heightKey(B))).toBe('500')
      expect(localStorage.getItem(SIDE_PANEL_HEIGHT_KEY)).toBe('540')
      expect(renderedHeight()).toBe('540px')
    })
  })

  describe('motion', () => {
    it('eases a chat switch on the panel\'s own open/close curve, and nothing else', async () => {
      localStorage.setItem(widthKey(A), '380')
      localStorage.setItem(widthKey(B), '900')
      const { switchSlot } = renderPanel({ slot: A })
      act(() => { ctl!.openView('git') })
      // Mounting is not a switch.
      expect(renderedWidth()).toBe('380px')
      expect(transitionStyle()).toBe('')

      switchSlot(B)
      expect(renderedWidth()).toBe('900px')
      // Same tween the dock wrapper opens with, so a switch reads as the same
      // gesture as opening the panel rather than as a jump.
      expect(transitionStyle()).toBe(sidePanelDimTransition('width').transition)
      await settleMotion()
      expect(transitionStyle()).toBe('')
    })

    it('never animates while the handle is being dragged', async () => {
      localStorage.setItem(widthKey(A), '380')
      localStorage.setItem(widthKey(B), '900')
      const { switchSlot } = renderPanel({ slot: A })
      act(() => { ctl!.openView('git') })
      switchSlot(B)
      expect(transitionStyle()).toBe(sidePanelDimTransition('width').transition)
      // The switch's tween is still running, but a transition during a drag
      // makes the edge lag the pointer, so holding the handle suppresses it at
      // once rather than letting the tween finish under the gesture.
      startDrag(-40)
      expect(transitionStyle()).toBe('')
      expect(renderedWidth()).toBe('940px')
      releaseDrag(-40)
      await settleMotion()
      expect(transitionStyle()).toBe('')
      expect(renderedWidth()).toBe('940px')
    })

    // jsdom lays nothing out, so the painted size a real browser reports mid-ease
    // is stubbed on the panel root. `paint` is what the edge shows right now.
    function stubPainted(paint: { width?: number; height?: number }) {
      const root = screen.getByTestId('side-panel-root')
      vi.spyOn(root, 'getBoundingClientRect').mockReturnValue({
        width: paint.width ?? 0, height: paint.height ?? 0, x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, toJSON: () => ({}),
      } as DOMRect)
    }

    it('starts a drag grabbed mid-ease from the width on screen, not the target', () => {
      localStorage.setItem(widthKey(A), '380')
      localStorage.setItem(widthKey(B), '900')
      const { switchSlot } = renderPanel({ slot: A })
      act(() => { ctl!.openView('git') })
      switchSlot(B)
      // The edge is on its way from 380 to 900 and has reached 700.
      stubPainted({ width: 700 })

      startDrag(-40)
      // No jump to 900: the gesture moves the edge the user grabbed.
      expect(renderedWidth()).toBe('740px')
      releaseDrag(-40)
      expect(localStorage.getItem(widthKey(B))).toBe('740')
      expect(localStorage.getItem(SIDE_PANEL_WIDTH_KEY)).toBe('740')
    })

    it('saves the width on screen for a press mid-ease that never moves', () => {
      localStorage.setItem(widthKey(A), '380')
      localStorage.setItem(widthKey(B), '900')
      const { switchSlot } = renderPanel({ slot: A })
      act(() => { ctl!.openView('git') })
      switchSlot(B)
      stubPainted({ width: 700 })

      dragHandle(0)
      // The panel stays where it was grabbed, and that is what is saved: what
      // the user sees and what is stored are the same number.
      expect(renderedWidth()).toBe('700px')
      expect(localStorage.getItem(widthKey(B))).toBe('700')
    })

    it('keeps the logical width once the ease is over, as before per-chat width', async () => {
      // A settled panel is where its target is, so a painted reading there is
      // not used: a clamped or maximized panel must keep the remembered size.
      localStorage.setItem(widthKey(A), '380')
      localStorage.setItem(widthKey(B), '900')
      const { switchSlot } = renderPanel({ slot: A })
      act(() => { ctl!.openView('git') })
      switchSlot(B)
      await settleMotion()
      stubPainted({ width: 700 })

      dragHandle(-40)
      expect(renderedWidth()).toBe('940px')
      expect(localStorage.getItem(widthKey(B))).toBe('940')
    })

    it('starts a bottom-dock drag grabbed mid-ease from the height on screen', () => {
      setSidePanelDock('bottom')
      localStorage.setItem(heightKey(A), '240')
      localStorage.setItem(heightKey(B), '600')
      const { switchSlot } = renderPanel({ slot: A, canDockBottom: true })
      act(() => { ctl!.openView('git') })
      switchSlot(B)
      stubPainted({ height: 500 })

      startDragV(-40)
      expect(renderedHeight()).toBe('540px')
      releaseDragV(-40)
      expect(localStorage.getItem(heightKey(B))).toBe('540')
    })

    it('still eases a chat switch after a window resize that also moved the reserve', async () => {
      // The last resize event can change `reserveW` (a sibling column clamps to
      // the new window), which re-runs the resize effect mid-settle. The flag
      // that suppresses easing during a window drag must not stay latched.
      localStorage.setItem(widthKey(A), '380')
      localStorage.setItem(widthKey(B), '900')
      const { setProps, switchSlot } = renderPanel({ slot: A, extraReserveW: 0 })
      act(() => { ctl!.openView('git') })

      act(() => {
        window.dispatchEvent(new Event('resize'))
        setProps({ extraReserveW: 100 })
      })
      await settleMotion()

      switchSlot(B)
      expect(renderedWidth()).toBe('900px')
      expect(transitionStyle()).toBe(sidePanelDimTransition('width').transition)
    })

    it('does not animate a drag in a chat that was never switched, before or after the release', () => {
      renderPanel({ slot: A })
      act(() => { ctl!.openView('git') })
      startDrag(-40)
      expect(transitionStyle()).toBe('')
      // Released with the width already settled, there is nothing to animate.
      releaseDrag(-40)
      expect(transitionStyle()).toBe('')
      expect(renderedWidth()).toBe(`${DEFAULT_W + 40}px`)
    })

    // The transition is gated to the chat switch the tests above cover. Maximize
    // and the browser tab's fill-width mode reach the same `effectiveWidth`, and
    // before per-chat sizes they moved the edge INSTANTLY, so easing them would
    // be a behavior change this feature does not need. These two guard that:
    // without the gate the edge animates and both assertions fail.
    it('leaves maximize instant, as it was before per-chat width', () => {
      localStorage.setItem(widthKey(A), '380')
      const { setProps } = renderPanel({ slot: A })
      act(() => { ctl!.openView('git') })
      expect(transitionStyle()).toBe('')

      act(() => { setProps({ expanded: true }) })
      // The width really moved, so the assertion below is about motion rather
      // than about a prop that never took effect.
      expect(renderedWidth()).not.toBe('380px')
      expect(transitionStyle()).toBe('')
    })

    it('leaves the browser tab\'s fill-width mode instant', () => {
      const { setProps } = renderPanel({ slot: A })
      act(() => { ctl!.openView('browser') })
      expect(transitionStyle()).toBe('')

      act(() => { setProps({ fillWidth: 1200 }) })
      expect(renderedWidth()).toBe('1200px')
      expect(transitionStyle()).toBe('')
    })

    it('keeps a restore instant when it lands in the same render as a chat switch', () => {
      // Leaving a chat maximized on its Browser tab clears `expanded` as the
      // slot changes, so the edge moves for both reasons at once. Restore was
      // instant before per-chat sizes, and riding the switch's tween would
      // change that.
      localStorage.setItem(widthKey(B), '900')
      const { setProps } = renderPanel({ slot: A, expanded: true })
      act(() => { ctl!.openView('git') })

      act(() => { setProps({ slot: B, expanded: false }) })
      expect(renderedWidth()).toBe('900px')
      expect(transitionStyle()).toBe('')
    })

    it('ends a running ease when maximize lands inside it', async () => {
      localStorage.setItem(widthKey(A), '380')
      localStorage.setItem(widthKey(B), '900')
      const { setProps, switchSlot } = renderPanel({ slot: A })
      act(() => { ctl!.openView('git') })
      switchSlot(B)
      expect(transitionStyle()).toBe(sidePanelDimTransition('width').transition)

      act(() => { setProps({ expanded: true }) })
      expect(transitionStyle()).toBe('')
      // And the restore after it is instant too: the mode change ended the ease.
      act(() => { setProps({ expanded: false }) })
      expect(renderedWidth()).toBe('900px')
      expect(transitionStyle()).toBe('')
      await settleMotion()
      expect(transitionStyle()).toBe('')
    })
  })

  // A slot key is user-supplied and the gateway preserves `__proto__` and
  // `constructor` verbatim, so the in-memory map must not read inherited
  // properties for them: those are non-nullish, skip the storage fallback, clamp
  // to NaN, and a drag would then save NaN to the bare key every chat seeds from.
  // The strip gets a plain key so only the panel's own size read is under test.
  describe('prototype-named chats', () => {
    for (const hostile of ['__proto__', 'constructor', 'toString']) {
      it(`renders and saves a real width for a chat named ${hostile}`, () => {
        renderPanel({ slot: hostile, tabsSlot: A })
        act(() => { ctl!.openView('git') })
        expect(renderedWidth()).toBe(`${DEFAULT_W}px`)
        dragHandle(-40)
        expect(renderedWidth()).toBe(`${DEFAULT_W + 40}px`)
        expect(localStorage.getItem(SIDE_PANEL_WIDTH_KEY)).toBe(String(DEFAULT_W + 40))
        expect(localStorage.getItem(widthKey(hostile))).toBe(String(DEFAULT_W + 40))
      })

      it(`renders and saves a real height for a chat named ${hostile}`, () => {
        setSidePanelDock('bottom')
        renderPanel({ slot: hostile, tabsSlot: A, canDockBottom: true })
        act(() => { ctl!.openView('git') })
        expect(renderedHeight()).toBe(`${DEFAULT_H}px`)
        dragHandleV(-40)
        expect(renderedHeight()).toBe(`${DEFAULT_H + 40}px`)
        expect(localStorage.getItem(SIDE_PANEL_HEIGHT_KEY)).toBe(String(DEFAULT_H + 40))
        expect(localStorage.getItem(heightKey(hostile))).toBe(String(DEFAULT_H + 40))
      })
    }
  })
})
