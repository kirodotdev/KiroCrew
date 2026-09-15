/**
 * Collapsing the message input for reading space.
 *
 * The composer is the highest-traffic surface in the app, so what this pins is a
 * regression on the common path rather than a missing feature. In the order the
 * failures would hurt:
 *
 *   1. A HALF-TYPED MESSAGE SURVIVES. Losing one is worse than having no collapse,
 *      so the test drives the real gesture with real text in the box and checks
 *      both halves of "survives": the text comes back, AND nothing asked the host
 *      to drop it. The second half is the one that matters -- the first would pass
 *      just as well if the collapse cleared the draft and something else happened
 *      to restore it.
 *   2. COLLAPSING UNMOUNTS, it does not hide. ChatInput's comment on the
 *      AnimatePresence gate says why: an unmounted composer cannot be a
 *      persistently focusable invisible element. A collapse that merely hid the box
 *      would leave a tab stop in empty space, and no visual test would notice.
 *   3. A TYPING GESTURE STILL REACHES A COLLAPSED COMPOSER. `/` is an explicit "I
 *      want to type", and the state is indefinite and survives a reload, so a
 *      silent no-op there is a standing dead end rather than a momentary one.
 *   4. FOCUS FOLLOWS THE GESTURE. Both controls unmount themselves on click, so
 *      without this focus lands on `body` and a keyboard user re-Tabs from the top
 *      of the page every single time.
 *   5. THERE IS A WAY BACK, and it is labelled.
 *
 * `Host` owns `value` on purpose: that is the real arrangement (ChatPage holds the
 * text in `input`, seeded from its per-slot `drafts`), and the property under test
 * is that the composer keeps no competing copy of it.
 */
import { describe, expect, it, beforeEach, vi } from 'vitest'
import { useState } from 'react'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'

// `directFilePicker = isMobile || isTouchDevice()` decides WHICH host the collapse
// row hangs off: the "+" drop-up on a pointer device, the overflow drop-up on
// touch. Both are exercised here, so the pointer class is a per-test flag rather
// than a fixed mock -- the touch layout having no host at all is the defect review
// caught, and a file that could only render one class could not have caught it.
const flags = vi.hoisted(() => ({ touch: false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => flags.touch }))
vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => flags.touch }))

import ChatInput from '../components/ChatInput'
import { requestComposerExpand, queryComposerOrExpand } from '../pages/chat/composerFocus'
import { renderWithProviders } from './helpers'

const COLLAPSED_KEY = 'mc-composer-collapsed'
// `collapsible` is opt-in, and ChatPage's single main composer is the only caller.
// Every case that exercises the feature must therefore pass it; the cases that pin
// what a NON-opted surface does (a split pane, the side chat) deliberately omit it.
const base = { value: '', onChange: vi.fn(), onSend: vi.fn(), onUploadFiles: vi.fn(), collapsible: true }

const openPlusMenu = () => fireEvent.click(screen.getByTitle('Add files & options'))
// The overflow is the repo's Radix DropdownMenu, which opens on KEYBOARD activation
// in jsdom -- its mouse open is PointerEvent-driven and jsdom does not deliver that.
// Same path `ChatHeaderMenu.slack.test.tsx` documents for the same wrapper.
const openOverflow = () => fireEvent.keyDown(
  screen.getByTestId('composer-more-trigger'), { key: 'Enter' },
)
const collapse = () => { openPlusMenu(); fireEvent.click(screen.getByTestId('composer-collapse-row')) }
const composer = () => screen.queryByLabelText('Message input')

/** Owns `value` the way ChatPage does, so an unmount of ChatInput cannot be what
 *  preserves the draft -- only the absence of a competing copy inside it can. */
function Host({ onChangeSpy }: { onChangeSpy?: (v: string) => void }) {
  const [value, setValue] = useState('')
  return (
    <ChatInput
      {...base}
      value={value}
      onChange={(v) => { onChangeSpy?.(v); setValue(v) }}
    />
  )
}

