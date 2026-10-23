// CodeChannelBadge — the marks a Slack-linked session wears: the Slack mark
// alone on a sidebar row (a link named for its channel), the full link in the
// session header, and the header link to a mirrored Slack thread. Nothing at
// all for any other session.
import { screen, fireEvent, render } from '@testing-library/react'
import { CodeChannelBadge, CodeChannelMark, SlackThreadLink, slackThreadUrl } from './CodeChannelBadge'
import { i18nT } from '../i18n/t'
import type { CodeChannelInfo } from '../types'

const WEB = 'https://slack.com/app_redirect?channel=C0CODE1&team=T0TEAM1'
const k = (key: string, vars?: Record<string, unknown>) => i18nT(`components.codeChannelBadge.${key}`, vars)

function info(over: Partial<CodeChannelInfo> = {}): CodeChannelInfo {
  return { channel_name: 'fix-login', status: 'active', web_url: WEB, ...over }
}

describe('CodeChannelBadge row: the Slack mark alone', () => {
  it('is a link to the channel in a new tab, named exactly for its channel', () => {
    render(<CodeChannelBadge info={info()} variant="row" />)
    const a = screen.getByRole('link')
    expect(a).toHaveAttribute('href', WEB)
    expect(a).toHaveAttribute('target', '_blank')
    expect(a).toHaveAttribute('rel', 'noopener noreferrer')
    expect(a).toHaveAttribute('aria-label', 'Open #fix-login in Slack')
    expect(a).toHaveAccessibleName('Open #fix-login in Slack')
    expect(a).toHaveAttribute('title', 'Open #fix-login in Slack')
  })

  it('renders no text at all: no channel name, no link glyph', () => {
    render(<CodeChannelBadge info={info()} variant="row" />)
    const a = screen.getByRole('link')
    expect(a.textContent).toBe('')
    expect(a.querySelector('svg.lucide-external-link')).toBeNull()
    expect(screen.queryByTestId('code-channel-name')).toBeNull()
    // Only the Slack mark, in its fixed box.
    const mark = screen.getByTestId('code-channel-slack-mark')
    expect(mark.parentElement).toBe(a)
    expect(a.children).toHaveLength(1)
    expect(mark.style.width).toBe('10px')
    expect(mark.style.height).toBe('10px')
    expect(mark).toHaveClass('shrink-0')
    expect(a).toHaveClass('shrink-0')
  })

  it('no pill: no border, background or padding around the mark', () => {
    render(<CodeChannelBadge info={info()} variant="row" />)
    const cls = (screen.getByRole('link').getAttribute('class') ?? '').split(/\s+/)
    expect(cls.filter(c => /^(border|bg-|px-|py-|p-)/.test(c))).toEqual([])
  })

  it('a channel with no known name gets the generic name', () => {
    render(<CodeChannelBadge info={info({ channel_name: '' })} variant="row" />)
    expect(screen.getByRole('link')).toHaveAttribute('aria-label', k('open_unnamed'))
  })

  it.each([
    ['archived', 'archived_hint'],
    ['wrapped_up', 'wrapped_up_hint'],
  ] as const)('a %s channel keeps its exact name and explains its state in the tooltip', (status, hint) => {
    render(<CodeChannelBadge info={info({ status })} variant="row" />)
    const a = screen.getByRole('link')
    expect(a).toHaveAttribute('aria-label', 'Open #fix-login in Slack')
    expect(a.getAttribute('title')?.split('\n')).toEqual(['Open #fix-login in Slack', k(hint)])
    expect(a).toHaveAttribute('data-code-channel-status', status)
  })

  it('an archived channel greys its mark', () => {
    render(<CodeChannelBadge info={info({ status: 'archived' })} variant="row" />)
    expect(screen.getByTestId('code-channel-slack-mark')).toHaveClass('opacity-50', 'grayscale')
  })

  it('no link from the backend renders a plain mark, never a dead link', () => {
    render(<CodeChannelBadge info={info({ web_url: '' })} variant="row" />)
    expect(screen.queryByRole('link')).toBeNull()
    expect(screen.getByRole('img')).toHaveAccessibleName(k('label'))
    expect(screen.getByTestId('code-channel-badge-row').textContent).toBe('')
  })

  it.each([
    'javascript:alert(1)',
    'https://evil.example/app_redirect?channel=C0CODE1&team=T0TEAM1',
    'https://slack.com/app_redirect?channel=C0CODE1&team=T0TEAM1&token=x',
  ])('a web_url that is not Slack\'s app_redirect renders a plain mark (%s)', (web_url) => {
    render(<CodeChannelBadge info={info({ web_url })} variant="row" />)
    expect(screen.queryByRole('link')).toBeNull()
    const { container } = render(<CodeChannelBadge info={info({ web_url })} variant="header" />)
    expect(container.querySelector('a')).toBeNull()
  })

  it('neither a click nor a middle click reaches the row underneath', () => {
    const onRow = vi.fn()
    const onRowAux = vi.fn()
    render(
      <div role="button" tabIndex={0} onClick={onRow} onAuxClick={onRowAux} onKeyDown={onRow}>
        <CodeChannelBadge info={info()} variant="row" />
      </div>,
    )
    fireEvent.click(screen.getByRole('link'))
    fireEvent(screen.getByRole('link'), new MouseEvent('auxclick', { bubbles: true, button: 1 }))
    expect(onRow).not.toHaveBeenCalled()
    expect(onRowAux).not.toHaveBeenCalled()
  })

  it('turns its transition off under reduced motion', () => {
    render(<CodeChannelBadge info={info()} variant="row" />)
    expect(screen.getByRole('link').className).toContain('motion-reduce:transition-none')
  })
})

