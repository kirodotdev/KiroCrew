import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, fireEvent, waitFor, within } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { RedactionCoach } from '../components/RedactionCards'
import { api } from '../api/client'

const copied: string[] = []
let copyResult = true
vi.mock('../utils/clipboard', async importOriginal => {
  const actual = await importOriginal<typeof import('../utils/clipboard')>()
  return {
    ...actual,
    copyToClipboard: (text: string) => {
      copied.push(text)
      return Promise.resolve(copyResult)
    },
  }
})

/**
 * The redaction UI (rfc-redaction-explain-and-reveal, prototype variants C+A):
 * a neutral lock tag per removed credential and a Blocked link chip per
 * removed URL, each opening one details card after its block.
 */

const PH = (d: string) => `[REDACTED: suspicious URL to ${d}]`
const CRED = '[REDACTED: credential]'

const link = (over: Record<string, unknown> = {}) => {
  const domain = (over.domain as string) ?? 'reviews.corp.example'
  return {
    domain,
    rule: 'exfil_query_length',
    path: '/reviews',
    query_chars: 290,
    url: `https://${domain}/reviews?filter=abc`,
    url_withheld: null,
    ...over,
  }
}

const cred = (over: Record<string, unknown> = {}) => ({
  ordinal: 0,
  rule: 'aws_secret_access_key',
  label: 'aws_secret_access_key = ',
  source: { type: 'file', path: '~/.aws/credentials', section: 'default' },
  view_command: 'aws configure get aws_secret_access_key --profile default',
  profile_command: null,
  ...over,
})

afterEach(() => {
  copied.length = 0
  copyResult = true
  vi.restoreAllMocks()
})

