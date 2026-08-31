import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import PinnedPrompt from '../pages/chat/PinnedPrompt'
import {
  derivePinnedPromptText,
  expandPastesCapped,
  nextPinnedPromptState,
  promptBody,
  promptImages,
  promptPreview,
  PINNED_PASTE_HEAD_CHARS,
} from '../utils/pinnedPrompt'
import { formatToken, type PasteBlock } from '../utils/pasteTokens'

// `id` is unique PER BLOCK, as makePasteId mints it -- deriving it from `seq`
// would hand two distinct blocks the same id and mask an aliasing defect.
let blockIds = 0
const block = (seq: number, content: string): PasteBlock =>
  ({ id: `b${seq}-${++blockIds}`, seq, lines: content.split('\n').length, content })

describe('derivePinnedPromptText — paste tokens', () => {
  const paste = block(1, 'first line\nsecond line\nthird line')
  const token = formatToken(paste)

  it('does not leave the token standing in the collapsed preview', () => {
    // The defect: a paste-only prompt pinned as a bare token and said nothing.
    const { text } = derivePinnedPromptText(token, [paste])
    expect(text).not.toContain('Paste #1')
    expect(text).toBe('first line second line third line')
  })

  it('NEGATIVE CONTROL: the pre-fix derivation did leave the token', () => {
    // The old path, pinned so the assertions above cannot pass vacuously.
    expect(promptPreview(token)).toContain('Paste #1')
    expect(promptBody(token)).toContain('Paste #1')
  })

  it('does not leave the token standing in the expanded body either', () => {
    // Expanding used to reward the click with the same placeholder.
    const { body } = derivePinnedPromptText(token, [paste])
    expect(body).not.toContain('Paste #1')
    expect(body).toBe('first line\nsecond line\nthird line')
  })

  it('keeps line structure in the body and flattens it in the preview', () => {
    const { text, body } = derivePinnedPromptText(`before\n${token}\nafter`, [paste])
    expect(text).toBe('before first line second line third line after')
    expect(body).toContain('\n')
    expect(body.startsWith('before\n')).toBe(true)
    expect(body.endsWith('\nafter')).toBe(true)
  })

  it('substitutes every token, and each from its own block', () => {
    const a = block(1, 'alpha')
    const b = block(2, 'beta')
    const { text } = derivePinnedPromptText(`${formatToken(a)} then ${formatToken(b)}`, [a, b])
    expect(text).toBe('alpha then beta')
  })

  it('reduces to the previous behaviour with no blocks', () => {
    const plain = 'just typed text with ```\ncode\n``` in it'
    const derived = derivePinnedPromptText(plain, [])
    expect(derived.text).toBe(promptPreview(plain))
    expect(derived.body).toBe(promptBody(plain))
    expect(derived.images).toEqual(promptImages(plain))
  })

  it('reduces to the previous behaviour when a block has no token in the text', () => {
    // Content whose token was already expanded (or lost) must not be rewritten.
    const orphan = block(9, 'unreferenced')
    const plain = 'no token here'
    expect(derivePinnedPromptText(plain, [orphan]).text).toBe(promptPreview(plain))
    expect(derivePinnedPromptText(plain, [orphan]).body).toBe(promptBody(plain))
  })
})

describe('derivePinnedPromptText — the paste is verbatim text, not markdown', () => {
  it('does not thumbnail an image that was inside the paste', () => {
    // Substituting before the image pass would invent an attachment.
    const p = block(1, 'look at this:\n![alt](/inside.png)\ndone')
    const { images } = derivePinnedPromptText(formatToken(p), [p])
    expect(images).toEqual([])
  })

  it('still collects a real attachment typed alongside the paste', () => {
    const p = block(1, 'pasted body line one\nline two')
    const { images } = derivePinnedPromptText(`![real](/r.png)\n${formatToken(p)}`, [p])
    expect(images).toEqual(['/r.png'])
  })

  it('does not delete a pasted line that spelled image markdown', () => {
    const p = block(1, 'a\n![alt](/inside.png)\nb')
    const { body } = derivePinnedPromptText(formatToken(p), [p])
    expect(body).toContain('![alt](/inside.png)')
  })

  it('does not fold a fence that was inside the paste into an ellipsis', () => {
    const p = block(1, 'x\n```js\nconst a = 1\n```\ny')
    const { body } = derivePinnedPromptText(formatToken(p), [p])
    expect(body).toContain('const a = 1')
    expect(body).toContain('```js')
  })

  it('still folds a fence the user TYPED outside the paste', () => {
    const p = block(1, 'pasted one\npasted two')
    const fence = '```'
    const typed = `see ${fence}\ntyped\n${fence} and ${formatToken(p)}`
    const { text } = derivePinnedPromptText(typed, [p])
    expect(text).toBe('see … and pasted one pasted two')
  })
})

