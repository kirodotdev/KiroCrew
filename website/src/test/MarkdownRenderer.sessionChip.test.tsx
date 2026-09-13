import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, screen, createEvent } from '@testing-library/react'

import MarkdownRenderer from '../components/MarkdownRenderer'
import { copyToClipboard } from '../utils/clipboard'
import { buildShareableUrl } from '../utils/shareUrl'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn(async () => undefined) }))

const KEY = 'chat-24-1784661951'
/** After KEY's mint time, which the SHORT-name form requires: slot numbers are
 *  reused, so a chip must know when the text was written. */
const WRITTEN = '2026-09-11T23:39:00Z'
const OTHER = 'chat-99-1700000000'

/** A roster with one open session, the shape ChatPage supplies. */
const roster = () => new Map([[KEY, 'Fix the pagination bug']])

let onSessionOpen: ReturnType<typeof vi.fn>

beforeEach(() => {
  vi.mocked(copyToClipboard).mockClear()
  onSessionOpen = vi.fn()
})

describe('session chip — a bare slot key in prose', () => {
  it('switches to the session on click', () => {
    render(
      <MarkdownRenderer
        content={`Session \`${KEY}\` is still active.`}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
      />,
    )
    const chip = screen.getByText(KEY)
    expect(chip.tagName).toBe('CODE')
    expect(chip).toHaveAttribute('role', 'button')
    expect(chip).toHaveAttribute('data-session-key', KEY)

    fireEvent.click(chip)
    expect(onSessionOpen).toHaveBeenCalledWith(KEY)
    // The whole point is that clicking does NOT merely copy, which is what the
    // pre-change fallback did.
    expect(copyToClipboard).not.toHaveBeenCalled()
  })

  it('normalises the dashboard_ transcript spelling to the slot key', () => {
    // `?sid=` cannot resolve the prefixed form, so it must not reach the handler.
    render(
      <MarkdownRenderer
        content={`See \`dashboard_${KEY}\`.`}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
      />,
    )
    const chip = screen.getByText(`dashboard_${KEY}`)
    expect(chip).toHaveAttribute('data-session-key', KEY)
    fireEvent.click(chip)
    expect(onSessionOpen).toHaveBeenCalledWith(KEY)
  })

  it('activates on Enter and Space', () => {
    render(
      <MarkdownRenderer content={`\`${KEY}\``} onSessionOpen={onSessionOpen} sessions={roster()} />,
    )
    const chip = screen.getByText(KEY)
    expect(chip).toHaveAttribute('tabindex', '0')
    fireEvent.keyDown(chip, { key: 'Enter' })
    fireEvent.keyDown(chip, { key: ' ' })
    expect(onSessionOpen).toHaveBeenCalledTimes(2)
  })

  it('copies the NORMALISED key on Ctrl/Cmd+click instead of switching', () => {
    render(
      <MarkdownRenderer
        content={`\`dashboard_${KEY}\``}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
      />,
    )
    const chip = screen.getByText(`dashboard_${KEY}`)
    fireEvent.click(chip, { metaKey: true })
    expect(copyToClipboard).toHaveBeenCalledWith(KEY)
    expect(onSessionOpen).not.toHaveBeenCalled()
  })

  it('names the session in its tooltip, not just the key', () => {
    render(
      <MarkdownRenderer content={`\`${KEY}\``} onSessionOpen={onSessionOpen} sessions={roster()} />,
    )
    const title = screen.getByText(KEY).getAttribute('title') ?? ''
    expect(title).toContain('Fix the pagination bug')
    expect(title).toContain('Click to switch to this session')
    expect(title).toContain('Ctrl+click to copy')
  })
})

