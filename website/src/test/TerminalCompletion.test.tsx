import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { Terminal } from '@xterm/xterm'
import TerminalCompletion from '../components/TerminalCompletion'

/* A minimal xterm stand-in: TerminalCompletion only reads the cursor's screen
 * row, subscribes to cursor/linefeed/render events, registers two OSC handlers
 * and claims the custom-key-handler slot. Driving those directly is far more
 * precise than booting a real Terminal into jsdom (which has no canvas). */
function makeTerm(line: string, cursorX: number, opts: { openTerminal?: boolean } = {}) {
  const cursorCbs: (() => void)[] = []
  const feedCbs: (() => void)[] = []
  const renderCbs: (() => void)[] = []
  const osc: Record<number, (data: string) => boolean> = {}
  let keyHandler: (e: KeyboardEvent) => boolean = () => true
  let row = line
  let wrapped = false
  /** Cell widths, sparse: anything unset is an ordinary single-width cell. */
  const widths = new Map<number, number>()
  /** Cell strings, sparse: anything unset is one code unit wide. */
  const chars = new Map<number, string>()
  const buf = {
    baseY: 0,
    cursorX,
    cursorY: 3,
    type: 'normal' as 'normal' | 'alternate',
    getLine: () => ({
      translateToString: () => row,
      get isWrapped() { return wrapped },
      get length() { return Math.max(row.length, 100) },
      getCell: (x: number) => ({
        getWidth: () => widths.get(x) ?? 1,
        getChars: () => chars.get(x) ?? 'x',
      }),
    }),
  }
  const screenEl = document.createElement('div')
  screenEl.className = 'xterm-screen'
  Object.defineProperty(screenEl, 'clientWidth', { value: 800, configurable: true })
  Object.defineProperty(screenEl, 'clientHeight', { value: 400, configurable: true })
  const element = document.createElement('div')
  element.appendChild(screenEl)
  // xterm types through a hidden textarea; composition events land there. It only
  // exists once `term.open()` has run, which for a real pane happens AFTER this
  // child's effects — `openTerminal: false` reproduces that first mount.
  const textarea = document.createElement('textarea')
  element.appendChild(textarea)
  let opened = opts.openTerminal !== false

  const term = {
    cols: 100,
    rows: 20,
    element,
    get textarea() { return opened ? textarea : null },
    buffer: { active: buf },
    parser: {
      registerOscHandler: (id: number, cb: (d: string) => boolean) => {
        osc[id] = cb
        return { dispose: () => { delete osc[id] } }
      },
    },
    onCursorMove: (cb: () => void) => { cursorCbs.push(cb); return { dispose: () => {} } },
    onLineFeed: (cb: () => void) => { feedCbs.push(cb); return { dispose: () => {} } },
    onRender: (cb: () => void) => {
      renderCbs.push(cb)
      return { dispose: () => { renderCbs.splice(renderCbs.indexOf(cb), 1) } }
    },
    attachCustomKeyEventHandler: (h: (e: KeyboardEvent) => boolean) => { keyHandler = h },
  }
  return {
    term: term as unknown as Terminal,
    /** Rewrite the cursor's screen row (what the shell's echo does for real). */
    setLine: (next: string) => { row = next },
    /** Move the fake cursor (the shell's own echo does this for real). */
    setCursor: (x: number) => { buf.cursorX = x },
    /** Switch screens the way vim/less/htop do on start-up. */
    setBufferType: (t: 'normal' | 'alternate') => { buf.type = t },
    /** Mark the cursor row as the continuation of a longer logical line. */
    setWrapped: (v: boolean) => { wrapped = v },
    /** Give a cell a non-single width: 2 for a CJK glyph, 0 for its spacer. */
    setCellWidth: (x: number, w: number) => { widths.set(x, w) },
    /** Give a cell a multi-code-unit string (base + combining mark, or a pair). */
    setCellChars: (x: number, s: string) => { chars.set(x, s) },
    /** What the parent's `term.open()` does: create the DOM and paint once. */
    openTerminal: () => { opened = true; renderCbs.forEach(cb => cb()) },
    moveCursor: () => cursorCbs.forEach(cb => cb()),
    lineFeed: () => feedCbs.forEach(cb => cb()),
    /** Finish an IME composition on the terminal's textarea. */
    compositionEnd: () => textarea.dispatchEvent(new CompositionEvent('compositionend')),
    /** Deliver a keydown the way xterm does. Events MUST be cancelable: the
     *  component reserves a key by cancelling the DOM event (returning false
     *  alone does not stop the browser default), so a non-cancelable event
     *  would make `defaultPrevented` silently untestable. */
    key: (init: KeyboardEventInit) => {
      const e = new KeyboardEvent('keydown', { cancelable: true, ...init })
      const handled = keyHandler(e)
      return { passedThrough: handled, prevented: e.defaultPrevented }
    },
    osc,
  }
}

const PROMPT = '~/work » '

let sent: string[] = []
vi.mock('../utils/terminalRegistry', () => ({
  sendRawToTerminalSession: (_id: string, data: string) => { sent.push(data); return true },
}))

interface MockEntry {
  name: string
  dir?: boolean
  at?: number
  kind?: 'sub' | 'flag'
  desc?: string
  nospace?: boolean
}

function mockComplete(entries: MockEntry[], prefix = '', dir: string | null = '/work') {
  return vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ entries, prefix, dir, truncated: false, cwd: dir }),
  })
}

/** A command-tier reply: `dir` is null, and each entry carries a `kind`. */
function mockCommand(entries: MockEntry[], prefix = '') {
  return mockComplete(entries, prefix, null)
}