describe('CodeChannelBadge header: the full link', () => {
  it('shows the Slack mark, "#name" and the link glyph, and opens the channel', () => {
    render(<CodeChannelBadge info={info()} variant="header" />)
    const a = screen.getByRole('link')
    expect(a).toHaveAttribute('href', WEB)
    expect(a).toHaveAttribute('target', '_blank')
    expect(a).toHaveAttribute('rel', 'noopener noreferrer')
    expect(screen.getByTestId('code-channel-slack-mark')).toBeInTheDocument()
    expect(screen.getByTestId('code-channel-name').textContent).toBe('#fix-login')
    expect(screen.getByTestId('code-channel-link-glyph')).toBeInTheDocument()
    // What the chip is comes first for a screen reader, then the channel.
    expect(a).toHaveAccessibleName(`${k('label')} #fix-login ${k('opens_in_slack')}`)
  })

  it('a long channel name truncates, with the whole name in the tooltip', () => {
    const long = 'a'.repeat(80)
    render(<CodeChannelBadge info={info({ channel_name: long })} variant="header" />)
    expect(screen.getByTestId('code-channel-name')).toHaveClass('truncate', 'min-w-0')
    expect(screen.getByRole('link').getAttribute('title')).toContain(`#${long}`)
  })

  it.each([
    ['archived', 'archived'],
    ['wrapped_up', 'wrapped_up'],
  ] as const)('a %s channel says so in words', (status, key) => {
    render(<CodeChannelBadge info={info({ status })} variant="header" />)
    expect(screen.getByTestId('code-channel-status').textContent).toBe(k(key))
  })

  it('an unnamed channel shows the generic label', () => {
    render(<CodeChannelBadge info={info({ channel_name: '' })} variant="header" />)
    expect(screen.getByRole('link').textContent).toContain(k('label_short'))
  })
})