describe('expandPastesCapped', () => {
  it('caps a huge block and marks that it was cut', () => {
    const huge = block(1, 'z'.repeat(PINNED_PASTE_HEAD_CHARS * 3))
    const out = expandPastesCapped(formatToken(huge), [huge])
    expect(out.length).toBe(PINNED_PASTE_HEAD_CHARS + 2)
    expect(out.endsWith(' …')).toBe(true)
  })

  it('does not mark a block that fits', () => {
    const small = block(1, 'short')
    expect(expandPastesCapped(formatToken(small), [small])).toBe('short')
  })

  it('caps at the boundary without adding a false ellipsis', () => {
    // Exactly at the budget is not truncation, so no marker.
    const exact = block(1, 'y'.repeat(PINNED_PASTE_HEAD_CHARS))
    const out = expandPastesCapped(formatToken(exact), [exact])
    expect(out.length).toBe(PINNED_PASTE_HEAD_CHARS)
    expect(out.endsWith('…')).toBe(false)
  })

  it('bounds what the card can ever be handed, per block', () => {
    // The negative control for the cap: without it, the card would receive the
    // whole paste and the scroll-path derivation would walk it every frame.
    const a = block(1, 'a'.repeat(PINNED_PASTE_HEAD_CHARS * 2))
    const b = block(2, 'b'.repeat(PINNED_PASTE_HEAD_CHARS * 2))
    const raw = `${formatToken(a)}${formatToken(b)}`
    const out = expandPastesCapped(raw, [a, b])
    expect(out.length).toBeLessThan(a.content.length + b.content.length)
    expect(out.length).toBe((PINNED_PASTE_HEAD_CHARS + 2) * 2)
  })

  it('applies mapBlock to the substituted text only', () => {
    const p = block(1, 'one\ntwo')
    const out = expandPastesCapped(`keep\tthis ${formatToken(p)}`, [p], s => s.replace(/\s+/g, ' '))
    // The block's newline is flattened; the surrounding tab is untouched.
    expect(out).toBe('keep\tthis one two')
  })

  it('leaves content alone when no token matches a block', () => {
    const orphan = block(7, 'nope')
    expect(expandPastesCapped('plain text', [orphan])).toBe('plain text')
  })

  it('ignores a token whose seq has no block', () => {
    // findTokenRanges pairs by seq; an unpaired token is left as written rather
    // than substituted from the wrong block.
    const p = block(1, 'mine')
    const stray = formatToken(block(4, 'irrelevant'))
    const out = expandPastesCapped(`${formatToken(p)} ${stray}`, [p])
    expect(out).toBe(`mine ${stray}`)
  })
})

describe('the card renders what the derivation hands it', () => {
  // The real component, given exactly the props ChatPage now computes. jsdom has
  // no layout, so the banner can never mount through ChatPage's own geometry.
  const paste = block(1, 'Window: last 7 days\nShadow branch 59,990\nPPCore 173,632')
  const prompt = `"\n${formatToken(paste)}\n"`

  const renderCard = (expanded: boolean) => {
    const { text, body, images } = derivePinnedPromptText(prompt, [paste])
    return render(
      <PinnedPrompt
        text={text}
        fullText={body}
        images={images}
        pushUp={0}
        bannerH={40}
        expanded={expanded}
        onToggleExpanded={() => {}}
        onJump={() => {}}
        onCollapsedHeight={() => {}}
      />,
    )
  }

  it('shows the paste, not the placeholder, on the collapsed card', () => {
    renderCard(false)
    expect(screen.getByTestId('pinned-prompt').textContent).toContain('Shadow branch 59,990')
    expect(screen.getByTestId('pinned-prompt').textContent).not.toContain('Paste #1')
  })

  it('shows the paste, not the placeholder, on the expanded card', () => {
    renderCard(true)
    expect(screen.getByTestId('pinned-prompt').textContent).toContain('PPCore 173,632')
    expect(screen.getByTestId('pinned-prompt').textContent).not.toContain('Paste #1')
  })

  it('NEGATIVE CONTROL: the pre-fix props put the placeholder on the card', () => {
    // Same component and prompt, derived the old way.
    render(
      <PinnedPrompt
        text={promptPreview(prompt)}
        fullText={promptBody(prompt)}
        images={promptImages(prompt)}
        pushUp={0}
        bannerH={40}
        expanded={false}
        onToggleExpanded={() => {}}
        onJump={() => {}}
        onCollapsedHeight={() => {}}
      />,
    )
    expect(screen.getByTestId('pinned-prompt').textContent).toContain('Paste #1')
  })
})

