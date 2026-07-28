import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { Terminal } from '@xterm/xterm'
import { Folder, File as FileIcon, CornerDownLeft } from 'lucide-react'
import {
  extractToken, commandStart, commandWord, shouldComplete, foldersOnly, atWordEnd,
  isPlainWord, isSafeName, commonPrefix, extendsWord, buildInsertion, acceptSuffix,
  unescapeWord,
} from '../utils/terminalCompletion'
import { sendRawToTerminalSession } from '../utils/terminalRegistry'

import { i18nT } from '../i18n/t'
/** Debounce between the last keystroke and the completion request. Short enough
 *  to feel instant, long enough that holding a key down issues one request. */
const DEBOUNCE_MS = 70
/** Rows shown before the list scrolls. */
const VISIBLE_ROWS = 8
const ROW_H = 22
/** Prompt-marker rows retained. Bounded so a long session cannot grow the map
 *  without limit; only the cursor's own row is ever read. */
const MARKER_LIMIT = 64
/** Grace window after `compositionend` in which Enter still belongs to the IME.
 *  Browsers disagree on whether the committing Enter is flagged as composing. */
const IME_GRACE_MS = 60

interface Entry {
  name: string
  dir: boolean
  /** Offset the typed fragment was matched at, for highlighting. */
  at?: number
  /** The synthetic "use the directory as typed" row — not a real dir entry. */
  here?: true
}

/**
 * An entry name with the matched fragment emphasised.
 *
 * Matching is a substring search, so the fragment can sit anywhere in the name
 * (`termi` inside `KiroCrew-terminal-completion`). Showing WHERE it matched is
 * what makes a non-prefix hit legible instead of looking arbitrary.
 */
function Matched({ name, at, len }: { name: string; at?: number; len: number }) {
  if (at == null || len === 0 || at + len > name.length) return <>{name}</>
  return (
    <>
      {name.slice(0, at)}
      <span className="text-text-strong underline decoration-accent">
        {name.slice(at, at + len)}
      </span>
      {name.slice(at + len)}
    </>
  )
}

interface Suggestions {
  entries: Entry[]
  /** The typed name prefix these entries matched (the part already on screen). */
  prefix: string
  /** The decoded shell word being completed — the key Escape suppresses. */
  token: string
  /** The same word as the terminal shows it, escapes intact — what an insertion
   *  is measured against, since that is the text a DEL would erase. */
  raw: string
  /** Absolute directory the entries came from — shown in the description bar. */
  dir: string
  truncated: boolean
  /** Column where the completed token starts, for anchoring the menu. */
  col: number
  /** Cursor row within the viewport, for anchoring the menu. */
  row: number
}

/** A debounced listing request: the query's inputs plus where to anchor its menu. */
interface Request {
  token: string
  raw: string
  col: number
  row: number
  foldersOnly: boolean
}

function sameRequest(a: Request, b: Request): boolean {
  return a.token === b.token && a.raw === b.raw && a.col === b.col
    && a.row === b.row && a.foldersOnly === b.foldersOnly
}

/** The listing route's response, validated field by field before use. */
interface CompleteResponse {
  entries?: unknown
  prefix?: unknown
  dir?: unknown
  truncated?: unknown
}

/**
 * Inline path completion for a web terminal pane.
 *
 * Renders an absolutely-positioned menu inside the (relative) terminal wrapper,
 * driven entirely from xterm's own screen buffer — the current row is read back
 * and the word under the cursor is completed against the PTY's live cwd. There
 * is no keystroke mirror to drift out of sync with the shell's line editor.
 *
 * Prompt boundaries come from OSC 133 (`B`) or OSC 697 (`NewCmd`/`EndPrompt`)
 * when the user's shell integration emits them, and from a prompt-terminator
 * heuristic otherwise; `commandStart` documents why the fallback being wrong is
 * benign.
 */
