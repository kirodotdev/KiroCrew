import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import { motion } from 'framer-motion'

import { useDialogFocusTrap } from '../hooks/useDialogFocusTrap'
import { useDocumentImeLatch } from '../hooks/useImeGuard'
import { i18nT } from '../i18n/t'
import { countLines } from '../utils/pasteTokens'

export interface PastePreviewEditorProps {
  open: boolean
  /** Viewport rect of the chip the editor is anchored to. */
  anchorRect: DOMRect | null
  /** Current paste content, shown editable in the textarea. */
  content: string
  /** Line count for the header label. */
  lines: number
  /**
   * Identity of the preview session — the previewed pill's node key. A change
   * while `open` stays true (another pill clicked) starts a fresh session:
   * default width, auto-fit height.
   */
  sessionKey?: string
  /**
   * Every change of the textarea, as it happens. The host writes it through to
   * the pill (and so to the draft) at once, so the panel never holds text that
   * exists nowhere else; `onClose` is the host's cue to put `content` back.
   */
  onChange?: (content: string) => void
  /** Commit the (possibly edited) content and close. */
  onSave: (content: string) => void
  /**
   * Dismiss, discarding the edit: the host restores `content`. Reached from
   * Cancel, from Escape (twice when there are unsaved edits — see below), or
   * from a pointerdown outside the panel when nothing was edited.
   */
  onClose: () => void
  /** Test-id prefix; defaults to 'paste-preview-editor'. */
  testIdPrefix?: string
}

/** Gap (px) between the chip and the popover, on whichever side it opens. */
const GAP = 6
/** Default panel width; the user can drag it wider (up to the viewport edge). */
const PANEL_WIDTH = 420
const MIN_PANEL_WIDTH = 280
/** Textarea metrics (font 11px × leading 1.5, p-2 + 1px border each side). */
const LINE_PX = 16.5
const TEXTAREA_CHROME_PX = 18
/** Header row + panel padding/border + gaps (everything that is not textarea),
 *  used only until the panel has been measured. */
const PANEL_CHROME_FALLBACK_PX = 52
const MIN_TEXTAREA_PX = 120
/** Px per arrow press on the resize grip, and per Shift+arrow press — the same
 *  steps as `ResizeHandle`, so every keyboard splitter in the app moves alike. */
const STEP = 16
const COARSE_STEP = 64

/**
 * The resize clamps. Both resize paths — the pointer drag and the arrow keys on
 * the grip — go through these two functions and nowhere else, so the keyboard
 * can never reach a size the pointer cannot (or vice versa): the floors are the
 * constants above, the caps are what the caller measured (room to the viewport
 * edge for width, room on the chosen side minus chrome for height).
 */
const clampPanelWidth = (w: number, maxW: number) => Math.max(MIN_PANEL_WIDTH, Math.min(maxW, w))
const clampTextareaHeight = (h: number, maxH: number) => Math.max(MIN_TEXTAREA_PX, Math.min(maxH, h))

/**
 * The click-to-edit paste preview: a portalled popover that opens ABOVE the
 * chip (flipping below only when there is no room above), holds the paste
 * content in an editable monospace textarea, and writes it back on Save.
 *
 * Presentational only — no Lexical. Styled like `PastePreviewTooltip`
 * (`rounded-md border border-border bg-bg-elevated shadow-lg`) and animated with
 * framer-motion to match it.
 *
 * Dismissal never destroys work in one gesture:
 * - Edits are reported live (`onChange`) and the host writes them through to
 *   the pill as they happen, so the text in the textarea is never the only copy
 *   of itself: a reload, a Back, or any teardown that unmounts the panel
 *   mid-edit leaves the draft holding the edit. "Unsaved" below means
 *   "not yet committed — Cancel would still put the original back".
 * - A pointerdown outside the panel COMMITS unsaved edits (`onSave`) and closes;
 *   with nothing edited it just closes. Clicking back into the composer to keep
 *   typing is the most common way out of the panel, and it used to discard.
 * - Escape with unsaved edits keeps the panel open and shows a hint; a second
 *   Escape (or Cancel) discards — `onClose`, and the host restores the
 *   original. Escape the IME owns (cancelling a candidate list) is claimed by
 *   the shared latch and never reaches the panel.
 * - Every other key is stopped at the panel, so the page's bubble-phase
 *   shortcuts (Ctrl+digit session jumps, the Settings chord) cannot fire from
 *   inside the textarea and unmount the panel with the edit still in it — the
 *   same boundary `Modal` and `ui/dialog` draw.
 * - Tab cycles WITHIN the panel (`useDialogFocusTrap`, the same trap those
 *   dialogs use). Without it, Tab from the last control parks focus behind the
 *   panel, where the next page chord is no longer stopped and would unmount a
 *   dirty panel without the outside-click save ever running.
 */

