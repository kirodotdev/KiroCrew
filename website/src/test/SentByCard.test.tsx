import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import SentByCard, {
  parseSentBy,
  sentByBody,
  stripSentByPrefix,
  sentByDefaultExpanded,
  sentByKind,
  sentByName,
} from '../pages/chat/SentByCard'
import type { ChatMessage } from '../types'

const PREFIX = '[sent by session member-conductor via session_send]\n\n'
const BODY = 'Take the first triage item.\nSecond line.'

const MEMBER = {
  session_key: 'member-conductor',
  via: 'session_send',
  title: 'conductor',
  agent: 'kirocrew-conductor',
  member_slug: 'conductor',
}
const WORKER = {
  session_key: 'chat-4-1789',
  via: 'send_message_origin',
  title: 'Triage #42',
  agent: 'kirocrew-worker',
  child: true,
}
const SESSION = { session_key: 'chat-9-1', via: 'session_send', title: '', agent: 'kirocrew' }

function makeMsg(over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    role: 'user',
    content: PREFIX + BODY,
    cls: 'msg msg-u',
    ts: '2026-09-14T03:00:00.000Z',
    meta: { sent_by: MEMBER },
    ...over,
  } as ChatMessage
}

describe('parseSentBy', () => {
  it('reads the gateway record off meta', () => {
    expect(parseSentBy(makeMsg())).toEqual(MEMBER)
  })

  it('is null for a plain user row and for a malformed record', () => {
    expect(parseSentBy(makeMsg({ meta: undefined }))).toBeNull()
    expect(parseSentBy(makeMsg({ meta: { sent_by: 'member-x' } }))).toBeNull()
    expect(parseSentBy(makeMsg({ meta: { sent_by: { title: 'no key' } } }))).toBeNull()
  })

  it('drops an empty member_slug and a non-true child flag', () => {
    expect(parseSentBy(makeMsg({ meta: { sent_by: { ...SESSION, member_slug: '' } } }))?.member_slug).toBeUndefined()
    expect(parseSentBy(makeMsg({ meta: { sent_by: { ...SESSION, child: 'yes' } } }))?.child).toBeUndefined()
    expect(parseSentBy(makeMsg({ meta: { sent_by: WORKER } }))?.child).toBe(true)
  })
})

describe('kind / name / body', () => {
  it('classifies member, worker and other session by relationship, not by door', () => {
    expect(sentByKind(MEMBER)).toBe('member')
    expect(sentByKind(WORKER)).toBe('worker')
    expect(sentByKind(SESSION)).toBe('session')
    // The same child reporting through session_send is still a worker...
    expect(sentByKind({ ...WORKER, via: 'session_send' })).toBe('worker')
    // ...and a non-child using the origin door is not.
    expect(sentByKind({ ...SESSION, via: 'send_message_origin' })).toBe('session')
  })

  it('names a member by slug, others by title, and a title-less session generically', () => {
    expect(sentByName(MEMBER)).toBe('conductor')
    expect(sentByName(WORKER)).toBe('Triage #42')
    expect(sentByName(SESSION)).toBe('another session')
  })

  it('strips only the provenance prefix line from the display body', () => {
    expect(sentByBody(makeMsg())).toBe(BODY)
    const origin = '[sent by session chat-4-1789 via send_message]\n\nDone.'
    expect(sentByBody(makeMsg({ content: origin }))).toBe('Done.')
    expect(sentByBody(makeMsg({ content: 'no prefix here' }))).toBe('no prefix here')
    // A bracket line that is not the provenance tag is content, kept verbatim.
    expect(sentByBody(makeMsg({ content: '[Triage #42]\nbody' }))).toBe('[Triage #42]\nbody')
  })

  it('strips the prefix from any preview text, leaving other text alone', () => {
    expect(stripSentByPrefix(PREFIX + 'hello')).toBe('hello')
    expect(stripSentByPrefix('[sent by session chat-1 via send_message] one line')).toBe('one line')
    expect(stripSentByPrefix('plain')).toBe('plain')
    expect(stripSentByPrefix('')).toBe('')
  })

  it('peer-member rows open by default; worker and session rows start folded', () => {
    expect(sentByDefaultExpanded('member')).toBe(true)
    expect(sentByDefaultExpanded('worker')).toBe(false)
    expect(sentByDefaultExpanded('session')).toBe(false)
  })
})

