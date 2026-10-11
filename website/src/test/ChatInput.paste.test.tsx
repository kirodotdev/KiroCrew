import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useState } from 'react'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

/**
 * fireEvent.paste passes eventProperties into the native event's clipboardData,
 * but jsdom's DataTransferItemList doesn't support our custom items array.
 * Instead we rely on the fact that React's SyntheticEvent reads from the native
 * event's clipboardData. We set `types` (which jsdom respects) and for the
 * file-upload path we verify the guard logic via the negative tests.
 */

describe('ChatInput paste: prefer text over image', () => {
  it('does NOT upload files when clipboard has text/plain alongside image (macOS Office copy)', () => {
    const onUploadFiles = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} onUploadFiles={onUploadFiles} />,
    )
    const textarea = screen.getByRole('textbox')
    // Simulate macOS Office clipboard: text/plain + text/html + Files (with image representation)
    fireEvent.paste(textarea, {
      clipboardData: {
        types: ['text/plain', 'text/html', 'Files'],
        items: [
          { kind: 'text', type: 'text/plain', getAsFile: () => null },
          { kind: 'file', type: 'image/png', getAsFile: () => new File(['px'], 'image.png', { type: 'image/png' }) },
        ],
        getData: () => 'Hello from Word',
      },
    })
    expect(onUploadFiles).not.toHaveBeenCalled()
  })

  it('DOES upload when clipboard has text/html + image but no text/plain (browser "Copy Image")', () => {
    // A <textarea> can only insert the text/plain representation. With no
    // text/plain present, deferring to the text paste would make the whole
    // gesture a silent no-op — so the image must win here. This is the
    // browser right-click "Copy Image" clipboard shape (issue #2489).
    const onUploadFiles = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} onUploadFiles={onUploadFiles} />,
    )
    const textarea = screen.getByRole('textbox')
    const file = new File(['px'], 'photo-vacation.png', { type: 'image/png' })
    fireEvent.paste(textarea, {
      clipboardData: {
        types: ['text/html', 'Files'],
        items: [
          { kind: 'file', type: 'image/png', getAsFile: () => file },
        ],
        getData: () => '',
      },
    })
    expect(onUploadFiles).toHaveBeenCalledTimes(1)
    expect(onUploadFiles.mock.calls[0][0]).toEqual([file])
  })

  it('allows file upload when clipboard has ONLY files (e.g. screenshot paste)', () => {
    const onUploadFiles = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} onUploadFiles={onUploadFiles} />,
    )
    const textarea = screen.getByRole('textbox')
    const file = new File(['px'], 'screenshot.png', { type: 'image/png' })
    fireEvent.paste(textarea, {
      clipboardData: {
        types: ['Files'],
        items: [{ kind: 'file', type: 'image/png', getAsFile: () => file }],
        getData: () => '',
      },
    })
    expect(onUploadFiles).toHaveBeenCalledWith([file])
  })
})