const noop = () => {}

/**
 * The Tab trap for the open panel, mounted only while the panel is — the shared
 * hook's focus-in runs on mount, so mount must mean open. Escape stays with the
 * panel's own two-step dismissal (`handleEscape: false`), and focus is handed
 * back by whoever closes the panel — `PillsPlugin` parks the caret after the
 * pill, an outside click leaves focus where it landed — so the hook's return
 * half is off (`restoreFocus: false`). The textarea takes focus after the hook's
 * focus-in (which lands on the first control, Cancel) so typing starts at once.
 */
function PanelFocusTrap({ panelRef, textareaRef }: {
  panelRef: RefObject<HTMLDivElement | null>
  textareaRef: RefObject<HTMLTextAreaElement | null>
}) {
  useDialogFocusTrap(panelRef, noop, { handleEscape: false, restoreFocus: false })
  useEffect(() => { textareaRef.current?.focus({ preventScroll: true }) }, [textareaRef])
  return null
}

export default function PastePreviewEditor({
  open,
  anchorRect,
  content,
  lines,
  sessionKey,
  onChange,
  onSave,
  onClose,
  testIdPrefix = 'paste-preview-editor',
}: PastePreviewEditorProps) {
  const [value, setValue] = useState(content)
  const panelRef = useRef<HTMLDivElement | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const [pos, setPos] = useState<{ left: number; top: number; above: boolean } | null>(null)

  // Unsaved edits, and whether the user has already pressed Escape once with
  // them pending (the hint is showing; the next Escape discards).
  const dirty = value !== content
  const [discardArmed, setDiscardArmed] = useState(false)
  // The header's line count follows the textarea while editing, so "6 lines"
  // does not sit over a textarea holding 3.
  const liveLines = dirty ? countLines(value) : lines

  // Re-seed the editable value each time the popover (re)opens on fresh content,
  // so a Cancel-then-reopen shows the saved content, not a stale local edit.
  useEffect(() => { if (open) { setValue(content); setDiscardArmed(false) } }, [open, content])
  // A new preview session (open, or a different pill while already open) starts
  // unsized at the default width. A LAYOUT effect declared before the sizing
  // effect below, so the reset is visible to it in the same commit — a passive
  // effect ran after it and left a 2-line paste at the previous paste's dragged
  // height, and never ran at all when the session changed with `open` staying
  // true.
  useLayoutEffect(() => {
    if (open) { userSizedRef.current = false; setPanelWidth(PANEL_WIDTH) }
  }, [open, sessionKey])

  const [textareaHeight, setTextareaHeight] = useState(MIN_TEXTAREA_PX)
  const [panelWidth, setPanelWidth] = useState<number>(PANEL_WIDTH)
  // Set once the user grabs the resize handle; auto-fit stops overriding them.
  const userSizedRef = useRef(false)
  // True for the duration of a resize gesture. The `left` clamp below follows
  // the panel's live width so a widened panel stays inside a narrowed viewport,
  // but during the gesture itself `left` must not move: on a right-clamped panel
  // a shrink would otherwise push `left` right on every pointermove, pinning the
  // grip at the viewport edge while the pointer walks away from it.
  const draggingRef = useRef(false)
  // Room (px) available to the textarea on the chosen side — the resize cap.
  const maxTextareaRef = useRef(MIN_TEXTAREA_PX)

  // Position + size the panel relative to the anchor. useLayoutEffect so the
  // first paint is already placed (no flash at 0,0).
  //
  // Side: ABOVE when the content-sized panel fits above, else whichever side
  // has more room. Textarea height: the CONTENT height, capped by the room on
  // the chosen side minus the panel chrome (header/padding — measured from the
  // real DOM once available), so a long paste fills the space and scrolls and
  // the panel can never leave the viewport. ABOVE is anchored by its bottom
  // edge, so it grows upward from the chip.
  useLayoutEffect(() => {
    if (!open || !anchorRect) { setPos(null); return }
    const compute = () => {
      const panelEl = panelRef.current
      const taEl = textareaRef.current
      const chrome = panelEl?.offsetHeight && taEl?.offsetHeight
        ? panelEl.offsetHeight - taEl.offsetHeight
        : PANEL_CHROME_FALLBACK_PX
      const roomAbove = anchorRect.top - 8 - GAP
      const roomBelow = window.innerHeight - anchorRect.bottom - 8 - GAP
      const contentPx = Math.ceil(lines * LINE_PX + TEXTAREA_CHROME_PX)
      const above = roomAbove >= contentPx + chrome || roomAbove >= roomBelow
      const room = above ? roomAbove : roomBelow
      const maxTa = Math.max(MIN_TEXTAREA_PX, room - chrome)
      maxTextareaRef.current = maxTa
      // Clamp against the panel's ACTUAL width: after a resize drag the panel is
      // wider than the default, and on a viewport that then narrows the right
      // edge — Save and the resize grip — would otherwise sit off-screen
      // (`maxWidth: calc(100vw - 16px)` measures from the viewport, not from
      // `left`, so it cannot pull it back).
      const clampedLeft = Math.max(8, Math.min(anchorRect.left, window.innerWidth - panelWidth - 8))
      if (userSizedRef.current) setTextareaHeight((h) => Math.min(h, maxTa))
      else setTextareaHeight(Math.max(MIN_TEXTAREA_PX, Math.min(contentPx, maxTa)))
      setPos((prev) => {
        // Mid-gesture the edge the user is not holding stays put (the drag's own
        // width cap already keeps the panel inside the viewport).
        const left = draggingRef.current && prev ? prev.left : clampedLeft
        return (prev && prev.left === left && prev.above === above && prev.top === (above ? -1 : anchorRect.bottom + GAP))
          ? prev
          : { left, top: above ? -1 : anchorRect.bottom + GAP, above }
      })
    }
    compute()
    const el = panelRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    // Re-measure once the real chrome height is known (fonts, header wrap).
    const ro = new ResizeObserver(() => compute())
    ro.observe(el)
    return () => ro.disconnect()
  }, [open, anchorRect, lines, panelWidth, sessionKey])

  // Corner resize handle. It sits on the corner AWAY from the chip (top-right
  // when the panel opens above, bottom-right below), so dragging it outward —
  // up/right or down/right — always makes the panel bigger. The native
  // textarea handle is disabled: with the panel's bottom edge anchored over the
  // chip, a bottom-right handle would grow the panel upward while the handle
  // itself stayed put, which reads as "it won't stretch".
  //
  // Both the drag and the arrow keys below read their caps from `resizeCaps`
  // and their starting width from `measuredPanelWidth`, and size through the
  // module-level clamps — one source for every number, so the two paths cannot
  // drift apart.
  const resizeCaps = (left: number) => ({
    // Room from the panel's left edge to the viewport edge, less the 8px margin.
    maxW: window.innerWidth - left - 8,
    // Room on the chosen side minus the panel chrome (measured by the layout effect).
    maxH: maxTextareaRef.current,
  })
  // The rendered width (the `maxWidth` cap may have shrunk it below state),
  // falling back to state where there is no layout.
  const measuredPanelWidth = () => panelRef.current?.offsetWidth || panelWidth
  const onResizePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!pos) return
    e.preventDefault()
    e.stopPropagation()
    const startX = e.clientX
    const startY = e.clientY
    const startW = measuredPanelWidth()
    const startH = textareaHeight
    const above = pos.above
    const { maxW, maxH } = resizeCaps(pos.left)
    userSizedRef.current = true
    draggingRef.current = true
    const handle = e.currentTarget
    if (typeof handle.setPointerCapture === 'function') handle.setPointerCapture(e.pointerId)
    const onMove = (ev: PointerEvent) => {
      const dx = ev.clientX - startX
      const dy = above ? startY - ev.clientY : ev.clientY - startY
      setPanelWidth(clampPanelWidth(startW + dx, maxW))
      setTextareaHeight(clampTextareaHeight(startH + dy, maxH))
    }
    const onUp = () => {
      draggingRef.current = false
      handle.removeEventListener('pointermove', onMove)
      handle.removeEventListener('pointerup', onUp)
      handle.removeEventListener('pointercancel', onUp)
      textareaRef.current?.focus()
    }
    handle.addEventListener('pointermove', onMove)
    handle.addEventListener('pointerup', onUp)
    handle.addEventListener('pointercancel', onUp)
  }
  // The keyboard half of the grip (the ARIA window-splitter pattern, as
  // `ResizeHandle`): Left/Right step the width, Up/Down the textarea height,
  // Shift for the coarse step. The vertical arrows follow the grip, not the
  // screen: the arrow that moves the grip AWAY from the chip grows the panel,
  // exactly as the drag's `dy` does above, so a user who has dragged it once
  // finds the keys going the same way. Only the four arrows are claimed — every
  // other key (Escape included) falls through to the panel's `isolateKeys`, so
  // the two-step dismissal is untouched.
  const onResizeKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (!pos) return
    const step = e.shiftKey ? COARSE_STEP : STEP
    let dw = 0
    let dh = 0
    switch (e.key) {
      case 'ArrowRight': dw = step; break
      case 'ArrowLeft': dw = -step; break
      case 'ArrowUp': dh = pos.above ? step : -step; break
      case 'ArrowDown': dh = pos.above ? -step : step; break
      default: return
    }
    e.preventDefault()
    const { maxW, maxH } = resizeCaps(pos.left)
    userSizedRef.current = true
    if (dw) setPanelWidth(clampPanelWidth(measuredPanelWidth() + dw, maxW))
    if (dh) setTextareaHeight(clampTextareaHeight(textareaHeight + dh, maxH))
  }
  // What the grip announces: the width axis (Left/Right), capped as the drag is.
  const widthMax = pos ? Math.max(MIN_PANEL_WIDTH, resizeCaps(pos.left).maxW) : undefined

  // The two dismissal gestures, each written so it cannot destroy an edit in
  // one step. Escape: clean → close; dirty → first press arms the hint, second
  // press discards. Outside pointerdown: dirty → save (the click is usually
  // "back to the composer, keep going", and the pointerdown runs before
  // whatever the click itself does — a session switch included — so the edit
  // is in the pill before the panel can be torn down); clean → close.
  const dismissFromEscape = () => {
    if (!dirty || discardArmed) { onClose(); return }
    setDiscardArmed(true)
  }
  const dismissFromOutside = () => {
    if (dirty) onSave(value)
    else onClose()
  }
  // Latest closures for the document listeners below, which are bound once per
  // open rather than re-bound on every keystroke.
  const dismissRef = useRef({ dismissFromEscape, dismissFromOutside })
  dismissRef.current = { dismissFromEscape, dismissFromOutside }

  // Keyboard boundary on the panel (see the component doc). Escape the IME owns
  // is claimed here — `claimSyntheticKey` consumes both the native event and
  // React's propagation flag — so it neither closes the panel nor reaches the
  // document fallback below. An accepted Escape is handled right here and
  // stopped, so no ancestor's Escape handling sees it. Every other key is
  // stopped so the page's bubble-phase document/window shortcuts cannot fire.
  // Tracking is document-scoped and keyed to `open`, the panel's lifecycle.
  const imeLatch = useDocumentImeLatch(open)
  const isolateKeys = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      if (!imeLatch.claimSyntheticKey(e)) return
      e.stopPropagation()
      dismissFromEscape()
      return
    }
    e.stopPropagation()
  }

  // Document fallbacks: Escape while focus is OUTSIDE the panel (bubble phase,
  // IME-claimed the same way), and a pointerdown outside the panel.
  useEffect(() => {
    if (!open) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || panelRef.current?.contains(e.target as Node)) return
      if (!imeLatch.claimKey(e)) return
      e.stopPropagation()
      dismissRef.current.dismissFromEscape()
    }
    const onPointerDown = (e: PointerEvent) => {
      if (panelRef.current?.contains(e.target as Node)) return
      dismissRef.current.dismissFromOutside()
    }
    document.addEventListener('keydown', onKeyDown)
    document.addEventListener('pointerdown', onPointerDown, true)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.removeEventListener('pointerdown', onPointerDown, true)
    }
  }, [open, imeLatch])

  return createPortal(
    open && anchorRect ? (
        <motion.div
          ref={panelRef}
          role="dialog"
          aria-label={i18nT('components.pastePreviewEditor.editor_aria')}
          data-testid={testIdPrefix}
          onKeyDown={isolateKeys}
          initial={{ opacity: 0, y: pos?.above ? 2 : -2 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.12 }}
          className="fixed z-[130] rounded-md border border-border bg-bg-elevated shadow-lg p-2 flex flex-col gap-2"
          style={{
            left: pos?.left ?? Math.max(8, anchorRect.left),
            // ABOVE: anchor the bottom edge just over the chip (height-independent).
            // BELOW: anchor the top edge just under it.
            ...(pos?.above
              ? { bottom: window.innerHeight - anchorRect.top + GAP }
              : { top: pos?.top ?? anchorRect.bottom + GAP }),
            width: `${panelWidth}px`,
            maxWidth: 'calc(100vw - 16px)',
          }}
        >
          <PanelFocusTrap panelRef={panelRef} textareaRef={textareaRef} />
          <div
            data-testid={`${testIdPrefix}-header`}
            // ABOVE puts the 16px resize grip on the top-right corner of the
            // panel's padding box, 8px into this `p-2` row: reserve that width
            // so the grip never paints over (and swallows pointerdown on) Save.
            className={`flex items-center justify-between gap-3${pos?.above ? ' pr-2' : ''}`}
          >
            {discardArmed && dirty ? (
              <span role="status" className="text-[11px] text-warn" data-testid={`${testIdPrefix}-unsaved`}>
                {i18nT('components.pastePreviewEditor.unsaved_hint')}
              </span>
            ) : (
              <span className="text-[11px] text-muted" data-testid={`${testIdPrefix}-lines`}>
                {i18nT('components.pastePreviewEditor.lines', { count: liveLines })}
              </span>
            )}
            <div className="flex items-center gap-2">
              <button
                type="button"
                className="text-[12px] text-muted hover:text-text bg-transparent border-none cursor-pointer p-0"
                onClick={onClose}
              >
                {i18nT('components.pastePreviewEditor.cancel')}
              </button>
              <button
                type="button"
                className="text-[12px] text-accent hover:text-accent-hover bg-transparent border-none cursor-pointer p-0 font-medium"
                onClick={() => onSave(value)}
              >
                {i18nT('components.pastePreviewEditor.save')}
              </button>
            </div>
          </div>
          <textarea
            ref={textareaRef}
            data-testid={`${testIdPrefix}-textarea`}
            value={value}
            onChange={(e) => { setValue(e.target.value); setDiscardArmed(false); onChange?.(e.target.value) }}
            aria-label={i18nT('components.pastePreviewEditor.editor_aria')}
            className="font-mono text-[11px] leading-[1.5] text-text bg-bg border border-border rounded-sm p-2 outline-hidden focus:border-accent"
            style={{
              whiteSpace: 'pre',
              overflow: 'auto',
              resize: 'none',
              boxSizing: 'border-box',
              width: '100%',
              height: textareaHeight,
            }}
          />
          {/* ARIA gives `separator` two flavours and jsx-a11y only models the
              static one: a FOCUSABLE separator is the window-splitter widget,
              which owns both a tab stop and the arrow keys. `[tabindex]` is also
              what puts it in `useDialogFocusTrap`'s FOCUSABLE selector, so Tab
              reaches it inside the panel. Focus ring as on `PasteBlockChip`. */}
          {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- focusable separator = the window-splitter widget; onKeyDown IS its documented operation */}
          <div
            role="separator"
            aria-label={i18nT('components.pastePreviewEditor.resize_aria')}
            // Left/Right move the announced value (the width), which per ARIA
            // is a `vertical` splitter.
            aria-orientation="vertical"
            aria-valuenow={panelWidth}
            aria-valuemin={MIN_PANEL_WIDTH}
            aria-valuemax={widthMax}
            // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- the tab stop is what promotes the separator to the window-splitter widget
            tabIndex={0}
            data-testid={`${testIdPrefix}-resize`}
            onPointerDown={onResizePointerDown}
            onKeyDown={onResizeKeyDown}
            className={`absolute right-0 h-4 w-4 touch-none text-muted rounded-sm focus:outline-hidden focus-visible:ring-1 focus-visible:ring-accent ${pos?.above ? 'top-0 cursor-nesw-resize' : 'bottom-0 cursor-nwse-resize'}`}
            style={{
              // Two short diagonal grip lines in the corner, mirrored per side;
              // `currentColor` comes from the theme's muted text token.
              backgroundImage:
                'linear-gradient(135deg, transparent 45%, currentColor 45%, currentColor 55%, transparent 55%), ' +
                'linear-gradient(135deg, transparent 70%, currentColor 70%, currentColor 80%, transparent 80%)',
              backgroundSize: '10px 10px',
              backgroundRepeat: 'no-repeat',
              backgroundPosition: pos?.above ? 'right 3px top 3px' : 'right 3px bottom 3px',
              transform: pos?.above ? 'scaleY(-1)' : undefined,
              opacity: 0.7,
            }}
          />
        </motion.div>
    ) : null,
    document.body,
  )
}