/** Render under a fresh QueryClient — the listing is a React Query. */
function renderCompletion(term: Terminal, active = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <TerminalCompletion term={term} sessionId="s1" active={active} />
    </QueryClientProvider>,
  )
}

/** Fire a cursor move, let the debounce elapse, and flush the query. */
async function trigger(h: { moveCursor: () => void }) {
  await act(async () => { h.moveCursor(); await vi.advanceTimersByTimeAsync(100) })
  // React Query hands its state change to the notify manager as a MICROTASK,
  // which the fake clock also owns — so draining it needs the async advance, not
  // a bare `Promise.resolve()`. Three hops: fetch resolve, notify, re-render.
  for (let i = 0; i < 3; i += 1) await act(async () => { await vi.advanceTimersByTimeAsync(1) })
}

beforeEach(() => {
  sent = []
  vi.useFakeTimers()
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('TerminalCompletion', () => {
  it('opens a menu for `cd ../` and lists directories', async () => {
    const fetchMock = mockComplete([{ name: 'KiroCrew', dir: true }, { name: 'notes', dir: true }])
    vi.stubGlobal('fetch', fetchMock)
    const line = `${PROMPT}cd ../`
    const h = makeTerm(line, line.length)
    renderCompletion(h.term)
    await trigger(h)

    expect(screen.getByTestId('terminal-completion')).toBeInTheDocument()
    expect(screen.getByText('KiroCrew/')).toBeInTheDocument()
    // `cd` is a directory-only command, so the request narrows the listing. No
    // `argv`: its ABSENCE is what selects the path tier server-side.
    const body = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(body).toEqual({ session_id: 's1', token: '../', folders_only: true })
  })

  it('asks the command tier for a non-path command, and stays shut when it has nothing', async () => {
    // `echo` is not a path command and `hello` cannot be a path, so this is the
    // command tier's word — but `echo` speaks no completion protocol, so the
    // backend answers with no entries and no menu appears.
    const fetchMock = mockComplete([])
    vi.stubGlobal('fetch', fetchMock)
    const line = `${PROMPT}echo hello`
    const h = makeTerm(line, line.length)
    renderCompletion(h.term)
    await trigger(h)

    const body = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(body).toEqual({
      session_id: 's1', token: 'hello', folders_only: false, argv: ['echo'],
    })
    expect(screen.queryByTestId('terminal-completion')).not.toBeInTheDocument()
  })

  it('asks for nothing while the command name itself is being typed', async () => {
    // No command word yet, so neither tier applies: completing the command NAME
    // is out of scope, and there is nothing to complete a path or a subcommand
    // against.
    const fetchMock = mockComplete([{ name: 'x', dir: false }])
    vi.stubGlobal('fetch', fetchMock)
    const line = `${PROMPT}gh`
    const h = makeTerm(line, line.length)
    renderCompletion(h.term)
    await trigger(h)

    expect(fetchMock).not.toHaveBeenCalled()
    expect(screen.queryByTestId('terminal-completion')).not.toBeInTheDocument()
  })

  it('stays closed when the directory has no matches', async () => {
    vi.stubGlobal('fetch', mockComplete([]))
    const line = `${PROMPT}cd zzz`
    const h = makeTerm(line, line.length)
    renderCompletion(h.term)
    await trigger(h)

    expect(screen.queryByTestId('terminal-completion')).not.toBeInTheDocument()
  })

  it('is dormant while the pane is hidden', async () => {
    const fetchMock = mockComplete([{ name: 'docs', dir: true }])
    vi.stubGlobal('fetch', fetchMock)
    const line = `${PROMPT}cd `
    const h = makeTerm(line, line.length)
    renderCompletion(h.term, false)
    await trigger(h)

    expect(fetchMock).not.toHaveBeenCalled()
  })

  // A substring hit is only legible if the menu shows WHERE it matched, and
  // accepting it has to replace the typed fragment rather than extend it.
  it('emphasises a mid-name match and replaces the typed fragment', async () => {
    vi.stubGlobal('fetch', mockComplete(
      [{ name: 'KiroCrew-terminal-completion', dir: true, at: 9 }], 'termi', '/work',
    ))
    const line = `${PROMPT}cd termi`
    const h = makeTerm(line, line.length)
    renderCompletion(h.term)
    await trigger(h)

    const row = screen.getByRole('option')
    expect(row).toHaveTextContent('KiroCrew-terminal-completion/')
    // Only the matched span is emphasised, not the whole name.
    expect(row.querySelector('span span')).toHaveTextContent('termi')
    // The name does not extend "termi", so the fragment is erased and retyped.
    await act(async () => { h.key({ key: 'Enter' }) })
    expect(sent).toEqual(['\x7f'.repeat(5) + 'KiroCrew-terminal-completion/'])
  })

  it('captions the highlighted row', async () => {
    vi.stubGlobal('fetch', mockComplete([{ name: 'docs', dir: true }], '', '/work/sub'))
    const line = `${PROMPT}ls sub/`
    const h = makeTerm(line, line.length)
    renderCompletion(h.term)
    await trigger(h)

    // The stop-here row leads, so the caption explains what it does...
    expect(screen.getByText('use this folder')).toBeInTheDocument()
    // ...and moving onto a real entry shows where the listing came from.
    await act(async () => { h.key({ key: 'ArrowDown' }) })
    expect(screen.getByText('/work/sub')).toBeInTheDocument()
  })

  describe('keyboard', () => {
    async function open(
      entries = [{ name: 'docs', dir: true }, { name: 'doctor', dir: true }],
      prefix = 'doc',
    ) {
      vi.stubGlobal('fetch', mockComplete(entries, prefix))
      const line = `${PROMPT}cd ${prefix}`
      const h = makeTerm(line, line.length)
      renderCompletion(h.term)
      await trigger(h)
      return h
    }

    it('passes keys through when the menu is closed', async () => {
      vi.stubGlobal('fetch', mockComplete([]))
      const line = `${PROMPT}cd zzz`
      const h = makeTerm(line, line.length)
      renderCompletion(h.term)
      await trigger(h)
      const r = h.key({ key: 'Enter' })
      expect(r.passedThrough).toBe(true)
      expect(r.prevented).toBe(false)
    })

    it('swallows arrow keys and moves the selection', async () => {
      const h = await open()
      const options = () => screen.getAllByRole('option')
      expect(options()[0]).toHaveAttribute('aria-selected', 'true')
      await act(async () => { expect(h.key({ key: 'ArrowDown' }).passedThrough).toBe(false) })
      expect(options()[1]).toHaveAttribute('aria-selected', 'true')
      // Wraps around to the top.
      await act(async () => { h.key({ key: 'ArrowDown' }) })
      expect(options()[0]).toHaveAttribute('aria-selected', 'true')
    })

    // Regression: returning false from xterm's custom key handler does NOT
    // cancel the DOM event. Without an explicit preventDefault, Tab moved focus
    // out of the terminal and Enter reached the PTY as a CR (executing the
    // line) instead of completing it.
    it('cancels the DOM event for every key it claims', async () => {
      for (const key of ['ArrowDown', 'ArrowUp', 'Tab', 'Enter', 'Escape']) {
        const h = await open()
        let prevented = false
        await act(async () => { prevented = h.key({ key }).prevented })
        expect(prevented, `${key} must be prevented`).toBe(true)
        cleanup()
        sent = []
      }
    })

    it('inserts the selected entry on Enter and closes', async () => {
      const h = await open()
      await act(async () => { expect(h.key({ key: 'Enter' }).passedThrough).toBe(false) })
      expect(sent).toEqual(['s/'])   // "doc" + "s" → docs/, trailing / keeps the path open
      expect(screen.queryByTestId('terminal-completion')).not.toBeInTheDocument()
    })

    // Accepting a directory must walk INTO it: the inserted "/" echoes back
    // through the PTY, which moves the cursor and re-opens the menu on the new
    // token — the whole point of appending the separator.
    it('re-opens on the next token after a directory is accepted', async () => {
      const h = await open()
      await act(async () => { h.key({ key: 'Enter' }) })
      expect(screen.queryByTestId('terminal-completion')).not.toBeInTheDocument()
      // The shell echoes "s/" — cursor advances and the row now ends in a slash.
      h.setLine(`${PROMPT}cd docs/`)
      h.setCursor(`${PROMPT}cd docs/`.length)
      vi.stubGlobal('fetch', mockComplete([{ name: 'inner', dir: true }], '', '/work/docs'))
      await trigger(h)
      expect(screen.getByTestId('terminal-completion')).toBeInTheDocument()
      expect(screen.getByText('inner/')).toBeInTheDocument()
    })

    it('extends to the common prefix on Tab without picking an entry', async () => {
      const h = await open([{ name: 'docs', dir: true }, { name: 'docsite', dir: true }], 'doc')
      await act(async () => { expect(h.key({ key: 'Tab' }).passedThrough).toBe(false) })
      expect(sent).toEqual(['s'])    // common prefix "docs" — no trailing separator
    })

    it('falls back to inserting the selection when Tab can add nothing', async () => {
      // "docs" and "doctor" share exactly what is already typed, so a second
      // Tab has to commit the highlighted entry instead of doing nothing.
      const h = await open()
      await act(async () => { h.key({ key: 'Tab' }) })
      expect(sent).toEqual(['s/'])
    })

    it('dismisses on Escape without inserting', async () => {
      const h = await open()
      await act(async () => { expect(h.key({ key: 'Escape' }).passedThrough).toBe(false) })
      expect(sent).toEqual([])
      expect(screen.queryByTestId('terminal-completion')).not.toBeInTheDocument()
    })

    // Regression: Escape must also cancel the pending debounce timer. Otherwise
    // the pending run re-opens the menu for whatever word is on screen by the
    // time it fires, so the dismissal only applies to the word it was pressed on.
    it('does not re-open for an edited word after Escape', async () => {
      const h = await open()
      const edited = `${PROMPT}cd docs`
      h.setLine(edited)
      h.setCursor(edited.length)
      const fetchMock = mockComplete([{ name: 'docs', dir: true }], 'docs')
      vi.stubGlobal('fetch', fetchMock)
      // The keystroke schedules a run; Escape lands before that run fires.
      await act(async () => { h.moveCursor() })
      await act(async () => { h.key({ key: 'Escape' }) })
      for (let i = 0; i < 4; i += 1) {
        await act(async () => { await vi.advanceTimersByTimeAsync(100) })
      }
      expect(fetchMock).not.toHaveBeenCalled()
      expect(screen.queryByTestId('terminal-completion')).not.toBeInTheDocument()
    })

    // Regression: accepting a folder re-opens on its contents, so without a
    // "stop here" row there is no way to settle on the level just chosen.
    it('offers a stop-here row first once the token names a directory', async () => {
      vi.stubGlobal('fetch', mockComplete([{ name: 'inner', dir: true }], '', '/work/docs'))
      const line = `${PROMPT}cd docs/`
      const h = makeTerm(line, line.length)
      renderCompletion(h.term)
      await trigger(h)
      const options = screen.getAllByRole('option')
      expect(options).toHaveLength(2)
      expect(options[0]).toHaveAttribute('aria-label', 'use this folder')
      expect(options[0]).toHaveAttribute('aria-selected', 'true')
      expect(options[1]).toHaveAttribute('aria-label', 'inner')
      // Accepting it types nothing and does not descend.
      await act(async () => { h.key({ key: 'Enter' }) })
      expect(sent).toEqual([])
      expect(screen.queryByTestId('terminal-completion')).not.toBeInTheDocument()
      // Suppressed for this token: the shell's echo must not pop it back open.
      await trigger(h)
      expect(screen.queryByTestId('terminal-completion')).not.toBeInTheDocument()
    })

    it('offers the stop-here row for a file command too', async () => {
      // `ls foo/` is a complete, useful command — the directory is itself a
      // valid argument, so there has to be a way to settle on it.
      vi.stubGlobal('fetch', mockComplete([{ name: 'inner', dir: true }], '', '/work/docs'))
      const line = `${PROMPT}ls docs/`
      const h = makeTerm(line, line.length)
      renderCompletion(h.term)
      await trigger(h)
      const options = screen.getAllByRole('option')
      expect(options).toHaveLength(2)
      expect(options[0]).toHaveAttribute('aria-label', 'use this folder')
    })

    it('omits the stop-here row while a name is still being typed', async () => {
      // `cd doc` is not a directory yet — there is nothing to confirm.
      await open()
      expect(screen.getAllByRole('option')).toHaveLength(2)
      expect(screen.getAllByRole('option')[0]).toHaveAttribute('aria-label', 'docs')
    })

    it('keeps Tab on the real entries when a stop-here row is present', async () => {
      vi.stubGlobal('fetch', mockComplete(
        [{ name: 'docs', dir: true }, { name: 'docsite', dir: true }], '', '/work/sub',
      ))
      const line = `${PROMPT}cd sub/`
      const h = makeTerm(line, line.length)
      renderCompletion(h.term)
      await trigger(h)
      await act(async () => { h.key({ key: 'Tab' }) })
      expect(sent).toEqual(['docs'])   // common prefix of the real rows only
    })

    // Regression: `acceptSuffix` appends a space after a file, leaving an empty
    // next word — which for a path command means "list the cwd", so the menu
    // would otherwise re-open immediately for the next argument.
    it('stays closed after a file completion ends the word', async () => {
      vi.stubGlobal('fetch', mockComplete([{ name: 'app.json', dir: false }], 'app'))
      const line = `${PROMPT}ls app`
      const h = makeTerm(line, line.length)
      renderCompletion(h.term)
      await trigger(h)
      await act(async () => { h.key({ key: 'Enter' }) })
      expect(sent).toEqual(['.json '])
      // The shell echoes the name plus the space; the next word is empty.
      const next = `${PROMPT}ls app.json `
      h.setLine(next)
      h.setCursor(next.length)
      vi.stubGlobal('fetch', mockComplete([{ name: 'other', dir: true }]))
      await trigger(h)
      expect(screen.queryByTestId('terminal-completion')).not.toBeInTheDocument()
      // Typing revives it.
      const typed = `${PROMPT}ls app.json o`
      h.setLine(typed)
      h.setCursor(typed.length)
      await trigger(h)
      expect(screen.getByTestId('terminal-completion')).toBeInTheDocument()
    })

    it('never swallows a modified key (Ctrl-C must reach the shell)', async () => {
      const h = await open()
      const ctrlC = h.key({ key: 'c', ctrlKey: true })
      expect(ctrlC.passedThrough).toBe(true)
      expect(ctrlC.prevented).toBe(false)
      expect(h.key({ key: 'Enter', metaKey: true }).passedThrough).toBe(true)
    })

    it('closes on a newline', async () => {
      const h = await open()
      await act(async () => { h.lineFeed() })
      expect(screen.queryByTestId('terminal-completion')).not.toBeInTheDocument()
    })

    // Regression: the query key is derived from the word, so submitting a
    // command and then typing the SAME word in the new directory left the key
    // unchanged — and an unchanged key does not refetch, so the menu offered the
    // previous directory's listing. Submission must drop the pending request.
    it('refetches for the same word after a submitted line', async () => {
      const h = await open()
      await act(async () => { h.lineFeed() })
      const second = mockComplete([{ name: 'child', dir: true }], 'doc', '/work/docs')
      vi.stubGlobal('fetch', second)
      await trigger(h)
      expect(second).toHaveBeenCalled()
      expect(screen.getByText('child/')).toBeInTheDocument()
    })
  })

  describe('suppression', () => {
    /** A row the trigger should refuse; returns the mock so callers assert on it. */
    async function attempt(
      line: string,
      cursorX = line.length,
      tweak?: (h: ReturnType<typeof makeTerm>) => void,
    ) {
      const fetchMock = mockComplete([{ name: 'docs', dir: true }])
      vi.stubGlobal('fetch', fetchMock)
      const h = makeTerm(line, cursorX)
      tweak?.(h)
      renderCompletion(h.term)
      await trigger(h)
      return { h, fetchMock }
    }

    // A TUI redraws arbitrary text: `> cd ./src` in a vim buffer satisfies the
    // prompt heuristic, and an open menu would then steal Escape/arrows/Enter
    // from the application.
    it('never opens on the alternate screen', async () => {
      const { fetchMock } = await attempt(`${PROMPT}cd ../`, undefined,
        h => h.setBufferType('alternate'))
      expect(fetchMock).not.toHaveBeenCalled()
      expect(screen.queryByTestId('terminal-completion')).not.toBeInTheDocument()
    })

    it('closes an open menu when an app switches to the alternate screen', async () => {
      vi.stubGlobal('fetch', mockComplete([{ name: 'docs', dir: true }], 'doc'))
      const line = `${PROMPT}cd doc`
      const h = makeTerm(line, line.length)
      renderCompletion(h.term)
      await trigger(h)
      expect(screen.getByTestId('terminal-completion')).toBeInTheDocument()
      h.setBufferType('alternate')
      await trigger(h)
      expect(screen.queryByTestId('terminal-completion')).not.toBeInTheDocument()
    })

    // Completing mid-word would insert the name in front of the surviving
    // suffix (`cd do⎸cs` → `cd docs/cs`).
    it('stays shut when the cursor is inside a word', async () => {
      const line = `${PROMPT}cd docs`
      const { fetchMock } = await attempt(line, PROMPT.length + 5)
      expect(fetchMock).not.toHaveBeenCalled()
    })

    it('opens again once the cursor reaches the end of the word', async () => {
      const line = `${PROMPT}cd docs`
      const { h, fetchMock } = await attempt(line, PROMPT.length + 5)
      expect(fetchMock).not.toHaveBeenCalled()
      h.setCursor(line.length)
      await trigger(h)
      expect(fetchMock).toHaveBeenCalled()
    })

    // `translateToString` hands back one PHYSICAL row while cursorX counts
    // cells, so these two shapes would yield a confidently wrong word.
    it('stays shut on a wrapped continuation row', async () => {
      const { fetchMock } = await attempt(`${PROMPT}cd ../`, undefined, h => h.setWrapped(true))
      expect(fetchMock).not.toHaveBeenCalled()
    })

    it('stays shut when a double-width cell precedes the cursor', async () => {
      const { fetchMock } = await attempt(`${PROMPT}cd ../`, undefined,
        h => h.setCellWidth(PROMPT.length + 1, 2))
      expect(fetchMock).not.toHaveBeenCalled()
    })

    it('ignores a wide cell that sits after the cursor', async () => {
      const line = `${PROMPT}cd ../`
      const { fetchMock } = await attempt(line, undefined, h => h.setCellWidth(line.length + 2, 2))
      expect(fetchMock).toHaveBeenCalled()
    })

    // Whitespace-only tokenisation cannot see that `D` is the tail of `My D`.
    it('stays shut inside a quoted word', async () => {
      const { fetchMock } = await attempt(`${PROMPT}cd "My D`)
      expect(fetchMock).not.toHaveBeenCalled()
    })

    // A cell can hold a base character plus combining marks, so the row's string
    // is longer than the columns it occupies and every later index is off by one.
    it('stays shut when a cell holds more than one code unit', async () => {
      const { fetchMock } = await attempt(`${PROMPT}cd ../`, undefined,
        h => h.setCellChars(PROMPT.length + 1, 'e\u0301'))
      expect(fetchMock).not.toHaveBeenCalled()
    })

    it('stays shut on a word ending in an unfinished escape', async () => {
      const { fetchMock } = await attempt(`${PROMPT}cd my\\`)
      expect(fetchMock).not.toHaveBeenCalled()
    })
  })

  // Regression: escaping a space is what puts `my\ dir/` on screen, so refusing
  // to complete an escaped word dead-ends the walk into the directory that the
  // trailing `/` exists for.
  describe('names containing spaces', () => {
    it('sends the decoded name and completes an escaped word', async () => {
      const fetchMock = mockComplete([{ name: 'my dir', dir: true }], 'my d')
      vi.stubGlobal('fetch', fetchMock)
      const line = `${PROMPT}cd my\\ d`
      const h = makeTerm(line, line.length)
      renderCompletion(h.term)
      await trigger(h)

      // The request carries the literal filesystem name, not the escaped text.
      expect(JSON.parse(fetchMock.mock.calls[0][1].body).token).toBe('my d')
      expect(screen.getByText('my dir/')).toBeInTheDocument()
      // `my\ d` is already on screen, so only the missing (escaped) tail is typed.
      await act(async () => { h.key({ key: 'Enter' }) })
      expect(sent).toEqual(['ir/'])
    })

    it('walks into a directory whose name contains a space', async () => {
      vi.stubGlobal('fetch', mockComplete([{ name: 'my dir', dir: true }], 'my'))
      const line = `${PROMPT}cd my`
      const h = makeTerm(line, line.length)
      renderCompletion(h.term)
      await trigger(h)
      await act(async () => { h.key({ key: 'Enter' }) })
      expect(sent).toEqual(['\\ dir/'])

      // The shell echoes the escaped name; the word on screen is now `my\ dir/`.
      const next = `${PROMPT}cd my\\ dir/`
      h.setLine(next)
      h.setCursor(next.length)
      const fetchMock = mockComplete([{ name: 'inner', dir: true }], '', '/work/my dir')
      vi.stubGlobal('fetch', fetchMock)
      await trigger(h)
      expect(JSON.parse(fetchMock.mock.calls[0][1].body).token).toBe('my dir/')
      expect(screen.getByText('inner/')).toBeInTheDocument()
    })
  })

  // Regression: `-c:!sh evil` accepted after `vim ` is parsed as Vim's `-c`
  // option and runs a command; escaping cannot help, since `\-c` is still `-c`.
  describe('option-shaped filenames', () => {
    async function openVim(entries: { name: string; dir: boolean }[]) {
      vi.stubGlobal('fetch', mockComplete(entries))
      const line = `${PROMPT}vim `
      const h = makeTerm(line, line.length)
      renderCompletion(h.term)
      await trigger(h)
      return h
    }

    it('types a leading-hyphen name as a path on Enter', async () => {
      const h = await openVim([{ name: '-c:!sh evil', dir: false }])
      await act(async () => { h.key({ key: 'Enter' }) })
      expect(sent).toEqual(['./-c:\\!sh\\ evil '])
    })

    it('guards Tab\'s common prefix as well', async () => {
      const h = await openVim([{ name: '-cx', dir: false }, { name: '-cy', dir: false }])
      await act(async () => { h.key({ key: 'Tab' }) })
      expect(sent).toEqual(['./-c'])
    })
  })

  describe('untrusted filenames', () => {
    /** `cat` takes files, so a bare fragment lists the cwd. */
    async function openWith(entries: { name: string; dir: boolean }[], prefix = 'my') {
      vi.stubGlobal('fetch', mockComplete(entries, prefix))
      const line = `${PROMPT}cat ${prefix}`
      const h = makeTerm(line, line.length)
      renderCompletion(h.term)
      await trigger(h)
      return h
    }

    // `touch $'my\nrm -rf x'` is a legal filename; typed verbatim its newline
    // would submit the second half to the shell as a command.
    it('drops a name containing a control character', async () => {
      await openWith([{ name: 'my.txt', dir: false }, { name: 'my\nrm -rf x', dir: false }])
      const options = screen.getAllByRole('option')
      expect(options).toHaveLength(1)
      expect(options[0]).toHaveAttribute('aria-label', 'my.txt')
    })

    it('never lets a control character reach the PTY on Enter or Tab', async () => {
      for (const key of ['Enter', 'Tab']) {
        const h = await openWith([{ name: 'my\nrm -rf x', dir: false }])
        // Nothing offered means nothing to accept: the menu never opened.
        expect(screen.queryByTestId('terminal-completion')).not.toBeInTheDocument()
        await act(async () => { h.key({ key }) })
        expect(sent, `${key} must type nothing`).toEqual([])
        cleanup()
        sent = []
      }
    })

    it('escapes shell metacharacters when a name is accepted', async () => {
      const h = await openWith([{ name: 'my file;rm -rf $x.txt', dir: false }])
      await act(async () => { h.key({ key: 'Enter' }) })
      // One shell word, escape by escape — no quoting, so the word stays
      // re-triggerable and the metacharacters are inert.
      expect(sent).toEqual(['\\ file\\;rm\\ -rf\\ \\$x.txt '])
    })

    it('escapes the common prefix inserted by Tab', async () => {
      const h = await openWith([
        { name: 'my file(a).txt', dir: false },
        { name: 'my file(b).txt', dir: false },
      ])
      await act(async () => { h.key({ key: 'Tab' }) })
      expect(sent).toEqual(['\\ file\\('])
    })

    it('leaves ordinary and non-ASCII names unescaped', async () => {
      const h = await openWith([{ name: 'my-项目_v2', dir: true }])
      await act(async () => { h.key({ key: 'Enter' }) })
      expect(sent).toEqual(['-项目_v2/'])
    })
  })

  describe('staleness', () => {
    async function open() {
      vi.stubGlobal('fetch', mockComplete([{ name: 'docs', dir: true }], 'doc'))
      const line = `${PROMPT}cd doc`
      const h = makeTerm(line, line.length)
      renderCompletion(h.term)
      await trigger(h)
      return h
    }

    // The entries describe a word that is no longer on screen, so inserting from
    // them would rewrite the line into something the user never saw. The key is
    // handed to the shell instead: Enter submits exactly what is displayed.
    it('aborts an acceptance whose word has since changed', async () => {
      for (const key of ['Enter', 'Tab']) {
        const h = await open()
        const moved = `${PROMPT}cd docz`
        h.setLine(moved)
        h.setCursor(moved.length)
        let r = { passedThrough: false, prevented: true }
        await act(async () => { r = h.key({ key }) })
        expect(r.passedThrough, `${key} must reach the shell`).toBe(true)
        expect(r.prevented).toBe(false)
        expect(sent).toEqual([])
        expect(screen.queryByTestId('terminal-completion')).not.toBeInTheDocument()
        cleanup()
        sent = []
      }
    })

    it('closes the menu when the request fails', async () => {
      const h = await open()
      expect(screen.getByTestId('terminal-completion')).toBeInTheDocument()
      // A further keystroke whose listing never arrives must not leave the
      // previous word's entries acceptable.
      const typed = `${PROMPT}cd docs`
      h.setLine(typed)
      h.setCursor(typed.length)
      vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
      await trigger(h)
      expect(screen.queryByTestId('terminal-completion')).not.toBeInTheDocument()
    })
  })

  describe('IME composition', () => {
    async function open() {
      vi.stubGlobal('fetch', mockComplete([{ name: 'docs', dir: true }], 'doc'))
      const line = `${PROMPT}cd doc`
      const h = makeTerm(line, line.length)
      renderCompletion(h.term)
      await trigger(h)
      return h
    }

    // Committing a Pinyin/Kana candidate uses Enter. Swallowing it would accept
    // a path and drop the text the user just composed.
    it('passes through the Enter that commits a candidate', async () => {
      const h = await open()
      const composing = h.key({ key: 'Enter', isComposing: true })
      expect(composing.passedThrough).toBe(true)
      expect(composing.prevented).toBe(false)
      // Browsers that report the commit key as keyCode 229 instead.
      expect(h.key({ key: 'Process', keyCode: 229 }).passedThrough).toBe(true)
      expect(sent).toEqual([])
    })

    it('passes through the Enter that ends composition', async () => {
      const h = await open()
      h.compositionEnd()
      const r = h.key({ key: 'Enter' })
      expect(r.passedThrough).toBe(true)
      expect(sent).toEqual([])
    })

    it('still accepts on a plain Enter once composition is over', async () => {
      const h = await open()
      h.compositionEnd()
      // Past the grace window: this Enter belongs to the menu again.
      vi.setSystemTime(Date.now() + 1000)
      await act(async () => { expect(h.key({ key: 'Enter' }).passedThrough).toBe(false) })
      expect(sent).toEqual(['s/'])
    })

    // Regression: this child's effects run BEFORE the parent's `term.open()`, so
    // on a first mount `term.textarea` is null and the grace listener was never
    // attached — the Enter that commits an IME candidate then accepted a path.
    it('attaches the composition listener when the terminal opens later', async () => {
      vi.stubGlobal('fetch', mockComplete([{ name: 'docs', dir: true }], 'doc'))
      const line = `${PROMPT}cd doc`
      const h = makeTerm(line, line.length, { openTerminal: false })
      renderCompletion(h.term)
      h.openTerminal()
      await trigger(h)
      h.compositionEnd()
      const r = h.key({ key: 'Enter' })
      expect(r.passedThrough).toBe(true)
      expect(sent).toEqual([])
    })
  })

  it('uses an OSC prompt marker to locate the command line', async () => {
    // With a marker there is no reliance on the prompt-terminator heuristic:
    // an exotic prompt (no `$`/`»`/`❯`) still resolves the command correctly.
    const exotic = 'diwm@host ~/work ⟩⟩ '
    const line = `${exotic}cd `
    const fetchMock = mockComplete([{ name: 'docs', dir: true }])
    vi.stubGlobal('fetch', fetchMock)
    const h = makeTerm(line, exotic.length)
    renderCompletion(h.term)
    // Both shell-integration flavours must be accepted, and both must consume
    // the sequence so it is never echoed into the screen buffer.
    expect(h.osc[133]).toBeTypeOf('function')
    expect(h.osc[697]).toBeTypeOf('function')
    expect(h.osc[133]('B')).toBe(true)
    expect(h.osc[697]('NewCmd=abc')).toBe(true)

    h.setCursor(line.length)   // shell echoed "cd "
    await trigger(h)
    expect(screen.getByTestId('terminal-completion')).toBeInTheDocument()
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).folders_only).toBe(true)
  })

  it('recognises a prompt with no marker via its terminator', async () => {
    const fetchMock = mockComplete([{ name: 'docs', dir: true }])
    vi.stubGlobal('fetch', fetchMock)
    const line = `${PROMPT}cd `
    const h = makeTerm(line, line.length)
    renderCompletion(h.term)
    await trigger(h)
    expect(screen.getByTestId('terminal-completion')).toBeInTheDocument()
  })
})

