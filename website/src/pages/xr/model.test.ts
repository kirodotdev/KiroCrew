import { describe, expect, it } from 'vitest'
import type { ChatMessage, ChatSlot } from '../../types'
import { fixtureBriefing, fixtureGoals, fixturePage, xrFixture } from './fixture'
import { buildGlance, trendOf } from './glance'
import { buildXrCrew, commandOf, extrasOf, firstLine, noticesFrom, replyOf, topicOf } from './model'
import { XR_MESSAGE_MAX, linesFrom } from './thread'

const crew = buildXrCrew(xrFixture(false))
const bot = (slug: string) => crew.bots.find(b => b.slug === slug)!

describe('what a Bot is taken to have said', () => {
  const mira = (reply: number) => buildXrCrew(xrFixture(false, reply)).bots.find(b => b.slug === 'mira')!

  it('never takes the person\'s own message for the Bot\'s: a new line while the Bot starts to work is not a reply', () => {
    expect(mira(1)).toMatchObject({ state: 'working', line: 'How are sign-ups today?' })
    expect(replyOf(mira(0), mira(1))).toBe('')
  })

  it('takes the new line as the Bot\'s when its turn has just ended', () => {
    expect(replyOf(mira(1), mira(2))).toBe('Sign-ups passed 1,700 today.')
  })

  it('says nothing when the turn ended without a word, when nothing changed, or when the Bot now asks', () => {
    expect(replyOf({ state: 'working', line: 'Can you look?' }, { state: 'idle', line: 'Can you look?' })).toBe('')
    expect(replyOf({ state: 'idle', line: 'a' }, { state: 'idle', line: 'b' })).toBe('')
    expect(replyOf({ state: 'working', line: 'a' }, { state: 'needs', line: 'b' })).toBe('')
    expect(replyOf(mira(0), mira(0))).toBe('')
  })
})

describe('the sessions at rest that a done sheet reads (P3)', () => {
  const slot = (over: Partial<ChatSlot>): ChatSlot => ({ key: 'own', messages: 2, running: false, mode: 'member', agent: 'juno', last_activity_ts: '2026-11-21T10:42:00Z', ...over })
  const fold = (slots: ChatSlot[], unread: string[] = []) => buildXrCrew({ members: [{ slug: 'juno', name: 'juno', display_name: 'Juno', slot_key: 'own' } as never], slots, approvals: [], questions: [], unread }).bots[0]

  it('lists its own thread and its workers when no turn runs, nothing waits and nothing was cut, with what each links and left open', () => {
    const juno = fold([
      slot({ title: 'Release notes', source_links: [{ provider: 'github', number: 7, url: 'https://example.invalid/7', state: 'open', ci: 'passed', kind: 'change' }] as never, todo: { description: '', tasks: [{ id: '1', text: 'write the tests', completed: false }, { id: '2', text: 'draft', completed: true }], completed: 1, total: 2, current: '1' } }),
      slot({ key: 'w1', mode: 'worker', mate_root: 'own', title: 'Trim the cache' } as never),
      slot({ key: 'w2', mode: 'worker', mate_root: 'own', running: true } as never),
      slot({ key: 'w3', mode: 'worker', mate_root: 'own', interrupted: true } as never),
      slot({ key: 'w4', mode: 'worker', mate_root: 'own', needs_input: true } as never),
    ], ['w1'])
    expect(juno.rest.map(r => [r.slot, r.direct, r.unread])).toEqual([['own', true, false], ['w1', false, true]])
    // Its own thread's title is the Bot's name, so it has no topic: Home's line says its last words instead.
    expect(juno.rest[0]).toMatchObject({ topic: '', made: [{ kind: 'change', number: 7, state: 'open', ci: 'passed' }], left: ['write the tests'] })
    expect(juno.rest[1]).toMatchObject({ topic: 'Trim the cache', made: [], left: [] })
  })
})

