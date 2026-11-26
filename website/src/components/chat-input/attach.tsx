import { useCallback, useEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { OverlayPortal } from '../overlayRoot'
import { Crop, FileText, PenLine, Plus } from 'lucide-react'
import { useAnchorRemeasure } from '../../hooks/useAnchorRemeasure'
import { usePressActivation } from '../../hooks/usePressActivation'
import { isScreenSnipSupported } from '../../hooks/useScreenSnip'
import { i18nT } from '../../i18n/t'
import { uiLocation } from '../../uiLocations/uiLocation'
import { GuideRevealScope } from '../../guide/GuideRevealScope'
import type { ComposerControl } from '../composerControl'
import type { useComposerPickers } from '../../chat-core/composer/pickers'
import type { ChatInputProps } from './props'
import { IS_MAC, shortcutPlatform } from '../../hooks/useKeyboardShortcuts'
import { chordMatchesEvent, shortcutEntry } from '../../lib/shortcutRegistry'

/* The attach control at the head of the bottom row: the "+" drop-up (upload,
   screenshot, sketch, collapse, and the `/` `@` `$` shortcuts) on a pointer
   device and on a touch host that is not collapsible, a bare file-input label
   on the collapsible touch host. It stays itself while uploads run: each
   upload in flight is a chip of its own above the composer (UploadingStrip),
   with its own status and cancel, so "+" and its menu stay usable. */

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
      if (trigger && parked()) trigger.focus()
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
  const { setSlashMenuOpen, setFilePickerOpen, setFileQuery, setSkillPickerOpen, setSkillQuery, closePickers } = pickers
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
    // The menu opens in place of any open picker, never stacked over it.
    if (!plusOpen) { measurePlus(); closePickers() }
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

/** ⌘⇧U / Ctrl+Shift+U in the composer: the keyboard twin of the Upload file
 *  tile, so the caller fires it only while that tile can be pressed. A key an
 *  input method took (the composer's IME guard, e.g. IBus's Ctrl+Shift+U
 *  Unicode entry) stays the input method's. */
export function isUploadFileChord(e: React.KeyboardEvent, ime: { isComposing: (e: React.KeyboardEvent) => boolean }): boolean {
  const platform = shortcutPlatform()
  const chord = shortcutEntry('upload-file')?.defaults[platform]
  return !!chord && !ime.isComposing(e) && chordMatchesEvent(e, chord, platform)
}

export function AttachMenu({ plus, onUploadFiles, directFilePicker, fileInputId, openPicker, isMac, touchInput, onScreenshot, collapseMenuRow, typedCommandMenus, onFileSelect, optimizeRow = null, appActionRows = null, stashMenuRows }: {
  /** App composer actions (`composerActions.tsx`), a group of their own after
   *  the host's shortcuts so an app's row never sits among the host's. */
  appActionRows?: ReactNode
  /** The "Optimize prompt" row, idle or mid-turn (#41795). Null where the
   *  host opted out or the row lives in the ⋯ overflow instead. */
  optimizeRow?: ReactNode
  plus: ReturnType<typeof usePlusMenu>
  onUploadFiles?: (files: File[]) => void
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
  /** The prompt-stash rows the "+" menu hosts (null when the stash is off). */
  stashMenuRows?: ReactNode
}) {
  const { plusOpen, setPlusOpen, setSketchOpen, plusWrapRef, plusBtnRef, plusMenuRef, plusRect, togglePlus, openTrigger } = plus
  // The "+" trigger opens its menu on the mouse press (usePressActivation);
  // keyboard and touch stay on click. The outside-press dismiss skips the
  // trigger's wrapper, so a press on it while open is a toggle. The menu moves
  // no focus on open, so the trigger takes focus as usual (no keepFocusOffTrigger).
  const bindPlusPress = usePressActivation()
  return (
    <>
      {onUploadFiles && (
        <div className="relative shrink-0" ref={plusWrapRef}>
          {directFilePicker ? (
            <label
              htmlFor={fileInputId}
              className="w-8 h-8 rounded-lg flex items-center justify-center transition-all bg-transparent cursor-pointer text-muted hover:text-text hover:bg-bg-hover"
              aria-label={i18nT('components.chatInput.attach_files')}
              title={i18nT('components.chatInput.attach_files')}
              {...uiLocation('composer.attach-files')}
            >
              <Plus size={18} />
            </label>
          ) : (
            <button
              className={`w-8 h-8 rounded-lg flex items-center justify-center cursor-pointer transition-all bg-transparent border-none ${plusOpen ? 'text-text bg-bg-hover' : 'text-muted hover:text-text hover:bg-bg-hover'}`}
              {...bindPlusPress<HTMLButtonElement>(togglePlus)}
              aria-haspopup="menu"
              aria-expanded={plusOpen}
              aria-label={i18nT('components.chatInput.add_files_options')}
              title={i18nT('components.chatInput.add_files_options')}
              {...uiLocation('composer.add-menu', plusBtnRef)}
            >
              <Plus size={18} className={`transition-transform ${plusOpen ? 'rotate-45' : ''}`} />
            </button>
          )}
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
                  title={i18nT(IS_MAC ? 'components.chatInput.upload_file_title_mac' : 'components.chatInput.upload_file_title')}
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
              {stashMenuRows}
              {appActionRows}
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