describe('ChatInput paste: clipboard image filename synthesis', () => {
  const pasteImages = (files: File[]) => {
    const onUploadFiles = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} onUploadFiles={onUploadFiles} />,
    )
    fireEvent.paste(screen.getByRole('textbox'), {
      clipboardData: {
        types: ['Files'],
        items: files.map(f => ({ kind: 'file', type: f.type, getAsFile: () => f })),
        getData: () => '',
      },
    })
    return onUploadFiles
  }

  it('renames the browser placeholder "image.png" to a timestamped pasted-image name', () => {
    // Chrome/Firefox hand EVERY pasted screenshot over as "image.png", so two
    // pastes in one message would render identical chip labels.
    const onUploadFiles = pasteImages([new File(['px'], 'image.png', { type: 'image/png' })])
    expect(onUploadFiles).toHaveBeenCalledTimes(1)
    const [uploaded] = onUploadFiles.mock.calls[0][0] as File[]
    expect(uploaded.name).toMatch(/^pasted-image-\d{8}-\d{9}\.png$/)
    expect(uploaded.type).toBe('image/png')
  })

  it('names an UNNAMED clipboard image (server rejects extension-less files)', () => {
    const unnamed = new File(['px'], '', { type: 'image/jpeg' })
    const onUploadFiles = pasteImages([unnamed])
    const [uploaded] = onUploadFiles.mock.calls[0][0] as File[]
    expect(uploaded.name).toMatch(/^pasted-image-\d{8}-\d{9}\.jpg$/)
  })

  it('keeps a real filename untouched (file copied from the OS file manager)', () => {
    const real = new File(['px'], 'photo-vacation.png', { type: 'image/png' })
    const onUploadFiles = pasteImages([real])
    // Same object through — pasted and picked files stay indistinguishable.
    expect(onUploadFiles.mock.calls[0][0]).toEqual([real])
    expect((onUploadFiles.mock.calls[0][0] as File[])[0].name).toBe('photo-vacation.png')
  })

  it('disambiguates multiple generic images in ONE paste (same-millisecond timestamp)', () => {
    const a = new File(['a'], 'image.png', { type: 'image/png' })
    const b = new File(['b'], 'image.png', { type: 'image/png' })
    const onUploadFiles = pasteImages([a, b])
    const [ua, ub] = onUploadFiles.mock.calls[0][0] as File[]
    expect(ua.name).not.toBe(ub.name)
    expect(ub.name).toMatch(/-2\.png$/)
  })

  it('suffixes count only RENAMED files — a real-named sibling produces no orphan "-2"', () => {
    const real = new File(['a'], 'photo-vacation.png', { type: 'image/png' })
    const generic = new File(['b'], 'image.png', { type: 'image/png' })
    const onUploadFiles = pasteImages([real, generic])
    const [ua, ub] = onUploadFiles.mock.calls[0][0] as File[]
    expect(ua.name).toBe('photo-vacation.png')
    // The single synthesized name is unsuffixed: it is the first rename.
    expect(ub.name).toMatch(/^pasted-image-\d{8}-\d{9}\.png$/)
  })

  it('never renames a non-image file (clipboard paste stays image-scoped)', () => {
    const doc = new File(['x'], 'notes.txt', { type: 'text/plain' })
    const onUploadFiles = pasteImages([doc])
    expect((onUploadFiles.mock.calls[0][0] as File[])[0].name).toBe('notes.txt')
  })
})

describe('ChatInput optimize: forwards paste content', () => {
  it('sends referenced paste blocks (seq + content) to the optimizer', async () => {
    const token = '[ Paste #1 · 40 lines ]'
    const value = `whats wrong with ${token}`
    const pasteBlocks = [{ id: 'a1', seq: 1, lines: 40, content: 'TRACEBACK: boom' }]

    // URL-aware mock: optimizer endpoint returns the optimize shape; any other
    // app fetch (e.g. SlashCommandMenu's command list) gets a benign empty array
    // so unrelated components don't throw on an unexpected response shape.
    const fetchMock = vi.fn((url: string) => {
      if (typeof url === 'string' && url.includes('/api/optimizer/optimize')) {
        return Promise.resolve({ ok: true, json: async () => ({ changed: false, optimized: value }) })
      }
      return Promise.resolve({ ok: true, json: async () => [] })
    })
    vi.stubGlobal('fetch', fetchMock)
    // jsdom has no execCommand; the optimizer's onSuccess write-back uses it.
    // Stub it so the post-fetch text write doesn't throw after the assertion.
    ;(document as unknown as { execCommand: () => boolean }).execCommand = vi.fn(() => true)

    renderWithProviders(
      <ChatInput
        value={value}
        onChange={vi.fn()}
        onSend={vi.fn()}
        connected={true}
        pasteBlocks={pasteBlocks}
        onPasteBlocksChange={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Optimize prompt' }))

    // The optimize request must carry the full paste content keyed by seq, so
    // the backend can forward it to the model without expanding the token.
    // Find the optimizer call specifically — other app fetches may fire too.
    await vi.waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (c) => typeof c[0] === 'string' && (c[0] as string).includes('/api/optimizer/optimize'),
      )
      expect(call).toBeTruthy()
    })
    const call = fetchMock.mock.calls.find(
      (c) => typeof c[0] === 'string' && (c[0] as string).includes('/api/optimizer/optimize'),
    )!
    const body = JSON.parse((call[1] as RequestInit).body as string)
    expect(body.prompt).toBe(value)
    expect(body.pastes).toEqual([{ seq: 1, content: 'TRACEBACK: boom' }])
  })
})