describe('the Bot-first fold', () => {
  it('shows Bots, not slots: one entry per roster row, in roster order', () => {
    expect(crew.bots.map(b => b.slug)).toEqual(['atlas', 'juno', 'mira', 'otto', 'pico', 'sage'])
  })

  it('folds the work a Bot handed out under that Bot as things with a topic', () => {
    const things = bot('atlas').things
    expect(things.map(t => t.topic)).toEqual(expect.arrayContaining(['Flaky test sweep', 'Trim the build cache', 'Release notes for 0.15']))
  })

  it('puts what waits on the person first, then what failed, then what runs', () => {
    expect(bot('atlas').things[0].state).toBe('needs')
  })

  it('keeps every thing a Bot is on: the full layer must be able to show them all', () => {
    expect(bot('atlas').things).toHaveLength(4)
  })

  it('still counts a thing flagged as waiting when no read carries its request', () => {
    const f = xrFixture(false)
    const c = buildXrCrew({ ...f, slots: [...f.slots, { key: 'w-mira-x', title: 'Chart colours', mate_root: 'member-mira', running: false, needs_input: true, last_message: 'Which palette?' } as ChatSlot] })
    const need = c.needs.find(n => n.slot === 'w-mira-x')!
    expect(need).toMatchObject({ kind: 'attention', bot: 'mira', topic: 'Chart colours' })
    expect(c.bots.find(b => b.slug === 'mira')!.state).toBe('needs')
  })

  it('says how its reads went, and assumes nothing when it is not told', () => {
    // With no list of sessions of its own (the land's sample crew) the list counts as whole.
    expect(crew.feeds).toEqual({ roster: 'ok', asks: 'ok', live: true, rows: true })
    const failed = buildXrCrew({ ...xrFixture(false), feeds: { roster: 'ok', asks: 'failed', live: false } })
    expect(failed.feeds.asks).toBe('failed')
  })

  it('binds an ask to its whole payload, and shows the fields beside the command', () => {
    expect(extrasOf('{"command":"rm x","cwd":"/srv","force":true}')).toEqual(['cwd: /srv', 'force: true'])
    const flaky = crew.needs.find(n => n.requestId === 'req-flaky-1')!
    expect(flaky.version).toContain('req-flaky-1')
    expect(flaky.version).toContain('--shard 3/8')
  })

  it('keeps every question of a card, in order', () => {
    const q = crew.needs.find(n => n.kind === 'question')!
    expect(q.questions.map(x => x.question)).toEqual(['Which region goes first?', 'Start today or after the release?'])
  })

  it('reads a Bot state from all of its work', () => {
    expect(crew.bots.map(b => b.state)).toEqual(['needs', 'needs', 'idle', 'working', 'failed', 'idle'])
  })

  it('names every ask by Bot and thing, and keeps the exact command', () => {
    const flaky = crew.needs.find(n => n.requestId === 'req-flaky-1')!
    expect(flaky).toMatchObject({ who: 'Atlas', bot: 'atlas', topic: 'Flaky test sweep', kind: 'approval', command: 'npm run test -- --shard 3/8 --retry 0' })
  })

  it('walks asks oldest first, so "next" is fair', () => {
    expect(crew.needs.map(n => n.requestId)).toEqual(['coord-cache-1', 'ask-region-1', 'req-flaky-1'])
  })

  it('counts one request once when the slot and the approvals list both carry it', () => {
    const f = xrFixture(false)
    const twice = buildXrCrew({ ...f, approvals: [...f.approvals, { id: 'req-flaky-1', slot: 'w-atlas-flaky', tool: 'shell' }] })
    expect(twice.needs.filter(n => n.requestId === 'req-flaky-1')).toHaveLength(1)
  })

  it('counts work no Bot owns, and never lists it', () => {
    expect(crew.unowned).toBe(1)
    expect(crew.bots.flatMap(b => b.things).some(t => t.slot === 'chat-7f3a9c21')).toBe(false)
  })

  it('still counts an ask on work no Bot owns, so the count stays true', () => {
    const f = xrFixture(false)
    const stray: ChatSlot = { key: 'stray-1', title: 'Fix the login page', running: false, needs_input: false, pending_approval: true, pending_approval_info: { request_id: 'r9', tool: 'shell', tool_input: 'ls' } } as ChatSlot
    const c = buildXrCrew({ ...f, slots: [...f.slots, stray] })
    expect(c.needs).toHaveLength(4)
    expect(c.needs.find(n => n.requestId === 'r9')).toMatchObject({ bot: '', topic: 'Fix the login page' })
  })

  it('gives a worker with a machine name no topic', () => {
    expect(topicOf({ key: 'chat-7f3a9c21', title: 'chat-7f3a9c21' })).toBe('')
    expect(topicOf({ key: 'k', title: 'worker-12ab34cd' })).toBe('')
    expect(topicOf({ key: 'k', title: 'Release notes for 0.15' })).toBe('Release notes for 0.15')
  })

  it('lets a new ask arrive as data', () => {
    const late = buildXrCrew(xrFixture(true))
    expect(late.needs).toHaveLength(4)
    expect(late.bots.find(b => b.slug === 'otto')!.state).toBe('needs')
  })

  it('reads the first sentence of a message and the command of a tool call', () => {
    expect(firstLine('**Done.** The rest follows.\n\nMore.')).toBe('Done.')
    expect(firstLine('See [the report](https://example.test/r) now')).toBe('See the report now')
    expect(commandOf('{"command":"ls -la"}')).toBe('ls -la')
    expect(commandOf('not json')).toBe('not json')
  })

  it('lists unread notices with a real time', () => {
    const notes = noticesFrom(xrFixture(false).notices)
    expect(notes).toHaveLength(2)
    expect(notes[0].ts).toBeGreaterThan(0)
  })
})

