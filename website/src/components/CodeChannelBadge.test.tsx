// CodeChannelBadge — the "Slack code channel" mark on a session that runs in a
// Slack code channel: a link to the channel in the sidebar row and the header,
// nothing at all for any other session.
import { screen, fireEvent, render } from '@testing-library/react'
import { CodeChannelBadge, CodeChannelMark } from './CodeChannelBadge'
import { i18nT } from '../i18n/t'
import type { CodeChannelInfo } from '../types'

const WEB = 'https://slack.com/app_redirect?channel=C0CODE1&team=T0TEAM1'
const k = (key: string, vars?: Record<string, unknown>) => i18nT(`components.codeChannelBadge.${key}`, vars)

function info(over: Partial<CodeChannelInfo> = {}): CodeChannelInfo {
  return { channel_name: 'fix-login', status: 'active', web_url: WEB, ...over }
}

describe('CodeChannelBadge', () => {
  it('row chip links to the channel in a new tab and names itself for a screen reader', () => {
    render(<CodeChannelBadge info={info()} variant="row" />)
    const a = screen.getByRole('link')
    expect(a).toHaveAttribute('href', WEB)
    expect(a).toHaveAttribute('target', '_blank')
    expect(a).toHaveAttribute('rel', 'noopener noreferrer')
    expect(a.textContent).toContain(k('label'))
    expect(a.textContent).toContain('#fix-login')
  })

  it('tooltip explains what a code channel is and where the click goes', () => {
    render(<CodeChannelBadge info={info()} variant="row" />)
    const title = screen.getByRole('link').getAttribute('title') ?? ''
    expect(title.split('\n')).toEqual([k('about'), k('open_named', { channel: '#fix-login' })])
  })

  it('header chip shows the label visibly', () => {
    render(<CodeChannelBadge info={info()} variant="header" />)
    expect(screen.getByText(k('label'))).not.toHaveClass('sr-only')
  })

  it.each([
    ['archived', 'archived', 'archived_hint'],
    ['wrapped_up', 'wrapped_up', 'wrapped_up_hint'],
  ] as const)('a %s channel says so in text and explains it in the tooltip', (status, key, hint) => {
    render(<CodeChannelBadge info={info({ status })} variant="row" />)
    expect(screen.getByTestId('code-channel-status').textContent).toContain(k(key))
    expect(screen.getByRole('link').getAttribute('title')).toContain(k(hint))
  })

  it('archived and wrapped up draw different glyphs, so they differ without the word', () => {
    const glyph = (status: CodeChannelInfo['status']) => {
      const { unmount } = render(<CodeChannelBadge info={info({ status })} variant="row" />)
      // On the row the glyph sits beside the Slack mark, apart from the word.
      const svg = screen.getByTestId('code-channel-status-glyph').getAttribute('class') ?? ''
      unmount()
      return svg
    }
    expect(glyph('archived')).not.toEqual(glyph('wrapped_up'))
  })

  it('an active channel shows no state', () => {
    render(<CodeChannelBadge info={info()} variant="row" />)
    expect(screen.queryByTestId('code-channel-status')).toBeNull()
  })

  it('a channel with no known name falls back to the generic label', () => {
    render(<CodeChannelBadge info={info({ channel_name: '' })} variant="row" />)
    const a = screen.getByRole('link')
    expect(a.textContent).toContain(k('label_short'))
    expect(a.getAttribute('title')).toContain(k('open_unnamed'))
  })

  it('an unnamed finished channel shows its state word and still names itself to a screen reader', () => {
    render(<CodeChannelBadge info={info({ channel_name: '', status: 'wrapped_up' })} variant="row" />)
    const text = screen.getByRole('link').textContent ?? ''
    expect(text).toContain(k('label'))
    expect(screen.getByTestId('code-channel-status').textContent).toBe(k('wrapped_up'))
    // The word that says what changed gets the width, not a generic prefix.
    expect(text).not.toContain(k('label_short'))
  })

  it('the phone marker is not the plain Slack mark', () => {
    render(<CodeChannelMark />)
    const mark = screen.getByTestId('mobile-code-channel-glyph')
    expect(mark).toHaveAttribute('aria-label', k('label'))
    expect(mark.querySelector('svg.lucide-code')).not.toBeNull()
  })

  it('no link from the backend renders a plain mark, never a dead link', () => {
    render(<CodeChannelBadge info={info({ web_url: '' })} variant="row" />)
    expect(screen.queryByRole('link')).toBeNull()
    expect(screen.getByTestId('code-channel-badge-row').textContent).toContain(k('label'))
  })

  it('neither a click nor a middle click reaches the row underneath', () => {
    const onRow = vi.fn()
    const onRowAux = vi.fn()
    // Stands in for the sidebar row: click switches session, middle click opens
    // the session in a background tab.
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

  it('turns its colour transition off under reduced motion', () => {
    render(<CodeChannelBadge info={info()} variant="row" />)
    expect(screen.getByRole('link').className).toContain('motion-reduce:transition-none')
  })
})