describe('session chip — a SHORT slot name', () => {
  // How a session is actually named in prose: the sidebar shows it, the agent
  // tools return it, and nobody types the mint timestamp.
  const SHORT = 'chat-24'

  it('resolves the short name against the roster and switches on click', () => {
    render(
      <MarkdownRenderer
        content={`Handed it to \`${SHORT}\`.`}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
        messageTs={WRITTEN}
      />,
    )
    const chip = screen.getByText(SHORT)
    expect(chip).toHaveAttribute('data-session-key', KEY)
    fireEvent.click(chip)
    // The FULL key reaches the handler: `?sid=` and the slot switcher both need it.
    expect(onSessionOpen).toHaveBeenCalledWith(KEY)
  })

  it('names the session in its tooltip', () => {
    render(
      <MarkdownRenderer content={`\`${SHORT}\``} onSessionOpen={onSessionOpen} sessions={roster()} messageTs={WRITTEN} />,
    )
    expect(screen.getByText(SHORT).getAttribute('title') ?? '').toContain('Fix the pagination bug')
  })

  it('copies the FULL key on Ctrl/Cmd+click, not the nickname', () => {
    render(
      <MarkdownRenderer content={`\`${SHORT}\``} onSessionOpen={onSessionOpen} sessions={roster()} messageTs={WRITTEN} />,
    )
    fireEvent.click(screen.getByText(SHORT), { metaKey: true })
    expect(copyToClipboard).toHaveBeenCalledWith(KEY)
    expect(onSessionOpen).not.toHaveBeenCalled()
  })

  it('leaves a short name no open session answers to as plain text', () => {
    render(
      <MarkdownRenderer content={'`chat-777`'} onSessionOpen={onSessionOpen} sessions={roster()} />,
    )
    const el = screen.getByText('chat-777')
    expect(el).not.toHaveAttribute('data-session-key')
    expect(el).toHaveAttribute('title', 'Click to copy')
  })

  it('refuses an AMBIGUOUS short name rather than picking one', () => {
    // Two open slots share slot number 24 across gateway generations. Guessing
    // would switch the reader to a session they did not name.
    render(
      <MarkdownRenderer
        content={`\`${SHORT}\``}
        onSessionOpen={onSessionOpen}
        sessions={new Map([[KEY, 'One'], ['chat-24-1700000000', 'Two']])}
      />,
    )
    expect(screen.getByText(SHORT)).not.toHaveAttribute('data-session-key')
  })

  it('does not let a shorter number claim a longer slot', () => {
    render(
      <MarkdownRenderer
        content={'`chat-2`'}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
      />,
    )
    // The roster holds chat-24-…, which `chat-2` must not claim.
    expect(screen.getByText('chat-2')).not.toHaveAttribute('data-session-key')
  })

  it('offers no chip for the short name of the session the reader is in', () => {
    render(
      <MarkdownRenderer
        content={`\`${SHORT}\``}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
        activeSession={KEY}
      />,
    )
    expect(screen.getByText(SHORT)).not.toHaveAttribute('data-session-key')
  })

  it('accepts the epoch NUMBER the transcript endpoint really sends', () => {
    // `ChatMessage.ts` is declared `string`, but the transcript endpoint sends epoch
    // numbers (see playwright/voice-recovery.spec.ts). Calling a string method on
    // that value threw and blanked the whole transcript, so the shape is validated
    // rather than trusted, and a number is a perfectly good absolute instant.
    render(
      <MarkdownRenderer
        content={`\`${SHORT}\``}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
        messageTs={1789049999 as unknown as string}
      />,
    )
    expect(screen.getByText(SHORT)).toHaveAttribute('data-session-key', KEY)
  })

  it('renders the message at all when the timestamp is a number', () => {
    // The regression this guards is not the chip, it is the CRASH: a throw here
    // unmounted the bubble and the transcript came back empty.
    render(
      <MarkdownRenderer
        content={'plain body text'}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
        messageTs={1789049999 as unknown as string}
      />,
    )
    expect(screen.getByText('plain body text')).toBeInTheDocument()
  })

  it('refuses a timestamp with no timezone, which is not an absolute instant', () => {
    // `Date.parse('2026-09-11T23:39:00')` reads a bare LOCAL time, so the same row
    // yields a different epoch per viewer timezone and one behind UTC shifts it
    // forward -- far enough to let a slot minted after the row pass the check.
    render(
      <MarkdownRenderer
        content={`\`${SHORT}\``}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
        messageTs="2026-09-11T23:39:00"
      />,
    )
    expect(screen.getByText(SHORT)).not.toHaveAttribute('data-session-key')
  })

  it('accepts an explicit offset as well as Z', () => {
    render(
      <MarkdownRenderer
        content={`\`${SHORT}\``}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
        messageTs="2026-09-11T23:39:00+08:00"
      />,
    )
    expect(screen.getByText(SHORT)).toHaveAttribute('data-session-key', KEY)
  })

  it('refuses the short name when the row carries no write time', () => {
    // Fail closed, and this is the common case on a surface with no message
    // identity. Slot numbers are reused, so without a write time the roster
    // cannot say whether this name means the session it named or the one that
    // later took its number -- and the wrong answer is silent.
    render(
      <MarkdownRenderer content={`\`${SHORT}\``} onSessionOpen={onSessionOpen} sessions={roster()} />,
    )
    expect(screen.getByText(SHORT)).not.toHaveAttribute('data-session-key')
  })

  it('still chips the FULL key with no write time, which names its generation', () => {
    render(
      <MarkdownRenderer content={`\`${KEY}\``} onSessionOpen={onSessionOpen} sessions={roster()} />,
    )
    expect(screen.getByText(KEY)).toHaveAttribute('data-session-key', KEY)
  })

  it('stays plain text in prose without backticks', () => {
    // Prose is not scanned for slot names — the author marks a session as one,
    // the same as every other chip in this renderer.
    render(
      <MarkdownRenderer
        content={`Handed it to ${SHORT} which is idle.`}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
      />,
    )
    expect(document.querySelector('[data-session-key]')).toBeNull()
  })

  it('switches in place from a short-name DEEP LINK, rather than reloading', () => {
    // The href resolves, so a plain click must be intercepted. Left ungated on
    // the full key only, the anchor navigated to the canonical `?sid=` instead,
    // remounting the whole app to reach a session already open in a tab.
    render(
      <MarkdownRenderer
        content={`[the other session](/chat?sid=${SHORT})`}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
        messageTs={WRITTEN}
      />,
    )
    const link = screen.getByText('the other session')
    // The attribute is rewritten to the canonical key, so a Cmd+click still lands.
    expect(link).toHaveAttribute('href', `/chat?sid=${KEY}`)
    fireEvent.click(link, { button: 0 })
    expect(onSessionOpen).toHaveBeenCalledWith(KEY)
  })

  it('refuses a short name whose only match was minted after the message', () => {
    // KEY's mint time is 1784661951; this message predates it, so the session it
    // names cannot be this one — a slot number reused by a later generation.
    render(
      <MarkdownRenderer
        content={`\`${SHORT}\``}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
        messageTs="2020-01-01T00:00:00Z"
      />,
    )
    expect(screen.getByText(SHORT)).not.toHaveAttribute('data-session-key')
  })

  it('still resolves when the message was written after the slot was minted', () => {
    render(
      <MarkdownRenderer
        content={`\`${SHORT}\``}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
        messageTs="2026-09-11T23:39:00Z"
      />,
    )
    expect(screen.getByText(SHORT)).toHaveAttribute('data-session-key', KEY)
  })

  it('leaves the FULL key alone when the message predates the slot', () => {
    // A full key names its generation exactly, so there is no aliasing to guard.
    render(
      <MarkdownRenderer
        content={`\`${KEY}\``}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
        messageTs="2020-01-01T00:00:00Z"
      />,
    )
    expect(screen.getByText(KEY)).toHaveAttribute('data-session-key', KEY)
  })

  it('declines an unresolvable short-name deep link instead of navigating to it', () => {
    // Symmetry with an unresolvable FULL key: both name a session, so both are
    // intercepted rather than left to reload the app onto a dead `?sid=` view
    // (#9914). Shape is what says "this names a session"; the roster only says
    // whether it is reachable.
    render(
      <MarkdownRenderer
        content={'[a closed session](/chat?sid=chat-777)'}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
      />,
    )
    const link = screen.getByText('a closed session')
    const click = createEvent.click(link, { button: 0 })
    fireEvent(link, click)
    expect(click.defaultPrevented).toBe(true)
    expect(onSessionOpen).not.toHaveBeenCalled()
    // And it does not LOOK live: a swallowed click behind an accent underline
    // promises an action that never comes, on every read of an old transcript.
    expect(link.className).not.toMatch(/underline/)
    expect(link.className).toContain('text-muted')
  })

  it('keeps the live-link styling on a session link that DOES open', () => {
    render(
      <MarkdownRenderer
        content={`[the other session](/chat?sid=${SHORT})`}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
        messageTs={WRITTEN}
      />,
    )
    const link = screen.getByText('the other session')
    expect(link.className).toMatch(/underline/)
    expect(link.className).toContain('text-accent')
  })
})

