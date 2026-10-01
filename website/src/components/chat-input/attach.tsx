import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Crop, FileText, Loader2, PenLine, Plus, X } from 'lucide-react'
import { useAnchorRemeasure } from '../../hooks/useAnchorRemeasure'
import { isScreenSnipSupported } from '../../hooks/useScreenSnip'
import { i18nT } from '../../i18n/t'
import type { ComposerControl } from '../composerControl'
import type { useComposerPickers } from './pickers'
import type { ChatInputProps } from './props'

/* The attach control at the head of the bottom row: the "+" drop-up (upload,
   screenshot, sketch, collapse, and the `/` `@` `$` shortcuts) on a pointer
   device, a bare file-input label on touch, and the cancel control that
   stands in for either while an upload is in flight. */

export function usePlusMenu({ pickers, value, onChange, composerControl }: {
  pickers: ReturnType<typeof useComposerPickers>
  value: string
  onChange: (v: string) => void
  composerControl: () => ComposerControl | null
}) {
  const { setSlashMenuOpen, setFilePickerOpen, setFileQuery, setSkillPickerOpen, setSkillQuery } = pickers
  // "+" drop-up menu (upload file / image + browse toggle).
  const [plusOpen, setPlusOpen] = useState(false)
  const [sketchOpen, setSketchOpen] = useState(false)
  const plusWrapRef = useRef<HTMLDivElement>(null)
  const plusBtnRef = useRef<HTMLButtonElement>(null)
  const plusMenuRef = useRef<HTMLDivElement>(null)
  const [plusRect, setPlusRect] = useState<DOMRect | null>(null)
  useEffect(() => {
    if (!plusOpen) return
    // Menu is portaled to <body> (escapes the input's overflow-hidden), so the
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

export function AttachMenu({ plus, onUploadFiles, uploading, onCancelUpload, directFilePicker, collapsible, fileInputId, openPicker, isMac, isMobile, onScreenshot, collapseMenuRow, typedCommandMenus, onFileSelect }: {
  plus: ReturnType<typeof usePlusMenu>
  onUploadFiles?: (files: File[]) => void
  uploading: boolean
  onCancelUpload?: () => void
  directFilePicker: boolean
  collapsible: boolean
  fileInputId: string
  openPicker: (imageOnly: boolean) => void
  isMac: boolean
  isMobile: boolean
  onScreenshot?: () => void
  /** The collapse row the "+" menu hosts (null when the host did not opt in). */
  collapseMenuRow: ReactNode
  typedCommandMenus: boolean
  onFileSelect: ChatInputProps['onFileSelect']
}) {
  const { plusOpen, setPlusOpen, setSketchOpen, plusWrapRef, plusBtnRef, plusMenuRef, plusRect, togglePlus, openTrigger } = plus
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
  const uploadCancelControl = uploading && onCancelUpload ? (
    <button
      type="button"
      onClick={onCancelUpload}
      className="relative w-8 h-8 rounded-lg flex items-center justify-center cursor-pointer transition-all bg-transparent border-none text-muted hover:text-danger hover:bg-danger/10"
      aria-label={i18nT('components.chatInput.cancel_upload')}
      title={i18nT('components.chatInput.cancel_upload')}
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
            >
              {uploading ? <Loader2 size={18} className="animate-spin" /> : <Plus size={18} />}
            </label>
          ) : (
            <button
              ref={plusBtnRef}
              className={`w-8 h-8 rounded-lg flex items-center justify-center cursor-pointer transition-all disabled:opacity-30 bg-transparent border-none ${plusOpen ? 'text-text bg-bg-hover' : 'text-muted hover:text-text hover:bg-bg-hover'}`}
              onClick={togglePlus}
              disabled={uploading}
              aria-haspopup="menu"
              aria-expanded={plusOpen}
              aria-label={i18nT('components.chatInput.add_files_options')}
              title={i18nT('components.chatInput.add_files_options')}
            >
              {uploading ? <Loader2 size={18} className="animate-spin" /> : <Plus size={18} className={`transition-transform ${plusOpen ? 'rotate-45' : ''}`} />}
            </button>
          ))}
          {!directFilePicker && plusOpen && plusRect && createPortal(
            <div
              ref={plusMenuRef}
              className="fixed w-[260px] rounded-xl bg-bg-elevated border border-border shadow-xl p-2 animate-slide-up z-[60]"
              style={{ left: Math.max(8, Math.min(plusRect.left, window.innerWidth - 260 - 8)), bottom: window.innerHeight - plusRect.top + 8 }}
            >
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => openPicker(false)}
                  className="flex-1 flex flex-col items-center gap-1.5 px-2 py-3 rounded-lg border border-border bg-transparent hover:bg-bg-hover hover:border-border-strong transition-all cursor-pointer"
                >
                  <FileText size={18} className="text-muted" />
                  <span className="text-[12px] font-medium text-text">{i18nT('components.chatInput.upload_file')}</span>
                </button>
                {(isScreenSnipSupported() || isMac) && !isMobile && onScreenshot && (
                  <button
                    type="button"
                    onClick={() => { setPlusOpen(false); onScreenshot() }}
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
                  title={i18nT('components.chatInput.sketch')}
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
              </div>
              {/* In-input trigger shortcuts: clicking inserts the sigil
               *  and opens the matching picker (same as typing /, @, $). */}
              <div className="mt-2 pt-2 border-t border-border flex flex-col gap-0.5">
                {typedCommandMenus && <button
                  type="button"
                  onClick={() => openTrigger('/')}
                  title={i18nT('components.chatInput.slash_commands')}
                  className="w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg bg-transparent hover:bg-bg-hover transition-colors cursor-pointer text-left"
                >
                  <span className="w-4 text-center text-[14px] font-mono leading-none text-muted shrink-0">/</span>
                  <div className="min-w-0">
                    <div className="text-[12px] font-medium text-text">{i18nT('components.chatInput.command')}</div>
                    <div className="text-[11px] text-muted leading-snug">{i18nT('components.chatInput.quick_actions_like_clearing_the_chat_or_checking')}</div>
                  </div>
                </button>}
                {onFileSelect && (
                  <button
                    type="button"
                    onClick={() => openTrigger('@')}
                    title={i18nT('components.chatInput.reference_a_file')}
                    className="w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg bg-transparent hover:bg-bg-hover transition-colors cursor-pointer text-left"
                  >
                    <span className="w-4 text-center text-[14px] font-mono leading-none text-muted shrink-0">@</span>
                    <div className="min-w-0">
                      <div className="text-[12px] font-medium text-text">{i18nT('components.chatInput.file')}</div>
                      <div className="text-[11px] text-muted leading-snug">{i18nT('components.chatInput.let_the_agent_read_one_of_your_files')}</div>
                    </div>
                  </button>
                )}
                {typedCommandMenus && <button
                  type="button"
                  onClick={() => openTrigger('$')}
                  title={i18nT('components.chatInput.use_a_skill')}
                  className="w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg bg-transparent hover:bg-bg-hover transition-colors cursor-pointer text-left"
                >
                  <span className="w-4 text-center text-[14px] font-mono leading-none text-muted shrink-0">$</span>
                  <div className="min-w-0">
                    <div className="text-[12px] font-medium text-text">{i18nT('components.chatInput.skill')}</div>
                    <div className="text-[11px] text-muted leading-snug">{i18nT('components.chatInput.apply_a_ready_made_set_of_instructions')}</div>
                  </div>
                </button>}
              </div>
            </div>,
            document.body
          )}
        </div>
      )}
      {/* Touch path: directFilePicker replaces the "+" drop-up with a
          bare file-input label, so the menu's Sketch row never mounts
          there and neither does the collapse row. Both need a host on
          touch, and the row cannot simply grow to fit them: with the
          attach label it would be three peer actions, and
          max-two-buttons-per-row is explicit that the third "goes into an
          overflow DropdownMenu (kebab / More), or leaves the row", with a
          trigger counting as ONE "regardless of how many items it holds".
          So the pencil becomes that trigger when there is a second action
          to host, and Sketch moves one tap deeper rather than losing its
          place. The row stays at two (label + trigger), and the non-touch
          branch keeps both actions in the "+" menu.

          Sketch alone keeps its dedicated pencil, so a surface that never
          opted into the collapse (a split pane, the side chat) is
          untouched by this. */}
      {onUploadFiles && directFilePicker && !collapsible && (
        <button
          className="w-8 h-8 rounded-lg flex items-center justify-center cursor-pointer transition-all disabled:opacity-30 bg-transparent border-none text-muted hover:text-text hover:bg-bg-hover shrink-0"
          onClick={() => setSketchOpen(true)}
          disabled={uploading}
          aria-haspopup="dialog"
          aria-label={i18nT('components.chatInput.sketch')}
          title={i18nT('components.chatInput.sketch')}
        >
          <PenLine size={17} />
        </button>
      )}
    </>
  )
}