describe('composer collapse', () => {
  beforeEach(() => {
    localStorage.removeItem(COLLAPSED_KEY)
    flags.touch = false
  })

  it('defaults to expanded, so installing this changes nothing until asked', () => {
    renderWithProviders(<ChatInput {...base} />)
    expect(composer()).toBeInTheDocument()
    expect(screen.queryByTestId('composer-collapsed-bar')).toBeNull()
  })

  /**
   * The entry point is a stacked menu ROW, not a peer button in an action row.
   *
   * Two independent reasons, and a later "simplification" back into the row would
   * reintroduce both: `website/AUTOSDE.yaml`'s `max-two-buttons-per-row` is
   * `blocking: true` and its grandfather clause forbids growing an already-3+
   * group, and an icon-only chevron in that row sat immediately after
   * ApprovalModePicker -- which renders "Normal" with no caret of its own -- so it
   * read as that picker's dropdown arrow instead of a collapse control.
   */
  it('is reached from the plus menu, not from the capped action row', () => {
    const { container } = renderWithProviders(<ChatInput {...base} />)
    // Not present until the menu is opened...
    expect(screen.queryByTestId('composer-collapse-row')).toBeNull()
    openPlusMenu()
    const row = screen.getByTestId('composer-collapse-row')
    expect(row).toHaveTextContent('Collapse the message input')
    // ...and it is not a sibling in the bottom action row.
    const actionRow = container.querySelector('.input-area .flex.items-center.justify-between')
    expect(actionRow?.contains(row)).toBe(false)
  })

  it('collapsing puts a labelled way back where the composer was', async () => {
    renderWithProviders(<ChatInput {...base} />)
    collapse()
    const bar = await screen.findByTestId('composer-collapsed-bar')
    expect(bar).toHaveAttribute('aria-expanded', 'false')
    expect(bar).toHaveAccessibleName('Show the message input')
  })

  /**
   * The unmount half of the posture, pinned where it is observable.
   *
   * The gate ANIMATES its exit and that animation does not complete under this test
   * environment's DOM, so a collapse driven by a click leaves the old textarea
   * mid-exit and "is it gone yet" cannot be answered here. A mount that is ALREADY
   * collapsed never renders it, which is the same gate reporting the same decision
   * with no animation in the way. A later change to a hidden-but-mounted composer
   * would keep every visual behaviour and silently reintroduce a focusable
   * invisible element.
   */
  it('never mounts the composer while collapsed, so it is not an invisible tab stop', () => {
    localStorage.setItem(COLLAPSED_KEY, '1')
    renderWithProviders(<ChatInput {...base} />)
    expect(composer()).toBeNull()
    expect(screen.getByTestId('composer-collapsed-bar')).toBeInTheDocument()
  })

  it('keeps a half-typed message across a collapse, and never asks the host to drop it', async () => {
    const onChangeSpy = vi.fn()
    renderWithProviders(<Host onChangeSpy={onChangeSpy} />)
    const typed = 'half-typed thing I have not sent yet'
    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: typed } })
    expect(screen.getByLabelText('Message input')).toHaveValue(typed)

    onChangeSpy.mockClear()
    collapse()
    // The bar appearing is the observable proof the collapse took effect; the old
    // textarea's removal is animated and is pinned by the fresh-mount test above.
    fireEvent.click(await screen.findByTestId('composer-collapsed-bar'))

    // Observable 1: the text is back in the box.
    expect(screen.getByLabelText('Message input')).toHaveValue(typed)
    // Observable 2: nothing on the collapse path asked the host to change the
    // draft at all. Without this, a collapse that cleared the text and an expand
    // that restored it from somewhere else would look identical to not touching it.
    expect(onChangeSpy).not.toHaveBeenCalled()
  })

  it('reports the waiting draft on the bar without making it the button name', () => {
    renderWithProviders(<Host />)
    fireEvent.change(screen.getByLabelText('Message input'), {
      target: { value: 'first line of the draft\nsecond line' },
    })
    collapse()

    const bar = screen.getByTestId('composer-collapsed-bar')
    // Visible to a reader: which message is waiting, not just that one is.
    expect(bar).toHaveTextContent('first line of the draft')
    expect(bar).not.toHaveTextContent('second line')
    // Not part of the accessible name: a screen reader gets the action, not the
    // user's own prose read back as a label. Two things hold this and either alone
    // suffices -- the explicit name (aria-label, or title as its fallback) beats
    // contents, and aria-hidden empties the contents -- so no single-attribute
    // mutation reddens this line and only removing BOTH does.
    expect(bar).toHaveAccessibleName('Show the message input')
  })

  /**
   * `/` is documented as "focus the chat input", and it resolves the textarea --
   * which a collapsed composer does not have. Left alone it would silently do
   * nothing, indefinitely, because this state survives a reload.
   */
  it('brings the box back when the user presses the documented "/" shortcut', () => {
    localStorage.setItem(COLLAPSED_KEY, '1')
    renderWithProviders(<ChatInput {...base} />)
    expect(composer()).toBeNull()

    fireEvent.keyDown(document, { key: '/' })

    expect(composer()).toBeInTheDocument()
    expect(localStorage.getItem(COLLAPSED_KEY)).toBe('0')
  })

  /**
   * Focus follows the gesture, because each control removes itself.
   *
   * jsdom/happy-dom apply focus synchronously once the element exists, but the
   * component defers to the next frame (the target is not committed yet), so both
   * directions are awaited on the frame rather than asserted on the click.
   */
  it('moves focus to whichever control replaced the one that was clicked', async () => {
    renderWithProviders(<ChatInput {...base} />)
    collapse()
    const bar = await screen.findByTestId('composer-collapsed-bar')
    await new Promise(requestAnimationFrame)
    // Not `body`: a keyboard user must not be dropped to the top of the page.
    expect(document.activeElement).toBe(bar)

    fireEvent.click(bar)
    const ta = await screen.findByLabelText('Message input')
    await new Promise(requestAnimationFrame)
    expect(document.activeElement).toBe(ta)
  })

  /**
   * The expand request reports whether anything actually expanded, and that return
   * value is load-bearing rather than informational.
   *
   * `focusComposer`/`revealComposer` schedule a one-frame retry after asking, and a
   * retry outlives the intent that asked for it -- the existing suite caught the
   * first version of this doing exactly that, with a retry queued by one caller
   * landing later and focusing a composer nobody had asked for. Reporting false
   * when nothing was collapsed is what keeps a lookup that missed for some other
   * reason from scheduling anything at all.
   */
  it('reports nothing to expand when no composer is collapsed', () => {
    renderWithProviders(<ChatInput {...base} />)
    expect(composer()).toBeInTheDocument()
    expect(requestComposerExpand()).toBe(false)
  })

  it('reports the expand, and brings the box back, when one is collapsed', () => {
    localStorage.setItem(COLLAPSED_KEY, '1')
    renderWithProviders(<ChatInput {...base} />)
    expect(composer()).toBeNull()
    // Wrapped in act for the same reason production defers its retry by a frame:
    // the listener answers synchronously, but the textarea does not exist until
    // React has committed the state change.
    let answered = false
    act(() => { answered = requestComposerExpand() })
    expect(answered).toBe(true)
    expect(composer()).toBeInTheDocument()
  })

  it('names itself on the bar when no draft is waiting', () => {
    localStorage.setItem(COLLAPSED_KEY, '1')
    renderWithProviders(<ChatInput {...base} />)
    const bar = screen.getByTestId('composer-collapsed-bar')
    // A bare full-width strip with only a glyph does not say what it does; with
    // nothing waiting there is no draft to show instead, so the control's own name
    // is what stands there.
    expect(bar).toHaveTextContent('Show the message input')
    expect(bar).toHaveAccessibleName('Show the message input')
  })

  it('is off unless a host asks for it, prop absent entirely', () => {
    // The DEFAULT is the invariant, and every other case here passes the flag
    // explicitly -- so without this one, flipping the default to true would make
    // the feature appear on every split pane and side chat with no test failing.
    // A mutation flipping it survived until this existed.
    const { collapsible: _omitted, ...withoutFlag } = base
    localStorage.setItem(COLLAPSED_KEY, '1')
    flags.touch = true
    renderWithProviders(<ChatInput {...withoutFlag} />)
    expect(composer()).toBeInTheDocument()
    expect(screen.queryByTestId('composer-collapsed-bar')).not.toBeInTheDocument()
    expect(screen.queryByTestId('composer-more-trigger')).not.toBeInTheDocument()
    expect(screen.getByTitle('Sketch')).toBeInTheDocument()
  })

  it('is reachable on touch, where the plus menu does not exist', () => {
    // The blocking finding. Moving the control off the capped action row into the
    // "+" menu satisfied max-two-buttons-per-row and simultaneously deleted the
    // action at 390px, because `directFilePicker` replaces that menu with a bare
    // file-input label. narrow-viewport-required names this exactly: "if a control
    // is the only host of an action, removing it on a phone removes the action."
    flags.touch = true
    renderWithProviders(<ChatInput {...base} />)
    expect(screen.queryByTitle('Add files & options')).not.toBeInTheDocument()
    openOverflow()
    fireEvent.click(screen.getByTestId('composer-collapse-row'))
    // The BAR is the assertion, not the textarea's absence: the gate animates its
    // exit and that animation does not complete under the test DOM, so a
    // click-driven unmount is not observable here (the fresh-mount case above
    // pins that half). The bar standing there proves the gesture reached
    // `collapseComposer`, which is the reachability property under test.
    expect(screen.getByTestId('composer-collapsed-bar')).toBeInTheDocument()
  })

  it('keeps the touch row at two controls by hosting Sketch in the same overflow', async () => {
    // The other half of the same rule: three peer actions (attach + sketch +
    // collapse) do not fit a row capped at two, and the cap's own remedy is an
    // overflow whose trigger "counts as ONE regardless of how many items it
    // holds". So Sketch moves one tap deeper rather than losing its place -- if it
    // vanished, this fix would have traded one unreachable action for another.
    flags.touch = true
    renderWithProviders(<ChatInput {...base} />)
    expect(screen.queryByTitle('Sketch')).not.toBeInTheDocument()
    openOverflow()
    expect(screen.getByTestId('composer-collapse-row')).toBeInTheDocument()
    // Clicked, not merely present: a row that renders its label and does nothing
    // would read as "Sketch is still reachable" to any presence-only assertion,
    // and a mutation that broke exactly that survived until this clicked it.
    // The open is deferred one macrotask past the menu's close commit (see the
    // component: two focus traps otherwise fight), so the wait is the production
    // behaviour rather than a test workaround.
    fireEvent.click(screen.getByTitle('Sketch'))
    await waitFor(() => expect(screen.getByTestId('sketch-dialog')).toBeInTheDocument())
  })

  it('keeps the upload guard on Sketch after it moved into the overflow', () => {
    // The blocking review finding. The pencil this replaced carried
    // `disabled={uploading}`; moving Sketch into the overflow dropped it, and
    // Sketch attaches through the same handler as an upload while the in-flight
    // flag is one shared boolean -- so the guard has to move with the control.
    flags.touch = true
    renderWithProviders(<ChatInput {...base} uploading />)
    openOverflow()
    const item = screen.getByTitle('Sketch')
    expect(item).toHaveAttribute('data-disabled')
    // ... and the collapse stays reachable, because disabling the whole trigger
    // mid-upload would take the collapse away with it.
    expect(screen.getByTestId('composer-collapse-row')).toBeInTheDocument()
    expect(screen.getByTestId('composer-more-trigger')).not.toBeDisabled()
  })

  it('leaves a surface that did not opt in exactly as it was', () => {
    // A split pane and the side chat mount the same component without the flag.
    // They keep their dedicated Sketch pencil and gain no overflow, so this
    // feature costs them no change at all.
    flags.touch = true
    renderWithProviders(<ChatInput {...base} collapsible={false} />)
    expect(screen.getByTitle('Sketch')).toBeInTheDocument()
    expect(screen.queryByTestId('composer-more-trigger')).not.toBeInTheDocument()
    expect(screen.queryByTestId('composer-collapse-row')).not.toBeInTheDocument()
  })

  it('never mounts a non-opted composer collapsed, whatever the shared key says', () => {
    // The reachability trap this opt-in exists to prevent: one key is shared, so
    // without the gate a pane mounted after the main composer was collapsed would
    // come up collapsed too -- in a surface that has no control to bring it back.
    localStorage.setItem(COLLAPSED_KEY, '1')
    renderWithProviders(<ChatInput {...base} collapsible={false} />)
    expect(composer()).toBeInTheDocument()
    expect(screen.queryByTestId('composer-collapsed-bar')).not.toBeInTheDocument()
  })

  it('does not let a non-opted composer answer the expand broadcast', () => {
    // The expand request is a window-level event every listener sees, and
    // answering it is what licenses the caller's one retry. A composer that can
    // never be collapsed must therefore stay silent, or it would report an expand
    // that did not happen and send focus chasing a composer nobody revealed.
    localStorage.setItem(COLLAPSED_KEY, '1')
    renderWithProviders(<ChatInput {...base} collapsible={false} />)
    expect(requestComposerExpand()).toBe(false)
  })

  it('hands the textarea to a caller that resolved it through the expand seam', async () => {
    // What Alt+Enter and the new-chat shortcut now do. Both deliberately skip
    // `focusComposer` (its touch guard would wrongly no-op a real keypress) and
    // both used a bare lookup, so both silently dead-ended on a collapsed
    // composer -- the exact failure this seam fixes for "/".
    localStorage.setItem(COLLAPSED_KEY, '1')
    renderWithProviders(<ChatInput {...base} />)
    expect(composer()).not.toBeInTheDocument()
    const seen: string[] = []
    // A SYNCHRONOUS act, deliberately: it flushes the expand on exit, which is the
    // order production runs in -- the listener's update is batched into a microtask
    // and microtasks drain before any rAF callback, so React has committed by the
    // time the resolver's retry frame fires. An `await act(async ...)` wrapping
    // both steps inverts that (the frame runs inside act, before its flush) and
    // fails against an ordering the browser never produces.
    act(() => { queryComposerOrExpand(ta => seen.push(ta.tagName)) })
    expect(composer()).toBeInTheDocument()
    await new Promise(requestAnimationFrame)
    expect(seen).toEqual(['TEXTAREA'])
  })

  it('never hands a main-chat intent to the side chat while collapsed', async () => {
    // The side chat is a SEPARATE conversation mounting this same component, so its
    // textarea carries the same `data-composer-input` hook. Collapsing unmounts the
    // main composer, which made the side chat's textarea the only match for the
    // document-wide fallback -- so a main-chat focus intent resolved to it, and a
    // quote-to-compose or widget PRE-FILL would have been typed into a different
    // conversation. ChatPage's own `handleAsk` comment records that the two are
    // deliberately different destinations.
    localStorage.setItem(COLLAPSED_KEY, '1')
    renderWithProviders(<ChatInput {...base} />)
    // Stand in for the side chat panel: its real wrapper carries this marker, and
    // ChatPage already uses that selector to find this very composer.
    const side = document.createElement('div')
    side.setAttribute('data-side-chat-input', '')
    const sideTa = document.createElement('textarea')
    sideTa.setAttribute('data-composer-input', '')
    sideTa.id = 'side-chat-composer'
    side.appendChild(sideTa)
    document.body.appendChild(side)
    try {
      const seen: string[] = []
      act(() => { queryComposerOrExpand(ta => seen.push(ta.id || 'main')) })
      // The collapsed MAIN composer came back...
      expect(composer()).toBeInTheDocument()
      await new Promise(requestAnimationFrame)
      // ...and it, not the side chat's, is what the caller was handed.
      expect(seen).toEqual(['main'])
      expect(seen).not.toContain('side-chat-composer')
    } finally {
      side.remove()
    }
  })

  it('persists the preference, and honours it on the next mount', () => {
    const first = renderWithProviders(<ChatInput {...base} />)
    collapse()
    expect(localStorage.getItem(COLLAPSED_KEY)).toBe('1')
    first.unmount()

    renderWithProviders(<ChatInput {...base} />)
    expect(composer()).toBeNull()
    expect(screen.getByTestId('composer-collapsed-bar')).toBeInTheDocument()

    // And back: the stored value follows the control in both directions, so a
    // reload cannot strand someone in a state they cannot leave.
    fireEvent.click(screen.getByTestId('composer-collapsed-bar'))
    expect(localStorage.getItem(COLLAPSED_KEY)).toBe('0')
    expect(composer()).toBeInTheDocument()
  })
})