describe('Blocked link chip', () => {
  it('matches the prototype: label, host and path, query length, Inspect', () => {
    const { getByTestId } = render(<MarkdownRenderer content={`The page: ${PH('reviews.corp.example')}`} blockedLinks={[link()]} slotKey="s1" />)
    const chip = getByTestId('blocked-link-chip')
    expect(chip.textContent).toContain('Blocked link')
    expect(getByTestId('blocked-link-target').textContent).toBe('reviews.corp.example/reviews')
    expect(getByTestId('blocked-link-query').textContent).toBe('⋯ query 290 chars')
    expect(getByTestId('blocked-link-inspect').textContent).toBe('Inspect')
    expect(chip.closest('a')).toBeNull()
  })

  it('Inspect opens the card after the paragraph with the prototype sections', () => {
    const { getByTestId, container } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
    fireEvent.click(getByTestId('blocked-link-inspect'))
    const card = getByTestId('blocked-link-card')
    expect(card.textContent).toContain('This link is blocked. It cannot open or preview.')
    expect(card.textContent).toContain('Destination: reviews.corp.example')
    expect(card.textContent).toContain('Reason: the query has 290 characters.')
    expect(getByTestId('blocked-link-review').textContent).toContain('Review full URL')
    expect(card.textContent).not.toContain('rule: exfil_query_length')
    fireEvent.click(within(getByTestId('blocked-link-review')).getByRole('button', { name: /Review full URL/ }))
    expect(card.textContent).toContain('rule: exfil_query_length · threshold 200')
    for (const id of ['blocked-link-copy', 'blocked-link-open-once', 'blocked-link-allow']) expect(getByTestId(id)).toBeTruthy()
    expect(card.textContent).toContain('Not suspicious? Report a false positive')
    expect(card.textContent).toContain('About blocked links')
    // The card follows the block holding the chip, not inside it.
    expect(container.querySelector('p')?.contains(card)).toBe(false)
  })

  it('Open once asks first, with Cancel focused, then opens without referrer', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    const { getByTestId, queryByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
    fireEvent.click(getByTestId('blocked-link-inspect'))
    fireEvent.click(getByTestId('blocked-link-open-once'))
    const confirm = getByTestId('blocked-link-open-confirm')
    expect(confirm.textContent).toContain('Open reviews.corp.example one time?')
    expect(confirm.textContent).toContain('The shown 290-character query will be sent to this host.')
    expect(document.activeElement?.textContent).toBe('Cancel')
    expect(open).not.toHaveBeenCalled()
    fireEvent.click(getByTestId('blocked-link-open-confirmed'))
    expect(open).toHaveBeenCalledWith('https://reviews.corp.example/reviews?filter=abc', '_blank', 'noopener,noreferrer')
    await waitFor(() => expect(queryByTestId('blocked-link-open-confirm')).toBeNull())
    expect(getByTestId('blocked-link-feedback').textContent).toContain('this link stays blocked here')
  })

  it('Allow for this host confirms, saves for the slot, and offers Undo', async () => {
    const allow = vi.spyOn(api, 'redactionAllowHost').mockResolvedValue({ ok: true, workspace: 'default' })
    const revoke = vi.spyOn(api, 'redactionRevokeHost').mockResolvedValue({ ok: true, removed: true })
    const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
    fireEvent.click(getByTestId('blocked-link-inspect'))
    fireEvent.click(getByTestId('blocked-link-allow'))
    expect(getByTestId('blocked-link-allow-confirm').textContent).toContain('this workspace only')
    expect(allow).not.toHaveBeenCalled()
    fireEvent.click(getByTestId('blocked-link-allow-confirmed'))
    await waitFor(() => expect(getByTestId('blocked-link-feedback').textContent).toContain('Allowed reviews.corp.example for this workspace'))
    expect(allow).toHaveBeenCalledWith('s1', 'reviews.corp.example')
    fireEvent.click(getByTestId('blocked-link-undo'))
    await waitFor(() => expect(revoke).toHaveBeenCalledWith('default', 'reviews.corp.example'))
  })

  it('asks the page to reload the slot once a host is allowed, so the link shows again', async () => {
    vi.spyOn(api, 'redactionAllowHost').mockResolvedValue({ ok: true, workspace: 'default' })
    const seen: unknown[] = []
    const onChange = (e: Event) => seen.push((e as CustomEvent).detail)
    window.addEventListener('mc:redaction-hosts-changed', onChange)
    try {
      const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
      fireEvent.click(getByTestId('blocked-link-inspect'))
      fireEvent.click(getByTestId('blocked-link-allow'))
      fireEvent.click(getByTestId('blocked-link-allow-confirmed'))
      await waitFor(() => expect(seen).toEqual([{ slot: 's1' }]))
    } finally {
      window.removeEventListener('mc:redaction-hosts-changed', onChange)
    }
  })

  it('keeps the card open with Undo when the reloaded reply shows the host as a plain link', async () => {
    vi.spyOn(api, 'redactionAllowHost').mockResolvedValue({ ok: true, workspace: 'default' })
    const revoke = vi.spyOn(api, 'redactionRevokeHost').mockResolvedValue({ ok: true } as never)
    const blocked = `See ${PH('reviews.corp.example')} now.`
    const restored = 'See [the page](https://reviews.corp.example/reviews?filter=abc) now.'
    const { getByTestId, queryByTestId, rerender, container } = render(
      <MarkdownRenderer content={blocked} blockedLinks={[link()]} slotKey="s1" messageTs="2026-09-25T02:50:00Z" />,
    )
    fireEvent.click(getByTestId('blocked-link-inspect'))
    fireEvent.click(getByTestId('blocked-link-allow'))
    fireEvent.click(getByTestId('blocked-link-allow-confirmed'))
    await waitFor(() => expect(getByTestId('blocked-link-undo')).toBeTruthy())
    // The server's reply after the reload: the link restored, its record gone.
    rerender(<MarkdownRenderer content={restored} blockedLinks={[]} slotKey="s1" messageTs="2026-09-25T02:50:00Z" />)
    expect(queryByTestId('blocked-link-chip')).toBeNull()
    expect(container.querySelector('a[href="https://reviews.corp.example/reviews?filter=abc"]')).toBeTruthy()
    expect(getByTestId('blocked-link-card')).toBeTruthy()
    fireEvent.click(getByTestId('blocked-link-undo'))
    await waitFor(() => expect(revoke).toHaveBeenCalledWith('default', 'reviews.corp.example'))
  })

  it('reopens the held card when the reloaded reply remounts', async () => {
    vi.spyOn(api, 'redactionAllowHost').mockResolvedValue({ ok: true, workspace: 'default' })
    const blocked = `See ${PH('reviews.corp.example')} now.`
    const restored = 'See [the page](https://reviews.corp.example/reviews?filter=abc) now.'
    const ts = '2026-09-25T02:52:00Z'
    const first = render(<MarkdownRenderer content={blocked} blockedLinks={[link()]} slotKey="s1" messageTs={ts} />)
    fireEvent.click(first.getByTestId('blocked-link-inspect'))
    fireEvent.click(first.getByTestId('blocked-link-allow'))
    fireEvent.click(first.getByTestId('blocked-link-allow-confirmed'))
    await waitFor(() => expect(first.getByTestId('blocked-link-undo')).toBeTruthy())
    // The reload rebuilds the row: a fresh renderer with the server's reply.
    first.unmount()
    const again = render(<MarkdownRenderer content={restored} blockedLinks={[]} slotKey="s1" messageTs={ts} />)
    expect(again.getByTestId('blocked-link-card')).toBeTruthy()
    expect(again.getByTestId('blocked-link-undo')).toBeTruthy()
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(again.queryByTestId('blocked-link-card')).toBeNull())
  })

  it('lets the plain link stand alone once the card of an allowed host closes', async () => {
    vi.spyOn(api, 'redactionAllowHost').mockResolvedValue({ ok: true, workspace: 'default' })
    const blocked = `See ${PH('reviews.corp.example')} now.`
    const restored = 'See [the page](https://reviews.corp.example/reviews?filter=abc) now.'
    const { getByTestId, queryByTestId, rerender } = render(
      <MarkdownRenderer content={blocked} blockedLinks={[link()]} slotKey="s1" messageTs="2026-09-25T02:51:00Z" />,
    )
    fireEvent.click(getByTestId('blocked-link-inspect'))
    fireEvent.click(getByTestId('blocked-link-allow'))
    fireEvent.click(getByTestId('blocked-link-allow-confirmed'))
    await waitFor(() => expect(getByTestId('blocked-link-undo')).toBeTruthy())
    rerender(<MarkdownRenderer content={restored} blockedLinks={[]} slotKey="s1" messageTs="2026-09-25T02:51:00Z" />)
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(queryByTestId('blocked-link-card')).toBeNull())
  })

  it('offers no Allow for a rule the host exemption does not relax', () => {
    const { getByTestId, queryByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link({ rule: 'exfil_percent_encoding' })]} slotKey="s1" />)
    fireEvent.click(getByTestId('blocked-link-inspect'))
    expect(queryByTestId('blocked-link-allow')).toBeNull()
  })

  it('a withheld address has no Open or Copy, only why', () => {
    const { getByTestId, queryByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link({ url: null, url_withheld: 'credential' })]} slotKey="s1" />)
    fireEvent.click(getByTestId('blocked-link-inspect'))
    expect(queryByTestId('blocked-link-open-once')).toBeNull()
    expect(queryByTestId('blocked-link-copy')).toBeNull()
    expect(getByTestId('blocked-link-withheld').textContent).toContain('looks like a secret')
  })

  it('Copy URL copies the kept address exactly', async () => {
    const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
    fireEvent.click(getByTestId('blocked-link-inspect'))
    fireEvent.click(getByTestId('blocked-link-copy'))
    await waitFor(() => expect(copied).toEqual(['https://reviews.corp.example/reviews?filter=abc']))
  })

  it('a placeholder with no record stays plain text', () => {
    const { container, queryByTestId } = render(<MarkdownRenderer content={PH('other.example')} blockedLinks={[link()]} />)
    expect(queryByTestId('blocked-link-chip')).toBeNull()
    expect(container.textContent).toContain(PH('other.example'))
  })

  it('a record whose url names another host is dropped', () => {
    const { queryByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link({ url: 'https://evil.example/x' })]} />)
    expect(queryByTestId('blocked-link-chip')).toBeNull()
  })
})

