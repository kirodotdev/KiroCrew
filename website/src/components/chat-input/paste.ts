import { useCallback, useEffect, useRef, useState } from 'react'
import type { PasteHoverHandle } from '../PasteHoverLayer'
import { clipboardFiles, hasPlainClipboardText, stripTrailingBlankLines } from '../composerPastePolicy'
import type { useImeGuard } from '../../hooks/useImeGuard'
import { isTouchDevice } from '../../utils/isTouchDevice'
import {
  type PasteBlock,
  shouldCollapse as shouldCollapsePaste,
  countLines,
  makePasteId,
  formatToken,
  tokenRangeAt,
  pruneBlocks,
  nextSeq,
  findTokenRanges,
} from '../../utils/pasteTokens'

/* Collapsed-paste tokens and the clipboard: a big paste becomes a
   `[ Paste #N · M lines ]` chip backed by a PasteBlock, the chip moves and
   deletes as one unit, copy and cut hand out the expanded text, and a file
   on the clipboard (or from the picker) goes to the upload path. */
/** True when the text on the caret's line, before the caret, is ONLY markdown
 *  blockquote markers — `>`, `> > `, optionally indented. A collapsed-paste
 *  chip then flows on that line (`> [ Paste #1 · N lines ]`) instead of being
 *  forced onto its own line, which strands the `>` above the chip and makes
 *  the user delete the injected newline to quote a paste. Whitespace alone
 *  (no `>`) is NOT a quote prefix — the own-line shape stays for those.
 *  Linear scan, no regex. */
function isBlockquotePrefix(linePrefix: string): boolean {
  let sawMarker = false
  for (let i = 0; i < linePrefix.length; i++) {
    const c = linePrefix.charCodeAt(i)
    if (c === 62 /* > */) { sawMarker = true; continue }
    if (c === 32 /* space */ || c === 9 /* \t */) continue
    return false
  }
  return sawMarker
}