describe('session chip — the honesty gates', () => {
  /** Assert the span fell back to the plain click-to-copy chip. */
  const expectCopyChip = (text: string) => {
    const el = screen.getByText(text)
    expect(el).toHaveAttribute('title', 'Click to copy')
    expect(el).not.toHaveAttribute('data-session-key')
    fireEvent.click(el)
    expect(copyToClipboard).toHaveBeenCalledWith(text)
    expect(onSessionOpen).not.toHaveBeenCalled()
  }

  it('offers no chip when the caller wired no roster', () => {
    // Absence of a roster is absence of KNOWLEDGE, not absence of the session.
    render(<MarkdownRenderer content={`\`${KEY}\``} onSessionOpen={onSessionOpen} />)
    expectCopyChip(KEY)
  })

  it('offers no chip when the caller wired no handler', () => {
    render(<MarkdownRenderer content={`\`${KEY}\``} sessions={roster()} />)
    const el = screen.getByText(KEY)
    expect(el).not.toHaveAttribute('data-session-key')
    expect(el).toHaveAttribute('title', 'Click to copy')
  })

  it('offers no chip for a session that is not open', () => {
    // Its transcript may still be on disk, but reopening that is a History resume,
    // not a slot switch — so a chip here could not do what it promises.
    render(
      <MarkdownRenderer content={`\`${OTHER}\``} onSessionOpen={onSessionOpen} sessions={roster()} />,
    )
    expectCopyChip(OTHER)
  })

  it('offers no chip for the session the reader is already in', () => {
    render(
      <MarkdownRenderer
        content={`\`${KEY}\``}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
        activeSession={KEY}
      />,
    )
    expectCopyChip(KEY)
  })

  it('offers no chip for a string that merely resembles a key', () => {
    // The roster entries are deliberately not slot keys, so neither span can be
    // resolved by the full grammar or by the short-name lookup.
    render(
      <MarkdownRenderer
        content={'`chat-24` and `chat-24-1784661951.jsonl`'}
        onSessionOpen={onSessionOpen}
        sessions={new Map([['chat-24', 'x'], [`${KEY}.jsonl`, 'y']])}
      />,
    )
    expect(screen.getByText('chat-24')).not.toHaveAttribute('data-session-key')
    expect(screen.getByText(`${KEY}.jsonl`)).not.toHaveAttribute('data-session-key')
  })

  it('drops a data-session-key forged in raw HTML', () => {
    // rehypeSanitize allowlists every `data-*`, so a forged pair reaches us intact.
    render(
      <MarkdownRenderer
        content={`<code data-session-key="${KEY}">not a key</code>`}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
      />,
    )
    expect(screen.getByText('not a key')).not.toHaveAttribute('data-session-key')
  })
})