describe('credential lock tag', () => {
  const ini = ['```ini', '[default]', CRED, '```'].join('\n')

  it('renders inside a code block as the label plus a blue lock tag, first one asking why', () => {
    const { getAllByTestId, getByTestId } = render(<MarkdownRenderer content={ini} redactions={[cred()]} slotKey="s1" />)
    const block = getByTestId('redacted-code-block')
    expect(block.textContent).toContain('aws_secret_access_key = ')
    const [tag] = getAllByTestId('credential-tag')
    expect(tag.textContent).toBe('credential· why?')
    expect(tag.className).toContain('border-info')
    expect(tag.className).not.toContain('danger')
  })

  it('tags a credential inside a diagram or diff fence too', () => {
    for (const lang of ['mermaid', 'excalidraw', 'diff', 'markdown']) {
      const block = ['```' + lang, `aws_secret_access_key = ${CRED}`, '```'].join('\n')
      const { getAllByTestId, unmount } = render(<MarkdownRenderer content={block} redactions={[cred()]} slotKey="s1" />)
      expect(getAllByTestId('credential-tag')).toHaveLength(1)
      unmount()
    }
  })

  it('opens the credential card with the prototype sections', () => {
    const { getByTestId } = render(<MarkdownRenderer content={ini} redactions={[cred()]} slotKey="s1" />)
    fireEvent.click(getByTestId('credential-tag'))
    const card = getByTestId('credential-card')
    expect(card.textContent).toContain('A credential was removed here')
    expect(card.textContent).toContain('It was removed before saving, so no copy of this conversation contains it')
    expect(card.textContent).toContain('Source: ~/.aws/credentials section [default].')
    expect(card.textContent).toContain('View the value in the built-in Terminal')
    expect(card.textContent).toContain('aws configure get aws_secret_access_key --profile default')
    fireEvent.click(within(getByTestId('credential-technical')).getByRole('button'))
    expect(getByTestId('credential-technical').textContent).toContain('rule: aws_secret_access_key')
    fireEvent.click(within(getByTestId('credential-more')).getByRole('button'))
    expect(getByTestId('credential-more').textContent).toContain('Copy path')
    expect(card.textContent).toContain('Not a secret? Report a false positive')
    const report = getByTestId('redaction-report') as HTMLAnchorElement
    const issue = new URL(report.href)
    expect(`${issue.origin}${issue.pathname}`).toBe('https://github.com/kirodotdev/KiroCrew/issues/new')
    expect(issue.searchParams.get('title')).toBe('Redaction false positive: aws_secret_access_key')
    expect(issue.searchParams.get('body')).toContain('**Card:** credential redaction')
    // Only the rule name leaves: no path, no command, no session.
    expect(report.href).not.toContain('credentials')
    expect(report.href).not.toContain('s1')
    expect(report.target).toBe('_blank')
    expect(card.textContent).toContain('About redaction')
  })

  it('Open in Terminal types the command without running it', async () => {
    const seen: string[] = []
    const onPrefill = (e: Event) => {
      const d = (e as CustomEvent).detail
      seen.push(d.command)
      window.dispatchEvent(new CustomEvent('mc:prefill-terminal-result', { detail: { reqId: d.reqId, ok: true } }))
    }
    window.addEventListener('mc:prefill-terminal', onPrefill)
    try {
      const { getByTestId } = render(<MarkdownRenderer content={ini} redactions={[cred()]} slotKey="s1" />)
      fireEvent.click(getByTestId('credential-tag'))
      fireEvent.click(getByTestId('redaction-open-terminal'))
      await waitFor(() => expect(getByTestId('redaction-terminal-added').textContent).toContain('nothing was run'))
      expect(seen).toEqual(['aws configure get aws_secret_access_key --profile default'])
    } finally {
      window.removeEventListener('mc:prefill-terminal', onPrefill)
    }
  })

  it('warns in red to check a command the agent ran before it is prefilled', () => {
    const ran = cred({ source: { type: 'command', command: 'cat .env' }, view_command: 'cat .env' })
    const { getByTestId } = render(<MarkdownRenderer content={ini} redactions={[ran]} slotKey="s1" />)
    fireEvent.click(getByTestId('credential-tag'))
    const warning = getByTestId('redaction-command-warning')
    expect(warning.textContent).toContain('Check it carefully before you press Enter')
    expect(warning.className).toContain('text-danger')
  })

  it('drops a record whose command carries a bidi or invisible character, so nothing is prefilled', () => {
    for (const bad of ['cat ~/.aws/credentials \u202e#', 'cat\u200b .env']) {
      const ran = cred({ source: { type: 'command', command: bad }, view_command: bad })
      const { queryByTestId, unmount } = render(<MarkdownRenderer content={ini} redactions={[ran]} slotKey="s1" />)
      expect(queryByTestId('credential-tag')).toBeNull()
      expect(queryByTestId('redaction-open-terminal')).toBeNull()
      unmount()
    }
  })

  it('shows no agent warning for a fixed command template', () => {
    const { getByTestId, queryByTestId } = render(<MarkdownRenderer content={ini} redactions={[cred()]} slotKey="s1" />)
    fireEvent.click(getByTestId('credential-tag'))
    expect(getByTestId('redaction-open-terminal')).toBeTruthy()
    expect(queryByTestId('redaction-command-warning')).toBeNull()
  })

  it('a session token recommends the profile', () => {
    const token = cred({ rule: 'aws_session_token', label: 'aws_session_token = ', source: { type: 'file', path: '~/.aws/credentials', section: 'dev' }, view_command: 'aws configure get aws_session_token --profile dev', profile_command: 'AWS_PROFILE=dev aws sts get-caller-identity' })
    const { getByTestId } = render(<MarkdownRenderer content={ini} redactions={[token]} slotKey="s1" />)
    expect(getByTestId('credential-tag').textContent).toContain('session token')
    fireEvent.click(getByTestId('credential-tag'))
    const card = getByTestId('credential-card')
    expect(card.textContent).toContain('A session token was removed here')
    expect(card.textContent).toContain('AWS_PROFILE=dev aws sts get-caller-identity')
  })

  it('copying the block turns each tag into its label plus <REDACTED>', async () => {
    const { getByTestId } = render(<MarkdownRenderer content={ini} redactions={[cred()]} slotKey="s1" />)
    fireEvent.click(getByTestId('redacted-code-block').querySelector('button[aria-label]') as HTMLElement)
    await waitFor(() => expect(copied).toEqual(['[default]\naws_secret_access_key = <REDACTED>']))
  })

  it('says so when copying the block fails', async () => {
    copyResult = false
    const { getByTestId } = render(<MarkdownRenderer content={ini} redactions={[cred()]} slotKey="s1" />)
    fireEvent.click(getByTestId('redacted-code-block').querySelector('button[aria-label]') as HTMLElement)
    await waitFor(() => expect(getByTestId('redacted-code-copy-error').textContent).toContain('Copy failed'))
  })

  it('says so when copying the source path fails', async () => {
    copyResult = false
    const { getByTestId, getAllByTestId } = render(<MarkdownRenderer content={ini} redactions={[cred()]} slotKey="s1" />)
    fireEvent.click(getAllByTestId('credential-tag')[0])
    fireEvent.click(within(getByTestId('credential-more')).getByRole('button'))
    fireEvent.click(getByTestId('credential-copy-path'))
    await waitFor(() => expect(getByTestId('credential-copy-path-error').textContent).toContain('Copy failed'))
  })

  it('pairs tags across blocks by ordinal', () => {
    const md = [`first ${CRED}`, '', '```ini', CRED, '```'].join('\n')
    const { getAllByTestId } = render(<MarkdownRenderer content={md} redactions={[cred({ ordinal: 0, label: '' }), cred({ ordinal: 1 })]} slotKey="s1" />)
    expect(getAllByTestId('credential-tag')).toHaveLength(2)
  })

  it('an unrecorded tag stays the plain placeholder', () => {
    const { queryByTestId, container } = render(<MarkdownRenderer content={`x ${CRED}`} redactions={[cred({ ordinal: 5 })]} />)
    expect(queryByTestId('credential-tag')).toBeNull()
    expect(container.textContent).toContain(CRED)
  })

  it('a record with a multi-line command is dropped', () => {
    const { queryByTestId } = render(<MarkdownRenderer content={`x ${CRED}`} redactions={[cred({ label: '', view_command: 'a\nb' })]} />)
    expect(queryByTestId('credential-tag')).toBeNull()
  })
})