describe('ChatInput optimize: promptOptimizer capability gates the keyboard shortcut', () => {
  // The promptOptimizer opt-out must cover EVERY optimize entry point, not just
  // the button and plus-menu row. A host that passed promptOptimizer={false}
  // (the side panel) treats the draft as literal text; Cmd/Ctrl+Shift+Enter
  // reaching optimizePrompt() there would rewrite that draft and lock the box
  // readOnly mid-flight. Pinned by both review lanes on PR #5128 round 9.
  const setup = (promptOptimizer: boolean) => {
    const fetchMock = vi.fn((url: string) => {
      if (typeof url === 'string' && url.includes('/api/optimizer/optimize')) {
        return Promise.resolve({ ok: true, json: async () => ({ changed: false, optimized: 'x' }) })
      }
      return Promise.resolve({ ok: true, json: async () => [] })
    })
    vi.stubGlobal('fetch', fetchMock)
    ;(document as unknown as { execCommand: () => boolean }).execCommand = vi.fn(() => true)
    renderWithProviders(
      <ChatInput
        value="a literal side question"
        onChange={vi.fn()}
        onSend={vi.fn()}
        connected={true}
        promptOptimizer={promptOptimizer}
      />,
    )
    return fetchMock
  }
  const pressOptimizeCombo = () =>
    fireEvent.keyDown(screen.getByRole('textbox'), {
      key: 'Enter',
      metaKey: true,
      shiftKey: true,
    })
  const optimizerCalls = (fetchMock: ReturnType<typeof vi.fn>) =>
    fetchMock.mock.calls.filter(
      (c) => typeof c[0] === 'string' && (c[0] as string).includes('/api/optimizer/optimize'),
    )

  it('does NOT call the optimizer on Cmd+Shift+Enter when promptOptimizer is off', async () => {
    const fetchMock = setup(false)
    pressOptimizeCombo()
    // Give any wrongly-fired request a tick to land before asserting absence.
    await new Promise((r) => setTimeout(r, 0))
    expect(optimizerCalls(fetchMock)).toHaveLength(0)
  })

  it('still calls the optimizer on Cmd+Shift+Enter when promptOptimizer is on (default)', async () => {
    const fetchMock = setup(true)
    pressOptimizeCombo()
    await vi.waitFor(() => expect(optimizerCalls(fetchMock).length).toBeGreaterThan(0))
  })
})

describe('ChatInput paste: showFullPastes', () => {
  const pasteText = (textarea: HTMLElement, text: string) =>
    fireEvent.paste(textarea, {
      clipboardData: { types: ['text/plain'], items: [], getData: () => text },
    })

  beforeEach(() => {
    ;(document as unknown as { execCommand: (...a: unknown[]) => boolean }).execCommand = vi.fn(() => false)
  })

  // The payload is over both collapse thresholds (4 lines, >200 chars), so the
  // only thing deciding chip-vs-text here is the setting.
  const big = `${'x'.repeat(250)}\nsecond\nthird\nfourth`

  it('collapses a large paste into a chip by default', () => {
    const onChange = vi.fn()
    const onPasteBlocksChange = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={onPasteBlocksChange} />,
    )
    pasteText(screen.getByRole('textbox'), big)
    expect(onChange).toHaveBeenCalledWith(expect.stringMatching(/^\[ Paste #1(?:\u2063[\u200b\u200c]+\u2063)? · 4 lines \]$/))
    expect(onPasteBlocksChange).toHaveBeenCalledWith([expect.objectContaining({ seq: 1, lines: 4, content: big })])
  })

  it('keeps a large paste as plain text when showFullPastes is on', () => {
    const onChange = vi.fn()
    const onPasteBlocksChange = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={onPasteBlocksChange} showFullPastes />,
    )
    // Trailing blank lines make handlePaste take its own insert path, so the
    // full text is observable here rather than being left to the browser.
    pasteText(screen.getByRole('textbox'), `${big}\n\n`)
    expect(onChange).toHaveBeenCalledWith(big)
    expect(onPasteBlocksChange).not.toHaveBeenCalled()
  })

  it('leaves a clean large paste entirely to the browser when showFullPastes is on', () => {
    const onChange = vi.fn()
    const onPasteBlocksChange = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={onPasteBlocksChange} showFullPastes />,
    )
    pasteText(screen.getByRole('textbox'), big)
    // Nothing to clean and nothing to collapse — the handler must not
    // preventDefault, or the paste becomes a silent no-op.
    expect(onChange).not.toHaveBeenCalled()
    expect(onPasteBlocksChange).not.toHaveBeenCalled()
  })
})

