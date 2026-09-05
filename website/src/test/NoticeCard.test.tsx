import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import NoticeCard, { parseNotice } from '../pages/chat/NoticeCard'
import RecoveryCard, { parseRecoveryMessage } from '../pages/chat/RecoveryCard'
import { defaultMessageRenderers, type MessageRenderContext } from '../app-sdk/messageRenderers'
import type { ChatMessage } from '../types'

describe('parseNotice', () => {
  it('strips a leading ℹ️ (with, without, or with doubled variation selector) as info', () => {
    expect(parseNotice('\u2139\uFE0F The model returned nothing twice.')).toEqual({
      tone: 'info',
      text: 'The model returned nothing twice.',
    })
    expect(parseNotice('\u2139 plain info sign').text).toBe('plain info sign')
    expect(parseNotice('\u2139\uFE0F\uFE0F doubled selector').text).toBe('doubled selector')
    expect(parseNotice('  \u2139\uFE0F  padded').text).toBe('padded')
  })

  it('maps ⚠️ to warn and ⛔ to its own blocked tone, stripping each', () => {
    expect(parseNotice('\u26A0\uFE0F Queued message dropped: session reset.')).toEqual({
      tone: 'warn',
      text: 'Queued message dropped: session reset.',
    })
    expect(parseNotice('\u26D4 Blocked by policy.')).toEqual({
      tone: 'blocked',
      text: 'Blocked by policy.',
    })
  })

  it('treats an unrecognized leading glyph as content, never stripping it', () => {
    // A future "tolerant" \p{Emoji} rewrite would eat meaning from any notice;
    // this pins the closed three-glyph set.
    expect(parseNotice('\u274C x')).toEqual({ tone: 'info', text: '\u274C x' })
    expect(parseNotice('\u{1F6AB} not ours')).toEqual({ tone: 'info', text: '\u{1F6AB} not ours' })
  })

  it('leaves emoji-free notices untouched as info (model fallback carries no prefix)', () => {
    const s = "sonnet isn't offered right now — this session is running on auto instead."
    expect(parseNotice(s)).toEqual({ tone: 'info', text: s })
  })

  it('only strips a LEADING glyph, never one inside the copy — CJK included', () => {
    const inner = 'see the \u2139\uFE0F marker above'
    expect(parseNotice(inner).text).toBe(inner)
    const cjk = '模型当前不可用 — 会话改用 auto 运行。'
    expect(parseNotice(cjk)).toEqual({ tone: 'info', text: cjk })
  })
})

