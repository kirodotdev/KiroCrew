/**
 * useComposerDraft — the composer's draft behaviour, now owned by the chat SDK.
 *
 * These pin the semantics that had drifted between the three composers, so a
 * future surface that re-derives one of them fails here instead of shipping a
 * fourth spelling. Every assertion is stated as the user-visible outcome ("the
 * released text is appended, not substituted"), because that is the thing a
 * reimplementation gets wrong — not the code shape.
 */
import { describe, it, expect, vi } from 'vitest'
import { StrictMode, type FocusEvent, type KeyboardEvent } from 'react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { renderHook, act } from '@testing-library/react'
import { useComposerDraft, pickedFromDraft, draftByteSize } from '../app-sdk/useComposerDraft'

describe('pickedFromDraft', () => {
  it('reads picks off the `, `-joined tail in the order they appear', () => {
    expect(pickedFromDraft('Merge it now, Show me the diff', ['Merge it now', 'Show me the diff']))
      .toEqual(['Merge it now', 'Show me the diff'])
  })

  it('reports nothing for an option the user has woven into their own sentence', () => {
    // No longer a removable block: it is prose, not a tail.
    expect(pickedFromDraft('should I Merge it now or wait', ['Merge it now'])).toEqual([])
  })

  it('peels the longest match so an option containing another peels as itself', () => {
    const options = ['bar', 'foo, bar']
    expect(pickedFromDraft('foo, bar', options)).toEqual(['foo, bar'])
  })

  it('keeps the user prose ahead of the picked block out of the picks', () => {
    expect(pickedFromDraft('here is context, Merge it now', ['Merge it now']))
      .toEqual(['Merge it now'])
  })
})

describe('draftByteSize', () => {
  it('measures UTF-8 bytes, not code units, so a non-ASCII draft is not under-counted', () => {
    // 4 characters, 8 UTF-8 bytes -- sizing by `.length` would report 4 and let a
    // submit through that the server refuses. Any non-ASCII script does this; a CJK
    // or emoji draft is worse still at three and four bytes per character.
    expect(draftByteSize('αβγδ')).toBe(8)
  })

  it('counts an astral-plane emoji as its four bytes', () => {
    expect(draftByteSize('👻')).toBe(4)
  })
})

/**
 * Drift guard: the side panel must CONSUME the SDK's draft behaviour, not carry a
 * second copy of it.
 *
 * Asserted against the source because that is where the regression happens: the
 * behavioural tests above keep passing while a reintroduced local helper quietly
 * diverges, which is exactly how the three implementations came to exist.
 */