describe('ChatInput paste: raw-paste chord', () => {
  const big = `${'x'.repeat(250)}\nsecond\nthird\nfourth`
  const chordThenPaste = (init: KeyboardEventInit) => {
    const onPasteBlocksChange = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} onPasteBlocksChange={onPasteBlocksChange} />,
    )
    const textarea = screen.getByRole('textbox')
    fireEvent.keyDown(textarea, init)
    fireEvent.paste(textarea, { clipboardData: { types: ['text/plain'], items: [], getData: () => big } })
    return onPasteBlocksChange
  }

  it.each([
    ['Ctrl+Shift+V', { key: 'V', code: 'KeyV', ctrlKey: true, shiftKey: true }],
    ['Cmd+Option+Shift+V', { key: '◊', code: 'KeyV', metaKey: true, altKey: true, shiftKey: true }],
  ])('keeps a large paste after %s out of a chip', (_chord, init) => {
    expect(chordThenPaste(init)).not.toHaveBeenCalled()
  })

  it.each([
    ['Cmd+Option+V', { key: '√', code: 'KeyV', metaKey: true, altKey: true }],
    ['Ctrl+Alt+Shift+V', { key: 'V', code: 'KeyV', ctrlKey: true, altKey: true, shiftKey: true }],
  ])('still collapses a large paste after %s', (_chord, init) => {
    expect(chordThenPaste(init)).toHaveBeenCalledWith([expect.objectContaining({ seq: 1, lines: 4, content: big })])
  })
})

