import { useEffect, useRef, useState } from 'react'
import type { Terminal } from '@xterm/xterm'
import { ClipboardPaste, Copy, TextSelect } from 'lucide-react'
import { SOFT_KEYS, pressTerminalKey } from '../utils/terminalKeys'
import { logicalLineBounds, logicalLineTop } from '../utils/terminalLogicalLine'
import { i18nT } from '../i18n/t'

/**
 * A row of soft keys for the keys a touch keyboard omits (Tab, Escape, arrows,
 * ^C). Rendered only on touch devices by the caller.
 *
 * This bar is also what keeps the right-swipe Tab gesture compliant: WCAG 2.5.1
 * requires every path-based gesture to have a single-pointer alternative, so the
 * swipe may accelerate Tab but must never be the only way to press it.
 *
 * It sits in the terminal pane's flow, BELOW the terminal, and never overlays
 * it — the shell's prompt lives on the bottom row, so an overlay would cover the
 * line being typed.
 *
 * The async keys (Copy and Paste) drop late deliveries by reading the
 * terminal's own layout: a clipboard read can take seconds on iOS (the
 * permission callout), and a clipboard op that resolves after the terminal
 * went invisible — tab switch, remote-instance switch, pane closed — must not
 * act on a shell the user cannot see. In every one of those the terminal's
 * host is display:none or detached, so `term.element.offsetParent` is null;
 * for Paste a newline-terminated clipboard would otherwise execute in a hidden
 * shell, and for Copy a late resolve would flip the key's status behind the
 * user's back.
 */
type PasteError = 'paste_failed' | 'paste_clipboard_empty' | 'paste_permission_needed' | 'paste_needs_https'

/** Literal-key lookup (runtime-assembled catalog keys are forbidden by
 *  dynamicKeys.test.ts, which is what keeps the dead-key scan meaningful). */
function pasteErrorLabel(k: PasteError): string {
  switch (k) {
    case 'paste_clipboard_empty': return i18nT('components.terminalKeyBar.paste_clipboard_empty')
    case 'paste_permission_needed': return i18nT('components.terminalKeyBar.paste_permission_needed')
    case 'paste_needs_https': return i18nT('components.terminalKeyBar.paste_needs_https')
    default: return i18nT('components.terminalKeyBar.paste_failed')
  }
}

/** Copy's transient status, keyed by cause. `copy_no_selection` is not an
 *  error — a tap with nothing selected must NOT copy the whole scrollback
 *  (an accidental buffer-wide copy on touch is worse than a no-op), so the
 *  key says "select text first" and stays a same-shape transient message.
 *  `copy_done` is the success beat — a bare clipboard write is invisible on
 *  touch, so a confirmed copy shows and announces "Copied" before reverting,
 *  the same way the failure states do. */
type CopyStatus = 'copy_failed' | 'copy_needs_https' | 'copy_permission_needed' | 'copy_no_selection' | 'copy_done'

/** The status states that read as an error (painted danger). `copy_no_selection`
 *  is guidance and `copy_done` is success — neither is an error. */
function isCopyError(k: CopyStatus): boolean {
  return k !== 'copy_no_selection' && k !== 'copy_done'
}

/** Literal-key lookup (runtime-assembled catalog keys are forbidden by
 *  dynamicKeys.test.ts, which is what keeps the dead-key scan meaningful). */
function copyStatusLabel(k: CopyStatus): string {
  switch (k) {
    case 'copy_no_selection': return i18nT('components.terminalKeyBar.copy_no_selection')
    case 'copy_done': return i18nT('components.terminalKeyBar.copy_done')
    case 'copy_permission_needed': return i18nT('components.terminalKeyBar.copy_permission_needed')
    case 'copy_needs_https': return i18nT('components.terminalKeyBar.copy_needs_https')
    default: return i18nT('components.terminalKeyBar.copy_failed')
  }
}

/** The Select key runs a STAGED CYCLE rather than a one-line-per-tap extend:
 *  tap 1 grabs the bottommost non-empty line, tap 2 the whole buffer, tap 3
 *  wraps back to the last line. Each stage announces itself so the multi-line
 *  capability is discoverable (it is otherwise invisible) and an overshoot
 *  recovers by tapping once more instead of restarting. `null` is the idle
 *  state (nothing selected / selection cleared). */
type SelectStage = 'select_line' | 'select_all'