describe('SentByCard', () => {
  it('renders a peer-member row expanded, with badge, name and the body minus the prefix', () => {
    render(<SentByCard message={makeMsg()} sentBy={MEMBER} />)
    const card = screen.getByTestId('sent-by-card')
    expect(card).toHaveAttribute('data-kind', 'member')
    expect(card).toHaveAttribute('data-expanded', 'true')
    expect(screen.getByTestId('sent-by-kind-badge')).toHaveTextContent('Member')
    expect(screen.getByTestId('sent-by-name')).toHaveTextContent('From conductor')
    expect(screen.getByTestId('sent-by-name')).toHaveAttribute('title', 'conductor · Agent: kirocrew-conductor')
    expect(screen.getByTestId('sent-by-body')).toHaveTextContent('Take the first triage item.')
    expect(screen.getByTestId('sent-by-body').textContent).not.toContain('[sent by session')
    expect(screen.queryByTestId('sent-by-preview')).toBeNull()
  })

  it('renders a worker report collapsed with a one-line preview, and expands on click', () => {
    const msg = makeMsg({
      content: '[sent by session chat-4-1789 via send_message]\n\nTriage #42 done: fixed the flake\nDetails follow.',
      meta: { sent_by: WORKER },
    })
    render(<SentByCard message={msg} sentBy={WORKER} />)
    const card = screen.getByTestId('sent-by-card')
    expect(card).toHaveAttribute('data-kind', 'worker')
    expect(card).toHaveAttribute('data-expanded', 'false')
    expect(screen.getByTestId('sent-by-kind-badge')).toHaveTextContent('Worker')
    expect(screen.getByTestId('sent-by-name')).toHaveTextContent('From Triage #42')
    expect(screen.getByTestId('sent-by-preview')).toHaveTextContent('Triage #42 done: fixed the flake')
    expect(screen.queryByTestId('sent-by-body')).toBeNull()
    const toggle = screen.getByTestId('sent-by-card-toggle')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('sent-by-body')).toHaveTextContent('Details follow.')
    expect(screen.queryByTestId('sent-by-preview')).toBeNull()
  })

  it('a non-member, non-child sender is a plain Session row, never named by a raw key', () => {
    render(<SentByCard message={makeMsg({ meta: { sent_by: SESSION } })} sentBy={SESSION} />)
    expect(screen.getByTestId('sent-by-card')).toHaveAttribute('data-kind', 'session')
    expect(screen.getByTestId('sent-by-kind-badge')).toHaveTextContent('Other session')
    expect(screen.getByTestId('sent-by-name')).toHaveTextContent('From another session')
    expect(screen.getByTestId('sent-by-name').textContent).not.toContain('chat-9-1')
    // An agent-less record names only the sender in the tooltip.
    const { unmount } = render(<SentByCard message={makeMsg({ meta: { sent_by: { ...SESSION, agent: '' } } })} sentBy={{ ...SESSION, agent: '' }} />)
    expect(screen.getAllByTestId('sent-by-name').at(-1)).toHaveAttribute('title', 'another session')
    unmount()
  })

  it('a long sender title is clamped in the header and kept whole in the tooltip', () => {
    const long = { ...SESSION, title: 'Unavailable session_send tool request for the peer confirmation flow' }
    render(<SentByCard message={makeMsg({ meta: { sent_by: long } })} sentBy={long} />)
    const name = screen.getByTestId('sent-by-name')
    expect(name.className).toContain('truncate')
    expect(name.className).not.toContain('shrink-0')
    expect(name).toHaveAttribute('title', `${long.title} · Agent: kirocrew`)
    // A session's title is quoted so an auto-title reads as a title, not a broken header.
    expect(name).toHaveTextContent(`From “${long.title}”`)
    // The folded preview yields first: it shrinks far harder than the name, and
    // the card is the inline-size container whose narrow rung (index.css) hides
    // the preview outright so the sender keeps the row. jsdom evaluates neither
    // flex nor @container, so the contract is pinned on the class hooks.
    const preview = screen.getByTestId('sent-by-preview')
    expect(preview.className).toContain('shrink-[8]')
    expect(preview.className).toContain('sent-by-preview')
    expect(screen.getByTestId('sent-by-card').className).toContain('sent-by-card')
    // The same narrow rung lifts the name's 45% cap (the name is then the only
    // flexible child), so the name span carries its CSS hook too.
    expect(name.className).toContain('sent-by-name')
  })

  it('members and workers keep the bare From form; a title-less session is not quoted', () => {
    render(<SentByCard message={makeMsg()} sentBy={MEMBER} />)
    expect(screen.getByTestId('sent-by-name')).toHaveTextContent('From conductor')
    expect(screen.getByTestId('sent-by-name').textContent).not.toContain('“')
    const { unmount } = render(<SentByCard message={makeMsg({ meta: { sent_by: WORKER } })} sentBy={WORKER} />)
    expect(screen.getAllByTestId('sent-by-name').at(-1)).toHaveTextContent('From Triage #42')
    unmount()
    render(<SentByCard message={makeMsg({ meta: { sent_by: SESSION } })} sentBy={SESSION} />)
    expect(screen.getAllByTestId('sent-by-name').at(-1)).toHaveTextContent('From another session')
  })
})