describe('ChatInput paste: strip trailing blank lines', () => {
  const pasteText = (textarea: HTMLElement, text: string) =>
    fireEvent.paste(textarea, {
      clipboardData: { types: ['text/plain'], items: [], getData: () => text },
    })

  // handlePaste prefers the native document.execCommand('insertText') path so
  // the textarea's own onChange fires. jsdom's execCommand is an unreliable
  // no-op, so by default force it to report failure — that exercises the
  // controlled-value fallback these assertions check. The native path gets its
  // own dedicated test that stubs execCommand to succeed.
  beforeEach(() => {
    ;(document as unknown as { execCommand: (...a: unknown[]) => boolean }).execCommand = vi.fn(() => false)
  })

  it('trims trailing blank lines a single-line copy carries in (line + empty rows)', () => {
    const onChange = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={vi.fn()} />,
    )
    pasteText(screen.getByRole('textbox'), 'just one line\n\n\n')
    expect(onChange).toHaveBeenCalledWith('just one line')
  })

  it('trims a single trailing newline too', () => {
    const onChange = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={vi.fn()} />,
    )
    pasteText(screen.getByRole('textbox'), 'hello world\n')
    expect(onChange).toHaveBeenCalledWith('hello world')
  })

  it('strips only the trailing run, not earlier line breaks', () => {
    const onChange = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={vi.fn()} />,
    )
    pasteText(screen.getByRole('textbox'), 'line1\nline2\n\n')
    expect(onChange).toHaveBeenCalledWith('line1\nline2')
  })

  it('does NOT intercept a clean paste (no trailing blanks) — leaves it to the browser', () => {
    const onChange = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={vi.fn()} />,
    )
    pasteText(screen.getByRole('textbox'), 'clean line')
    // No preventDefault path taken → onChange fires via the native input event,
    // which jsdom does not dispatch for fireEvent.paste, so our handler stays out.
    expect(onChange).not.toHaveBeenCalled()
  })

  it('leaves a trailing-spaces-only paste untouched (no newline in the run)', () => {
    const onChange = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={vi.fn()} />,
    )
    pasteText(screen.getByRole('textbox'), 'trailing spaces   ')
    expect(onChange).not.toHaveBeenCalled()
  })

  it('does NOT intercept an all-blank-lines clipboard (leaves it to the browser, never a silent no-op)', () => {
    const onChange = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={vi.fn()} />,
    )
    pasteText(screen.getByRole('textbox'), '\n\n\n')
    expect(onChange).not.toHaveBeenCalled()
  })

  it('handles a large space run without pathological backtracking (linear strip)', () => {
    const onChange = vi.fn()
    const onPasteBlocksChange = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={onPasteBlocksChange} />,
    )
    // 200k spaces + a char — the exact shape that made the old trailing-strip
    // regex backtrack quadratically (~2s). The linear scan stops at the first
    // non-whitespace char from the end, so it must finish near-instantly. The
    // chunk is >200 chars so it collapses into a chip.
    const payload = ' '.repeat(200_000) + 'x'
    const t0 = performance.now()
    pasteText(screen.getByRole('textbox'), payload)
    const elapsed = performance.now() - t0
    expect(onPasteBlocksChange).toHaveBeenCalled()
    expect(elapsed).toBeLessThan(1000)
  })

  it('uses the native execCommand insertText path when it verifiably inserted (keeps the real input pipeline)', () => {
    const onChange = vi.fn()
    const textarea = () => screen.getByRole('textbox') as HTMLTextAreaElement
    // A truthful stub: the real execCommand mutates the field. Anything that
    // only returns true without touching the DOM is the iOS shape covered below.
    const exec = vi.fn((_cmd: unknown, _ui: unknown, text: unknown) => {
      textarea().value = String(text)
      return true
    })
    ;(document as unknown as { execCommand: (...a: unknown[]) => boolean }).execCommand = exec
    renderWithProviders(
      <ChatInput value="" onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={vi.fn()} />,
    )
    pasteText(textarea(), 'just one line\n\n\n')
    // Inserted via the native input pipeline with the trimmed text …
    expect(exec).toHaveBeenCalledWith('insertText', false, 'just one line')
    // … and the controlled value is reconciled to exactly what landed in the
    // DOM. In a browser the textarea's own onChange has already reported this
    // same string, so React bails; asserting it here is what keeps a native
    // insert React never saw from being reverted to the stale `value` prop.
    expect(onChange).toHaveBeenCalledWith('just one line')
  })

  it('still lands the paste when execCommand reports success but inserts NOTHING (iOS native paste callout)', () => {
    // The regression this guards: handlePaste has already called
    // preventDefault(), so returning on the strength of the boolean alone means
    // the tap on iOS's "Paste" produces no text and no error — the paste just
    // vanishes. Verified against the DOM, the controlled splice must still run.
    const onChange = vi.fn()
    const exec = vi.fn(() => true) // reports success, leaves the field empty
    ;(document as unknown as { execCommand: (...a: unknown[]) => boolean }).execCommand = exec
    renderWithProviders(
      <ChatInput value="" onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={vi.fn()} />,
    )
    pasteText(screen.getByRole('textbox'), 'just one line\n\n\n')
    expect(exec).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenCalledWith('just one line')
  })

  it('positions the caret itself when the native insert only CLAIMED to work (the DOM read-back is load-bearing)', async () => {
    // The other two cases pin that the text lands. This one pins the read-back
    // that decides it: `inserted && ta.value === next`. Trusting the boolean
    // alone still lands the text (onChange runs before the early return) but
    // skips our caret placement, leaving the caret wherever the browser left
    // it — so without a caret assertion that half of the fix is untested.
    //
    // The paste goes at the START of existing text on purpose. Assigning to
    // `.value` parks the caret at the end, which is exactly where our own
    // placement would land for an end-of-field paste — the two outcomes would
    // coincide and the assertion would prove nothing.
    const Host = () => {
      const [v, setV] = useState('AB')
      return <ChatInput value={v} onChange={setV} onSend={vi.fn()} onPasteBlocksChange={vi.fn()} />
    }
    // The iOS shape: reports success, never touches the field.
    ;(document as unknown as { execCommand: (...a: unknown[]) => boolean }).execCommand = vi.fn(() => true)
    renderWithProviders(<Host />)
    const ta = screen.getByRole('textbox') as HTMLTextAreaElement
    ta.focus() // the rAF is gated on document.activeElement === ta
    ta.setSelectionRange(0, 0)
    pasteText(ta, 'x\n\n')
    await vi.waitFor(() => expect(ta.value).toBe('xAB'))
    // Caret sits after the inserted text, not at the end of the field.
    await vi.waitFor(() => expect(ta.selectionStart).toBe(1))
    expect(ta.selectionEnd).toBe(1)
  })

  it('reconciles against the DOM, not the requested text (a partial native insert is not success)', () => {
    // A native path that inserts something OTHER than what was asked for must
    // not be treated as authoritative either — the controlled value is the one
    // the send path reads, so it has to be written explicitly.
    const onChange = vi.fn()
    const textarea = () => screen.getByRole('textbox') as HTMLTextAreaElement
    ;(document as unknown as { execCommand: (...a: unknown[]) => boolean }).execCommand = vi.fn(() => {
      textarea().value = 'just one'
      return true
    })
    renderWithProviders(
      <ChatInput value="" onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={vi.fn()} />,
    )
    pasteText(textarea(), 'just one line\n\n\n')
    expect(onChange).toHaveBeenCalledWith('just one line')
  })
})