describe('TerminalCompletion — command tier', () => {
  it('offers subcommands with their descriptions for `gh pr `', async () => {
    const fetchMock = mockCommand([
      { name: 'create', kind: 'sub', desc: 'Create a pull request' },
      { name: 'checkout', kind: 'sub', desc: 'Check out a pull request in git' },
    ])
    vi.stubGlobal('fetch', fetchMock)
    const line = `${PROMPT}gh pr `
    const h = makeTerm(line, line.length)
    renderCompletion(h.term)
    await trigger(h)

    const menu = screen.getByTestId('terminal-completion')
    expect(menu).toHaveAttribute('data-mode', 'command')
    expect(screen.getByText('create')).toBeInTheDocument()
    // The tool's own help is what answers "what does this do" — the question that
    // would otherwise send the user to `--help`.
    expect(screen.getAllByText('Create a pull request')).toHaveLength(2)
    // Full argv, so the backend knows WHERE in the command tree the cursor is.
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      session_id: 's1', token: '', folders_only: false, argv: ['gh', 'pr'],
    })
  })

  it('sends the whole argv, flag words included', async () => {
    // Cobra's position in its own tree depends on the flags already typed, so
    // dropping `--repo o/r` would ask about a different command.
    const fetchMock = mockCommand([{ name: 'list', kind: 'sub', desc: '' }])
    vi.stubGlobal('fetch', fetchMock)
    const line = `${PROMPT}gh --repo o/r pr `
    const h = makeTerm(line, line.length)
    renderCompletion(h.term)
    await trigger(h)

    expect(JSON.parse(fetchMock.mock.calls[0][1].body).argv)
      .toEqual(['gh', '--repo', 'o/r', 'pr'])
  })

  it('completes a flag word, which the path tier refuses outright', async () => {
    const fetchMock = mockCommand([
      { name: '--title', kind: 'flag', desc: 'Title for the pull request' },
    ], '--ti')
    vi.stubGlobal('fetch', fetchMock)
    const line = `${PROMPT}gh pr create --ti`
    const h = makeTerm(line, line.length)
    renderCompletion(h.term)
    await trigger(h)

    expect(screen.getByTestId('terminal-completion')).toBeInTheDocument()
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).token).toBe('--ti')
  })

  it('types an accepted flag verbatim — no `./` path guard', async () => {
    // The guard exists so a FILE called `--force` is typed as `./--force`. A FLAG
    // called `--force` must be typed as `--force`; `./--force` would name a path
    // that does not exist. Same characters, opposite correct answers.
    vi.stubGlobal('fetch', mockCommand([{ name: '--force', kind: 'flag', desc: '' }], '--f'))
    const line = `${PROMPT}gh pr merge --f`
    const h = makeTerm(line, line.length)
    renderCompletion(h.term)
    await trigger(h)

    await act(async () => { h.key({ key: 'Enter' }) })
    expect(sent).toEqual(['orce '])
  })

  it('appends no separator when the protocol says the value is unfinished', async () => {
    // cobra's NoSpace directive: the cursor must land where the value goes, not one
    // space past it. Shown on a cobra tool rather than git, because git flag
    // completion is refused outright (its probe could execute a `!` alias).
    vi.stubGlobal('fetch', mockCommand(
      [{ name: '--message=', kind: 'flag', desc: '', nospace: true }], '--m',
    ))
    const line = `${PROMPT}gh pr create --m`
    const h = makeTerm(line, line.length)
    renderCompletion(h.term)
    await trigger(h)

    await act(async () => { h.key({ key: 'Enter' }) })
    expect(sent).toEqual(['essage='])
  })

  it('re-opens on the next word after a subcommand, to walk down the tree', async () => {
    // The path tier suppresses the empty word after accepting a FILE (the word is
    // finished). A subcommand is the opposite: accepting `pr` should immediately
    // offer `create`, which is how the tree is walked without typing.
    vi.stubGlobal('fetch', mockCommand([{ name: 'pr', kind: 'sub', desc: 'Manage PRs' }], 'p'))
    const line = `${PROMPT}gh p`
    const h = makeTerm(line, line.length)
    renderCompletion(h.term)
    await trigger(h)

    await act(async () => { h.key({ key: 'Enter' }) })
    expect(sent).toEqual(['r '])

    // The echo lands and the cursor moves on: the next word is empty and the menu
    // is NOT suppressed, so a fresh request goes out for `gh pr `.
    const next = `${PROMPT}gh pr `
    h.setLine(next)
    h.setCursor(next.length)
    await trigger(h)
    expect(screen.getByTestId('terminal-completion')).toBeInTheDocument()
  })

  it('shows no description bar when the protocol supplies no help text', async () => {
    // git's `--list-cmds` returns bare names. An empty strip under the rows would
    // read as a rendering bug.
    vi.stubGlobal('fetch', mockCommand([
      { name: 'commit', kind: 'sub', desc: '' },
      { name: 'checkout', kind: 'sub', desc: '' },
    ], 'c'))
    const line = `${PROMPT}git c`
    const h = makeTerm(line, line.length)
    renderCompletion(h.term)
    await trigger(h)

    const menu = screen.getByTestId('terminal-completion')
    expect(menu).toHaveAttribute('data-mode', 'command')
    // Two option rows and nothing else — no caption element.
    expect(screen.getAllByRole('option')).toHaveLength(2)
    expect(menu.querySelector('.border-t')).toBeNull()
  })

  it('does not synthesise the "use this folder" row in command mode', async () => {
    // That row confirms a directory the user already named. A subcommand list has
    // no such case, and offering it would be an unacceptable no-op row.
    vi.stubGlobal('fetch', mockCommand([{ name: 'add', kind: 'sub', desc: '' }]))
    const line = `${PROMPT}git remote `
    const h = makeTerm(line, line.length)
    renderCompletion(h.term)
    await trigger(h)

    const rows = screen.getAllByRole('option')
    expect(rows).toHaveLength(1)
    expect(rows[0]).toHaveAttribute('aria-label', 'add')
  })

  it('aborts acceptance when the command word changed under the same token', async () => {
    // The bug this pins: `gh pr c` and `git c` share the token `c`, so a
    // token-only staleness check let a menu computed for one tool be accepted into
    // the other's command line — silently corrupting it.
    vi.stubGlobal('fetch', mockCommand([{ name: 'checkout', kind: 'sub', desc: '' }], 'c'))
    const line = `${PROMPT}gh pr c`
    const h = makeTerm(line, line.length)
    renderCompletion(h.term)
    await trigger(h)
    expect(screen.getByTestId('terminal-completion')).toBeInTheDocument()

    // Same token, different command. Enter must go to the shell untouched.
    const changed = `${PROMPT}git c`
    h.setLine(changed)
    h.setCursor(changed.length)
    let passedThrough = false
    await act(async () => { passedThrough = h.key({ key: 'Enter' }).passedThrough })
    expect(passedThrough).toBe(true)
    expect(sent).toEqual([])
  })

  it('drops an entry that does not belong to the tier that was asked for', async () => {
    // The tier the CLIENT asked for decides how an accepted value is typed —
    // escaped and `./`-guarded for a path, verbatim for a command token. An entry
    // arriving without a recognised `kind` therefore cannot be reinterpreted as
    // the other kind; it is dropped, and with nothing left the menu stays shut.
    vi.stubGlobal('fetch', mockComplete(
      [{ name: '--rf', kind: 'bogus' as unknown as 'sub', desc: '' }], '--r', null,
    ))
    const line = `${PROMPT}gh pr --r`
    const h = makeTerm(line, line.length)
    renderCompletion(h.term)
    await trigger(h)

    expect(screen.queryByTestId('terminal-completion')).not.toBeInTheDocument()
    await act(async () => { h.key({ key: 'Enter' }) })
    expect(sent).toEqual([])
  })

  it('drops a command value that is not a protocol-shaped token', async () => {
    // The command tier types its values WITHOUT escaping, so a value carrying
    // shell metacharacters is refused rather than escaped — there is no such flag,
    // and inserting `--x; rm -rf ~` verbatim would be a command injection.
    vi.stubGlobal('fetch', mockCommand([
      { name: '--x; rm -rf ~', kind: 'flag', desc: 'hostile' },
      { name: '--safe', kind: 'flag', desc: 'ok' },
    ], '--'))
    const line = `${PROMPT}gh pr --`
    const h = makeTerm(line, line.length)
    renderCompletion(h.term)
    await trigger(h)

    const rows = screen.getAllByRole('option')
    expect(rows).toHaveLength(1)
    expect(rows[0]).toHaveAttribute('aria-label', '--safe')
  })

  it('does not offer a path entry through the command tier', async () => {
    // A path listing has no business carrying `kind`, and a command listing has no
    // business carrying bare filesystem entries — either would mean the tiers had
    // crossed, so each drops what is not its own.
    vi.stubGlobal('fetch', mockCommand([{ name: 'notes', dir: true }]))
    const line = `${PROMPT}gh pr `
    const h = makeTerm(line, line.length)
    renderCompletion(h.term)
    await trigger(h)

    expect(screen.queryByTestId('terminal-completion')).not.toBeInTheDocument()
  })
})