describe('the pinned reducer does not alias two distinct pastes', () => {
  // Same seq and same line count, so formatToken yields IDENTICAL collapsed text
  // for both -- and equal content length, so a content-shape key would collide.
  const first = block(1, 'alpha\nbravo\ncharlie')
  const second = block(1, 'delta\nechos\nfoxtrot')
  const prompt = formatToken(first)

  const input = (b: PasteBlock, over: Record<string, unknown> = {}) => ({
    idx: 4, ts: 't1', raw: prompt, pastes: [b], machineLabel: null, push: 0, bannerH: 40, ...over,
  })

  it('the two really are indistinguishable to a seq+length key', () => {
    expect(second.content.length).toBe(first.content.length)
    expect(second.lines).toBe(first.lines)
    expect(formatToken(second)).toBe(formatToken(first))
  })

  it('derives the second message from its OWN paste, not the first one held', () => {
    const a = nextPinnedPromptState(null, input(first))
    const b = nextPinnedPromptState(a, input(second, { idx: 9, ts: 't2' }))
    expect(a.text).toBe('alpha bravo charlie')
    expect(b.text).toBe('delta echos foxtrot')
  })

  it('CONTROL: an unchanged scroll frame returns the IDENTICAL object', () => {
    // The no-re-render property the reducer replaced the cache to keep.
    const a = nextPinnedPromptState(null, input(first))
    expect(nextPinnedPromptState(a, input(first))).toBe(a)
  })

  it('carries the derivation forward when only the push geometry moved', () => {
    const a = nextPinnedPromptState(null, input(first))
    const b = nextPinnedPromptState(a, input(first, { push: 12 }))
    expect(b).not.toBe(a)
    expect(b.push).toBe(12)
    // Same strings, so no regex walk was needed to produce them.
    expect(b.text).toBe(a.text)
    expect(b.full).toBe(a.full)
    expect(b.images).toBe(a.images)
  })
})

describe('bodyBeyondPreview earns the expand chevron', () => {
  it('is set for a SHORT multiline paste, whose body the preview flattens away', () => {
    // The defect: this shape never clamps, so the flag is the only thing that can
    // mount the chevron -- and the body is the whole point of expanding.
    const p = block(1, 'first\nsecond\nthird')
    const st = nextPinnedPromptState(null, {
      idx: 1, ts: 't', raw: formatToken(p), pastes: [p], machineLabel: null, push: 0, bannerH: 40,
    })
    expect(st.text).toBe('first second third')
    expect(st.full).toBe('first\nsecond\nthird')
    expect(st.bodyBeyondPreview).toBe(true)
  })

  it('NEGATIVE CONTROL: a genuinely single-line paste does NOT earn one', () => {
    const p = block(1, 'one single line of pasted text')
    const st = nextPinnedPromptState(null, {
      idx: 1, ts: 't', raw: formatToken(p), pastes: [p], machineLabel: null, push: 0, bannerH: 40,
    })
    expect(st.full).toBe(st.text)
    expect(st.bodyBeyondPreview).toBe(false)
  })

  it('stays set for a machine row, whose label is not its body', () => {
    const st = nextPinnedPromptState(null, {
      idx: 1, ts: 't', raw: 'payload', pastes: [], machineLabel: 'Auto-nudge - cycle 17',
      machineBody: 'Babysit the run. Check CI.', push: 0, bannerH: 40,
    })
    expect(st.bodyBeyondPreview).toBe(true)
  })
})
