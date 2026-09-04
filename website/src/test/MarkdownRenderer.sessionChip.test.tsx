import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, screen } from '@testing-library/react'

import MarkdownRenderer from '../components/MarkdownRenderer'
import { copyToClipboard } from '../utils/clipboard'
import { buildShareableUrl } from '../utils/shareUrl'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn(async () => undefined) }))

const KEY = 'chat-24-1784661951'
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
