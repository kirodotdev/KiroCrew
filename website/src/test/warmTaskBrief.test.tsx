import { describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { defaultMessageRenderers, mergeRenderers, resolveRenderer, type MessageRenderContext } from '../app-sdk/messageRenderers'
import { isCrewmateChatRow } from '../components/chat/crewmateBubbles'
import WarmBriefCard, { factWords, warmBriefBodyOf, warmBriefOf, type WarmBrief } from '../components/chat/WarmBriefCard'
import type { ChatMessage } from '../types'
import { fmtMessageTime } from '../pages/chat/messageTime'
import { searchableText, searchableTextMemo } from '../utils/searchableText'
import waitingCss from '../components/chat/warmBriefWaiting.css?raw'

vi.mock('../utils/clipboard', async (orig) => ({
  ...(await orig<typeof import('../utils/clipboard')>()),
  copyToClipboard: vi.fn(() => Promise.resolve(true)),
}))
import { copyToClipboard } from '../utils/clipboard'

vi.mock('../components/WidgetFrame', async () => {
  const { useContext } = await import('react')
  const { WidgetFrameProfileContext } = await import('../components/widgetFrameProfile')
  return {
    default: function WidgetFrameStub({ html, messageTs, slotKey }: { html: string; messageTs?: string; slotKey?: string }) {
      const profile = useContext(WidgetFrameProfileContext)
      return <div data-testid="widget-frame" data-html={html} data-ts={messageTs ?? ''} data-slot={slotKey ?? ''} data-profile={profile} />
    },
  }
})

vi.mock('../components/markdown/MermaidBlock', async (orig) => ({
  ...(await orig<typeof import('../components/markdown/MermaidBlock')>()),
  MermaidBlock: ({ code }: { code: string }) => <div data-testid="mermaid-block" data-code={code} />,
}))

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
      needs_you: ['x', 2, '', null, 'y', 'z', 'w', 'v'], next: 'not a list',
      extra: [
        { title: 'Risk', items: ['a', 'b', 'c', 'd', 'e'] },
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
    expect(b.needsYou).toEqual(['x', 'y', 'z', 'w'])
    expect(b.next).toEqual([])
    expect(b.extra).toEqual([{ title: 'Risk', items: ['a', 'b', 'c', 'd'] }, { title: 'Cost', items: ['one run'] }])
  })

  it.each([
    ['on_track', 'on_track'], ['needs_you', 'needs_you'], ['blocked', 'blocked'], ['done', 'done'],
    ['stuck', null], [3, null], [undefined, null],
  ])('status %s reads as %s', (status, expected) => {
    expect(warmBriefOf(brief({ headline: 'Ready.', status }))!.status).toBe(expected)
  })

  it('an older brief draws with now as the headline and no status chip', () => {
    const b = warmBriefOf(brief({ asked: 'Fix the test', now: 'PR is open.', needs_you: ['Approve'], next: ['Merge'] }))!
    expect(b).toEqual({
      status: null, headline: 'PR is open.', asked: 'Fix the test', remaining: '',
      done: [], needsYou: ['Approve'], next: ['Merge'], extra: [], body: '', waiting: [], waitingTotal: 0, widget: '', links: [], asks: [], checked: [], lines: [],
    })
    render(<WarmBriefCard brief={b} />)
    expect(screen.queryByTestId('warm-brief-status')).toBeNull()
    // No status: the plain Needs you box, not the waiting cards.
    expect(screen.queryByTestId('warm-brief-waiting')).toBeNull()
    expect(screen.getByTestId('warm-brief-needs-you')).toHaveTextContent('Needs youApprove')
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
  status: null, headline: 'PR is open.', asked: '', remaining: '',
  done: [], needsYou: [], next: [], extra: [], body: '', waiting: [], waitingTotal: 0, widget: '', links: [], asks: [], checked: [], lines: [],
}

describe('WarmBriefCard', () => {
  it('draws the sections in report order: status, what needs you, ask, done, next, extra', () => {
    render(<WarmBriefCard brief={warmBriefOf(brief(FULL))!} />)
    const card = screen.getByTestId('warm-brief-card')
    const order = [...card.querySelectorAll('[data-testid^="warm-brief-"]')]
      .map(el => el.getAttribute('data-testid'))
    expect(order).toEqual([
      'warm-brief-status', 'warm-brief-headline', 'warm-brief-asks', 'warm-brief-waiting',
      'warm-brief-needs-you', 'warm-brief-waiting-card',
      'warm-brief-asked', 'warm-brief-done', 'warm-brief-next', 'warm-brief-extra',
    ])
    expect(screen.getByTestId('warm-brief-status')).toHaveTextContent('Waiting on you')
    expect(screen.getByTestId('warm-brief-status')).toHaveAttribute('data-status', 'needs_you')
    // The stored count of an older brief is ignored: no orb, no "2 of 5 done".
    expect(screen.queryByRole('progressbar')).toBeNull()
    expect(card).not.toHaveTextContent('2 of 5')
    expect(screen.getByTestId('warm-brief-asks')).toHaveTextContent('Left: Three fixes, then one green nightly')
    expect(screen.getByTestId('warm-brief-extra')).toHaveTextContent('Risk')
    expect(screen.getByTestId('warm-brief-extra')).toHaveTextContent('The fix touches a shared helper')
  })

  it('leaves out every empty section', () => {
    render(<WarmBriefCard brief={EMPTY} />)
    for (const id of ['status', 'needs-you', 'asks', 'asked', 'done', 'next', 'extra']) {
      expect(screen.queryByTestId(`warm-brief-${id}`)).toBeNull()
    }
    expect(screen.getByTestId('warm-brief-headline')).toHaveTextContent('PR is open.')
  })

  it('shows what is left without a bar', () => {
    render(<WarmBriefCard brief={{ ...EMPTY, remaining: 'One review' }} />)
    expect(screen.getByTestId('warm-brief-asks')).toHaveTextContent('Left: One review')
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

  it.each(['waiting on you', 'done', 'left: three', 'risk', 'fix', 'you asked: fix', 'next', 'helper'])(
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

describe('a free-form brief', () => {
  const FREE = {
    status: 'needs_you', headline: 'Nearly there 🎉', needs_you: ['Approve the push so CI runs'],
    progress: { accepted: 2, total: 3 }, body: 'You asked for a green nightly.\n\n- two fixes in\n- one to go',
    widget: '<div style="color:var(--accent)">timeline</div>',
  }

  it('draws the body after the waiting cards, then a stored widget in the sandboxed frame', () => {
    render(<WarmBriefCard brief={warmBriefOf(brief(FREE))!} />)
    const card = screen.getByTestId('warm-brief-card')
    const order = [...card.querySelectorAll('[data-testid^="warm-brief-"]')]
      .map(el => el.getAttribute('data-testid'))
    expect(order).toEqual([
      'warm-brief-status', 'warm-brief-headline', 'warm-brief-asks', 'warm-brief-waiting',
      'warm-brief-needs-you', 'warm-brief-waiting-card', 'warm-brief-body', 'warm-brief-widget',
    ])
    const body = screen.getByTestId('warm-brief-body')
    expect(body).toHaveTextContent('You asked for a green nightly.')
    // The body is markdown, drawn by the chat's own renderer.
    expect([...body.querySelectorAll('li')].map(li => li.textContent)).toEqual(['two fixes in', 'one to go'])
    expect(screen.getByTestId('widget-frame')).toHaveAttribute('data-html', FREE.widget)
    expect(screen.getByTestId('widget-frame')).toHaveAttribute('data-profile', 'welcome')
  })

  it('drops a widget over the bound and a non-string body or widget', () => {
    const big = warmBriefOf(brief({ headline: 'x', widget: '<p>' + 'a'.repeat(8000) + '</p>' }))!
    expect(big.widget).toBe('')
    const odd = warmBriefOf(brief({ headline: 'x', body: ['a'], widget: { html: 'b' } }))!
    expect([odd.body, odd.widget]).toEqual(['', ''])
    render(<WarmBriefCard brief={odd} />)
    expect(screen.queryByTestId('warm-brief-widget')).toBeNull()
  })

  it.each(['green nightly', 'one to go', 'timeline'])('search counts "%s" as the card shows it', (term) => {
    const m = brief(FREE)
    const { container } = render(<WarmBriefCard brief={warmBriefOf(m)!} />)
    const card = container.querySelector('[data-testid="warm-brief-card"]')!
    const shown = (card.textContent ?? '').toLowerCase().split(term).length - 1
    expect(searchableText(m).toLowerCase().split(term).length - 1).toBe(shown)
  })
})

describe('a picture the model chose', () => {
  const PICTURE = [
    'Since you left:',
    '',
    '```mermaid',
    'flowchart LR',
    'A[Fix PR] --> B{Approve push}',
    '```',
    '',
    '| Check | State |',
    '| --- | --- |',
    '| Unit | green |',
    '',
    '<mcwidget title="Nightly runs"><div style="color:var(--text)">Mon Tue Wed</div></mcwidget>',
  ].join('\n')
  const shown = () => render(<WarmBriefCard brief={warmBriefOf(brief({ status: 'needs_you', headline: 'Back.', body: PICTURE }))!} />)

  it('draws a mermaid block as a diagram, the same as an assistant reply', () => {
    shown()
    const body = screen.getByTestId('warm-brief-body')
    const block = body.querySelector('[data-testid="mermaid-block"]')
    expect(block).not.toBeNull()
    expect(block).toHaveAttribute('data-code', expect.stringContaining('A[Fix PR] --> B{Approve push}'))
    expect(body.textContent).not.toContain('```')
  })

  it('draws a markdown table as a table', () => {
    shown()
    const table = screen.getByTestId('warm-brief-body').querySelector('table')
    expect(table).not.toBeNull()
    expect(table).toHaveTextContent('Unit')
    expect(table).toHaveTextContent('green')
  })

  it('draws an <mcwidget> only in the sandboxed widget frame', () => {
    shown()
    const body = screen.getByTestId('warm-brief-body')
    const frame = body.querySelector('[data-testid="widget-frame"]')
    expect(frame).not.toBeNull()
    expect(frame!.getAttribute('data-html')).toContain('Mon Tue Wed')
    // The HTML is never live in the chat page itself.
    expect(body.textContent).not.toContain('Mon Tue Wed')
    expect(body.textContent).not.toContain('<mcwidget')
  })

  it('hands its widget to the same frame as an ordinary reply, under the welcome profile', async () => {
    const { default: MarkdownRenderer } = await import('../components/MarkdownRenderer')
    const widget = '<mcwidget title="Live"><button onclick="this.textContent=1">0</button><script>1</script></mcwidget>'
    const m = { ...brief({ status: 'needs_you', headline: 'Back.', body: widget }), ts: TS }
    const welcome = render(<>{warmBriefBodyOf(m, 'member-radar')}</>)
    const fromWelcome = welcome.container.querySelector('[data-testid="widget-frame"]')!
    const chat = render(<MarkdownRenderer content={widget} messageTs={TS} slotKey="member-radar" />)
    const fromChat = chat.container.querySelector('[data-testid="widget-frame"]')!
    // Same html, message time and session; only the profile differs.
    expect(fromChat).toHaveAttribute('data-profile', 'chat')
    expect(fromWelcome).toHaveAttribute('data-profile', 'welcome')
    fromChat.setAttribute('data-profile', 'welcome')
    expect(fromWelcome.outerHTML).toBe(fromChat.outerHTML)
    // Scripts and handlers reach the frame untouched; the frame sets the sandbox.
    expect(fromWelcome.getAttribute('data-html')).toContain('<script>1</script>')
    expect(fromWelcome).toHaveAttribute('data-slot', 'member-radar')
  })

  it('draws every body link as plain text, never an anchor', () => {
    render(<WarmBriefCard brief={warmBriefOf(brief({ status: 'needs_you', headline: 'Back.', body: 'See [the PR](https://evil.example/x) and https://evil.example/y' }))!} />)
    const body = screen.getByTestId('warm-brief-body')
    expect(body.querySelector('a')).toBeNull()
    expect(body).toHaveTextContent('See the PR and https://evil.example/y')
  })

  it("draws the gateway's link row, one link per listed PR, labelled from its own URL", () => {
    const ours = 'https://github.com/kirodotdev/KiroCrew/pull/12'
    const theirs = 'https://github.com/acme/other/pull/12'
    const m = brief({
      status: 'needs_you', headline: 'Back.', body: `#12 is green. [x](${ours})`,
      links: [ours, theirs, ours, 'https://evil.example/pull/1', 'https://github.com/a/b/pull/1?x=1', 42],
    })
    render(<WarmBriefCard brief={warmBriefOf(m)!} />)
    const row = screen.getByTestId('warm-brief-links')
    const anchors = Array.from(row.querySelectorAll('a'))
    expect(anchors.map(a => [a.textContent, a.getAttribute('href')])).toEqual([
      ['kirodotdev/KiroCrew#12', ours],
      ['acme/other#12', theirs],
    ])
    for (const a of anchors) {
      expect(a).toHaveAttribute('rel', 'noopener noreferrer')
      // The chat's own PR pill, not a style of the card's.
      expect(a.closest('.mc-md-ref-chip')).not.toBeNull()
    }
    // The body is drawn as written: its #N and its link stay text.
    const body = screen.getByTestId('warm-brief-body')
    expect(body.querySelector('a')).toBeNull()
    expect(body).toHaveTextContent('#12 is green. x')
    expect(searchableText(m)).toContain('kirodotdev/KiroCrew#12\nacme/other#12')
  })

  it('draws no link row when the gateway read no PR', () => {
    render(<WarmBriefCard brief={warmBriefOf(brief({ status: 'on_track', headline: 'Back.', body: '#12 is green.' }))!} />)
    expect(screen.queryByTestId('warm-brief-links')).toBeNull()
  })

  it("leaves the widget's HTML out of chat search", () => {
    const m = brief({ status: 'needs_you', headline: 'Back.', body: PICTURE })
    const text = searchableText(m)
    expect(text).not.toContain('Mon Tue Wed')
    expect(text).toContain('Since you left:')
  })
})

describe('the waiting cards', () => {
  const SHOT = [
    { title: 'B1 #22110: Windows race', ask: 'Pick the 3-line fix or the full lock rewrite' },
    { title: 'B5 #23387', ask: 'File the idle-spin report' },
    { title: 'B4 #22564', ask: 'Sign off the preload RFC' },
  ]
  const briefOf = (fields: Record<string, unknown>) => warmBriefOf(brief({ headline: 'Back.', status: 'needs_you', ...fields }))!
  const cardTexts = () => screen.queryAllByTestId('warm-brief-waiting-card').map(el => el.textContent)

  it.each([
    [{ accepted: 2, total: 5 }],
    [{ accepted: 44, total: 44 }],
  ])('a stored count %j draws no progress picture and no count label', (progress) => {
    const b = briefOf({ status: 'on_track', progress })
    expect(b).not.toHaveProperty('progress')
    const { container } = render(<WarmBriefCard brief={b} />)
    expect(screen.queryByRole('progressbar')).toBeNull()
    expect(screen.queryByTestId('warm-brief-orb')).toBeNull()
    expect(screen.queryByTestId('warm-brief-progress-count')).toBeNull()
    expect(container.querySelector('[class*="wb-orb"]')).toBeNull()
    expect(container.textContent).not.toMatch(/\d+ of \d+/)
  })

  it('three waiting: every card shows, no fold', () => {
    render(<WarmBriefCard brief={briefOf({ waiting: SHOT, waiting_total: 3 })} />)
    expect(cardTexts()).toEqual(SHOT.map(w => w.title + w.ask))
    expect(screen.queryByTestId('warm-brief-more')).toBeNull()
  })

  it('eight waiting: three show, the rest fold and none is lost', () => {
    const many = Array.from({ length: 8 }, (_, i) => ({ title: `Item ${i + 1}`, ask: `Do ${i + 1}` }))
    render(<WarmBriefCard brief={briefOf({ waiting: many, waiting_total: 8, needs_you: ['Also pick a name'] })} />)
    expect(cardTexts()).toEqual(['Item 1Do 1', 'Item 2Do 2', 'Item 3Do 3'])
    const more = screen.getByTestId('warm-brief-more')
    expect(more).toHaveTextContent('6 more')
    expect(more).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(more)
    expect(cardTexts()).toEqual([...many.map(w => w.title + w.ask), 'Also pick a name'])
    expect(more).toHaveTextContent('Show fewer')
    fireEvent.click(more)
    expect(cardTexts()).toHaveLength(3)
  })

  it('items the gateway counted past its list are still counted in the fold', () => {
    const listed = Array.from({ length: 50 }, (_, i) => ({ title: `Item ${i}`, ask: '' }))
    render(<WarmBriefCard brief={briefOf({ waiting: listed, waiting_total: 57 })} />)
    expect(screen.getByTestId('warm-brief-more')).toHaveTextContent('54 more')
    fireEvent.click(screen.getByTestId('warm-brief-more'))
    expect(cardTexts()).toHaveLength(50)
    expect(screen.getByTestId('warm-brief-unlisted')).toHaveTextContent('7 more not listed')
  })

  it('nothing waiting: only the status and headline', () => {
    render(<WarmBriefCard brief={briefOf({ status: 'on_track' })} />)
    expect(screen.queryByTestId('warm-brief-waiting')).toBeNull()
    expect(screen.queryByTestId('warm-brief-asks')).toBeNull()
    expect(screen.getByTestId('warm-brief-status')).toHaveAttribute('data-status', 'on_track')
  })

  it('a long title keeps every character in a wrapping card', () => {
    const long = 'A very very long waiting title '.repeat(4).trim()
    render(<WarmBriefCard brief={briefOf({ waiting: [{ title: long, ask: 'x' }], waiting_total: 1 })} />)
    const card = screen.getByTestId('warm-brief-waiting-card')
    expect(card.textContent).toBe(long + 'x')
    expect(card).toHaveClass('wb-wait-card')
  })

  it('malformed waiting entries are dropped and a bad total falls back to the list', () => {
    const b = briefOf({ waiting: [{ title: 'ok', ask: 3 }, { ask: 'no title' }, 'text', null], waiting_total: '9' })
    expect(b.waiting).toEqual([{ title: 'ok', ask: '' }])
    expect(b.waitingTotal).toBe(1)
  })

  it.each(['b1 #22110', 'pick the 3-line', 'idle-spin', 'rfc'])('search counts "%s" as the cards show it', (term) => {
    const m = brief({ headline: 'Back.', status: 'needs_you', waiting: SHOT, waiting_total: 3 })
    const { container } = render(<WarmBriefCard brief={warmBriefOf(m)!} />)
    const shownCount = (container.textContent ?? '').toLowerCase().split(term).length - 1
    expect(shownCount).toBeGreaterThan(0)
    expect(searchableText(m).toLowerCase().split(term).length - 1).toBe(shownCount)
  })
})

describe('structured asks', () => {
  const ASK = {
    what: 'Push the fix',
    why: 'Only you can approve a push',
    options: [
      { label: 'Push now', consequence: 'CI reruns' },
      { label: 'Wait for CI', consequence: '' },
    ],
    recommended: 'Push now',
    recommended_why: 'The fix is tested',
  }

  it('draws why, each consequence and the recommendation as text, and search reads them', () => {
    const m = brief({ headline: 'Back.', status: 'needs_you', asks: [ASK] })
    render(<WarmBriefCard brief={warmBriefOf(m)!} />)
    const ask = screen.getByTestId('warm-brief-ask')
    expect(ask.textContent).toContain('Why: Only you can approve a push')
    expect(ask.textContent).toContain('Push now: CI reruns')
    expect(screen.getByTestId('warm-brief-recommended').textContent).toBe('Recommended: Push now (The fix is tested)')
    expect(ask.querySelector('[data-recommended]')?.textContent).toBe('Push now: CI reruns')
    expect(ask.querySelectorAll('button, a')).toHaveLength(0)
    for (const term of ['why: only you', 'push now: ci reruns', 'wait for ci', 'the fix is tested']) {
      expect(searchableText(m).toLowerCase()).toContain(term)
    }
  })

  it('drops a recommendation outside the labels and asks without a what', () => {
    const m = brief({ headline: 'Back.', status: 'needs_you', asks: [{ ...ASK, recommended: 'Merge' }, { why: 'x' }, 'text'] })
    const b = warmBriefOf(m)!
    expect(b.asks).toHaveLength(1)
    expect(b.asks[0].recommended).toBe('')
    expect(b.asks[0].recommendedWhy).toBe('')
  })
})

describe('checked facts', () => {
  // The gateway stores machine values; the card says them in the UI language.
  const PR = { id: 'f1', kind: 'pr', label: 'kirodotdev/KiroCrew#12', values: { state: 'merged', review: 'APPROVED' }, source: 'github', read_at: '09:30' }
  const TASK = { id: 'f2', kind: 'task', label: 'Ship the importer', values: { status: 'progress' }, source: 'tasks' }

  it('translates each machine value and keeps every check count', () => {
    expect(factWords({ state: 'open', mergeable: 'MERGEABLE', checks: { passed: 10, failed: 0, pending: 2, unknown: 1 } })).toEqual([
      'open', 'no conflicts', 'checks 10 passed, 0 failed, 2 running, 1 unknown',
    ])
    // No conflicts is not ready to merge: only CLEAN says that.
    expect(factWords({ mergeable: 'CLEAN' })).toEqual(['ready to merge'])
    expect(factWords({ not_checked_since: '09:30' })).toEqual(['not checked since 09:30'])
    expect(factWords({ not_checked_since: 'never' })).toEqual(['never checked'])
    expect(factWords({ state: 'open', checks: 'not fully read', mergeable: 'UNKNOWN' })).toEqual(['open', 'checks not fully read'])
    // A value the card does not know is shown as read, never dropped.
    expect(factWords({ review: 'SOME_NEW_STATE' })).toEqual(['some new state'])
  })

  it('lists each fact the gateway read, by its id, as plain text that search reads', () => {
    const lines = [{ text: '#12 merged', kind: 'pr', facts: ['f1'] }, { text: 'Importer {f2} is open', kind: 'task', facts: ['f2'] }]
    const m = brief({ headline: 'Back.', status: 'on_track', lines, checked: [PR, TASK] })
    render(<WarmBriefCard brief={warmBriefOf(m)!} />)
    const section = screen.getByTestId('warm-brief-checked')
    expect(section.textContent).toContain('Checked')
    expect([...section.querySelectorAll('li')].map(li => li.textContent)).toEqual([
      '¹ kirodotdev/KiroCrew#12: merged · approved (GitHub 09:30)',
      '² Ship the importer: ongoing (task list)',
    ])
    expect(section.querySelectorAll('button, a')).toHaveLength(0)
    expect(searchableText(m)).toContain('² Ship the importer: ongoing (task list)')
    // Each line's marks come from its facts field; text is never read for ids.
    expect([...screen.getByTestId('warm-brief-lines').querySelectorAll('li')].map(li => li.textContent)).toEqual([
      '#12 merged¹',
      'Importer {f2} is open²',
    ])
    expect(searchableText(m)).toContain('#12 merged¹')
  })

  it('leaves the body as written: braces in a picture are never read as citations', () => {
    const body = '<mcwidget><script>const f1 = 1; const v = {f1};</script></mcwidget>'
    expect(warmBriefOf(brief({ headline: 'Back.', body }))!.body).toBe(body)
  })

  it('keeps every fact id the gateway can mint, three digits included, and drops malformed ids', () => {
    const many = Array.from({ length: 200 }, (_, i) => ({ ...TASK, id: `f${i + 1}` }))
    const lines = [{ text: 'B099 is open', kind: 'task', facts: ['f100', 'f0', 'f01', 'f201', 'x', 7] }]
    const b = warmBriefOf(brief({ headline: 'Back.', lines, checked: many }))!
    expect(b.checked).toHaveLength(200)
    expect(b.lines).toEqual([{ text: 'B099 is open', facts: ['f100'] }])
  })

  it('draws a failed read through the shared error surface, with the RFC wording', () => {
    const failed = { id: 'f3', kind: 'pr', label: 'kirodotdev/KiroCrew#13', values: { not_checked_since: '09:30' }, source: 'github' }
    const m = brief({ headline: 'Back.', status: 'on_track', checked: [PR, failed] })
    render(<WarmBriefCard brief={warmBriefOf(m)!} />)
    expect(screen.getByTestId('warm-brief-checked').textContent).not.toContain('#13')
    const notice = screen.getByTestId('warm-brief-not-checked')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice.textContent).toContain('³ kirodotdev/KiroCrew#13: not checked since 09:30 (GitHub)')
    expect(searchableText(m)).toContain('not checked since 09:30')
  })

  it('drops a fact without a well-formed id or a label, and draws no section without facts', () => {
    const m = brief({ headline: 'Back.', checked: [{ ...PR, id: 'x1' }, { ...TASK, label: '' }, 'f3', { ...TASK, id: 'f3' }] })
    expect(warmBriefOf(m)!.checked.map(f => f.id)).toEqual(['f3'])
    render(<WarmBriefCard brief={warmBriefOf(brief({ headline: 'Back.' }))!} />)
    expect(screen.queryByTestId('warm-brief-checked')).toBeNull()
  })
})

describe('the waiting cards stylesheet', () => {
  const css = waitingCss

  it('never moves', () => {
    expect(css).not.toMatch(/animation\s*:|transition\s*:|@keyframes/)
  })

  it('draws waiting cards as text, not as controls: no shadow and no box', () => {
    const card = css.match(/\.wb-wait-card \{[^}]*\}/)![0]
    expect(card).not.toMatch(/box-shadow|background|border-radius/)
  })

  it('colours only with theme variables and wraps card text', () => {
    const rules = css.replace(/\/\*[\s\S]*?\*\//g, '')
    expect(rules.match(/#[0-9a-f]{3,8}\b/gi) ?? []).toEqual([])
    expect(rules).not.toMatch(/gradient\(/)
    expect(css).toMatch(/\.wb-wait-card \{[^}]*overflow-wrap: anywhere/)
  })
})