describe('SideChat consumes the SDK draft behaviour', () => {
  const src = readFileSync(join(__dirname, '..', 'pages', 'chat', 'SideChat.tsx'), 'utf-8')

  it('imports the hook instead of defining the draft helpers itself', () => {
    // Either the module or the barrel is fine -- pinning one spelling would fail on an
    // innocent import tidy-up while the behaviour it guards is untouched.
    expect(src).toMatch(/from '\.\.\/\.\.\/app-sdk(\/useComposerDraft)?'/)
    expect(src).toMatch(/useComposerDraft\(\{/)
  })

  it('carries no local copy of the helpers the SDK now owns', () => {
    expect(src).not.toMatch(/function\s+pickedFromDraft\s*\(/)
    expect(src).not.toMatch(/function\s+mergeDraft\s*\(/)
    // The byte check must go through the hook, so the limit is measured one way.
    expect(src).not.toContain('new Blob([q]).size')
  })

  it('delegates Enter and IME to the native composer, so an IME commit is never a send', () => {
    // ChatInput owns the keydown path and its IME guard (useImeGuard). A
    // hand-rolled Enter branch here is what missed the IME guard originally.
    expect(src).toContain('<ChatInput')
    expect(src).not.toContain('submitOnEnter(')
    expect(src).not.toMatch(/e\.key === 'Enter' && !e\.shiftKey/)
  })

  it('resolves the composer textarea for the Select-to-Ask seed through its own wrapper', () => {
    // The seed handler places the caret by querying the native composer's
    // textarea inside SideChat's own wrapper — no dedicated ref prop on
    // ChatInput. Without this the side-seed event prefills a draft the user
    // cannot see focused.
    expect(src).toContain("composerWrapRef.current?.querySelector<HTMLTextAreaElement>('textarea[data-composer-input]')")
  })
})

describe('useComposerDraft', () => {
  const setup = (followUpOptions: string[] = [], maxBytes = 0) =>
    renderHook(({ options }: { options: string[] }) =>
      useComposerDraft({ followUpOptions: options, maxBytes }),
    { initialProps: { options: followUpOptions } })

  it('toggles an option into the draft and back out again', () => {
    const { result } = setup(['Merge it now', 'Show me the diff'])

    act(() => result.current.toggleOption('Merge it now'))
    expect(result.current.draft).toBe('Merge it now')
    expect([...result.current.picked]).toEqual(['Merge it now'])

    act(() => result.current.toggleOption('Show me the diff'))
    expect(result.current.draft).toBe('Merge it now, Show me the diff')

    act(() => result.current.toggleOption('Merge it now'))
    expect(result.current.draft).toBe('Show me the diff')
    expect([...result.current.picked]).toEqual(['Show me the diff'])
  })

  it('restores the punctuation it consumed when the last pick is removed', () => {
    const { result } = setup(['Ship it'])
    act(() => result.current.setDraft('some context,'))
    act(() => result.current.toggleOption('Ship it'))
    expect(result.current.draft).toBe('some context, Ship it')
    act(() => result.current.toggleOption('Ship it'))
    // The trailing comma the draft already had is put back verbatim, not swallowed.
    expect(result.current.draft).toBe('some context,')
  })

  it('appends the picked block after the user prose instead of replacing it', () => {
    const { result } = setup(['Ship it'])
    act(() => result.current.setDraft('but check CI first'))
    act(() => result.current.toggleOption('Ship it'))
    expect(result.current.draft).toBe('but check CI first, Ship it')
  })

  it('leaves no dangling separator when the draft was edited between toggles', () => {
    // The memo fast-path is void once the user has typed, so the base has to be
    // recovered from the text — including the `, ` the toggle itself inserted.
    // Getting that wrong leaves the composer holding "context edited, " and the
    // user submits a trailing separator they never typed.
    const { result } = setup(['Ship it'])
    act(() => result.current.setDraft('context'))
    act(() => result.current.toggleOption('Ship it'))
    expect(result.current.draft).toBe('context, Ship it')
    act(() => result.current.setDraft('context edited, Ship it'))
    act(() => result.current.toggleOption('Ship it'))
    expect(result.current.draft).toBe('context edited')
  })

  it('keeps a pick as text after it stops being offered', () => {
    const { result, rerender } = setup(['Ship it'])
    act(() => result.current.toggleOption('Ship it'))
    expect([...result.current.picked]).toEqual(['Ship it'])
    rerender({ options: [] })
    // The text stays — it is the user's. It also stays PICKED while the draft is
    // untouched, because the memo knows what was chosen; the offered list controls
    // what `FollowUpBar` draws, so an unoffered pick is never rendered anyway.
    expect(result.current.draft).toBe('Ship it')
  })

  it('stops treating an unoffered option as a removable block once the draft is edited', () => {
    const { result, rerender } = setup(['Ship it'])
    act(() => result.current.toggleOption('Ship it'))
    rerender({ options: [] })
    // Typing voids the memo, so the picks fall back to text derivation against the
    // CURRENT offered list — which no longer contains it.
    act(() => result.current.setDraft('Ship it and more'))
    expect([...result.current.picked]).toEqual([])
  })

  describe('overlapping options', () => {
    // One option is another plus the separator, so reading the picks back off the text
    // is ambiguous: the scan prefers the longest match and reports a set the user never
    // chose. A base inferred from that wrong set deletes the user's own prose.
    const OVERLAP = ['Run tests', 'Check CI, Run tests']

    it('highlights the option the user actually picked, not the longer superstring', () => {
      const { result } = setup(OVERLAP)
      act(() => result.current.setDraft('Check CI'))
      act(() => result.current.toggleOption('Run tests'))
      expect(result.current.draft).toBe('Check CI, Run tests')
      expect([...result.current.picked]).toEqual(['Run tests'])
    })

    it('never erases the prose the draft started with', () => {
      const { result } = setup(OVERLAP)
      act(() => result.current.setDraft('Check CI'))
      act(() => result.current.toggleOption('Run tests'))
      // Unpicking returns the draft to exactly what the user had typed.
      act(() => result.current.toggleOption('Run tests'))
      expect(result.current.draft).toBe('Check CI')
    })

    it('adds the longer option instead of consuming the draft when it is clicked', () => {
      const { result } = setup(OVERLAP)
      act(() => result.current.setDraft('Check CI'))
      act(() => result.current.toggleOption('Run tests'))
      act(() => result.current.toggleOption('Check CI, Run tests'))
      // It was not among the picks, so clicking it is an ADD. Whatever the wording, the
      // one thing that must hold is that the user's 'Check CI' survives.
      expect(result.current.draft).toContain('Check CI')
      expect(result.current.draft).not.toBe('')
      expect([...result.current.picked]).toEqual(['Run tests', 'Check CI, Run tests'])
    })
  })

  it('does not delete a pick that stopped being offered when another is toggled off', () => {
    // The regression the memo's block key exists for. Two options are picked, then a
    // new turn offers only one of them. Removing the still-offered pick must not take
    // the other away: it is text the user chose and never asked to remove.
    const { result, rerender } = setup(['Alpha', 'Beta'])
    act(() => result.current.setDraft('context'))
    act(() => result.current.toggleOption('Alpha'))
    act(() => result.current.toggleOption('Beta'))
    expect(result.current.draft).toBe('context, Alpha, Beta')
    rerender({ options: ['Beta'] })
    act(() => result.current.toggleOption('Beta'))
    expect(result.current.draft).toBe('context, Alpha')
  })

  it('merges handed-back text into the draft without discarding it', () => {
    const { result } = setup()
    act(() => result.current.setDraft('half-written question'))
    act(() => result.current.mergeIntoDraft('cancelled queue entry'))
    expect(result.current.draft).toBe('half-written question\n\ncancelled queue entry')
  })

  it('refuses a submit above the byte limit, measured in UTF-8 bytes', () => {
    const { result } = setup([], 10)
    expect(result.current.exceedsByteLimit('short')).toBe(false)
    // 6 two-byte characters = 12 bytes > 10, while `.length` would be 6 and pass.
    expect(result.current.exceedsByteLimit('αβγδεζ')).toBe(true)
  })

  it('allows a submit that lands exactly on the limit', () => {
    // The bound is "above", not "at" — an off-by-one here refuses a draft the server
    // would have accepted, and the user has no way to tell why.
    const { result } = setup([], 10)
    expect(result.current.exceedsByteLimit('0123456789')).toBe(false)
    expect(result.current.exceedsByteLimit('0123456789!')).toBe(true)
  })

  it('applies no client-side limit when none was given', () => {
    const { result } = setup()
    expect(result.current.exceedsByteLimit('x'.repeat(1_000_000))).toBe(false)
  })

  it('seeds from initialDraft, and ignores later changes to it', () => {
    const { result, rerender } = renderHook(
      ({ seed }: { seed: string }) => useComposerDraft({ initialDraft: seed }),
      { initialProps: { seed: 'seeded' } },
    )
    expect(result.current.draft).toBe('seeded')
    act(() => result.current.setDraft('typed over it'))
    rerender({ seed: 'a different seed' })
    // Re-seeding a draft the user has since typed into would destroy their text.
    expect(result.current.draft).toBe('typed over it')
  })

  describe('under StrictMode', () => {
    /**
     * StrictMode double-invokes state updaters to surface impure ones. A toggle has to
     * remember what it wrote (so removing the block can restore the punctuation it
     * consumed), and doing that remembering INSIDE an updater means the second pass sees
     * a memo the first pass already overwrote — it then infers the base from the text and
     * silently eats the user's trailing comma.
     *
     * Asserted through StrictMode specifically: every other test in this file renders
     * without it and passes either way, so none of them can see this.
     */
    const strict = (options: string[]) => renderHook(
      () => useComposerDraft({ followUpOptions: options }),
      { wrapper: ({ children }) => <StrictMode>{children}</StrictMode> },
    )

    it('restores the punctuation a toggle consumed', () => {
      const { result } = strict(['Ship it'])
      act(() => result.current.setDraft('some context,'))
      act(() => result.current.toggleOption('Ship it'))
      expect(result.current.draft).toBe('some context, Ship it')
      act(() => result.current.toggleOption('Ship it'))
      expect(result.current.draft).toBe('some context,')
    })

    it('round-trips two picks without disturbing the prose', () => {
      const { result } = strict(['Alpha', 'Beta'])
      act(() => result.current.setDraft('context'))
      act(() => result.current.toggleOption('Alpha'))
      act(() => result.current.toggleOption('Beta'))
      expect(result.current.draft).toBe('context, Alpha, Beta')
      act(() => result.current.toggleOption('Alpha'))
      expect(result.current.draft).toBe('context, Beta')
    })
  })

  describe('controlled mode', () => {
    it('reads the caller value and reports writes instead of storing them', () => {
      const onDraftChange = vi.fn()
      const { result, rerender } = renderHook(
        ({ draft }: { draft: string }) => useComposerDraft({ draft, onDraftChange }),
        { initialProps: { draft: 'owned by the surface' } },
      )
      expect(result.current.draft).toBe('owned by the surface')
      act(() => result.current.setDraft('next'))
      expect(onDraftChange).toHaveBeenCalledWith('next')
      // Nothing was stored here — the value only changes when the caller says so.
      expect(result.current.draft).toBe('owned by the surface')
      rerender({ draft: 'next' })
      expect(result.current.draft).toBe('next')
    })

    it('resolves an updater against the caller value, never an internal one', () => {
      const onDraftChange = vi.fn()
      const { result } = renderHook(() =>
        useComposerDraft({ draft: 'from the surface', onDraftChange }))
      act(() => result.current.setDraft(prev => prev + ' + more'))
      expect(onDraftChange).toHaveBeenCalledWith('from the surface + more')
    })

    it('routes a follow-up toggle through the caller as well', () => {
      const onDraftChange = vi.fn()
      const { result } = renderHook(() =>
        useComposerDraft({ draft: 'context', followUpOptions: ['Ship it'], onDraftChange }))
      act(() => result.current.toggleOption('Ship it'))
      expect(onDraftChange).toHaveBeenCalledWith('context, Ship it')
    })
  })

  describe('auto-grow', () => {
    /** jsdom never lays out, so `scrollHeight` is pinned to model a given content height. */
    const attach = (scrollHeight: number) => {
      const el = document.createElement('textarea')
      Object.defineProperty(el, 'scrollHeight', { value: scrollHeight, configurable: true })
      document.body.appendChild(el)
      return el
    }

    it('grows to the content height and stops at the cap', () => {
      const { result } = renderHook(() => useComposerDraft({ maxHeight: 240 }))
      const el = attach(120)
      result.current.textareaRef.current = el
      act(() => result.current.setDraft('a few lines'))
      expect(el.style.height).toBe('120px')

      const tall = attach(9999)
      result.current.textareaRef.current = tall
      act(() => result.current.setDraft('a very long question'))
      // Past the cap the box scrolls instead of growing without bound.
      expect(tall.style.height).toBe('240px')
    })

    it('restores the overflow the surface set, so measuring does not change how it scrolls', () => {
      // The measurement needs overflow hidden, but leaving it hidden would silently
      // disable scrolling on a draft that has reached the cap — the box would clip
      // text the user cannot scroll to.
      const { result } = renderHook(() => useComposerDraft({ maxHeight: 240 }))
      const el = attach(400)
      el.style.overflow = 'auto'
      result.current.textareaRef.current = el
      act(() => result.current.setDraft('overflowing draft'))
      expect(el.style.overflow).toBe('auto')
      expect(el.style.height).toBe('240px')
    })
  })

  describe('submitOnEnter', () => {
    const keyEvent = (over: Record<string, unknown> = {}) => ({
      key: 'Enter',
      shiftKey: false,
      keyCode: 13,
      nativeEvent: { isComposing: false },
      preventDefault: vi.fn(),
      ...over,
    }) as unknown as KeyboardEvent<HTMLTextAreaElement>

    it('submits on a plain Enter', () => {
      const { result } = setup()
      const submit = vi.fn()
      const e = keyEvent()
      act(() => result.current.submitOnEnter(e, submit))
      expect(submit).toHaveBeenCalledTimes(1)
      expect(e.preventDefault).toHaveBeenCalled()
    })

    it('inserts a newline on Shift+Enter instead of submitting', () => {
      const { result } = setup()
      const submit = vi.fn()
      const e = keyEvent({ shiftKey: true })
      act(() => result.current.submitOnEnter(e, submit))
      expect(submit).not.toHaveBeenCalled()
      expect(e.preventDefault).not.toHaveBeenCalled()
    })

    it('leaves keys other than Enter to the browser entirely', () => {
      const { result } = setup()
      const submit = vi.fn()
      const e = keyEvent({ key: 'a' })
      act(() => result.current.submitOnEnter(e, submit))
      expect(submit).not.toHaveBeenCalled()
      // Swallowing the default here would stop the character being typed at all.
      expect(e.preventDefault).not.toHaveBeenCalled()
    })

    /**
     * The regression this hook exists to close on the side panel: an IME sends a
     * final Enter to COMMIT the candidate the user just chose. Reading it as a
     * submit sends a half-written question, and the text is already gone from the
     * box — there is nothing to recover. Three independent signals are honoured
     * because no single one is reliable across browsers.
     */
    it('does not submit the Enter that commits an IME candidate (native flag)', () => {
      const { result } = setup()
      const submit = vi.fn()
      act(() => result.current.submitOnEnter(keyEvent({ nativeEvent: { isComposing: true } }), submit))
      expect(submit).not.toHaveBeenCalled()
    })

    it('does not submit while the browser reports keyCode 229 (IME processing)', () => {
      const { result } = setup()
      const submit = vi.fn()
      act(() => result.current.submitOnEnter(keyEvent({ keyCode: 229 }), submit))
      expect(submit).not.toHaveBeenCalled()
    })

    it('does not submit between compositionStart and compositionEnd', () => {
      const { result } = setup()
      const submit = vi.fn()
      const e = keyEvent()
      act(() => result.current.composition.onCompositionStart())
      // Both browser signals say "not composing" — only the tracked state knows.
      act(() => result.current.submitOnEnter(e, submit))
      expect(submit).not.toHaveBeenCalled()
      // Declining the submit still consumes the key: the default action would put a
      // line break in the question the user is composing.
      expect(e.preventDefault).toHaveBeenCalled()
    })

    it('submits again once the composition has settled', () => {
      vi.useFakeTimers()
      try {
        const { result } = setup()
        const submit = vi.fn()
        act(() => result.current.composition.onCompositionStart())
        act(() => result.current.composition.onCompositionEnd())
        // Still guarded immediately after commit — the Enter that ended the
        // composition can arrive after compositionEnd.
        act(() => result.current.submitOnEnter(keyEvent(), submit))
        expect(submit).not.toHaveBeenCalled()
        act(() => { vi.advanceTimersByTime(60) })
        act(() => result.current.submitOnEnter(keyEvent(), submit))
        expect(submit).toHaveBeenCalledTimes(1)
      } finally {
        vi.useRealTimers()
      }
    })

    /**
     * The recovery half of the guard's contract. A composition abandoned WITHOUT a
     * `compositionend` (focus moves away mid-composition, an OS-level IME cancel,
     * Escape in some IME/browser pairs) leaves the latch set. Without a recovery
     * path every later Enter takes the composing early-return, and the surface can
     * never send again until it remounts.
     */
    describe('abandoned-composition recovery', () => {
      const blurEvent = () =>
        ({} as unknown as FocusEvent<HTMLTextAreaElement>)

      it('submits again after a composition abandoned by blur', () => {
        const { result } = setup()
        const submit = vi.fn()
        act(() => result.current.composition.onCompositionStart())
        // No compositionEnd — focus just leaves the box mid-composition.
        act(() => result.current.composition.onBlur(blurEvent()))
        act(() => result.current.submitOnEnter(keyEvent(), submit))
        expect(submit).toHaveBeenCalledTimes(1)
      })

      it('submits again after a composition abandoned by Escape', () => {
        const { result } = setup()
        const submit = vi.fn()
        act(() => result.current.composition.onCompositionStart())
        act(() => result.current.submitOnEnter(keyEvent({ key: 'Escape', keyCode: 27 }), submit))
        expect(submit).not.toHaveBeenCalled()
        act(() => result.current.submitOnEnter(keyEvent(), submit))
        expect(submit).toHaveBeenCalledTimes(1)
      })

      it('leaves Escape itself to the surface: no submit, no preventDefault', () => {
        // A surface's own Escape behaviour (closing the panel, dismissing a picker)
        // must still run — the reset is a side effect, not a claim on the key.
        const { result } = setup()
        const submit = vi.fn()
        const e = keyEvent({ key: 'Escape', keyCode: 27 })
        act(() => result.current.submitOnEnter(e, submit))
        expect(submit).not.toHaveBeenCalled()
        expect(e.preventDefault).not.toHaveBeenCalled()
      })

      it('still blocks a genuine composition Enter after recovery is wired', () => {
        // Recovery must not loosen the guard: between compositionStart and blur or
        // Escape, an Enter is an IME commit and must not send.
        const { result } = setup()
        const submit = vi.fn()
        act(() => result.current.composition.onCompositionStart())
        act(() => result.current.submitOnEnter(keyEvent(), submit))
        expect(submit).not.toHaveBeenCalled()
      })
    })
  })
})