describe('first-run coach', () => {
  beforeEach(() => window.localStorage.clear())

  it('teaches the three facts; Got it closes it for this session only', async () => {
    const { getByTestId, queryByTestId, unmount } = render(<RedactionCoach count={3} slotKey="s1" />)
    const coach = getByTestId('redaction-coach')
    expect(coach.textContent).toContain('3 values were removed before saving this reply')
    expect(coach.textContent).toContain('Removed before saving.')
    expect(coach.textContent).toContain('Always on.')
    expect(coach.textContent).toContain('Check the source.')
    expect(coach.textContent).toContain('Keep secrets at their source.')
    expect(getByTestId('redaction-coach-never').textContent).toBe("Don't show again")
    fireEvent.click(getByTestId('redaction-coach-got-it'))
    await waitFor(() => expect(queryByTestId('redaction-coach')).toBeNull())
    unmount()
    expect(render(<RedactionCoach count={3} slotKey="s1" />).queryByTestId('redaction-coach')).toBeNull()
    // Another session still teaches it once.
    expect(render(<RedactionCoach count={3} slotKey="s2" />).queryByTestId('redaction-coach')).toBeTruthy()
  })

  it("Don't show again closes it in every session", async () => {
    const { getByTestId, queryByTestId, unmount } = render(<RedactionCoach count={3} slotKey="s1" />)
    fireEvent.click(getByTestId('redaction-coach-never'))
    await waitFor(() => expect(queryByTestId('redaction-coach')).toBeNull())
    unmount()
    expect(render(<RedactionCoach count={3} slotKey="s2" />).queryByTestId('redaction-coach')).toBeNull()
  })
})

describe('first reply with removed values', () => {
  beforeEach(() => window.localStorage.clear())

  it('keeps the labelled lock tag and puts the coach right after the block it explains', () => {
    const md = ['```ini', CRED, '```', '', `Then ${PH('reviews.corp.example')}`].join('\n')
    const { getByTestId, getAllByTestId } = render(
      <MarkdownRenderer content={md} redactions={[cred()]} blockedLinks={[link()]} slotKey="s1" redactionCoach />,
    )
    const [tag] = getAllByTestId('credential-tag')
    expect(tag.textContent).toBe('credential· why?')
    const coach = getByTestId('redaction-coach')
    const block = getByTestId('redacted-code-block')
    const chip = getByTestId('blocked-link-chip')
    expect(block.compareDocumentPosition(coach) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(coach.compareDocumentPosition(chip) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(coach.querySelector('ol')?.className).toContain('list-decimal')
  })
})
