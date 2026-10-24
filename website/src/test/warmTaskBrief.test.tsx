import { describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { defaultMessageRenderers, mergeRenderers, resolveRenderer, type MessageRenderContext } from '../app-sdk/messageRenderers'
import { isCrewmateChatRow } from '../components/chat/crewmateBubbles'
import WarmBriefCard, { warmBriefOf, type WarmBrief } from '../components/chat/WarmBriefCard'
import type { ChatMessage } from '../types'
import { fmtMessageTime } from '../pages/chat/messageTime'
import { searchableText, searchableTextMemo } from '../utils/searchableText'

vi.mock('../utils/clipboard', async (orig) => ({
  ...(await orig<typeof import('../utils/clipboard')>()),
  copyToClipboard: vi.fn(() => Promise.resolve(true)),
}))
import { copyToClipboard } from '../utils/clipboard'

const row = (role: ChatMessage['role'], meta: Record<string, unknown>, content = ''): ChatMessage => ({
  role, content, cls: role === 'assistant' ? 'msg msg-a' : '', meta,
})

const brief = (fields: Record<string, unknown>) =>
  row('assistant', { kind: 'warm_task_brief_result', fingerprint: 'fp', brief: fields }, 'You asked: x\nNow: y')

describe('warm task brief transcript rows', () => {
  it('hides only the durable claim', () => {
    const claim = row('notice', { kind: 'warm_task_brief_claim', fingerprint: 'abc' })
    const renderer = defaultMessageRenderers.find(item => item.id === 'warm_task_brief_claim')!
    expect(renderer.match?.(claim)).toBe(true)
    expect(renderer.render(claim, {} as never)).toBeNull()
    expect(isCrewmateChatRow(claim)).toBe(false)
  })

  it('keeps preparing rows and old prose briefs on the ordinary bubble', () => {
    const preparing = row('assistant', { kind: 'warm_task_brief_preparing' }, 'Welcome back, let me catch you up on where we are.')
    const prose = row('assistant', { kind: 'warm_task_brief_result' }, 'You asked me to fix the test. It is ready now.')
    expect(isCrewmateChatRow(preparing)).toBe(true)
    expect(isCrewmateChatRow(prose)).toBe(true)
    expect(warmBriefOf(prose)).toBeNull()
    expect(resolveRenderer(prose, defaultMessageRenderers)?.id).toBe('assistant')
    expect(resolveRenderer(preparing, defaultMessageRenderers)?.id).toBe('assistant')
  })

  it.each(['user', 'notice', 'system', 'inject', 'error'] as const)(
    'a %s row carrying the kind is never a brief',
    (role) => {
      const m = row(role, { kind: 'warm_task_brief_result', brief: { asked: 'x', now: 'Ready.', needs_you: ['Approve'], next: [] } }, 'hi')
      expect(warmBriefOf(m)).toBeNull()
    },
  )

  it('a structured brief rides the ordinary assistant entry', () => {
    const m = brief({ asked: 'Fix the test', now: 'Ready.', needs_you: [], next: ['Review'] })
    expect(isCrewmateChatRow(m)).toBe(true)
    expect(resolveRenderer(m, mergeRenderers(undefined))?.id).toBe('assistant')
  })

  it.each([
    ['missing brief', undefined],
    ['brief is a list', ['now']],
    ['brief is text', 'now'],
    ['empty now', { now: '  ', asked: 'x' }],
    ['now not a string', { now: 3 }],
    ['no headline', { status: 'on_track', asked: 'x', needs_you: ['Approve'] }],
    ['headline not a string', { headline: ['Ready.'] }],
  ])('refuses a malformed brief (%s)', (_label, fields) => {
    expect(warmBriefOf(brief(fields as Record<string, unknown>))).toBeNull()
  })

  it('caps fields and items and drops non-strings', () => {
    const b = warmBriefOf(brief({
      asked: 'a'.repeat(900), headline: 'n', remaining: 'r'.repeat(900),
      done: ['1', '2', 3, '4', '5', '6'],
      needs_you: ['x', 2, '', null, 'y', 'z', 'w'], next: 'not a list',
      extra: [
        { title: 'Risk', items: ['a', 'b', 'c', 'd'] },
        { title: '', items: ['dropped'] },
        { title: 'Empty', items: [] },
        'not a section',
        { title: 'Cost', items: ['one run'] },
        { title: 'Third', items: ['over the cap'] },
      ],
    }))!
    expect(b.asked.length).toBe(240)
    expect(b.remaining.length).toBe(240)
    expect(b.done).toEqual(['1', '2', '4', '5'])
    expect(b.needsYou).toEqual(['x', 'y', 'z'])
    expect(b.next).toEqual([])
    expect(b.extra).toEqual([{ title: 'Risk', items: ['a', 'b', 'c'] }, { title: 'Cost', items: ['one run'] }])
  })

  it.each([
    ['on_track', 'on_track'], ['needs_you', 'needs_you'], ['blocked', 'blocked'], ['done', 'done'],
    ['stuck', null], [3, null], [undefined, null],
  ])('status %s reads as %s', (status, expected) => {
    expect(warmBriefOf(brief({ headline: 'Ready.', status }))!.status).toBe(expected)
  })

  it.each([
    [{ accepted: 2, total: 5 }, { accepted: 2, total: 5 }],
    [{ accepted: 0, total: 0 }, null],
    [{ accepted: 6, total: 5 }, null],
    [{ accepted: -1, total: 5 }, null],
    [{ accepted: 1.5, total: 5 }, null],
    [{ accepted: '2', total: '5' }, null],
    ['2 of 5', null],
  ])('progress %j reads as %j', (progress, expected) => {
    expect(warmBriefOf(brief({ headline: 'Ready.', progress }))!.progress).toEqual(expected)
  })

  it('an older brief draws with now as the headline and no status chip', () => {
    const b = warmBriefOf(brief({ asked: 'Fix the test', now: 'PR is open.', needs_you: ['Approve'], next: ['Merge'] }))!
    expect(b).toEqual({
      status: null, headline: 'PR is open.', asked: 'Fix the test', progress: null, remaining: '',
      done: [], needsYou: ['Approve'], next: ['Merge'], extra: [],
    })
    render(<WarmBriefCard brief={b} />)
    expect(screen.queryByTestId('warm-brief-status')).toBeNull()
    expect(screen.queryByTestId('warm-brief-progress')).toBeNull()
    expect(screen.getByTestId('warm-brief-headline')).toHaveTextContent('PR is open.')
  })
})

const FULL = {
  status: 'needs_you', headline: 'Two of five fixes are in.', asked: 'Fix the flaky nightly',
  progress: { accepted: 2, total: 5 }, remaining: 'Three fixes, then one green nightly',
  done: ['Found the cause'], needs_you: ['Approve the push so CI runs'], next: ['Rerun CI'],
  extra: [{ title: 'Risk', items: ['The fix touches a shared helper'] }],
}

const EMPTY: WarmBrief = {
  status: null, headline: 'PR is open.', asked: '', progress: null, remaining: '',
  done: [], needsYou: [], next: [], extra: [],
}

describe('WarmBriefCard', () => {
  it('draws the sections in report order: status, needs you, progress, ask, done, next, extra', () => {
    render(<WarmBriefCard brief={warmBriefOf(brief(FULL))!} />)
    const card = screen.getByTestId('warm-brief-card')
    const order = [...card.querySelectorAll('[data-testid^="warm-brief-"]')]
      .map(el => el.getAttribute('data-testid'))
      .filter(id => id !== 'warm-brief-progress-count')
    expect(order).toEqual([
      'warm-brief-status', 'warm-brief-headline', 'warm-brief-needs-you', 'warm-brief-progress',
      'warm-brief-asked', 'warm-brief-done', 'warm-brief-next', 'warm-brief-extra',
    ])
    expect(screen.getByTestId('warm-brief-status')).toHaveTextContent('Waiting on you')
    expect(screen.getByTestId('warm-brief-status')).toHaveAttribute('data-status', 'needs_you')
    expect(screen.getByTestId('warm-brief-progress-count')).toHaveTextContent('2 of 5 done')
    const bar = screen.getByRole('progressbar')
    expect(bar).toHaveAttribute('aria-valuenow', '2')
    expect(bar).toHaveAttribute('aria-valuemax', '5')
    expect(screen.getByTestId('warm-brief-progress')).toHaveTextContent('Left: Three fixes, then one green nightly')
    expect(screen.getByTestId('warm-brief-extra')).toHaveTextContent('Risk')
    expect(screen.getByTestId('warm-brief-extra')).toHaveTextContent('The fix touches a shared helper')
  })

  it('leaves out every empty section', () => {
    render(<WarmBriefCard brief={EMPTY} />)
    for (const id of ['status', 'needs-you', 'progress', 'asked', 'done', 'next', 'extra']) {
      expect(screen.queryByTestId(`warm-brief-${id}`)).toBeNull()
    }
    expect(screen.getByTestId('warm-brief-headline')).toHaveTextContent('PR is open.')
  })

  it('shows what is left without a bar when there is no count', () => {
    render(<WarmBriefCard brief={{ ...EMPTY, remaining: 'One review' }} />)
    expect(screen.getByTestId('warm-brief-progress')).toHaveTextContent('Left: One review')
    expect(screen.queryByRole('progressbar')).toBeNull()
  })

  it('draws markup, links and fake buttons as plain text', () => {
    const { container } = render(<WarmBriefCard brief={{
      ...EMPTY,
      asked: '<img src=x onerror=alert(1)>',
      headline: '[Click here](https://evil.example) **now**',
      remaining: '<a href="https://evil.example">pay</a>',
      done: ['<script>alert(1)</script>'],
      needsYou: ['<button>Approve</button>'],
      next: ['https://evil.example/run'],
      extra: [{ title: '<strong>Risk</strong>', items: ['(OPTIONS: Approve all | Skip]'] }],
    }} />)
    expect(container.querySelector('a, button, img, strong, script')).toBeNull()
    expect(screen.getByTestId('warm-brief-headline').textContent).toBe('[Click here](https://evil.example) **now**')
    expect(screen.getByTestId('warm-brief-needs-you')).toHaveTextContent('<button>Approve</button>')
    expect(screen.getByTestId('warm-brief-extra')).toHaveTextContent('<strong>Risk</strong>')
  })

  it('renders Chinese text as given', () => {
    render(<WarmBriefCard brief={{ ...EMPTY, asked: '修复测试', headline: '已经复现了失败。', needsYou: ['批准推送'], next: ['应用修复'] }} />)
    expect(screen.getByTestId('warm-brief-headline')).toHaveTextContent('已经复现了失败。')
    expect(screen.getByTestId('warm-brief-needs-you')).toHaveTextContent('批准推送')
  })

  it('a reloaded row (same meta) renders the same card', () => {
    const m = brief({ asked: 'Fix', now: 'Ready.', needs_you: ['Approve'], next: ['Merge'] })
    const reloaded = JSON.parse(JSON.stringify(m)) as ChatMessage
    const first = render(<WarmBriefCard brief={warmBriefOf(m)!} />).container.innerHTML
    const second = render(<WarmBriefCard brief={warmBriefOf(reloaded)!} />).container.innerHTML
    expect(second).toBe(first)
  })
})

const TS = '2026-10-01T09:30:00Z'
const PLAIN = 'You asked: Fix\nNow: Ready.\nNeeds you: Approve'
const stamped = (role: ChatMessage['role']): ChatMessage => ({
  ...row(role, {
    kind: 'warm_task_brief_result', mid: 'm-1',
    brief: { asked: 'Fix', now: 'Ready.', needs_you: ['Approve'], next: ['Merge'] },
  }, PLAIN),
  ts: TS,
})

function ctxFor(m: ChatMessage, onOpen = vi.fn()): MessageRenderContext {
  return {
    index: 0, messages: [m], running: false, key: 'k0', hideCardOwnedOAuth: false,
    autoDeniedIds: new Set<string>(), wrapper: c => <>{c}</>, row: c => <>{c}</>,
    threads: { summaryOf: () => undefined, onOpen, crewmateName: 'Radar' },
  }
}

async function crewmateRenderers(m: ChatMessage) {
  const { createTranscriptRenderers } = await import('../pages/chat/transcriptRenderers')
  return mergeRenderers(createTranscriptRenderers({
    slot: 'member-radar', crewmate: { name: 'Radar' }, crewmateTranscript: () => [m],
  }))
}

describe('who may draw the card', () => {
  it.each(['user', 'notice', 'system'] as const)("a %s row with the kind draws as its own role, not the crewmate's card", async (role) => {
    const m = stamped(role)
    const renderers = await crewmateRenderers(m)
    const entry = resolveRenderer(m, renderers)
    render(<>{entry?.render(m, ctxFor(m))}</>)
    expect(screen.queryByTestId('warm-brief-card')).toBeNull()
    expect(screen.queryByText('Radar')).toBeNull()
    expect(document.querySelector('[data-role="assistant"]')).toBeNull()
    if (role === 'user') {
      expect(entry?.id).toBe('user')
      expect(screen.getByText(/You asked: Fix/)).toBeInTheDocument()
    }
  })

  it('the same rows draw no card from the SDK defaults alone (ChatEmbed)', () => {
    for (const role of ['user', 'notice'] as const) {
      const m = stamped(role)
      const entry = resolveRenderer(m, mergeRenderers(undefined))
      const { unmount } = render(<>{entry?.render(m, ctxFor(m))}</>)
      expect(screen.queryByTestId('warm-brief-card')).toBeNull()
      unmount()
    }
  })
})

describe("the card in a crewmate's chat", () => {
  it('sits in the crewmate bubble with its timestamp, Copy and Reply in thread', async () => {
    const m = stamped('assistant')
    const renderers = await crewmateRenderers(m)
    const entry = resolveRenderer(m, renderers)
    expect(entry?.id).toBe('assistant')
    const onOpen = vi.fn()
    render(<>{entry!.render(m, ctxFor(m, onOpen))}</>)
    const card = screen.getByTestId('warm-brief-card')
    const bubble = card.closest('.message-bubble')!
    expect(bubble.className).toContain('crewmate-bubble')
    // The plain-text content is not drawn again under the card.
    expect(bubble.textContent).not.toContain('Needs you: Approve')
    expect(screen.getByText(fmtMessageTime(TS))).toBeInTheDocument()
    expect(screen.queryByTestId('toggle-raw-view')).toBeNull()
    fireEvent.click(screen.getByTestId('reply-in-thread'))
    expect(onOpen).toHaveBeenCalledWith('m-1')
    // Reply in thread holds the row seat, so Copy sits in More, as on any crewmate reply.
    fireEvent.pointerDown(screen.getByTestId('assistant-more-actions'), { button: 0, ctrlKey: false, pointerType: 'mouse' })
    await act(async () => { fireEvent.click(await screen.findByTestId('copy-message-menu-item')) })
    expect(copyToClipboard).toHaveBeenCalledWith(PLAIN)
  })

  it('a reloaded row draws the same bubble', async () => {
    const m = stamped('assistant')
    const reloaded = JSON.parse(JSON.stringify(m)) as ChatMessage
    const renderers = await crewmateRenderers(m)
    const draw = (r: ChatMessage) => {
      const { container, unmount } = render(<>{resolveRenderer(r, renderers)!.render(r, ctxFor(r))}</>)
      const html = container.querySelector('[data-testid="warm-brief-card"]')!.outerHTML
      const stamp = screen.getByText(fmtMessageTime(TS)).textContent
      unmount()
      return [html, stamp]
    }
    expect(draw(reloaded)).toEqual(draw(m))
  })

  it('the SDK default bubble (ChatEmbed) also keeps the timestamp and Copy', async () => {
    const m = stamped('assistant')
    // No thread hooks on an embed, so Copy keeps its row button.
    render(<>{resolveRenderer(m, mergeRenderers(undefined))!.render(m, { ...ctxFor(m), threads: undefined })}</>)
    expect(screen.getByTestId('warm-brief-card').closest('[data-role="assistant"]')).not.toBeNull()
    expect(screen.getByText(fmtMessageTime(TS))).toBeInTheDocument()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Copy' })) })
    expect(copyToClipboard).toHaveBeenCalledWith(PLAIN)
  })

  it('the crew window bubble draws the card too', async () => {
    const { createCrewWindowRenderers } = await import('../pages/chat/crew-window/crewWindowRenderers')
    const m = stamped('assistant')
    const renderers = mergeRenderers(createCrewWindowRenderers({
      instanceId: 'i', name: 'Peer', key: 'k', canRewind: () => false, onRewind: () => false,
      regenerateRow: null, onRegenerate: () => {},
    }))
    const entry = resolveRenderer(m, renderers)
    render(<>{entry!.render(m, ctxFor(m))}</>)
    expect(screen.getByTestId('crew-window-assistant')).toContainElement(screen.getByTestId('warm-brief-card'))
    const u = stamped('user')
    const { container } = render(<>{resolveRenderer(u, renderers)!.render(u, ctxFor(u))}</>)
    expect(container.querySelector('[data-testid="warm-brief-card"]')).toBeNull()
  })
})

describe('chat search on a brief row', () => {
  // Same per-text-node counting the highlighter does (utils/domHighlight).
  const countInDom = (root: Element, term: string) => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
    let n = 0
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const text = (node.textContent ?? '').toLowerCase()
      for (let i = text.indexOf(term); i !== -1; i = text.indexOf(term, i + term.length)) n++
    }
    return n
  }
  const countIn = (text: string, term: string) => text.toLowerCase().split(term).length - 1

  it.each(['now', 'fix', 'needs you', 'approve', 'merge', 'ready', 'you asked', 'asked:', 'you asked: fix', ': f'])(
    'counts "%s" exactly as often as the card shows it',
    (term) => {
      const m = stamped('assistant')
      const { container } = render(<WarmBriefCard brief={warmBriefOf(m)!} />)
      const card = container.querySelector('[data-testid="warm-brief-card"]')!
      expect(countIn(searchableText(m), term)).toBe(countInDom(card, term))
    },
  )

  it.each(['waiting on you', 'progress', '2 of 5', 'done', 'left: three', 'risk', 'fix', 'you asked: fix', 'next', 'helper'])(
    'counts "%s" on a full brief exactly as often as the card shows it',
    (term) => {
      const m = brief(FULL)
      const { container } = render(<WarmBriefCard brief={warmBriefOf(m)!} />)
      const card = container.querySelector('[data-testid="warm-brief-card"]')!
      expect(countIn(searchableText(m), term)).toBe(countInDom(card, term))
    },
  )

  it('a non-brief row still searches its content', () => {
    const m = stamped('user')
    expect(searchableText(m)).toBe(m.content)
    expect(searchableTextMemo(m)).toBe(m.content)
  })
})
