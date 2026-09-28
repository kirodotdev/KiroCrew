/**
 * The pinned-prompt card stands in for a user BUBBLE, not for the whole row.
 *
 * While a prompt is pinned its transcript row is `visibility: hidden` and the
 * PinnedPrompt card is drawn in its place (see ChatPage's row `visibility`,
 * VirtualTranscript's `isRowHidden`, ChatMessageList's `hiddenRow`). The row
 * also holds the message's action strip — copy, copy link, pin, edit, the
 * timestamp — which the card does not carry, so hiding the row wholesale took
 * the strip away with the bubble. Since the top-edge hand-off (#11462) a row
 * hides the moment its top crosses the fold, so a prompt taller than the
 * viewport can never show its strip at all: reaching the bubble's bottom means
 * its top is already above the fold. That is the report this pins: a long
 * user message with no copy / copy-link / pin / timestamp row.
 *
 * The strip is restored by a stylesheet rule keyed on the wrapper's
 * `data-pinned-standin` marker (index.css), so the assertion runs the REAL
 * wrapper (ChatMessageList's virtualized mount) under the REAL rule, read from
 * index.css itself — the test does not restate the rule.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { render } from '@testing-library/react'
import type { ChatMessage } from '../types'
import { i18nT } from '../i18n/t'

import ChatMessageList from '../app-sdk/ChatMessageList'
import UserMessage from '../pages/chat/UserMessage'

const INDEX_CSS = readFileSync(resolve(process.cwd(), 'src/index.css'), 'utf-8')

/** The stand-in rules exactly as index.css ships them (comments stripped). */
function pinnedStandinRules(): string {
  const src = INDEX_CSS.replace(/\/\*[\s\S]*?\*\//g, '')
  const out: string[] = []
  const re = /([^{}]+)\{([^{}]*)\}/g
  let m: RegExpExecArray | null
  while ((m = re.exec(src)) !== null) {
    if (m[1].includes('data-pinned-standin')) out.push(`${m[1].trim()}{${m[2].trim()}}`)
  }
  return out.join('\n')
}

const LONG_PROMPT = Array.from({ length: 60 }, (_, i) =>
  `Line ${i + 1} of a prompt far taller than any viewport, pasted in one go.`).join('\n')

const TS = '2026-09-24T16:54:00.000Z'
const MESSAGES: ChatMessage[] = [
  { role: 'user', content: LONG_PROMPT, cls: '', ts: TS },
  { role: 'assistant', content: 'Read it all.', cls: '', ts: '2026-09-24T16:55:00.000Z' },
]

/** The real wrapper under the real rule. `stripUncovered` is the host's "the
 *  row's action strip is still on screen below the card" flag (ChatPane's
 *  `pinHiddenRow`, derived in usePinnedPrompt from the strip's own rect), which
 *  is the only state in which the strip is shown under the card. */
function renderPinned(stripUncovered = true) {
  const style = document.createElement('style')
  style.setAttribute('data-test-stylesheet', 'pinned-standin')
  style.textContent = pinnedStandinRules()
  document.head.append(style)
  const utils = render(
    <ChatMessageList
      messages={MESSAGES}
      running={false}
      hiddenRow={{ ts: TS, index: 0, stripUncovered }}
      transcript={{ sessionId: 'test:pinned-standin' }}
    />,
  )
  const rows = [...utils.container.querySelectorAll<HTMLElement>('[data-display-index]')]
  const row = rows.find(r => r.textContent?.includes('Line 60 of a prompt'))
  if (!row) throw new Error('the long prompt row did not mount')
  return { ...utils, row, cleanup: () => style.remove() }
}

describe('pinned stand-in row keeps its action strip', () => {
  afterEach(() => {
    document.querySelectorAll('[data-test-stylesheet="pinned-standin"]').forEach(n => n.remove())
  })

  it('hides the bubble the card stands in for', () => {
    const { row } = renderPinned()
    expect(row.style.visibility, 'the pinned row is hidden by visibility').toBe('hidden')
    const bubble = row.querySelector<HTMLElement>('.message-bubble')
    expect(bubble, 'the row renders its bubble').not.toBeNull()
    expect(getComputedStyle(bubble!).visibility, 'the bubble stays hidden under the card').toBe('hidden')
  })

  it('keeps the copy button and the timestamp visible beneath the card', () => {
    const { row } = renderPinned()
    const copy = row.querySelector<HTMLElement>(`button[title="${i18nT('pages.chat.userMessage.copy')}"]`)
    expect(copy, 'the action strip renders inside the hidden row').not.toBeNull()
    // A `visibility: visible` descendant of a hidden ancestor is drawn and
    // hit-testable — the one property that lets the strip survive the row's
    // hiding without moving it out of the row.
    expect(getComputedStyle(copy!).visibility, 'copy button beneath the pinned stand-in').toBe('visible')
    const stamp = row.querySelector<HTMLElement>('span.tabular-nums')
    expect(stamp, 'the timestamp renders inside the hidden row').not.toBeNull()
    expect(getComputedStyle(stamp!).visibility, 'timestamp beneath the pinned stand-in').toBe('visible')
  })

  it('hides the strip again once it has slid under the card, so nothing invisible stays focusable', () => {
    // The card rests at its clamp and the strip has scrolled on under it or
    // behind the header. A `visible` strip there is drawn nowhere the reader
    // can see yet still in the tab order, and each Tab stop scrolls the
    // transcript away from the position the pin holds — so the marker goes bare
    // and the strip inherits the row's `hidden` again.
    const { row } = renderPinned(false)
    expect(row.getAttribute('data-pinned-standin'), 'bare marker once the strip is covered').toBe('')
    const copy = row.querySelector<HTMLElement>(`button[title="${i18nT('pages.chat.userMessage.copy')}"]`)
    expect(copy, 'the action strip still renders inside the hidden row').not.toBeNull()
    expect(getComputedStyle(copy!).visibility, 'copy button once the strip is under the card').toBe('hidden')
    const stamp = row.querySelector<HTMLElement>('span.tabular-nums')
    expect(getComputedStyle(stamp!).visibility, 'timestamp once the strip is under the card').toBe('hidden')
  })

  it('shows the strip outright rather than behind the hover reveal', () => {
    // The card sits in an overlay that is a SIBLING of the scroller, so hovering
    // it can never be `group-hover` for the row beneath: the strip has to be
    // shown outright while the row is a stand-in, or on a hover-capable pointer
    // it stays invisible until the reader finds the blank gap by accident.
    // happy-dom does not compile the Tailwind `opacity-0` utility, so the
    // computed value cannot carry this; the shipped rule text can.
    expect(pinnedStandinRules(), 'index.css stand-in rule').toMatch(/opacity:\s*1\b/)
  })

  it('leaves Edit out of the re-shown strip, because its editor would open inside the hidden row', () => {
    // Editing swaps the bubble for a textarea + Cancel/Send tree that carries no
    // strip hook, so under the row's `visibility: hidden` it would be an editor
    // nobody can see, focus or dismiss. The wrapper here is the host rule's
    // shape (hidden by visibility, marked as the stand-in mid-fold);
    // ChatMessageList's own wrapper is exercised above.
    const style = document.createElement('style')
    style.setAttribute('data-test-stylesheet', 'pinned-standin')
    style.textContent = pinnedStandinRules()
    document.head.append(style)
    const { container } = render(
      <div data-display-index={0} data-pinned-standin="folding" style={{ visibility: 'hidden' }}>
        <UserMessage
          content={LONG_PROMPT}
          timestamp="12:58 AM"
          renderContent={c => <p>{c}</p>}
          canEdit
          onEditResend={() => {}}
        />
      </div>,
    )
    const edit = container.querySelector<HTMLElement>(`button[title="${i18nT('pages.chat.userMessage.edit_resend')}"]`)
    expect(edit, 'the pencil renders (canEdit)').not.toBeNull()
    expect(getComputedStyle(edit!).display, 'Edit while standing in').toBe('none')
    const copy = container.querySelector<HTMLElement>(`button[title="${i18nT('pages.chat.userMessage.copy')}"]`)
    expect(getComputedStyle(copy!).visibility, 'Copy while standing in').toBe('visible')
    expect(getComputedStyle(copy!).display, 'Copy keeps its box').not.toBe('none')
  })
})