/** Literal-key lookup for the Select stage label — one compact string serves
 *  both the visible button (readable beside the min-width Copy/Paste controls
 *  at 320–390px) and the sr-only live-region announcement. A separate "full
 *  teaching sentence" variant existed but differed by a single word, cost
 *  2 keys × 13 locales to keep in sync, and had exactly one consumer — merged
 *  away (First Principles review). Same discipline as copyStatusLabel (no
 *  runtime-assembled catalog keys). */
function selectStageShortLabel(k: SelectStage): string {
  switch (k) {
    case 'select_all': return i18nT('components.terminalKeyBar.select_all_short')
    default: return i18nT('components.terminalKeyBar.select_line_short')
  }
}

export default function TerminalKeyBar({ term }: { term: Terminal }) {
  // Paste is a soft key too, for the same reason the others exist: desktop
  // paste rides Ctrl/Cmd+V into xterm's hidden textarea, an event a touch
  // keyboard never fires, and no long-press callout surfaces over the xterm
  // viewport. It differs from SOFT_KEYS in being async (clipboard permission)
  // and fallible, so it is rendered inline rather than forced into TermKey.
  // ONE bar-level transient status slot shared by Paste and Copy. The three
  // keys share a single truncating toolbar row, so independent transient
  // states could stand side by side and truncate each other (First Principles
  // review counted the unhandled pairs: paste↔select, paste↔copy — the
  // earlier code cleared only copy↔select, pair by pair). A single slot makes
  // the mutual exclusion STRUCTURAL: setting a new transient replaces
  // whatever was showing, and a Select tap clears the slot regardless of
  // kind. The Select stage label is deliberately NOT in this slot — it is
  // persistent (selection-lifecycle-tied), not a timed transient.
  // Which failure message a key shows stays keyed by cause — the UX review's
  // point: the code already distinguishes these branches, and folding them
  // into one generic string leaves the deny-path user (the common iOS
  // first-run) with no visible remedy.
  type BarStatus = { kind: 'paste'; key: PasteError } | { kind: 'copy'; key: CopyStatus }
  const [barStatus, setBarStatus] = useState<BarStatus | null>(null)
  const barTimer = useRef<ReturnType<typeof setTimeout>>()
  // Derived per-key views keep the render sites reading naturally.
  const pasteError = barStatus?.kind === 'paste' ? barStatus.key : null
  const copyStatus = barStatus?.kind === 'copy' ? barStatus.key : null
  // Show a transient on the bar: replaces any prior transient (either kind)
  // and re-arms the single 4000ms revert.
  const showBar = (next: BarStatus | null) => {
    setBarStatus(next)
    clearTimeout(barTimer.current)
    if (next) barTimer.current = setTimeout(() => setBarStatus(null), 4000)
  }
  const aliveRef = useRef(true)
  // In-flight guard: a second tap while the clipboard read is pending would
  // issue a second read and a duplicate delivery into the PTY; a late success
  // could also clear a failure state the user has not read yet.
  const busyRef = useRef(false)
  // Copy is the OTHER half of the touch clipboard story: xterm drag-selection
  // never happens on touch, so CliPanel's selection toolbar (its only Copy
  // path, wired to mouseup) never appears — leaving touch devices able to
  // paste (since #5571) but with no way to copy OUT. This key reads the
  // terminal's current selection and writes it to the clipboard. Its status,
  // keyed by cause, is shown on the key the same way Paste shows its errors.
  // Separate in-flight guard from paste: the two actions are independent, and
  // a copy writeText resolving must not clear a paste failure the user has
  // not read yet — the guard, not the status slot, provides that ordering.
  const copyBusyRef = useRef(false)
  // The Select key's current cycle stage, and its rendered label. `selectStage`
  // (the ref) is the last stage reached (line → all) so the NEXT tap knows where
  // to advance to; a wraparound tap (after 'all') and any selection clear reset
  // it to null. It is a ref, not state, because reading it must not depend on a
  // re-render having flushed. `selectStatus` is the rendered/announced stage and
  // DOES drive a render.
  //
  // The label is PERSISTENT, not timed: it stays visible for the whole life of
  // the selection and is cleared only when the selection actually goes away
  // (a successful Copy, or an external clear the terminal reports via
  // onSelectionChange below). An earlier revision reverted the label on a
  // 4000ms timer while selectStageRef persisted — so a user returning after >4s
  // saw a button reading "Select", tapped it expecting a fresh stage-1 line, and
  // got selectAll() instead (the exact accidental buffer-wide grab the Copy
  // copy_no_selection rationale warns against). The label must never claim a
  // stage the buffer no longer has, nor drop a stage the buffer still holds.
  const selectStageRef = useRef<SelectStage | null>(null)
  const [selectStatus, setSelectStatus] = useState<SelectStage | null>(null)
  useEffect(() => {
    aliveRef.current = true
    return () => { aliveRef.current = false; clearTimeout(barTimer.current) }
  }, [])

  // Keep the visible stage label honest about what the buffer actually holds:
  // when the user clears the selection through the terminal itself (ESC, a tap
  // in the viewport, a reflow) xterm fires onSelectionChange with an empty
  // selection. Drop the cycle stage AND the label together so the next Select
  // tap restarts at stage 1 and the button never advertises a stage the buffer
  // no longer has. Subscribed once and disposed on unmount.
  useEffect(() => {
    const onChange = term.onSelectionChange?.(() => {
      if (!aliveRef.current) return
      if ((term.getSelection?.() ?? '') === '') {
        selectStageRef.current = null
        setSelectStatus(null)
      }
    })
    return () => { onChange?.dispose?.() }
  }, [term])

  const handlePaste = () => {
    if (busyRef.current) return
    // A denied permission, a non-secure context, and an empty clipboard must
    // all be VISIBLE: silently doing nothing reads as a broken key on exactly
    // the devices this bar exists for. The failed state reverts after a beat
    // so the key stays usable for a retry once the user grants permission.
    const fail = (key: PasteError) => {
      busyRef.current = false
      if (!aliveRef.current) return
      showBar({ kind: 'paste', key })
    }
    // Optional-chain the whole path: `navigator.clipboard` is undefined in
    // non-secure contexts, and `readText` is missing on engines that ship
    // write-only clipboard support. This failure is PERMANENT for the page
    // (plain-HTTP LAN access) — naming the remedy matters, because a generic
    // "Paste failed" that auto-reverts reads as a transient glitch and the
    // user retries forever.
    const read = navigator.clipboard?.readText?.bind(navigator.clipboard)
    if (!read) { fail('paste_needs_https'); return }
    busyRef.current = true
    read()
      .then(text => {
        // Staleness gate: between the tap and the clipboard resolving, the
        // user may have switched tabs, switched to a remote instance, or
        // closed this pane — in every one of those the terminal's host is
        // display:none or detached, so its offsetParent is null (the same
        // guard CliPanel's refit path uses). A missing element (never
        // opened, or disposed mid-read) fails closed the same way. This
        // layout read subsumes the tab-visibility prop the component once
        // took: the pane's `visible` toggles display on the wrapper that
        // CONTAINS the terminal, so the DOM already answers the question.
        if (!aliveRef.current || !term.element?.offsetParent) {
          busyRef.current = false
          return
        }
        // An empty clipboard pastes nothing — say so rather than no-op.
        if (!text) { fail('paste_clipboard_empty'); return }
        busyRef.current = false
        showBar(null)
        // term.paste routes through the same onData → PTY socket pipeline as
        // typing, converts newlines to carriage returns, and wraps the payload
        // in bracketed-paste markers whenever the running application enabled
        // that mode — which is what keeps a multi-line paste from executing
        // line-by-line in shells/editors that opt in.
        term.paste(text)
      })
      // One catch for both legs: a rejected read AND a throw out of
      // term.paste (disposed terminal) — otherwise the latter is an unhandled
      // rejection. A NotAllowedError is the permission prompt saying no, and
      // its message must name the remedy, not just the failure.
      .catch((err: unknown) => {
        const denied = err instanceof DOMException && err.name === 'NotAllowedError'
        fail(denied ? 'paste_permission_needed' : 'paste_failed')
      })
  }

  const handleCopy = () => {
    if (copyBusyRef.current) return
    // Same visible-status discipline as Paste: a denied permission or a
    // non-secure context reads as a broken key otherwise, on exactly the
    // devices this bar exists for. The status reverts after a beat so the
    // key stays usable for a retry.
    const show = (key: CopyStatus) => {
      copyBusyRef.current = false
      if (!aliveRef.current) return
      // On the failure path (the common iOS first-run permission deny) the long
      // remedy label ("Allow clipboard access") shares the 390px row with the
      // still-standing Select stage label ("Line · tap for all") — two
      // truncating labels mute the decision-critical remedy. Collapse the
      // select label back to idle AND reset the stage ref with it: the label
      // and the cycle must move together (a button reading "Select" whose tap
      // runs selectAll() is the exact accidental buffer-wide grab the
      // copy_no_selection rationale warns against). The selection highlight
      // stays; after the failure the next Select tap honestly restarts at
      // stage 1, re-selecting the bottommost line. copy_no_selection/copy_done
      // are guidance/success, not errors — leave the stage alone for those.
      if (isCopyError(key)) {
        setSelectStatus(null)
        selectStageRef.current = null
      }
      showBar({ kind: 'copy', key })
    }
    // Copy the CURRENT selection only. term.getSelection() is '' when nothing
    // is selected — do NOT fall back to the whole buffer: an accidental
    // scrollback-wide copy is a worse surprise on touch than a no-op, so say
    // "select text first" instead.
    const selection = term.getSelection?.() ?? ''
    if (!selection) { show('copy_no_selection'); return }
    // Optional-chain the whole path: `navigator.clipboard` is undefined in
    // non-secure contexts, and `writeText` is missing on engines that ship
    // read-only clipboard support. This failure is PERMANENT for this key
    // (plain-HTTP LAN access), so name the HTTPS remedy rather than a generic
    // "Copy failed" that auto-reverts and reads as a transient glitch.
    //
    // Deliberately NOT utils/clipboard.ts's copyToClipboard: its execCommand
    // fallback creates a textarea and calls ta.select(), which moves focus off
    // the terminal — on the touch devices this bar exists for, that dismisses
    // the on-screen keyboard and collapses the layout mid-interaction, a worse
    // outcome than the named remedy. The fallback would also fold the
    // permission-denied and needs-HTTPS states into one generic failure,
    // losing exactly the actionable remedies this key's review rounds added.
    // (Same reasoning as CliPanel's own direct writeText at its copy path.)
    const write = navigator.clipboard?.writeText?.bind(navigator.clipboard)
    if (!write) { show('copy_needs_https'); return }
    copyBusyRef.current = true
    write(selection)
      .then(() => {
        // Staleness gate mirrors Paste's: a writeText can be held open by an
        // iOS permission callout, and a copy that resolves after the pane
        // went invisible (tab switch, instance switch, pane closed) should
        // not flip the key's status behind the user's back. A missing element
        // (never opened, disposed mid-write) fails closed the same way. No PTY
        // side effect here — copy only touches the clipboard — so this gate
        // governs the visible status, not data safety.
        if (!aliveRef.current || !term.element?.offsetParent) {
          copyBusyRef.current = false
          return
        }
        copyBusyRef.current = false
        // Clear the selection now that its text is safely on the clipboard.
        // Placed AFTER the staleness gate so a copy that resolved on a pane
        // the user has since left never mutates that pane's selection. This is
        // what the Select key's restart-from-bottom design assumes: the next
        // Select tap on this pane starts a fresh selection from the bottom,
        // so a repeat Select→Copy cycle grabs current scrollback, not stale
        // text from the prior copy.
        // SECOND gate: only if the selection is still the one we copied. A
        // writeText can resolve late (iOS permission callout), and the user
        // may have built a NEW selection via Select in the meantime — that
        // newer selection was never copied, so erasing it (and its stage)
        // would silently destroy work the clipboard does not hold. Leave the
        // new selection and its stage label untouched; the "Copied" beat
        // below still reports the completed write of the OLD text.
        if ((term.getSelection?.() ?? '') === selection) {
          term.clearSelection?.()
          // The selection is gone, so the stale stage label no longer describes
          // anything — clear it alongside the ref that tracks the cycle stage. The
          // next Select tap restarts at stage 1. (No timer to clear: the stage
          // label is persistent, cleared by selection changes, not a countdown.)
          setSelectStatus(null)
          selectStageRef.current = null
        }
        // A bare clipboard write is invisible: nothing on screen changes, so
        // on touch the user cannot tell the copy took. Surface a transient
        // "Copied" beat through the SAME status/timer machinery the failure
        // states use — visible on the key and announced from the sibling
        // status region — then auto-revert to idle.
        showBar({ kind: 'copy', key: 'copy_done' })
      })
      // A NotAllowedError is the permission prompt saying no; name the remedy.
      .catch((err: unknown) => {
        const denied = err instanceof DOMException && err.name === 'NotAllowedError'
        show(denied ? 'copy_permission_needed' : 'copy_failed')
      })
  }

  // Select is the SOURCE half of the touch copy story: xterm's drag-selection
  // is a mouse gesture that never fires on touch, so before this key there was
  // no way to CREATE the selection that Copy reads — Copy could only ever say
  // "select text first". Tapping Select builds a selection out of the buffer
  // itself, and runs a STAGED CYCLE — each tap widens the reach and ANNOUNCES
  // the new stage, so the multi-line capability is discoverable (a silent
  // extend was invisible) and an overshoot recovers with one more tap instead
  // of a manual restart:
  //   • Tap 1 (idle): select the bottommost NON-EMPTY buffer line ABOVE the
  //     cursor's row — the line the user most likely wants. A live shell
  //     reprints a fresh prompt after every command and the cursor rests on it,
  //     so the literal bottom line is that prompt, not the output; searching
  //     above the cursor row skips it (falling back to the cursor row when
  //     nothing non-empty sits above it). Announced ("Last line — all").
  //   • Tap 2: select the ENTIRE buffer (term.selectAll) and announce it
  //     ("All selected — restart").
  //   • Tap 3: WRAP AROUND to stage 1 (a fresh bottommost line) — this is the
  //     overshoot recovery.
  //   • Any external clear (a successful Copy, or clearSelection) also drops
  //     the stage back to idle, so the next tap restarts at stage 1.
  //
  // The cycle is a deliberate two stages: last line → whole buffer. An earlier
  // review round added an intermediate "last output block" stage bounded by
  // the first empty-line gap above the selection, but that boundary is fragile
  // — bash/zsh print no blank line between a command's output and the next
  // prompt, so the gap walk usually over-reaches — and it served no named user
  // need distinct from "the last line" and "everything". Removing it (First
  // Principles' subtraction) leaves the two endpoints users actually reach for.
  const handleSelect = () => {
    const buffer = term.buffer?.active
    if (!buffer) return
    // A Select tap owns the status region now: clear ANY live transient —
    // copy status or paste error alike — via the shared bar slot (its single
    // revert timer is cancelled inside showBar, so cleared text cannot
    // resurrect). Guidance like "Tap Select, then Copy" or a paste failure
    // would otherwise stand beside the expanding Select announcement,
    // truncating both at phone width (390px).
    showBar(null)
    // Rows are absolute buffer indices (scrollback + viewport). translateToString(true)
    // trims trailing whitespace so a visually blank row reads as ''.
    const lineText = (row: number): string =>
      buffer.getLine(row)?.translateToString(true).trim() ?? ''

    // A single logical line can span several physical rows when it wraps: xterm
    // flags each continuation row with isWrapped===true (a row marked isWrapped
    // continues the row above). At phone widths (~40 cols) the headline cases —
    // a long error message, a URL — usually wrap, so selecting the found row
    // alone (selectLines(row,row)) would copy only its LAST fragment. Expand the
    // found row through its continuation rows in BOTH directions to the logical
    // line's boundaries, then select the whole span. Same isWrapped access
    // pattern as TerminalCompletion's wrap guard (buffer.getLine(row)?.isWrapped).
    const isWrapped = (row: number): boolean => buffer.getLine(row)?.isWrapped === true
    // Select the LOGICAL line containing `row` via the shared wrap-walk helper
    // (utils/terminalLogicalLine) — the single source of truth the touch
    // range-select hook also uses, so the two can't diverge on wrap semantics.
    const selectLogicalLine = (row: number) => {
      const [top, bottom] = logicalLineBounds(row, buffer.length, isWrapped)
      term.selectLines?.(top, bottom)
    }

    // Surface + announce the reached stage: the compact label renders on the
    // key (persistent for the life of the selection) and is read from the
    // sibling sr-only status region. The stage is recorded so the next tap
    // knows where to advance to. No timer — the label stays until the
    // selection clears (Copy success or an external clear via
    // onSelectionChange), so it never claims a stage the buffer no longer has.
    const announce = (stage: SelectStage) => {
      selectStageRef.current = stage
      setSelectStatus(stage)
    }

    const hasSelection = (term.getSelection?.() ?? '') !== ''
    // An external clear (Copy success, clearSelection) drops the cycle back to
    // idle even if a stale stage lingers in the ref, so a cleared selection
    // always restarts at stage 1.
    const stage = hasSelection ? selectStageRef.current : null

    // Stage 1 (idle, or wraparound from 'all'): the bottommost non-empty line
    // STRICTLY ABOVE the cursor's row. In a live shell a fresh prompt (`$ `) is
    // reprinted after every command and the cursor sits on it, so the literal
    // bottommost non-empty line is that prompt — not the output the user wants
    // to copy (the headline copy-an-error case). Searching only ABOVE the cursor
    // row skips the prompt and lands on the last output line. When no non-empty
    // line exists above the cursor (e.g. a bare prompt with no output yet), fall
    // back to the cursor row itself so the tap is never a no-op on a non-empty
    // buffer.
    const selectBottomLine = (): boolean => {
      // Absolute cursor row = viewport-relative cursorY + the scrollback offset
      // (baseY). Clamp into the buffer in case the stub/terminal omits either.
      const cursorRow = (buffer.baseY ?? 0) + (buffer.cursorY ?? 0)
      // Anchor at what the user can SEE. When the viewport is scrolled up into
      // scrollback (viewportY < baseY), the cursor/prompt sits below the
      // visible area — anchoring there would highlight an off-screen line and
      // Copy would then confirm "Copied" for text the user never saw (UX
      // finding: silent wrong clipboard content on the core scrollback-copy
      // case). Instead anchor at the bottom row of the visible viewport; at
      // the live bottom (viewportY === baseY) this degrades to the cursor-row
      // anchor, keeping the skip-the-prompt behaviour.
      const viewportY = buffer.viewportY ?? buffer.baseY ?? 0
      const scrolledUp = viewportY < (buffer.baseY ?? 0)
      const viewportBottom = viewportY + Math.max((term.rows ?? 1) - 1, 0)
      const anchorRow = scrolledUp
        ? Math.min(viewportBottom, buffer.length - 1)
        : cursorRow
      // Search start: at the live bottom the anchor is the prompt row the
      // cursor rests on — skip the cursor's whole LOGICAL line, not just its
      // last physical row. A long command wraps onto the cursor row, so the
      // rows immediately above it are continuation fragments of the active
      // command; starting at `cursorRow - 1` would land on such a fragment
      // and selectLogicalLine would expand back down through the cursor row,
      // making Copy write the active command instead of prior output (GPT
      // review: wrapped-command grab). Walk to the top of the cursor's
      // logical line first, then search strictly above that. When the cursor
      // row sits BELOW the buffer's rows (alt-screen edge, or a stub without
      // a real prompt row), there is no cursor line inside the buffer to
      // skip — search from the buffer's own bottom row as before. Scrolled
      // up, the anchor is the bottom VISIBLE row, ordinary content the user
      // is looking at — include it, otherwise a populated bottom line is
      // skipped and Copy writes the row above it (GPT review: wrong-text
      // off-by-one).
      let cursorLineTop = cursorRow
      if (cursorRow < buffer.length) {
        cursorLineTop = logicalLineTop(cursorRow, isWrapped)
      }
      const start = Math.min(scrolledUp ? anchorRow : cursorLineTop - 1, buffer.length - 1)
      for (let row = start; row >= 0; row--) {
        if (lineText(row) !== '') {
          selectLogicalLine(row)
          announce('select_line')
          return true
        }
      }
      // Nothing non-empty above the anchor: fall back to the anchor row itself
      // when it has content, so a buffer that only holds the prompt line still
      // selects something rather than no-opping.
      if (anchorRow >= 0 && anchorRow < buffer.length && lineText(anchorRow) !== '') {
        selectLogicalLine(anchorRow)
        announce('select_line')
        return true
      }
      return false // all-empty buffer (above and at anchor): nothing to select
    }

    if (stage === null) {
      selectBottomLine()
      return
    }

    if (stage === 'select_line') {
      // Tap 2: everything.
      term.selectAll?.()
      announce('select_all')
      return
    }

    // stage === 'select_all' → Tap 3: wrap around to a fresh bottommost line.
    selectBottomLine()
  }

  return (
    <div
      data-testid="terminal-key-bar"
      role="toolbar"
      aria-label={i18nT('components.terminalKeyBar.terminal_keys')}
      className="flex shrink-0 items-center gap-1 border-t border-border py-1"
    >
      {/* The key caps scroll in their OWN region (flex-1 min-w-0) so the
          clipboard region stays pinned on-screen: at real phone widths
          (375–393px) the caps + Copy/Paste exceed the viewport, and a toolbar
          that scrolls as a whole hides the clipboard keys in the horizontal
          overflow with no scroll affordance. */}
      <div data-testid="terminal-key-caps" className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">
      {SOFT_KEYS.map(k => {
        const Icon = k.icon
        return (
          <button
            key={k.aria}
            type="button"
            aria-label={k.aria}
            title={k.aria}
            // Keep the press from moving focus off xterm's textarea: a blur closes
            // the on-screen keyboard, so tapping Tab would dismiss the very
            // keyboard the user is typing on.
            onPointerDown={e => e.preventDefault()}
            onClick={() => pressTerminalKey(term, k)}
            className="flex min-w-[2.25rem] shrink-0 items-center justify-center whitespace-nowrap rounded-md border border-border bg-bg-elevated px-2 py-1.5 font-mono text-[13px] text-text active:bg-bg-hover"
          >
            {Icon ? <Icon className="h-4 w-4" aria-hidden="true" /> : k.label}
          </button>
        )
      })}
      </div>
      {/* Separated region (right-aligned, behind a divider): the clipboard
          action group (Copy, Paste), deliberately a DIFFERENT visual group
          from the key-cap row — these are clipboard actions, not key presses,
          and max-two-buttons-per-row's cap is per visual group ("Controls in a
          genuinely different row or a separated region" do not count), so the
          pre-existing key-cap row does not grow.
          Width discipline is CONTAINER-relative, never viewport-relative: the
          terminal can live in a narrow docked pane inside a wide window, so a
          vw cap would let a failure label expand past the pane and be clipped
          by the pane's overflow-hidden. max-w-[70%] caps against the toolbar
          itself and min-w-0 lets the buttons truncate inside it.
          Both action groups (Select, and Copy/Paste) SHRINK: with the short
          stage label on the button ("All · tap to restart" at its longest) the
          Select group is small even in a status state, so it no longer needs to
          refuse shrinking to stay readable. An earlier revision pinned it
          shrink-0 with a wide max-w-[78%] to fit the FULL teaching sentence on
          the button — but that non-shrinking group crowded the min-width
          Copy/Paste controls into CliPanel's overflow clipping at 320px with a
          long localized status (GPT finding). The full sentence now lives in the
          sr-only live region only; the visible short label lets the group
          shrink like its clipboard sibling so neither clips the other. */}
      <div className="flex min-w-0 max-w-[70%] shrink items-center gap-1 border-l border-border pl-2">
        {/* Select is the SOURCE for Copy on touch: it builds a selection out of
            the buffer (a staged cycle — last line → whole buffer → wrap around —
            announced at each stage) because xterm's drag-selection never fires
            on touch. It lives in its OWN separated group (border divider) so
            the clipboard group to its right keeps exactly two actions (Copy,
            Paste) under the max-two-buttons-per-group rule; the flow still reads
            left-to-right: Select, then Copy. */}
        <button
          type="button"
          aria-label={selectStatus ? selectStageShortLabel(selectStatus) : i18nT('components.terminalKeyBar.select')}
          title={selectStatus ? selectStageShortLabel(selectStatus) : i18nT('components.terminalKeyBar.select')}
          // Same focus preservation as the other keys: a blur would dismiss the
          // on-screen keyboard mid-composition.
          onPointerDown={e => e.preventDefault()}
          onClick={handleSelect}
          className="flex min-w-[2.25rem] max-w-full items-center justify-center gap-1 whitespace-nowrap rounded-md border border-border bg-bg-elevated px-2 py-1.5 font-mono text-[13px] text-text active:bg-bg-hover"
        >
          <TextSelect className="h-4 w-4" aria-hidden="true" />
          {/* Always-visible text label: the glyph alone is ambiguous and
              `title` never shows on touch. In a stage state the label becomes
              the SHORT stage string ("Line · tap for all") so the staged cycle
              is discoverable on screen without crowding the Copy/Paste controls
              at phone width — the FULL teaching sentence is carried by the
              button's aria-label and the sr-only live region below. Hidden from
              the accessible name so the button's aria-label is the single
              announced name. */}
          <span aria-hidden="true" className="min-w-0 truncate">
            {selectStatus ? selectStageShortLabel(selectStatus) : i18nT('components.terminalKeyBar.select')}
          </span>
        </button>
        {/* Announcement lives OUTSIDE the button for the same ARIA reason as
            Copy/Paste: role=button is children-presentational, so a nested live
            region is pruned, and the button is never focused. It reads the same
            compact stage label the button shows — the "same status/live-region
            machinery copy_done uses". */}
        <span role="status" aria-live="polite" className="sr-only">
          {selectStatus ? selectStageShortLabel(selectStatus) : ''}
        </span>
      </div>
      {/* Clipboard group proper: exactly two actions (Copy, Paste), divided
          from the Select group by its own border so no visual group exceeds
          two buttons. */}
      <div className="flex min-w-0 max-w-[70%] shrink items-center gap-1 border-l border-border pl-2">
        <button
          type="button"
          aria-label={copyStatus ? copyStatusLabel(copyStatus) : i18nT('components.terminalKeyBar.copy')}
          title={copyStatus ? copyStatusLabel(copyStatus) : i18nT('components.terminalKeyBar.copy')}
          // Same focus preservation as the other keys: a blur would dismiss the
          // on-screen keyboard mid-composition.
          onPointerDown={e => e.preventDefault()}
          onClick={handleCopy}
          className="flex min-w-[2.25rem] max-w-full items-center justify-center gap-1 whitespace-nowrap rounded-md border border-border bg-bg-elevated px-2 py-1.5 font-mono text-[13px] text-text active:bg-bg-hover"
        >
          {/* h-4 w-4 to match the sibling arrow/Paste icons this bar pins. */}
          <Copy className={`h-4 w-4 ${copyStatus && isCopyError(copyStatus) ? 'text-danger' : ''}`} aria-hidden="true" />
          {/* Always-visible text label: the clipboard glyph alone is
              copy/paste-ambiguous and `title` never shows on touch. In a
              status state the label becomes the cause-specific message (not
              color alone). copy_no_selection is guidance and copy_done is
              success, so neither is painted danger. Hidden from the accessible
              name so the sibling status region below is not double-read. */}
          <span aria-hidden="true" className={`min-w-0 truncate ${copyStatus && isCopyError(copyStatus) ? 'text-danger' : ''}`}>
            {copyStatus ? copyStatusLabel(copyStatus) : i18nT('components.terminalKeyBar.copy')}
          </span>
        </button>
        {/* Announcement lives OUTSIDE the button for the same ARIA reason as
            Paste's: role=button is children-presentational, so a nested live
            region is pruned, and the button is never focused. */}
        <span role="status" aria-live="polite" className="sr-only">
          {copyStatus ? copyStatusLabel(copyStatus) : ''}
        </span>
        <button
          type="button"
          aria-label={pasteError ? pasteErrorLabel(pasteError) : i18nT('components.terminalKeyBar.paste')}
          title={pasteError ? pasteErrorLabel(pasteError) : i18nT('components.terminalKeyBar.paste')}
          // Same focus preservation as the other keys: a blur would dismiss the
          // on-screen keyboard mid-composition.
          onPointerDown={e => e.preventDefault()}
          onClick={handlePaste}
          className="flex min-w-[2.25rem] max-w-full items-center justify-center gap-1 whitespace-nowrap rounded-md border border-border bg-bg-elevated px-2 py-1.5 font-mono text-[13px] text-text active:bg-bg-hover"
        >
          {/* h-4 w-4, matching the sibling arrow icons this bar already pins
              (see the icon-sizing test) — `lucide-inline`'s 1em box would draw
              this one icon smaller than its neighbours. */}
          <ClipboardPaste className={`h-4 w-4 ${pasteError ? 'text-danger' : ''}`} aria-hidden="true" />
          {/* Always-visible text label: the clipboard glyph alone is
              copy/paste-ambiguous and `title` never shows on touch. In a
              failure state the label becomes the cause-specific message (not
              color alone). Hidden from the accessible name so the sibling
              status region below is not double-read. */}
          <span aria-hidden="true" className={`min-w-0 truncate ${pasteError ? 'text-danger' : ''}`}>
            {pasteError ? pasteErrorLabel(pasteError) : i18nT('components.terminalKeyBar.paste')}
          </span>
        </button>
        {/* Announcement lives OUTSIDE the button: role=button is
            children-presentational in ARIA, so a live region nested inside it
            gets pruned and never fires — and the button is never focused
            (pointerdown is cancelled to keep the on-screen keyboard up), so a
            swapped aria-label would not be re-announced either. */}
        <span role="status" aria-live="polite" className="sr-only">
          {pasteError ? pasteErrorLabel(pasteError) : ''}
        </span>
      </div>
    </div>
  )
}