describe('ChatInput paste: value-change seam over a staged @mention (#18263)', () => {
  // The two intercepting paste branches (large-paste collapse, trailing-blank
  // strip) preventDefault and splice the value themselves, so no
  // `insertFromPaste` beforeinput fires and the native mention seam never sees
  // them. The interceptor must widen the replaced range through
  // `mentionEditRange` on its own, else pasting over `main.ts` inside
  // `@src/main.ts` leaves the half-reference `@src/` and the chip unstages.
  //
  // Here `mentionEditRange` is stubbed to report the whole `@src/main.ts` span
  // whenever the selection cuts into it; the real span scan is covered by the
  // composerFileMentions unit tests and the Playwright spec. This test proves
  // paste.ts routes its splice through whatever range the stub returns.
  const value = 'review @src/main.ts now'
  const mentionAt = value.indexOf('@src/main.ts')
  const mentionEnd = mentionAt + '@src/main.ts'.length
  // Stub: expand any selection that cuts strictly into the mention to the whole span.
  const mentionEditRange = (_t: string, s: number, e: number) =>
    (mentionAt < s && s < mentionEnd) || (mentionAt < e && e < mentionEnd)
      ? { start: Math.min(s, mentionAt), end: Math.max(e, mentionEnd) }
      : null
  // Select the `main.ts` fragment inside the mention (a cut-into selection).
  const selStart = value.indexOf('main.ts')
  const selEnd = selStart + 'main.ts'.length
  const selectFragment = (ta: HTMLTextAreaElement) => {
    ta.focus()
    ta.setSelectionRange(selStart, selEnd)
  }
  const big = `${'x'.repeat(250)}\nsecond\nthird\nfourth`

  beforeEach(() => {
    ;(document as unknown as { execCommand: (...a: unknown[]) => boolean }).execCommand = vi.fn(() => false)
  })

  it('a LARGE paste over the mention splices out the whole mention, not just the selection', () => {
    const onChange = vi.fn()
    const onPasteBlocksChange = vi.fn()
    renderWithProviders(
      <ChatInput value={value} onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={onPasteBlocksChange} mentionEditRange={mentionEditRange} />,
    )
    const ta = screen.getByRole('textbox') as HTMLTextAreaElement
    selectFragment(ta)
    fireEvent.paste(ta, { clipboardData: { types: ['text/plain'], items: [], getData: () => big } })
    // The big paste collapses to a chip, and the spliced value drops the WHOLE
    // `@src/main.ts` — no `@src/` fragment survives.
    const next = onChange.mock.calls.at(-1)?.[0] as string
    expect(next).not.toMatch(/@src\//)
    expect(next.startsWith('review ')).toBe(true)
    expect(next.endsWith(' now')).toBe(true)
    expect(onPasteBlocksChange).toHaveBeenCalledTimes(1)
  })

  it('a trailing-blank small paste over the mention splices out the whole mention', () => {
    const onChange = vi.fn()
    renderWithProviders(
      <ChatInput value={value} onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={vi.fn()} mentionEditRange={mentionEditRange} />,
    )
    const ta = screen.getByRole('textbox') as HTMLTextAreaElement
    selectFragment(ta)
    // Trailing blank lines force the handler's own insert path (execCommand stubbed false).
    fireEvent.paste(ta, { clipboardData: { types: ['text/plain'], items: [], getData: () => 'README.md\n\n' } })
    expect(onChange).toHaveBeenCalledWith('review README.md now')
  })

  it('a paste whose selection does NOT cut into a mention is spliced normally', () => {
    const onChange = vi.fn()
    renderWithProviders(
      <ChatInput value={value} onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={vi.fn()} mentionEditRange={mentionEditRange} />,
    )
    const ta = screen.getByRole('textbox') as HTMLTextAreaElement
    ta.focus()
    // Select the trailing "now" — outside the mention, so the range is unchanged.
    const s = value.indexOf('now')
    ta.setSelectionRange(s, s + 'now'.length)
    fireEvent.paste(ta, { clipboardData: { types: ['text/plain'], items: [], getData: () => 'later\n\n' } })
    expect(onChange).toHaveBeenCalledWith('review @src/main.ts later')
  })

  it('a COLLAPSED-caret paste inside a mention inserts there, it does not delete the mention', () => {
    // A collapsed caret inside `@src/main.ts` is a plain insertion the user is
    // making INSIDE the mention, not an edit over it — pasting must insert at
    // the caret, never expand the empty range to the whole mention and delete
    // it (GPT 6.1). Mirrors the beforeinput seam's `s === en` guard.
    const onChange = vi.fn()
    renderWithProviders(
      <ChatInput value={value} onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={vi.fn()} mentionEditRange={mentionEditRange} />,
    )
    const ta = screen.getByRole('textbox') as HTMLTextAreaElement
    ta.focus()
    // Caret collapsed strictly inside the mention, right before `main.ts`.
    const caret = value.indexOf('main.ts')
    ta.setSelectionRange(caret, caret)
    // Trailing blanks force the handler's own insert path (execCommand stubbed false).
    fireEvent.paste(ta, { clipboardData: { types: ['text/plain'], items: [], getData: () => 'X\n\n' } })
    // Inserted at the caret; the whole mention text survives.
    expect(onChange).toHaveBeenCalledWith('review @src/Xmain.ts now')
  })

  it('a CUT over a paste token that also cuts into a mention splices out the whole mention', () => {
    // handleCut is the second sibling of the paste interceptor the First
    // Principles review named (#18263 item 3): it preventDefaults and splices
    // the value itself, so no `deleteByCut` beforeinput fires and the native
    // seam never runs. A cut whose selection covers a `[ Paste #N ]` token and
    // also cuts into a staged mention must widen the excised range to the whole
    // mention, else `@src/` is stranded and the chip unstages.
    const token = '[ Paste #1 · 3 lines ]'
    const cutValue = `${token} @src/main.ts`
    const pasteBlocks = [{ id: 'c1', seq: 1, lines: 3, content: 'one\ntwo\nthree' }]
    const mAt = cutValue.indexOf('@src/main.ts')
    const mEnd = mAt + '@src/main.ts'.length
    const cutRange = (_t: string, s: number, e: number) =>
      (mAt < s && s < mEnd) || (mAt < e && e < mEnd)
        ? { start: Math.min(s, mAt), end: Math.max(e, mEnd) }
        : null
    const onChange = vi.fn()
    const onPasteBlocksChange = vi.fn()
    renderWithProviders(
      <ChatInput
        value={cutValue}
        onChange={onChange}
        onSend={vi.fn()}
        pasteBlocks={pasteBlocks}
        onPasteBlocksChange={onPasteBlocksChange}
        mentionEditRange={cutRange}
      />,
    )
    const ta = screen.getByRole('textbox') as HTMLTextAreaElement
    ta.focus()
    // Select from the token's start into the middle of the mention (`main.ts`).
    const selFrom = 0
    const selTo = cutValue.indexOf('main.ts')
    ta.setSelectionRange(selFrom, selTo)
    const setData = vi.fn()
    fireEvent.cut(ta, { clipboardData: { setData, getData: () => '' } })
    // The spliced value drops the WHOLE `@src/main.ts` — no `@src/` fragment
    // survives — because handleCut widened its range through mentionEditRange.
    const next = onChange.mock.calls.at(-1)?.[0] as string
    expect(next).not.toMatch(/@src\//)
    // The paste token's expanded content still reached the clipboard.
    expect(setData).toHaveBeenCalledWith('text/plain', expect.stringContaining('one\ntwo\nthree'))
  })
})
