import { useCallback, useEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { OverlayPortal } from '../overlayRoot'
import { Crop, FileText, Loader2, PenLine, Plus, X } from 'lucide-react'
import { useAnchorRemeasure } from '../../hooks/useAnchorRemeasure'
import { usePressActivation } from '../../hooks/usePressActivation'
import { isScreenSnipSupported } from '../../hooks/useScreenSnip'
import { i18nT } from '../../i18n/t'
import { uiLocation } from '../../uiLocations/uiLocation'
import { GuideRevealScope } from '../../guide/GuideRevealScope'
import type { ComposerControl } from '../composerControl'
import type { useComposerPickers } from '../../chat-core/composer/pickers'
import type { ChatInputProps } from './props'

/* The attach control at the head of the bottom row: the "+" drop-up (upload,
   screenshot, sketch, collapse, and the `/` `@` `$` shortcuts) on a pointer
   device and on a touch host that is not collapsible, a bare file-input label
   on the collapsible touch host, and the cancel control that stands in for
   either while an upload is in flight. */

/**
 * Where focus goes once the "+" -> Upload file picker closes.
 *
 * Opening the picker closes the "+" menu, and the menu's Upload file button
 * was the focused element, so focus falls to <body> and stays there after the
 * OS dialog closes: the user has to click the composer before typing. The
 * next step after attaching a file is writing the message, so a chosen file
 * returns focus to the composer (APG allows a different destination than the
 * invoker when the workflow calls for it); the composer keeps its own
 * selection, so the draft's caret is where the user left it. A cancelled
 * picker returns focus to the "+" trigger, the invoker, so the user can reopen
 * the menu or Tab on.
 *
 * Focus is only reclaimed while it is still parked on what the picker flow
 * left behind: <body>, the hidden file input, or the trigger. A user who has
 * moved to another control keeps it. Nothing runs when the upload itself
 * finishes or fails: by then focus is already in the composer, or wherever the
 * user put it.
 *
 * Pointer path only, keyed on the input mode rather than on the "+" button:
 * on touch (a touch device, or the mobile breakpoint) "+" still opens its menu
 * on non-collapsible hosts, but focusing the composer there would raise the
 * soft keyboard, which the composer avoids doing on its own (see the
 * autoFocusKey effect in ChatInput), so neither return runs on touch.
 */
function useUploadFocusReturn({ fileInputRef, plusBtnRef, composerControl, isTouchInput }: {
  fileInputRef: RefObject<HTMLInputElement | null>
  plusBtnRef: RefObject<HTMLButtonElement | null>
  composerControl: () => ComposerControl | null
  isTouchInput: () => boolean
}) {
  const composerControlRef = useRef(composerControl)
  composerControlRef.current = composerControl
  const isTouchInputRef = useRef(isTouchInput)
  isTouchInputRef.current = isTouchInput
  useEffect(() => {
    const input = fileInputRef.current
    if (!input) return
    const parked = () => {
      if (isTouchInputRef.current()) return false
      const active = document.activeElement
      return !active || active === document.body || active === input || active === plusBtnRef.current
    }
    // Runs before ChatInput's React handler clears `input.value`: a native
    // listener on the target fires ahead of React's root delegation.
    const onChange = () => {
      if (plusBtnRef.current && input.files?.length && parked()) composerControlRef.current()?.focus()
    }
    const onCancel = () => {
      const trigger = plusBtnRef.current
      if (!trigger || !parked()) return
      if (!trigger.disabled) trigger.focus()
      else composerControlRef.current()?.focus()
    }
    input.addEventListener('change', onChange)
    input.addEventListener('cancel', onCancel)
    return () => {
      input.removeEventListener('change', onChange)
      input.removeEventListener('cancel', onCancel)
    }
  }, [fileInputRef, plusBtnRef])
}

export function usePlusMenu({ pickers, value, onChange, composerControl, fileInputRef, isTouchInput }: {
  pickers: ReturnType<typeof useComposerPickers>
  value: string
  onChange: (v: string) => void
  composerControl: () => ComposerControl | null
  fileInputRef: RefObject<HTMLInputElement | null>
  /** Read when the picker closes, so it may close over state declared later in render. */
  isTouchInput: () => boolean
}) {
  const { setSlashMenuOpen, setFilePickerOpen, setFileQuery, setSkillPickerOpen, setSkillQuery } = pickers
  // "+" drop-up menu (upload file / image + browse toggle).
  const [plusOpen, setPlusOpen] = useState(false)
  const [sketchOpen, setSketchOpen] = useState(false)
  const plusWrapRef = useRef<HTMLDivElement>(null)
  const plusBtnRef = useRef<HTMLButtonElement>(null)
  const plusMenuRef = useRef<HTMLDivElement>(null)
  useUploadFocusReturn({ fileInputRef, plusBtnRef, composerControl, isTouchInput })
  const [plusRect, setPlusRect] = useState<DOMRect | null>(null)
  useEffect(() => {
    if (!plusOpen) return
    // Menu is portaled to the body-level overlay root (escapes the input's overflow-hidden), so the
    // outside-click guard must also exclude the portaled menu, not just the button.
    const h = (e: MouseEvent) => {
      const t = e.target as Node
      if (!plusWrapRef.current?.contains(t) && !plusMenuRef.current?.contains(t)) setPlusOpen(false)
    }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [plusOpen])
  const measurePlus = useCallback(() => {
    if (plusBtnRef.current) setPlusRect(plusBtnRef.current.getBoundingClientRect())
  }, [])
  // Keeps the portaled "+" menu anchored while the trigger moves under it --
  // notably when the mobile keyboard closes (visualViewport-only signal).
  useAnchorRemeasure(plusOpen, measurePlus)
  const togglePlus = () => {
    if (!plusOpen) measurePlus()
    setPlusOpen(o => !o)
  }
  // Open an in-input trigger picker from the + menu (mirrors typing the sigil):
  //  '/' slash commands (whole-input), '@' file mention, '$' skill. Appends the
  //  sigil at a word boundary, opens the matching picker, then refocuses the box.
  const openTrigger = (sigil: '/' | '@' | '$') => {
    setPlusOpen(false)
    let nextValue = '/'
    if (sigil === '/') {
      onChange(nextValue)
      setSlashMenuOpen(true); setFilePickerOpen(false); setSkillPickerOpen(false)
    } else {
      // Append at the end, exactly as the base textarea path always has —
      // the menu gesture is "start a mention", not "insert at caret", and the
      // default path is the declared rollback target, so its observable
      // behavior must not change.
      const sep = value === '' || /\s$/.test(value) ? '' : ' '
      nextValue = value + sep + sigil
      onChange(nextValue)
      setSlashMenuOpen(false)
      if (sigil === '@') { setFilePickerOpen(true); setFileQuery(''); setSkillPickerOpen(false) }
      else { setSkillPickerOpen(true); setSkillQuery(''); setFilePickerOpen(false) }
    }
    // Engine-neutral twin of the base `el.setSelectionRange(n, n)`: place the
    // caret at the end of the new value in whichever composer is live.
    const nextCaret = nextValue.length
    requestAnimationFrame(() => composerControl()?.setSelection(nextCaret, nextCaret, { focus: true }))
  }

  return { plusOpen, setPlusOpen, sketchOpen, setSketchOpen, plusWrapRef, plusBtnRef, plusMenuRef, plusRect, togglePlus, openTrigger }
}

export function AttachMenu({ plus, onUploadFiles, uploading, onCancelUpload, uploadCount, directFilePicker, fileInputId, openPicker, isMac, touchInput, onScreenshot, collapseMenuRow, typedCommandMenus, onFileSelect, optimizeRow = null }: {
  /** "Optimize prompt" while a turn runs (#23572), a menu row because the
   *  busy send cluster is at its button cap. Null when idle: the cluster's
   *  own Optimize button is back then. */
  optimizeRow?: ReactNode
  plus: ReturnType<typeof usePlusMenu>
  onUploadFiles?: (files: File[]) => void
  uploading: boolean
  onCancelUpload?: () => void
  /** Uploads the cancel control aborts; above one, its label names the count. */
  uploadCount: number
  directFilePicker: boolean
  fileInputId: string
  openPicker: (imageOnly: boolean) => void
  isMac: boolean
  /** A phone or a touch screen: no Screenshot tile, which needs a desktop. */
  touchInput: boolean
  onScreenshot?: () => void
  /** The collapse row the "+" menu hosts (null when the host did not opt in). */
  collapseMenuRow: ReactNode
  typedCommandMenus: boolean
  onFileSelect: ChatInputProps['onFileSelect']
}) {
  const { plusOpen, setPlusOpen, setSketchOpen, plusWrapRef, plusBtnRef, plusMenuRef, plusRect, togglePlus, openTrigger } = plus
  // The "+" trigger opens its menu on the mouse press (usePressActivation);
  // keyboard and touch stay on click. The outside-press dismiss skips the
  // trigger's wrapper, so a press on it while open is a toggle. The menu moves
  // no focus on open, so the trigger takes focus as usual (no keepFocusOffTrigger).
  const bindPlusPress = usePressActivation()
  /**
   * The exit from an upload in flight, and the reason it REPLACES the attach
   * control rather than sitting beside it.
   *
   * The bottom icon row is already at `max-two-buttons-per-row`: two blocking
   * findings drove Sketch off it and into an overflow precisely to keep it at
   * two (see `collapseMenuRowElement` in chat-input/collapse.tsx), so a third
   * sibling here would regrow the
   * row the same rule just shrank, on the narrowest viewport, in both layouts.
   *
   * Replacing costs nothing, because the attach control is already inert while
   * `uploading`: its `htmlFor` is dropped and the pointer branch is `disabled`.
   * So the slot holds no action to displace, and the thing the user is already
   * looking at while they wait becomes the thing they press to stop.
   *
   * The spinner is kept, but BEHIND the glyph rather than as a second icon.
   * A 9px X inside an 18px spinner read to a blind reviewer as "a 'lines'
   * icon, the kind that usually means a menu", and they said they would press
   * it to find out what it was, which discards minutes of a 512 MB upload with
   * no undo. So the X carries the meaning at a legible size with a destructive
   * hover tint, and the liveness is a faint ring that cannot be mistaken for
   * the glyph. The tint matters on the pointer path for a second reason: this
   * slot was inert mid-upload on main, so a click that used to do nothing now
   * ends the transfer, and the control has to stop reading as the attach
   * button's spot doing attach things.
   */
  // One press aborts EVERY upload in flight (paste, a drop and a Sketch insert
  // all start one ungated), so with two or more the label says how many stop.
  const cancelLabel = uploadCount > 1
    ? i18nT('components.chatInput.cancel_n_uploads', { count: uploadCount })
    : i18nT('components.chatInput.cancel_upload')
  const uploadCancelControl = uploading && onCancelUpload ? (
    <button
      type="button"
      onClick={onCancelUpload}
      className="relative w-8 h-8 rounded-lg flex items-center justify-center cursor-pointer transition-all bg-transparent border-none text-muted hover:text-danger-text hover:bg-danger/10"
      aria-label={cancelLabel}
      title={cancelLabel}
    >
      <Loader2 size={28} strokeWidth={1.5} className="animate-spin absolute inset-0 m-auto opacity-30" />
      <X size={16} strokeWidth={2.5} />
    </button>
  ) : null
  return (
    <>
      {onUploadFiles && (
        <div className="relative shrink-0" ref={plusWrapRef}>
          {uploadCancelControl || (directFilePicker ? (
            /* Association is intentionally absent while uploads disable the control. */
            <label
              htmlFor={uploading ? undefined : fileInputId}
              aria-disabled={uploading || undefined}
              className={`w-8 h-8 rounded-lg flex items-center justify-center transition-all bg-transparent ${uploading ? 'opacity-30 cursor-default' : 'cursor-pointer text-muted hover:text-text hover:bg-bg-hover'}`}
              aria-label={i18nT('components.chatInput.attach_files')}
              title={i18nT('components.chatInput.attach_files')}
              {...uiLocation('composer.attach-files')}
            >
              {uploading ? <Loader2 size={18} className="animate-spin" /> : <Plus size={18} />}
            </label>
          ) : (
            <button
              className={`w-8 h-8 rounded-lg flex items-center justify-center cursor-pointer transition-all disabled:opacity-30 bg-transparent border-none ${plusOpen ? 'text-text bg-bg-hover' : 'text-muted hover:text-text hover:bg-bg-hover'}`}
              {...bindPlusPress<HTMLButtonElement>(togglePlus)}
              disabled={uploading}
              aria-haspopup="menu"
              aria-expanded={plusOpen}
              aria-label={i18nT('components.chatInput.add_files_options')}
              title={i18nT('components.chatInput.add_files_options')}
              {...uiLocation('composer.add-menu', plusBtnRef)}
            >
              {uploading ? <Loader2 size={18} className="animate-spin" /> : <Plus size={18} className={`transition-transform ${plusOpen ? 'rotate-45' : ''}`} />}
            </button>
          ))}
          {/* The drop-up's scope owner, outside its own open conditional. */}
          <GuideRevealScope id="menu:composer.add-menu" open={!directFilePicker && plusOpen}>
          {!directFilePicker && plusOpen && plusRect && <OverlayPortal>
            <div
              ref={plusMenuRef}
              className="fixed w-[260px] rounded-xl bg-bg-elevated border border-border shadow-xl p-2 animate-slide-up z-[60]"
              style={{ left: Math.max(8, Math.min(plusRect.left, window.innerWidth - 260 - 8)), bottom: window.innerHeight - plusRect.top + 8 }}
            >
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => openPicker(false)}
                  {...uiLocation('composer.add-menu.upload')}
                  className="flex-1 flex flex-col items-center gap-1.5 px-2 py-3 rounded-lg border border-border bg-transparent hover:bg-bg-hover hover:border-border-strong transition-all cursor-pointer"
                >
                  <FileText size={18} className="text-muted" />
                  <span className="text-[12px] font-medium text-text">{i18nT('components.chatInput.upload_file')}</span>
                </button>
                {(isScreenSnipSupported() || isMac) && !touchInput && onScreenshot && (
                  <button
                    type="button"
                    onClick={() => { setPlusOpen(false); onScreenshot() }}
                    {...uiLocation('composer.add-menu.screenshot')}
                    className="flex-1 flex flex-col items-center gap-1.5 px-2 py-3 rounded-lg border border-border bg-transparent hover:bg-bg-hover hover:border-border-strong transition-all cursor-pointer"
                  >
                    <Crop size={18} className="text-muted" />
                    <span className="text-[12px] font-medium text-text">{i18nT('components.chatInput.screenshot')}</span>
                  </button>
                )}
              </div>
              {/* Sketch is a full-width menu ROW, not a third tile: the
                  tile group above is capped at two peer actions by the
                  max-two-buttons-per-row rule, and wrapping a third onto
                  a second grid line is the remedy that rule explicitly
                  rejects. A stacked row (the same shape as the trigger
                  shortcuts below) is its own row by construction. */}
              <div className="mt-2 flex flex-col gap-0.5">
                <button
                  type="button"
                  onClick={() => { setPlusOpen(false); setSketchOpen(true) }}
                  {...uiLocation('composer.add-menu.sketch')}
                  className="w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg bg-transparent hover:bg-bg-hover transition-colors cursor-pointer text-left"
                >
                  <PenLine size={14} className="w-4 shrink-0 text-muted lucide-inline" />
                  <div className="min-w-0">
                    <div className="text-[12px] font-medium text-text">{i18nT('components.chatInput.sketch')}</div>
                    <div className="text-[11px] text-muted leading-snug">{i18nT('components.chatInput.sketch_desc')}</div>
                  </div>
                </button>
                {/* Collapse for reading, a menu ROW for the same reason Sketch
                    is one: the tile group above and the bottom action row are
                    both capped at two peer actions, and this is a third
                    action either way. A stacked row is its own row by
                    construction.

                    It also has to NOT be an icon-only control down in that
                    action row. It was, and review caught what the frames
                    show plainly: an unaccompanied chevron immediately after
                    ApprovalModePicker — which renders "Normal" with no caret
                    of its own (it imports no chevron icon) — reads as that
                    picker's dropdown arrow, so the entry point for this
                    whole feature parsed as a mode menu. Here it carries its
                    own name and a description instead.

                    One definition, shared with the touch overflow — see
                    `collapseMenuRowElement` (chat-input/collapse.tsx), which
                    also explains why touch needs a second host at all. */}
                {collapseMenuRow}
                {optimizeRow}
              </div>
              {/* In-input trigger shortcuts: clicking inserts the sigil
               *  and opens the matching picker (same as typing /, @, $). */}
              <div className="mt-2 pt-2 border-t border-border flex flex-col gap-0.5">
                {typedCommandMenus && <button
                  type="button"
                  onClick={() => openTrigger('/')}
                  title={i18nT('components.chatInput.slash_commands')}
                  {...uiLocation('composer.add-menu.slash')}
                  className="w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg bg-transparent hover:bg-bg-hover transition-colors cursor-pointer text-left"
                >
                  <span className="w-4 text-center text-[14px] font-mono leading-none text-muted shrink-0">/</span>
                  <div className="min-w-0">
                    <div className="text-[12px] font-medium text-text">{i18nT('components.chatInput.command')}</div>
                  </div>
                </button>}
                {onFileSelect && (
                  <button
                    type="button"
                    onClick={() => openTrigger('@')}
                    {...uiLocation('composer.add-menu.reference-file')}
                    className="w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg bg-transparent hover:bg-bg-hover transition-colors cursor-pointer text-left"
                  >
                    <span className="w-4 text-center text-[14px] font-mono leading-none text-muted shrink-0">@</span>
                    <div className="min-w-0">
                      <div className="text-[12px] font-medium text-text">{i18nT('components.chatInput.reference_a_file')}</div>
                    </div>
                  </button>
                )}
                {typedCommandMenus && <button
                  type="button"
                  onClick={() => openTrigger('$')}
                  {...uiLocation('composer.add-menu.skill')}
                  className="w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg bg-transparent hover:bg-bg-hover transition-colors cursor-pointer text-left"
                >
                  <span className="w-4 text-center text-[14px] font-mono leading-none text-muted shrink-0">$</span>
                  <div className="min-w-0">
                    <div className="text-[12px] font-medium text-text">{i18nT('components.chatInput.use_a_skill')}</div>
                  </div>
                </button>}
              </div>
            </div>
          </OverlayPortal>}
          </GuideRevealScope>
        </div>
      )}
      {/* Touch path: `directFilePicker` (touch AND a collapsible host, see
          ChatInput) replaces the "+" drop-up with a bare file-input label,
          so the menu's Sketch and collapse rows never mount there. They hang
          off the touch overflow instead (ChatInput's `composer-more-trigger`),
          which keeps the row at two controls under max-two-buttons-per-row.
          Every other touch host keeps the "+" menu, with Sketch a row in it. */}
    </>
  )
}