describe('the glance', () => {
  const glance = (slug: string) => buildGlance(bot(slug), fixtureGoals(slug), fixtureBriefing(slug), fixturePage(slug))

  it('has a verdict, at most three numbers with a trend, the asks, what is next and a time', () => {
    const g = glance('atlas')
    expect(g.verdict).toBe('Release 0.15 is one step from the tag: two flaky tests block it.')
    expect(g.numbers.map(n => [n.value, n.target, n.trend])).toEqual([['9', '12', 'up'], ['2', '0', 'down'], ['2', '0', 'flat']])
    expect(g.needs).toHaveLength(2)
    expect(g.next).toBe('Release notes for 0.15')
    expect(g.page).toBe('live')
  })

  it('still works for a Bot with no dashboard page and no goals', () => {
    const g = glance('sage')
    expect(g).toMatchObject({ page: 'empty', numbers: [], goals: [], verdict: 'Nothing new since this morning.' })
  })

  it('gives each fact its own time, so an old number cannot pass for a new one', () => {
    const g = glance('atlas')
    expect(g.numbers.every(n => n.at > 0)).toBe(true)
    expect(g.verdictAt).not.toBe(g.numbers[0].at)
  })

  it('carries how the goals and briefing reads went', () => {
    const g = buildGlance(bot('mira'), null, null, 'unknown', { goals: 'failed', briefing: 'loading' })
    expect(g).toMatchObject({ goalsFeed: 'failed', briefFeed: 'loading', numbers: [] })
  })

  it('reads a trend only from readings that are plain numbers', () => {
    expect(trendOf([{ value: 'n/a' }, { value: '3' }])).toBe('none')
    expect(trendOf([{ value: 'about 3 of 10' }, { value: 'about 5 of 10' }])).toBe('none')
    expect(trendOf([{ value: '1,490' }, { value: '1,610' }])).toBe('up')
    expect(trendOf([{ value: '80%' }, { value: '75%' }])).toBe('down')
  })
})

describe('a transcript as lines', () => {
  const msg = (role: string, content: string): ChatMessage => ({ role, content, cls: '' })

  it('keeps who spoke, drops markup and folds a run of tool rows into one', () => {
    const lines = linesFrom([
      msg('user', 'Where does it stand?'),
      msg('tool', 'read a.md'), msg('tool', 'read b.md'),
      msg('assistant', '**Two** things are left.<script>alert(1)</script>'),
    ])
    expect(lines.map(l => l.who)).toEqual(['you', 'tool', 'bot'])
    expect(lines[1]).toMatchObject({ text: 'read b.md', run: 2 })
    expect(lines[2].text).toBe('Two things are left.<script>alert(1)</script>')
  })

  it('keeps code inside a fence exactly as written', () => {
    const [line] = linesFrom([msg('assistant', 'Run:\n```sh\nls **/*.ts | grep "<T>"\n```')])
    expect(line.text).toBe('Run:\nls **/*.ts | grep "<T>"')
  })

  it('marks a message that was cut, with how much is missing', () => {
    const [line] = linesFrom([msg('assistant', 'x'.repeat(XR_MESSAGE_MAX + 25))])
    expect(line.cut).toBe(25)
    expect([...line.text]).toHaveLength(XR_MESSAGE_MAX)
  })

  it('sends sample threads through the same conversion as live ones', async () => {
    const { fixtureMessages } = await import('./fixture')
    expect(linesFrom(fixtureMessages('member-atlas')).map(l => l.who).slice(0, 3)).toEqual(['you', 'bot', 'you'])
  })
})