export function usePasteTokens({ value, onChange, pasteBlocks, onPasteBlocksChange, showFullPastes, onUploadFiles, inputRef, valueRef, valueFromUserRef, recordCaret, ime }: {
  value: string
  onChange: (v: string) => void
  pasteBlocks: PasteBlock[]
  onPasteBlocksChange?: (next: PasteBlock[]) => void
  showFullPastes: boolean
  onUploadFiles?: (files: File[]) => void
  inputRef: React.RefObject<HTMLTextAreaElement>
  valueRef: React.MutableRefObject<string>
  valueFromUserRef: React.MutableRefObject<boolean>
  recordCaret: () => void
  ime: ReturnType<typeof useImeGuard>
}) {
  // Backdrop mirror that paints chip backgrounds behind paste tokens; its scroll
  // is kept in lockstep with the textarea (see syncMirrorScroll on the textarea).
  const mirrorRef = useRef<HTMLDivElement>(null)
  // Hover detection layer that shows paste previews on mouseover; scroll-synced
  // identically to the backdrop mirror.
  const hoverRef = useRef<PasteHoverHandle>(null)
  // Id of the open paste-preview tooltip (or null). Wired to the textarea's
  // aria-describedby so keyboard/screen-reader users get the preview announced
  // when the caret enters a token — the AT half of the paste-preview a11y fix.
  const [pastePreviewPanelId, setPastePreviewPanelId] = useState<string | null>(null)
  // True for the next paste only when the user pressed Cmd/Ctrl+Shift+V, so
  // handlePaste inserts the full text inline instead of collapsing it to a
  // `[ Paste #N ]` chip. Set on that keydown, cleared on any other keydown.
  const rawPasteRef = useRef(false)

  /** The paste-token keys: a token deletes, and the caret and selection step,
   *  as one unit. True when the key was consumed here; a word, line or
   *  document jump is left to the browser and only snapped afterwards. */
  const handleTokenKey = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Atomic paste-token handling — keep caret out of token interior and
    // treat tokens as single deletable units. Runs before Enter/history so
    // edits on or around a token never reach the default textarea handling.
    if (pasteBlocks.length && !ime.isComposing(e)) {
      const ta = e.currentTarget
      const v = valueRef.current
      const ss = ta.selectionStart ?? 0
      const se = ta.selectionEnd ?? 0
      const isCollapsed = ss === se
      const ranges = findTokenRanges(v, pasteBlocks)

      const removeBlockAtom = (r: { start: number; end: number; block: PasteBlock }) => {
        e.preventDefault()
        const next = v.slice(0, r.start) + v.slice(r.end)
        onChange(next)
        onPasteBlocksChange?.(pasteBlocks.filter(b => b.id !== r.block.id))
        requestAnimationFrame(() => {
          const el = inputRef.current
          if (el) el.setSelectionRange(r.start, r.start)
        })
      }

      // Backspace with caret just past a token → delete whole token
      if (e.key === 'Backspace' && isCollapsed && !e.metaKey && !e.ctrlKey && !e.altKey) {
        const adj = ranges.find(r => r.end === ss)
        if (adj) { removeBlockAtom(adj); return true }
      }
      // Cmd+Backspace (line-back delete on Mac) — extend deletion to cover
      // any token that intersects the caret-to-line-start range, so we never
      // slice a token mid-text. Also drops the associated PasteBlock(s).
      if (e.key === 'Backspace' && isCollapsed && e.metaKey) {
        const lineStart = v.lastIndexOf('\n', ss - 1) + 1
        const intersecting = ranges.filter(r => r.start < ss && r.end > lineStart)
        if (intersecting.length) {
          e.preventDefault()
          const deleteStart = Math.min(lineStart, ...intersecting.map(r => r.start))
          const removedIds = new Set(
            ranges.filter(r => r.start >= deleteStart && r.end <= ss).map(r => r.block.id),
          )
          const next = v.slice(0, deleteStart) + v.slice(ss)
          onChange(next)
          onPasteBlocksChange?.(pasteBlocks.filter(b => !removedIds.has(b.id)))
          requestAnimationFrame(() => {
            const el = inputRef.current
            if (el) el.setSelectionRange(deleteStart, deleteStart)
          })
          return true
        }
      }
      // Alt/Ctrl+Backspace (word-back delete) — if caret is adjacent to a
      // token, treat as full-token delete (same as plain Backspace). Beyond
      // that, we leave native behavior alone; word boundaries are fuzzy and
      // tokens are on their own line, so the common case is the adjacent one.
      if (e.key === 'Backspace' && isCollapsed && (e.altKey || e.ctrlKey) && !e.metaKey) {
        const adj = ranges.find(r => r.end === ss)
        if (adj) { removeBlockAtom(adj); return true }
      }
      // Delete with caret just before a token → delete whole token
      if (e.key === 'Delete' && isCollapsed && !e.metaKey && !e.ctrlKey && !e.altKey) {
        const adj = ranges.find(r => r.start === ss)
        if (adj) { removeBlockAtom(adj); return true }
      }
      // Cmd+Delete (forward line-delete on Mac) — mirror Cmd+Backspace in
      // the forward direction: extend deletion to cover intersecting tokens.
      if (e.key === 'Delete' && isCollapsed && e.metaKey) {
        const nextNl = v.indexOf('\n', ss)
        const lineEnd = nextNl === -1 ? v.length : nextNl
        const intersecting = ranges.filter(r => r.end > ss && r.start < lineEnd)
        if (intersecting.length) {
          e.preventDefault()
          const deleteEnd = Math.max(lineEnd, ...intersecting.map(r => r.end))
          const removedIds = new Set(
            ranges.filter(r => r.start >= ss && r.end <= deleteEnd).map(r => r.block.id),
          )
          const next = v.slice(0, ss) + v.slice(deleteEnd)
          onChange(next)
          onPasteBlocksChange?.(pasteBlocks.filter(b => !removedIds.has(b.id)))
          requestAnimationFrame(() => {
            const el = inputRef.current
            if (el) el.setSelectionRange(ss, ss)
          })
          return true
        }
      }
      // Alt/Ctrl+Delete (word-forward delete) — adjacent-token atomic delete.
      if (e.key === 'Delete' && isCollapsed && (e.altKey || e.ctrlKey) && !e.metaKey) {
        const adj = ranges.find(r => r.start === ss)
        if (adj) { removeBlockAtom(adj); return true }
      }
      // Arrow left/right — skip over token as if it were a single character
      if (e.key === 'ArrowLeft' && isCollapsed && !e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey) {
        const adj = ranges.find(r => r.end === ss)
        if (adj) {
          e.preventDefault()
          requestAnimationFrame(() => inputRef.current?.setSelectionRange(adj.start, adj.start))
          return true
        }
      }
      if (e.key === 'ArrowRight' && isCollapsed && !e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey) {
        const adj = ranges.find(r => r.start === ss)
        if (adj) {
          e.preventDefault()
          requestAnimationFrame(() => inputRef.current?.setSelectionRange(adj.end, adj.end))
          return true
        }
      }
      // Shift+Arrow — extend selection past the whole token in one step
      if (e.key === 'ArrowLeft' && e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey) {
        const dir = ta.selectionDirection || 'forward'
        const active = dir === 'backward' ? ss : se
        const adj = ranges.find(r => r.end === active)
        if (adj) {
          e.preventDefault()
          requestAnimationFrame(() => {
            const el = inputRef.current; if (!el) return
            if (dir === 'backward') el.setSelectionRange(adj.start, se, 'backward')
            else el.setSelectionRange(ss, adj.start, ss <= adj.start ? 'forward' : 'backward')
          })
          return true
        }
      }
      if (e.key === 'ArrowRight' && e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey) {
        const dir = ta.selectionDirection || 'forward'
        const active = dir === 'backward' ? ss : se
        const adj = ranges.find(r => r.start === active)
        if (adj) {
          e.preventDefault()
          requestAnimationFrame(() => {
            const el = inputRef.current; if (!el) return
            if (dir === 'backward') el.setSelectionRange(adj.end, se, adj.end <= se ? 'backward' : 'forward')
            else el.setSelectionRange(ss, adj.end, 'forward')
          })
          return true
        }
      }

      // Post-keydown snap for word/line/document-jump shortcuts
      // (Alt+Arrow on Mac, Ctrl+Arrow on Win/Linux, Cmd+Arrow line jump, Home/End).
      // The browser performs the native jump; we check afterwards if caret or
      // selection endpoint landed strictly inside a token and snap it out in
      // the direction of motion.
      const isNavKey = e.key === 'ArrowLeft' || e.key === 'ArrowRight' || e.key === 'Home' || e.key === 'End'
      const hasNavModifier = e.altKey || e.ctrlKey || e.metaKey || e.key === 'Home' || e.key === 'End'
      if (isNavKey && hasNavModifier) {
        const leftward = e.key === 'ArrowLeft' || e.key === 'Home'
        requestAnimationFrame(() => {
          const el = inputRef.current; if (!el) return
          const freshRanges = findTokenRanges(el.value, pasteBlocks)
          if (!freshRanges.length) return
          const nss = el.selectionStart ?? 0
          const nse = el.selectionEnd ?? 0
          const snapPos = (p: number) => {
            for (const r of freshRanges) {
              if (p > r.start && p < r.end) return leftward ? r.start : r.end
            }
            return p
          }
          const a = snapPos(nss)
          const b = snapPos(nse)
          if (a === nss && b === nse) return
          const dir = el.selectionDirection || 'forward'
          el.setSelectionRange(Math.min(a, b), Math.max(a, b), dir as 'forward' | 'backward' | 'none')
        })
      }
    }
    return false
  }, [pasteBlocks, onChange, onPasteBlocksChange, ime, valueRef, inputRef])

  /** Intercept clipboard paste — files go to upload path, big text gets collapsed into a token. */
  const handlePaste = useCallback((e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    // Cmd/Ctrl+Shift+V bypass: consume the one-shot flag up front, before any
    // early return below, so it can never leak into a later paste (e.g. a
    // context-menu paste with no intervening keydown to clear it).
    const forceRaw = rawPasteRef.current
    rawPasteRef.current = false
    // File paste takes precedence — but not when text is also insertable. Only
    // text/plain defers: a <textarea> can only ever insert the text/plain
    // representation, so when the clipboard carries text/html WITHOUT
    // text/plain (a browser's "Copy Image", an Office chart copy) deferring
    // would make the whole paste a silent no-op — there is no text to insert.
    // macOS Office TEXT copies do include text/plain alongside their junk
    // image rendering of the selection, so real text pastes still win over
    // the image (see ChatInput.paste.test.tsx).
    const hasText = hasPlainClipboardText(e.clipboardData)
    const files = clipboardFiles(e.clipboardData)
    if (files.length && onUploadFiles && !hasText) {
      e.preventDefault()
      onUploadFiles(files)
      return
    }
    // Text paste. Sources that serialize rendered HTML (web pages, PDFs, chat
    // bubbles, table cells) routinely tack trailing blank lines onto a copied
    // "single line", and a <textarea> inserts them verbatim — so the paste shows
    // the line followed by several empty rows. Strip a trailing run of blank
    // lines up front (only whitespace runs that include a newline; a paste
    // ending in plain spaces and interior blank lines are untouched). Raw paste
    // (Cmd/Ctrl+Shift+V) opts out entirely.
    const pasted = e.clipboardData.getData('text')
    const cleaned = forceRaw ? pasted : stripTrailingBlankLines(pasted)

    const ta = e.currentTarget
    const start = ta.selectionStart ?? value.length
    const end = ta.selectionEnd ?? start
    const before = value.slice(0, start)
    const after = value.slice(end)

    // Big paste → collapse into a `[ Paste #N ]` chip. Uses the cleaned text so
    // the chip's line count and stored content exclude the stripped blanks.
    // `showFullPastes` opts out for every paste, the same way forceRaw opts out
    // for one; the paste then falls through to the plain-insert path below.
    if (onPasteBlocksChange && !forceRaw && !showFullPastes && shouldCollapsePaste(cleaned)) {
      e.preventDefault()
      const block: PasteBlock = { id: makePasteId(), seq: nextSeq(pasteBlocks), lines: countLines(cleaned), content: cleaned }
      const token = formatToken(block)
      // Surround the token with newlines so the chip lives on its own line —
      // long-form pasted content rarely flows with typed text around it.
      // Skip the leading newline when the caret is at the start of a line,
      // and the trailing one when the caret is at the end of a line. Also
      // skip the leading one when everything before the caret on its line is
      // a bare blockquote prefix (`> `, `> > `, optionally indented): the
      // user is quoting the paste, and forcing the chip down a line strands
      // the `>` above it.
      const linePrefix = before.slice(before.lastIndexOf('\n') + 1)
      const leadingNewline = before && !before.endsWith('\n') && !isBlockquotePrefix(linePrefix) ? '\n' : ''
      const trailingNewline = after && !after.startsWith('\n') ? '\n' : ''
      const insert = leadingNewline + token + trailingNewline
      valueFromUserRef.current = true // a paste is a real user edit, not a draft restore
      onChange(before + insert + after)
      onPasteBlocksChange([...pasteBlocks, block])
      // Restore caret right after the inserted token + trailing newline.
      requestAnimationFrame(() => {
        if (ta && document.activeElement === ta) {
          const pos = before.length + insert.length
          ta.setSelectionRange(pos, pos)
        }
      })
      return
    }

    // Small paste. Only intercept when trailing blanks were actually stripped
    // AND something remains — an all-blank clipboard (cleaned === '') is left to
    // the browser so the paste is never a silent no-op.
    if (cleaned !== pasted && cleaned !== '') {
      e.preventDefault()
      const next = before + cleaned + after
      // Insert through the native input path so the textarea's own onChange runs:
      // that fires the /, @, $ picker detection, marks the edit user-driven, and
      // keeps native undo. Fall back to a controlled-value splice where
      // execCommand is unavailable (jsdom/tests) or reports failure.
      let inserted = false
      try {
        inserted = typeof document.execCommand === 'function' && document.execCommand('insertText', false, cleaned)
      } catch { inserted = false }
      // That boolean is not evidence on its own. iOS Safari's native paste
      // callout reports success on a <textarea> and can leave the field
      // untouched, and this branch has ALREADY called preventDefault() — so
      // trusting the return value drops the paste with no visible trace at all.
      // Read the DOM back instead, and reconcile the controlled value either
      // way: after a real insert this is the same string the textarea's own
      // onChange already pushed up (React bails), while an insert React never
      // saw would otherwise be reverted to the stale `value` prop on the next
      // render — the same silent vanish by a different route.
      const nativeOk = inserted && ta.value === next
      valueFromUserRef.current = true
      onChange(next)
      if (nativeOk) return // the native insert placed the caret itself
      requestAnimationFrame(() => {
        if (ta && document.activeElement === ta) {
          const pos = before.length + cleaned.length
          ta.setSelectionRange(pos, pos)
        }
      })
    }
  }, [onUploadFiles, onPasteBlocksChange, pasteBlocks, value, onChange, showFullPastes, valueFromUserRef])

  /** Replace a collapsed-paste token with its full content in the textarea and
   *  drop the backing block. The caret lands just past the inserted content. */
  const expandTokenRange = useCallback((range: { start: number; end: number; block: PasteBlock }) => {
    const expanded = value.slice(0, range.start) + range.block.content + value.slice(range.end)
    onChange(expanded)
    onPasteBlocksChange?.(pasteBlocks.filter(b => b.id !== range.block.id))
    requestAnimationFrame(() => {
      const ta = inputRef.current
      if (ta) {
        const pos = range.start + range.block.content.length
        ta.setSelectionRange(pos, pos)
        ta.focus()
      }
    })
  }, [value, pasteBlocks, onPasteBlocksChange, onChange, inputRef])

  /** Click/tap on a collapsed-paste token expands it to the original full
   *  content in the textarea.
   *
   *  Two gestures reach expansion, because a single gesture cannot serve both
   *  pointer classes:
   *   - Mouse: a two-step click — 1st click (detail=1) selects the token as a
   *     range (visual highlight), a quick 2nd click (detail>=2, the browser's
   *     own double-click) expands. `event.detail` is the click count the
   *     browser computes with its double-click timing, so no ref/selection
   *     tracking is needed and Chrome/Electron/Safari/Firefox all agree.
   *   - Touch: a single tap expands. Two discrete taps never coalesce into a
   *     `detail>=2` click the way mouse clicks do, so the double-click path is
   *     unreachable under a finger; gating expansion on it left the token only
   *     ever selectable on touch, never openable. A tap matches the sent-bubble
   *     PastedChip, which is a real <button> that toggles on one tap. */
  const handleTextareaClick = useCallback((e: React.MouseEvent<HTMLTextAreaElement>) => {
    if (!onPasteBlocksChange || !pasteBlocks.length) return
    const ta = e.currentTarget
    const caret = ta.selectionStart ?? 0
    const range = tokenRangeAt(value, pasteBlocks, caret)
    if (!range) return

    // Touch has no double-click to reach the expand branch below, so the first
    // tap inside a token expands directly — the select-first step is a
    // mouse-only refinement.
    if (isTouchDevice()) { expandTokenRange(range); return }

    if (e.detail < 2) {
      // First click in a (potential) sequence — highlight the token as an
      // atomic range. If the user doesn't click again within the browser's
      // double-click window, nothing else happens.
      requestAnimationFrame(() => {
        const el = inputRef.current
        if (el) el.setSelectionRange(range.start, range.end)
      })
      return
    }

    // e.detail >= 2 — second (or more) click in a rapid sequence on the
    // same region — expand.
    expandTokenRange(range)
  }, [value, pasteBlocks, onPasteBlocksChange, expandTokenRange, inputRef])

  /** Snap selection endpoints that land inside a token range to the nearest edge.
   *  Covers drag-select that ends mid-token, touch/long-press handles on mobile,
   *  and any other non-keyboard way selection could split a token. */
  const handleSelectSnap = useCallback(() => {
    recordCaret()
    if (!pasteBlocks.length) return
    const ta = inputRef.current
    if (!ta) return
    const ss = ta.selectionStart ?? 0
    const se = ta.selectionEnd ?? 0
    // Keyboard/AT peek: a collapsed caret landing inside a token opens the
    // preview (the handle no-ops for a non-collapsed selection).
    hoverRef.current?.handleCaret(ss, se)
    // Collapsed caret inside a token is handled by the click expander — skip.
    if (ss === se) return
    const ranges = findTokenRanges(ta.value, pasteBlocks)
    if (!ranges.length) return
    const snap = (pos: number) => {
      for (const r of ranges) {
        if (pos > r.start && pos < r.end) {
          // Snap to the nearer edge (ties go to the start).
          return pos - r.start <= r.end - pos ? r.start : r.end
        }
      }
      return pos
    }
    const newSs = snap(ss)
    const newSe = snap(se)
    if (newSs === ss && newSe === se) return
    const dir = ta.selectionDirection || 'forward'
    ta.setSelectionRange(Math.min(newSs, newSe), Math.max(newSs, newSe), dir as 'forward' | 'backward' | 'none')
  }, [pasteBlocks, recordCaret, inputRef])

  /** Prune paste blocks whose token was deleted from the textarea. */
  useEffect(() => {
    if (!onPasteBlocksChange || !pasteBlocks.length) return
    const pruned = pruneBlocks(value, pasteBlocks)
    if (pruned !== pasteBlocks) onPasteBlocksChange(pruned)
  }, [value, pasteBlocks, onPasteBlocksChange])

  /** Copy/cut that spans one or more collapsed-paste tokens writes the
   *  expanded content to the clipboard instead of the literal token text.
   *  Without this, pasting elsewhere yields "[ Paste #1 · 5 lines ]"
   *  zombie strings that look like chips but have no backing block. Only
   *  tokens *fully* covered by the selection are expanded; partial overlaps
   *  fall back to the literal slice (rare — drag-select snaps to token
   *  edges via handleSelectSnap). */
  const expandSelectionForClipboard = useCallback(
    (start: number, end: number): string | null => {
      if (!pasteBlocks.length || start === end) return null
      const ranges = findTokenRanges(value, pasteBlocks)
      const covered = ranges.filter(r => r.start >= start && r.end <= end)
      if (!covered.length) return null
      let out = ''
      let cursor = start
      for (const r of covered) {
        out += value.slice(cursor, r.start)
        out += r.block.content
        cursor = r.end
      }
      out += value.slice(cursor, end)
      return out
    },
    [value, pasteBlocks],
  )

  const handleCopy = useCallback((e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const ta = e.currentTarget
    const expanded = expandSelectionForClipboard(ta.selectionStart ?? 0, ta.selectionEnd ?? 0)
    if (expanded === null) return
    e.clipboardData.setData('text/plain', expanded)
    e.preventDefault()
  }, [expandSelectionForClipboard])

  const handleCut = useCallback((e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const ta = e.currentTarget
    const start = ta.selectionStart ?? 0
    const end = ta.selectionEnd ?? 0
    const expanded = expandSelectionForClipboard(start, end)
    if (expanded === null) return
    e.clipboardData.setData('text/plain', expanded)
    // Manually excise the selection from the textarea; the pruneBlocks
    // effect above will drop any blocks whose token text was removed.
    const nextValue = value.slice(0, start) + value.slice(end)
    onChange(nextValue)
    requestAnimationFrame(() => {
      if (ta) ta.setSelectionRange(start, start)
    })
    e.preventDefault()
  }, [expandSelectionForClipboard, value, onChange])

  const handleFileInputChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || [])
    if (files.length && onUploadFiles) onUploadFiles(files)
    e.target.value = '' // reset so same file can be re-selected
  }, [onUploadFiles])

  return {
    mirrorRef, hoverRef, pastePreviewPanelId, setPastePreviewPanelId, rawPasteRef,
    handleTokenKey, handlePaste, handleTextareaClick, handleSelectSnap, handleCopy, handleCut, handleFileInputChange,
  }
}