describe('SlackThreadLink: the header link to a mirrored Slack thread', () => {
  const THREAD = { slack_linked: true, slack_channel: 'C0THREAD1', slack_thread_ts: '1712793600.123456' }

  it('links to the thread permalink and carries no credential', () => {
    render(<SlackThreadLink slot={THREAD} />)
    const a = screen.getByRole('link')
    // The exact URL: Slack's own permalink, nothing appended.
    expect(a).toHaveAttribute('href', 'https://slack.com/archives/C0THREAD1/p1712793600123456')
    expect(a).toHaveAttribute('target', '_blank')
    expect(a).toHaveAttribute('rel', 'noopener noreferrer')
    expect(a).toHaveAttribute('title', k('thread_open'))
    expect(a.textContent).toContain(k('thread_label'))
  })

  it('renders nothing for a session with no Slack thread, or a code channel', () => {
    const { container, rerender } = render(<SlackThreadLink slot={{ slack_linked: false }} />)
    expect(container).toBeEmptyDOMElement()
    rerender(<SlackThreadLink slot={{ ...THREAD, code_channel: info() }} />)
    expect(container).toBeEmptyDOMElement()
    rerender(<SlackThreadLink slot={undefined} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing while the gateway cannot yet tell it is not a code channel', () => {
    const { container } = render(<SlackThreadLink slot={{ ...THREAD, code_channel_pending: true }} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('a paused mirror says so in words and in the tooltip', () => {
    const link = { channel: 'slack', label: 'Slack', target: '…0001', direction: 'out' as const, live: true }
    const { rerender } = render(<SlackThreadLink slot={{ ...THREAD, links: [{ ...link, paused: false }] }} />)
    expect(screen.queryByTestId('slack-thread-paused')).toBeNull()
    rerender(<SlackThreadLink slot={{ ...THREAD, links: [{ ...link, paused: true }] }} />)
    expect(screen.getByTestId('slack-thread-paused').textContent).toBe(k('thread_paused'))
    expect(screen.getByRole('link').getAttribute('title')?.split('\n')).toEqual([k('thread_open'), k('thread_paused_hint')])
    // A paused ORIGIN row is not the mirror this chip names.
    rerender(<SlackThreadLink slot={{ ...THREAD, links: [{ ...link, direction: 'origin', paused: true }] }} />)
    expect(screen.queryByTestId('slack-thread-paused')).toBeNull()
  })

  it.each([
    ['javascript:alert(1)', '1712793600.123456'],
    ['C0THREAD1/../x', '1712793600.123456'],
    ['C0THREAD1', 'p1712793600'],
    ['C0THREAD1', '1712793600.1'],
    ['', ''],
  ])('refuses malformed ids (%s, %s)', (channel, ts) => {
    expect(slackThreadUrl(channel, ts)).toBe('')
    const { container } = render(<SlackThreadLink slot={{ slack_linked: true, slack_channel: channel, slack_thread_ts: ts }} />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('SlackThreadLink: a session started in Slack links its own thread', () => {
  // Kyle's build-9 session: a thread in #testing, key slack:1792588893.063579.
  const BORN = { slack_linked: false, slack_channel: '', slack_thread_ts: '', slack_origin_channel: 'C0C9R9WUXM5', slack_origin_thread_ts: '1792588893.063579' }
  const origin = { channel: 'slack', label: 'Slack', target: '…UXM5', direction: 'origin' as const, live: true }

  it('links the thread it lives in', () => {
    render(<SlackThreadLink slot={BORN} />)
    const a = screen.getByRole('link')
    expect(a).toHaveAttribute('href', 'https://slack.com/archives/C0C9R9WUXM5/p1792588893063579')
    expect(a).toHaveAttribute('rel', 'noopener noreferrer')
    expect(a.textContent).toContain(k('thread_label'))
  })

  it('a mirrored session still links its mirror, not stray origin ids', () => {
    render(<SlackThreadLink slot={{ ...BORN, slack_linked: true, slack_channel: 'C0THREAD1', slack_thread_ts: '1712793600.123456' }} />)
    expect(screen.getByRole('link')).toHaveAttribute('href', 'https://slack.com/archives/C0THREAD1/p1712793600123456')
  })

  it('renders nothing on a code channel or while that is unknown', () => {
    const { container, rerender } = render(<SlackThreadLink slot={{ ...BORN, code_channel: info() }} />)
    expect(container).toBeEmptyDOMElement()
    rerender(<SlackThreadLink slot={{ ...BORN, code_channel_pending: true }} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('reads paused from the origin row, not an outbound one', () => {
    const { rerender } = render(<SlackThreadLink slot={{ ...BORN, links: [{ ...origin, paused: true }] }} />)
    expect(screen.getByTestId('slack-thread-paused').textContent).toBe(k('thread_paused'))
    rerender(<SlackThreadLink slot={{ ...BORN, links: [{ ...origin, direction: 'out' as const, paused: true }] }} />)
    expect(screen.queryByTestId('slack-thread-paused')).toBeNull()
  })

  it.each([
    ['javascript:alert(1)', '1792588893.063579'],
    ['C0C9R9WUXM5', '1792588893'],
    ['', '1792588893.063579'],
    ['C0C9R9WUXM5', ''],
  ])('renders nothing for malformed origin ids (%s, %s)', (channel, ts) => {
    const { container } = render(<SlackThreadLink slot={{ ...BORN, slack_origin_channel: channel, slack_origin_thread_ts: ts }} />)
    expect(container).toBeEmptyDOMElement()
  })
})

it('the phone marker is not the plain Slack mark', () => {
  render(<CodeChannelMark />)
  const mark = screen.getByTestId('mobile-code-channel-glyph')
  expect(mark).toHaveAttribute('aria-label', k('label'))
  expect(mark.querySelector('svg.lucide-code')).not.toBeNull()
})