export default function TerminalCompletion({ term, sessionId, active }: {
  term: Terminal
  sessionId: string
  /** False while the pane is hidden — no polling, no menu. */
  active: boolean
}) {
  const [sug, setSug] = useState<Suggestions | null>(null)
  /** The word the listing query is currently keyed on — `null` while idle. */
  const [req, setReq] = useState<Request | null>(null)
  const [selected, setSelected] = useState(0)
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  /** Absolute buffer row → column at which the shell's command line begins. */
  const markers = useRef(new Map<number, number>())
  /** Word whose menu is suppressed until the word changes — set by Escape, by
   *  the "use this folder" row, and after a file completion ends the word. */
  const dismissed = useRef<string | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  /** When the last IME composition finished — see `IME_GRACE_MS`. */
  const imeEndAt = useRef(0)
  // Key handling reads the live suggestion list; a ref keeps xterm's single
  // custom-key-handler slot from being re-attached on every state change.
  const stateRef = useRef<{ sug: Suggestions | null; selected: number }>({ sug: null, selected: 0 })
  stateRef.current = { sug, selected }

  const close = useCallback(() => {
    // The debounce timer dies with the menu: a pending run would re-open it for
    // whatever word is on screen when it fires, silently undoing an Escape.
    if (timer.current) { clearTimeout(timer.current); timer.current = null }
    setSug(null)
    setSelected(0)
  }, [])

  /* ── Prompt markers from shell integration ── */
  useEffect(() => {
    const record = () => {
      const buf = term.buffer.active
      const map = markers.current
      map.set(buf.baseY + buf.cursorY, buf.cursorX)
      if (map.size > MARKER_LIMIT) {
        // Drop the oldest rows; insertion order is chronological.
        for (const k of [...map.keys()].slice(0, map.size - MARKER_LIMIT)) map.delete(k)
      }
    }
    // OSC 133;B — "prompt ended, command input starts here" (the de-facto
    // standard, emitted by VS Code / WezTerm / starship shell integrations).
    const osc133 = term.parser.registerOscHandler(133, (data) => {
      if (data === 'B' || data.startsWith('B;')) record()
      return true
    })
    // OSC 697 — kiro-cli's (Fig-lineage) integration; NewCmd/EndPrompt mark the
    // same boundary.
    const osc697 = term.parser.registerOscHandler(697, (data) => {
      if (data === 'EndPrompt' || data.startsWith('NewCmd')) record()
      return true
    })
    return () => { osc133.dispose(); osc697.dispose() }
  }, [term])

  /* ── Reading the shell word out of the screen buffer ── */
  /**
   * The word under the cursor, or `null` when this row is one V1 refuses to
   * reason about. Every refusal below turns a *plausible but wrong* completion
   * into no completion, which is the only honest outcome for a completer that
   * reads the screen rather than the shell's line editor.
   *
   * Used by the trigger AND by acceptance, so a menu is only ever acted on
   * while the buffer still satisfies the conditions that opened it.
   */
  const readWord = useCallback((): {
    token: string; raw: string; start: number; command: string
  } | null => {
    const buf = term.buffer.active
    // vim/less/htop draw on the alternate buffer, where the cursor sweeps over
    // arbitrary text: a redrawn line such as `> cd ./src` satisfies the prompt
    // heuristic, and an open menu would then steal Escape/Enter/Tab/arrows from
    // the TUI. There is no shell line to complete here at all.
    if (buf.type === 'alternate') return null
    const row = buf.baseY + buf.cursorY
    const bufLine = buf.getLine(row)
    if (!bufLine) return null
    // `translateToString` returns ONE physical row while `cursorX` counts cells,
    // so the string index and the cursor column only agree on an unwrapped row
    // whose cells are one column wide AND one code unit long. A continuation row
    // has lost the start of the word; the cell shapes below shift every later
    // column — all of them yield a wrong word rather than a missing one.
    if (bufLine.isWrapped) return null
    const cursorX = Math.min(buf.cursorX, bufLine.length)
    for (let x = 0; x < cursorX; x += 1) {
      const cell = bufLine.getCell(x)
      // A double-width (CJK) cell or its zero-width spacer.
      if ((cell?.getWidth() ?? 1) !== 1) return null
      // One column, several code units: a base character plus combining marks
      // (`e` + U+0301) or a supplementary character. Recovering the mapping would
      // mean rebuilding the row cell by cell and re-deriving every column index;
      // refusing costs a rare row its menu and never mistypes a path.
      if ((cell?.getChars() ?? '').length > 1) return null
    }
    // No trim: the cursor can sit past the last non-space character (`ls ⎸`),
    // and a trimmed row would make that look like the previous word.
    const line = bufLine.translateToString(false)
    // Mid-word the chosen name would be inserted in front of the surviving
    // suffix (`cd do⎸cs` → `cd docs/cs`), so only a word-final cursor completes.
    if (!atWordEnd(line, cursorX)) return null
    const { token: raw, start } = extractToken(line, cursorX)
    const marker = markers.current.get(row)
    if (!isPlainWord(line, commandStart(line, start, marker), start, raw)) return null
    // The request carries the decoded name (`my dir/`), the insertion logic the
    // on-screen form (`my\ dir/`).
    return { token: unescapeWord(raw), raw, start, command: commandWord(line, start, marker) }
  }, [term])

  /* ── The listing query ── */
  // The completed word is part of the key, so the next keystroke supersedes this
  // query: React Query drops the previous one and aborts the `signal` handed to
  // its fetch with it, which is what keeps a slow listing from answering for a
  // word that is no longer on screen.
  const { data, isError } = useQuery({
    queryKey: ['terminal-completions', sessionId, req?.token ?? '', req?.foldersOnly ?? false],
    enabled: active && req != null,
    // A directory listing is only true for the instant it was read, and this one
    // is re-read per keystroke: a remembered answer would offer names that may
    // already be gone, so nothing is ever served from cache and nothing is kept.
    staleTime: 0,
    gcTime: 0,
    retry: false,
    refetchOnWindowFocus: false,
    queryFn: async ({ signal }): Promise<CompleteResponse | null> => {
      if (!req) return null
      const r = await fetch('/api/terminal/complete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          token: req.token,
          folders_only: req.foldersOnly,
        }),
        signal,
      })
      if (!r.ok) throw new Error(`terminal completion failed: ${r.status}`)
      return await r.json() as CompleteResponse
    },
  })

  /* ── Turn a listing into the menu ── */
  // Held in state rather than derived, so the menu that is on screen keeps its
  // (still-accurate) entries while the next word's listing is in flight instead
  // of blinking out on every keystroke. Entries are only ever paired with the
  // word they were read for — `stale()` re-checks that at acceptance time.
  useEffect(() => {
    if (!req) return
    // A failure leaves nothing to accept, so the menu must not stay open showing
    // the previous word's entries.
    if (isError) { close(); return }
    if (!data) return
    // React Query owns the request lifecycle now, so a reply can still land after
    // Escape (or the stop-here row) closed the menu for this word.
    if (dismissed.current === req.token) return
    const listed: Entry[] = (Array.isArray(data.entries) ? data.entries : [])
      // Names are typed into the PTY verbatim on acceptance, so a name carrying a
      // control character is dropped at the door: it can never be offered,
      // highlighted, or dragged into Tab's common prefix.
      .filter((e: Entry) => typeof e?.name === 'string' && isSafeName(e.name))
    if (listed.length === 0) { close(); return }
    // A word that ends in `/` already names a complete directory, so the user may
    // well be done — the first row confirms THAT directory instead of forcing a
    // descent into a child. Without it, accepting a folder immediately re-opens on
    // its contents and there is no way to stop at the level you just chose.
    // Offered for any path command: for `cd` it is the destination, for
    // `ls`/`du`/`chmod` the directory is itself a valid argument. (kiro-cli
    // synthesises the same row, labelled "Enter the current directory", but only
    // for `cd`-style commands — that leaves `ls foo/` with no way out.)
    const entries: Entry[] = req.token.endsWith('/')
      ? [{ name: '', dir: true, here: true }, ...listed]
      : listed
    setSug({
      entries,
      prefix: typeof data.prefix === 'string' ? data.prefix : '',
      token: req.token,
      raw: req.raw,
      dir: typeof data.dir === 'string' ? data.dir : '',
      truncated: Boolean(data.truncated),
      col: req.col,
      row: req.row,
    })
    setSelected(0)
  }, [data, isError, req, close])

  /* ── Recompute on cursor movement ── */
  useEffect(() => {
    if (!active) { close(); setReq(null); return }
    const run = () => {
      const word = readWord()
      if (!word || !shouldComplete(word.token, word.command)) {
        close()
        setReq(null)
        dismissed.current = null
        return
      }
      if (dismissed.current === word.token) return
      dismissed.current = null
      const next: Request = {
        token: word.token,
        raw: word.raw,
        col: word.start,
        row: term.buffer.active.cursorY,
        foldersOnly: foldersOnly(word.command),
      }
      // Identity is preserved for an unchanged request so a bare cursor movement
      // does not churn the query key (or reset the highlighted row).
      setReq(prev => (prev && sameRequest(prev, next) ? prev : next))
    }
    const schedule = () => {
      if (timer.current) clearTimeout(timer.current)
      timer.current = setTimeout(run, DEBOUNCE_MS)
    }
    const cursor = term.onCursorMove(schedule)
    // A newline means the line was submitted, so any pending request is about a
    // command that no longer exists. Dropping `req` matters beyond tidiness: the
    // query key is derived from the word, so submitting `cd foo` and then typing
    // the SAME word in the child directory would leave the key unchanged, and an
    // unchanged key does not refetch — the menu would offer the parent's listing.
    const feed = term.onLineFeed(() => {
      close()
      setReq(null)
      dismissed.current = null
    })
    return () => {
      cursor.dispose()
      feed.dispose()
      if (timer.current) { clearTimeout(timer.current); timer.current = null }
    }
  }, [term, active, close, readWord])

  /* ── Accept a suggestion ── */
  const accept = useCallback((entry: Entry, sug: Suggestions) => {
    // Menu closes first: the insertion echoes back through the PTY, which fires
    // onCursorMove and re-opens the menu for the NEW token (so accepting a
    // directory immediately lists its contents).
    close()
    if (entry.here) {
      // "Stop here" — the token is already the path the user wants. Nothing is
      // typed, and the token is suppressed so the echo-driven re-open does not
      // immediately pop the child listing back up.
      dismissed.current = sug.token
      return
    }
    // Defence in depth: the listing is already filtered, so reaching this with a
    // control character would mean a second ingestion path appeared.
    if (!isSafeName(entry.name)) return
    const { erase, text } = buildInsertion(sug.raw, entry.name, acceptSuffix(entry.dir))
    if (!entry.dir) {
      // A file completion ends the word — `acceptSuffix` appended a space, so the
      // next word is empty, and for a path command an empty word means "list the
      // cwd". Left alone the menu re-opens instantly for the NEXT argument, which
      // reads as the completion having failed. Suppress that empty word; typing
      // anything (or deleting back into the name) revives it.
      dismissed.current = ''
    }
    sendRawToTerminalSession(sessionId, '\x7f'.repeat(erase) + text)
  }, [sessionId, close])

  /* ── Key interception (only while the menu is open) ── */
  useEffect(() => {
    /**
     * Claim a key for the menu.
     *
     * Returning false only stops xterm from handling the key — it returns early
     * from its own keydown handler WITHOUT calling `preventDefault`, so the
     * browser default still runs. Left alone that breaks two keys outright:
     * Tab moves focus out of the terminal (landing on the page's skip link),
     * and Enter goes on to fire `keypress`, which xterm turns into a CR and
     * sends to the PTY — the shell executes the line instead of the menu
     * completing it. Cancelling the DOM event here is what actually reserves
     * the key; the keypress event is suppressed as a consequence.
     */
    const claim = (e: KeyboardEvent) => {
      e.preventDefault()
      e.stopPropagation()
      return false
    }
    /**
     * Whether the menu still describes what is on screen.
     *
     * A failed or late request, or a keystroke landing between render and
     * keypress, can leave entries computed for a PREVIOUS word. Inserting from
     * those would rewrite the line into something the user never saw, so the
     * word is re-read from the buffer at acceptance time and a mismatch aborts:
     * the menu closes and the key goes to the shell untouched (Enter submits
     * exactly the visible line, Tab runs the shell's own completion).
     */
    const stale = (s: Suggestions) => readWord()?.token !== s.token
    term.attachCustomKeyEventHandler((e) => {
      if (e.type !== 'keydown') return true
      // An IME candidate is committed with a keydown the browser marks as
      // composing (Chrome reports keyCode 229 for it); swallowing that Enter
      // would accept a path instead of the text the user just composed. Some
      // browsers report the committing key as non-composing, hence the grace
      // window after `compositionend`.
      if (e.isComposing || e.keyCode === 229) return true
      if (e.key === 'Enter' && Date.now() - imeEndAt.current < IME_GRACE_MS) return true
      const s = stateRef.current.sug
      if (!s) return true
      if (e.ctrlKey || e.metaKey || e.altKey) return true
      const n = s.entries.length
      switch (e.key) {
        case 'ArrowDown':
          setSelected(i => (i + 1) % n)
          return claim(e)
        case 'ArrowUp':
          setSelected(i => (i - 1 + n) % n)
          return claim(e)
        case 'Escape':
          dismissed.current = s.token
          close()
          return claim(e)
        case 'Enter':
          if (stale(s)) { close(); return true }
          accept(s.entries[stateRef.current.selected], s)
          return claim(e)
        case 'Tab': {
          if (stale(s)) { close(); return true }
          // The synthetic "stop here" row has no name, so it must not drag the
          // common prefix down to nothing.
          const common = commonPrefix(s.entries.filter(x => !x.here).map(x => x.name))
          // Only insert a common prefix that actually EXTENDS what was typed.
          // Matching is case-insensitive, so `doc` can match both `Docs` and
          // `DoConfig` whose shared prefix is `Do` — two characters, one FEWER
          // than the user has typed. Inserting it would shorten the word and
          // silently drop a character, so a non-extending prefix falls through to
          // committing the highlighted entry instead.
          if (extendsWord(common, s.prefix)) {
            // Escaped (and `./`-guarded) through the same choke point as an
            // outright acceptance — a partial prefix is filesystem-derived too.
            const { erase, text } = buildInsertion(s.raw, common)
            close()
            sendRawToTerminalSession(sessionId, '\x7f'.repeat(erase) + text)
          } else {
            accept(s.entries[stateRef.current.selected], s)
          }
          return claim(e)
        }
        default:
          return true
      }
    })
    // xterm exposes a single handler slot; hand it back on unmount so a future
    // consumer (or a re-mount of this pane) is not shadowed by a stale closure.
    return () => { term.attachCustomKeyEventHandler(() => true) }
  }, [term, sessionId, accept, close, readWord])

  /* ── IME composition boundary ── */
  useEffect(() => {
    const done = () => { imeEndAt.current = Date.now() }
    let attached: HTMLTextAreaElement | null = null
    const attach = (): boolean => {
      if (attached) return true
      const ta = term.textarea
      if (!ta) return false
      attached = ta
      // xterm routes typing through a hidden textarea, which is where composition
      // events land.
      ta.addEventListener('compositionend', done)
      return true
    }
    // On FIRST mount this child's effects run before the parent has called
    // `term.open()`, so the textarea does not exist yet and attaching here alone
    // would leave IME Enter unguarded for the life of the pane. The first render
    // event is the earliest point the element is guaranteed to be there.
    const rendered = term.onRender(() => { if (attach()) rendered.dispose() })
    attach()
    return () => {
      rendered.dispose()
      attached?.removeEventListener('compositionend', done)
    }
  }, [term])

  /* ── Anchor the menu to the token's cell, clamped inside the pane ── */
  useLayoutEffect(() => {
    if (!sug) { setPos(null); return }
    const screen = term.element?.querySelector('.xterm-screen') as HTMLElement | null
    const menu = menuRef.current
    const pane = menu?.offsetParent as HTMLElement | null
    if (!screen || !menu || !pane) return
    const cellW = screen.clientWidth / Math.max(1, term.cols)
    const cellH = screen.clientHeight / Math.max(1, term.rows)
    const w = menu.offsetWidth
    const h = menu.offsetHeight
    const M = 4
    const left = Math.max(M, Math.min(sug.col * cellW, pane.clientWidth - w - M))
    const below = (sug.row + 1) * cellH
    const above = below - cellH - h
    // Prefer dropping below the cursor line; flip above when it would be cut off.
    const fitsBelow = below + h <= pane.clientHeight - M
    setPos({ left, top: fitsBelow ? below : Math.max(M, above) })
  }, [sug, term])

  /* ── Keep the selected row in view ── */
  useEffect(() => {
    const list = listRef.current
    if (!list) return
    const top = selected * ROW_H
    if (top < list.scrollTop) list.scrollTop = top
    else if (top + ROW_H > list.scrollTop + list.clientHeight) {
      list.scrollTop = top + ROW_H - list.clientHeight
    }
  }, [selected, sug])

  /* ── Bottom bar: what the highlighted row means ── */
  const caption = useMemo(() => {
    if (!sug) return ''
    return sug.entries[selected]?.here
      ? 'use this folder'
      : sug.dir + (sug.truncated ? ' (truncated)' : '')
  }, [sug, selected])

  if (!sug) return null
  return (
    <div
      ref={menuRef}
      data-testid="terminal-completion"
      role="listbox"
      aria-label={i18nT('components.terminalCompletion.path_completions')}
      className="absolute z-30 flex flex-col overflow-hidden rounded-md border border-border bg-bg-elevated shadow-lg"
      style={{
        left: pos?.left ?? 0,
        top: pos?.top ?? 0,
        minWidth: 200,
        maxWidth: '70%',
        // Hidden until measured so it never paints at an unclamped position.
        opacity: pos ? 1 : 0,
        pointerEvents: 'none',
      }}
    >
      <div ref={listRef} className="overflow-y-auto" style={{ maxHeight: VISIBLE_ROWS * ROW_H }}>
        {sug.entries.map((e, i) => (
          <div
            key={e.here ? '\u0000here' : e.name}
            role="option"
            aria-selected={i === selected}
            aria-label={e.here ? 'use this folder' : e.name}
            className={`flex items-center gap-1.5 px-2 font-mono text-[12px] whitespace-nowrap ${
              i === selected ? 'bg-accent-subtle text-text-strong' : 'text-text'
            }`}
            style={{ height: ROW_H }}
          >
            {e.here
              ? <CornerDownLeft className="h-3 w-3 shrink-0 text-accent" aria-hidden="true" />
              : e.dir
                ? <Folder className="h-3 w-3 shrink-0 text-accent" aria-hidden="true" />
                : <FileIcon className="h-3 w-3 shrink-0 text-muted" aria-hidden="true" />}
            <span className={`truncate ${e.here ? 'text-muted' : ''}`}>
              {e.here ? './' : <Matched name={e.name} at={e.at} len={sug.prefix.length} />}
              {e.dir && !e.here ? '/' : ''}
            </span>
          </div>
        ))}
      </div>
      {/* Description bar: what the highlighted row will do. */}
      <div className="border-t border-border px-2 py-1 text-[10.5px] text-muted">
        <span className="block truncate font-mono" title={caption}>{caption}</span>
      </div>
    </div>
  )
}