describe('NoticeCard', () => {
  it('renders info copy with the emoji stripped and a lucide info glyph in its place', () => {
    const { container } = render(
      <NoticeCard content={'\u2139\uFE0F The model returned nothing twice — auto-continuing once.'} />,
    )
    expect(
      screen.getByText('The model returned nothing twice — auto-continuing once.'),
    ).toBeInTheDocument()
    expect(container.textContent).not.toContain('\u2139')
    expect(container.querySelector('svg.lucide-info')).not.toBeNull()
    expect(container.querySelector('[data-testid="notice-card"]')!.getAttribute('data-tone')).toBe('info')
  })

  it('renders a ⚠️ notice with the warning triangle and announces the severity', () => {
    const { container } = render(<NoticeCard content={'\u26A0\uFE0F Queued message dropped.'} />)
    expect(container.textContent).not.toContain('\u26A0')
    expect(container.querySelector('svg.lucide-info')).toBeNull()
    const icon = container.querySelector('svg.lucide-triangle-alert')!
    expect(icon).not.toBeNull()
    expect(icon.classList.contains('text-warn')).toBe(true)
    expect(container.querySelector('[data-testid="notice-card"]')!.getAttribute('data-tone')).toBe('warn')
    // The stripped emoji was the only accessible severity signal; a
    // visually-hidden label must replace it for assistive tech.
    expect(container.querySelector('.sr-only')!.textContent).toContain('Warning')
  })

  it('renders a ⛔ notice with its own Ban glyph in the danger color, not the warn triangle', () => {
    const { container } = render(<NoticeCard content={'\u26D4 Tool call blocked by policy.'} />)
    expect(container.textContent).not.toContain('\u26D4')
    expect(container.querySelector('svg.lucide-triangle-alert')).toBeNull()
    const icon = container.querySelector('svg.lucide-ban')!
    expect(icon).not.toBeNull()
    expect(icon.classList.contains('text-danger')).toBe(true)
    expect(container.querySelector('[data-testid="notice-card"]')!.getAttribute('data-tone')).toBe('blocked')
    expect(container.querySelector('.sr-only')!.textContent).toContain('Blocked')
  })

  it('announces no severity for a routine info notice', () => {
    const { container } = render(<NoticeCard content={'\u2139\uFE0F routine'} />)
    expect(container.querySelector('.sr-only')).toBeNull()
  })

  it('spans the full column width like the RecoveryCard it stacks with', () => {
    const { container } = render(<NoticeCard content="notice" />)
    const card = container.querySelector('[data-testid="notice-card"]')!
    // classList (token) checks — a substring check would be satisfied by
    // max-w-full alone, letting the shrink-to-content defect back in.
    expect(card.classList.contains('w-full')).toBe(true)
    expect(card.classList.contains('self-center')).toBe(true)
  })

  /**
   * The unification pin: NoticeCard exists to share RecoveryCard's visual
   * grammar. For each metric FAMILY the row's classes are filtered by prefix
   * and compared as exact sets on BOTH components, so a conflicting addition
   * (px-4 alongside px-3, rounded-lg alongside rounded-md) fails just as a
   * deletion does. If RecoveryCard's chrome evolves, this fails and the two
   * move together instead of drifting back into two styles.
   */
  it('shares RecoveryCard metric classes, with no conflicting additions', () => {
    const notice = render(<NoticeCard content="n" />)
    const noticeCard = notice.container.querySelector('[data-testid="notice-card"]')!
    const noticeRow = noticeCard.firstElementChild!
    const noticeIcon = noticeCard.querySelector('svg')!

    const parsed = parseRecoveryMessage('[Empty response — automatic recovery]\nbody')!
    const recovery = render(<RecoveryCard parsed={parsed} />)
    const recoveryCard = recovery.container.querySelector('[data-testid="recovery-card"]')!
    const recoveryRow = recovery.container.querySelector('[data-testid="recovery-card-toggle"]')!
    const recoveryIcon = recovery.container.querySelectorAll('[data-testid="recovery-card-toggle"] svg')[1]!

    // Family regex uses (^|:) so a variant-prefixed conflict (sm:px-4,
    // hover:rounded-lg) is caught alongside a bare one.
    const family = (el: Element, re: RegExp) => [...el.classList].filter(c => re.test(c)).sort()
    // The mandated metric values, pinned as exact family sets on the NoticeCard
    // side — cross-component equality alone would stay green if both cards
    // drifted together off the agreed recipe.
    const containerFamilies: Array<[string, RegExp, string[]]> = [
      ['radius', /(?:^|:)rounded/, ['rounded-md']],
      ['ring', /(?:^|:)ring/, ['ring-1', 'ring-border', 'ring-inset']],
      ['background', /(?:^|:)bg-/, ['bg-card']],
      ['text color', /^text-(?!\[)/, ['text-muted']],
    ]
    for (const [name, re, expected] of containerFamilies) {
      expect(family(noticeCard, re), `container ${name}`).toEqual(expected)
      expect(family(recoveryCard, re), `recovery container ${name}`).toEqual(expected)
    }
    const rowFamilies: Array<[string, RegExp, string[]]> = [
      ['padding-x', /(?:^|:)px-/, ['px-3']],
      ['padding-y', /(?:^|:)py-/, ['py-2']],
      ['gap', /(?:^|:)gap-/, ['gap-2']],
      ['font size', /(?:^|:)text-\[/, ['text-[13px]']],
      ['line height', /(?:^|:)leading-/, ['leading-5']],
    ]
    for (const [name, re, expected] of rowFamilies) {
      expect(family(noticeRow, re), `row ${name}`).toEqual(expected)
      expect(family(recoveryRow, re), `recovery row ${name}`).toEqual(expected)
    }
    // Same rendered glyph size on both cards (the size prop lands as attrs).
    expect(noticeIcon.getAttribute('width')).toBe(recoveryIcon.getAttribute('width'))
    expect(noticeIcon.getAttribute('height')).toBe(recoveryIcon.getAttribute('height'))
    expect(noticeIcon.getAttribute('width')).toBe('13')
    // Wrapping-row alignment: top-aligned with the icon nudged onto the first
    // line's center; a rem/em calc so a non-16px root font-size cannot drift it.
    expect(noticeRow.classList.contains('items-start')).toBe(true)
    expect(noticeIcon.classList.contains('mt-[calc((1.25rem-1em)/2)]')).toBe(true)
  })
})

describe('registry wiring', () => {
  it("ChatPage renders the notice role through NoticeCard, not a hand-rolled box", () => {
    const here = dirname(fileURLToPath(import.meta.url))
    const src = readFileSync(resolve(here, '../pages/ChatPage.tsx'), 'utf8')
    // Since chat-core P5-a the page dispatches through the app-sdk registry;
    // the notice row is the page's `notice` host entry (same id as the default).
    expect(src).toMatch(/id: 'notice', roles: \['notice'\], render: [^\n]*<NoticeCard/)
    // The old inline branch carried its own class recipe; its return must be
    // gone so the style cannot fork again at this call site.
    expect(src).not.toMatch(/m\.role === 'notice'.*className=/)
  })

  it('the app-sdk default registry renders notice rows through NoticeCard too', () => {
    // Every non-ChatPage transcript surface (side chat, ChatEmbed, app panels)
    // resolves notice rows through this registry — the second path that let
    // the two styles fork in the first place.
    const entry = defaultMessageRenderers.find(r => r.roles.includes('notice'))!
    expect(entry).toBeDefined()
    const m: ChatMessage = {
      role: 'notice',
      content: '\u2139\uFE0F The model returned nothing twice — auto-continuing once.',
      cls: 'msg msg-info',
      ts: '2026-08-26T00:00:00.000Z',
    }
    const ctx = { row: (node: React.ReactNode) => node } as unknown as MessageRenderContext
    const { container } = render(<>{entry.render(m, ctx)}</>)
    expect(container.querySelector('[data-testid="notice-card"]')).not.toBeNull()
    expect(container.textContent).not.toContain('\u2139')
  })
})