describe('session chip — a /chat?sid= deep link', () => {
  const link = `[the other session](/chat?sid=${KEY})`

  it('switches in place and does not open a second browser tab', () => {
    render(
      <MarkdownRenderer content={link} onSessionOpen={onSessionOpen} sessions={roster()} />,
    )
    const anchor = screen.getByText('the other session').closest('a')!
    // `ALLOWED_PROTOCOLS` holds only the vscode schemes, so without the session
    // branch a root-relative href counts as external and gains `_blank`.
    expect(anchor).not.toHaveAttribute('target')
    expect(anchor).toHaveAttribute('href', `/chat?sid=${KEY}`)

    fireEvent.click(anchor, { button: 0 })
    expect(onSessionOpen).toHaveBeenCalledWith(KEY)
  })

  it('leaves a modified click to the browser', () => {
    // The href stays real so Cmd+click still opens the session in a new tab.
    render(
      <MarkdownRenderer content={link} onSessionOpen={onSessionOpen} sessions={roster()} />,
    )
    const anchor = screen.getByText('the other session').closest('a')!
    fireEvent.click(anchor, { button: 0, metaKey: true })
    expect(onSessionOpen).not.toHaveBeenCalled()
  })

  it('stays an ordinary external-style link when the session is not open', () => {
    // Negative control for the assertion above: `target` is absent BECAUSE the
    // session resolved, not because the branch always drops it.
    render(
      <MarkdownRenderer
        content={`[gone](/chat?sid=${OTHER})`}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
      />,
    )
    const anchor = screen.getByText('gone').closest('a')!
    expect(anchor).toHaveAttribute('target', '_blank')
    fireEvent.click(anchor, { button: 0 })
    expect(onSessionOpen).not.toHaveBeenCalled()
  })

  it('ignores a foreign origin whose path and query would otherwise match', () => {
    render(
      <MarkdownRenderer
        content={`[away](https://elsewhere.example/chat?sid=${KEY})`}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
      />,
    )
    const anchor = screen.getByText('away').closest('a')!
    fireEvent.click(anchor, { button: 0 })
    expect(onSessionOpen).not.toHaveBeenCalled()
  })

  it('ignores the backslash origin escape and keeps the tab-safe attributes', () => {
    // `/\host/chat` resolves to origin `host` while looking root-relative; chipping it
    // dropped `rel="noopener noreferrer"` from an anchor aimed at that foreign origin.
    render(
      <MarkdownRenderer
        content={`[escape](/\\evil.example/chat?sid=${KEY})`}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
      />,
    )
    const anchor = screen.getByText('escape').closest('a')!
    expect(anchor).toHaveAttribute('target', '_blank')
    expect(anchor).toHaveAttribute('rel', 'noopener noreferrer')
    fireEvent.click(anchor, { button: 0 })
    expect(onSessionOpen).not.toHaveBeenCalled()
  })

  it('rewrites the href to the canonical key so a modified click still resolves', () => {
    // The modified click goes to the browser, so a `dashboard_…` sid left in the
    // attribute would open a session `?sid=` cannot resolve.
    render(
      <MarkdownRenderer
        content={`[other](/chat?sid=dashboard_${KEY})`}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
      />,
    )
    const anchor = screen.getByText('other').closest('a')!
    expect(anchor).toHaveAttribute('href', `/chat?sid=${KEY}`)
    fireEvent.click(anchor, { button: 0, metaKey: true })
    expect(onSessionOpen).not.toHaveBeenCalled()
  })

  it('switches in place for the ABSOLUTE share link the app itself mints', () => {
    // `buildShareableUrl` emits `${origin}/chat?sid=…`, so a pasted Copy-link is
    // the common shape; gating the chip on a root-relative href left it a dead end.
    const shared = buildShareableUrl(KEY)
    expect(shared).toBe(`${window.location.origin}/chat?sid=${KEY}`)
    render(
      <MarkdownRenderer content={`[shared](${shared})`} onSessionOpen={onSessionOpen} sessions={roster()} />,
    )
    const anchor = screen.getByText('shared').closest('a')!
    expect(anchor).not.toHaveAttribute('target', '_blank')
    fireEvent.click(anchor, { button: 0 })
    expect(onSessionOpen).toHaveBeenCalledWith(KEY)
  })

  it('normalises an absolute share link to a root-relative canonical href', () => {
    // A modified click follows the attribute, so the absolute form must come back
    // root-relative or that click reloads the whole app to reach the session.
    render(
      <MarkdownRenderer
        content={`[shared](${window.location.origin}/chat/fix-the-bug?sid=dashboard_${KEY})`}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
      />,
    )
    const anchor = screen.getByText('shared').closest('a')!
    expect(anchor).toHaveAttribute('href', `/chat/fix-the-bug?sid=${KEY}`)
  })

  it('still refuses a foreign origin now that absolute hrefs are read', () => {
    // Widening to absolute must not widen the origin promise: the same path on
    // another host keeps its tab-safe attributes and never switches.
    render(
      <MarkdownRenderer
        content={`[away](https://elsewhere.example/chat/fix-the-bug?sid=${KEY})`}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
      />,
    )
    const anchor = screen.getByText('away').closest('a')!
    expect(anchor).toHaveAttribute('target', '_blank')
    expect(anchor).toHaveAttribute('rel', 'noopener noreferrer')
    fireEvent.click(anchor, { button: 0 })
    expect(onSessionOpen).not.toHaveBeenCalled()
  })

  it('still refuses the percent-encoded backslash escape', () => {
    // Refused either way — undecoded on the path test, decoded on the origin test —
    // so neither entry path can regress alone.
    render(
      <MarkdownRenderer
        content={`[escape](/%5Cevil.example/chat?sid=${KEY})`}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
      />,
    )
    const anchor = screen.getByText('escape').closest('a')!
    expect(anchor).toHaveAttribute('target', '_blank')
    fireEvent.click(anchor, { button: 0 })
    expect(onSessionOpen).not.toHaveBeenCalled()
  })

  it('resolves a percent-encoded title slug, which is what the decode buys', () => {
    // `%2F` is the one shape the decode changes the answer for: undecoded the path
    // reads `/chat%2F…` and never matches, so this pins the decode itself.
    render(
      <MarkdownRenderer
        content={`[shared](/chat%2Ffix-the-bug?sid=${KEY})`}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
      />,
    )
    const anchor = screen.getByText('shared').closest('a')!
    fireEvent.click(anchor, { button: 0 })
    expect(onSessionOpen).toHaveBeenCalledWith(KEY)
  })

  it('leaves a MESSAGE-targeted link unintercepted so its target survives', () => {
    // The session switch cannot carry `msg`, so taking this link over would land the
    // reader in the right session at the wrong place. It must stay a plain anchor.
    render(
      <MarkdownRenderer
        content={`[jump](/chat?sid=${KEY}&msg=2026-08-30T12:00:00Z)`}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
      />,
    )
    const anchor = screen.getByText('jump').closest('a')!
    expect(anchor).toHaveAttribute('href', `/chat?sid=${KEY}&msg=2026-08-30T12:00:00Z`)
    expect(anchor).toHaveAttribute('target', '_blank')
    fireEvent.click(anchor, { button: 0 })
    expect(onSessionOpen).not.toHaveBeenCalled()
  })

  it('leaves a mid-targeted link unintercepted as well', () => {
    render(
      <MarkdownRenderer
        content={`[jump](/chat?sid=${KEY}&mid=abc123)`}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
      />,
    )
    const anchor = screen.getByText('jump').closest('a')!
    fireEvent.click(anchor, { button: 0 })
    expect(onSessionOpen).not.toHaveBeenCalled()
  })
})

describe('session chip — copy acknowledgment', () => {
  it('acknowledges the Ctrl+click copy the tooltip advertises', () => {
    // The tooltip promises Ctrl+click copies, so the gesture needs the same
    // confirmation the click-to-copy chip it replaces gave.
    render(
      <MarkdownRenderer content={`\`${KEY}\``} onSessionOpen={onSessionOpen} sessions={roster()} />,
    )
    const chip = screen.getByText(KEY)
    expect(chip.getAttribute('title')).not.toBe('Copied!')

    fireEvent.click(chip, { ctrlKey: true })
    expect(copyToClipboard).toHaveBeenCalledWith(KEY)
    expect(onSessionOpen).not.toHaveBeenCalled()
    expect(chip).toHaveAttribute('title', 'Copied!')
  })
})
