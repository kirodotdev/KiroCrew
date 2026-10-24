// @vitest-environment happy-dom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { __resetLinkMetaForTests } from '../lib/linkMeta'
import { resetAutolinkRulesForTest, setConfigAutolinkRules } from '../utils/autolinkRules'

// A link an operator link pattern made keeps the matched text as its label
// with link previews on. Unfurling it replaced `PROJ-123` with the page title,
// or with the SSO login host when the tracker sits behind sign-in, so the key
// the pattern exists for vanished from the message.

const TRACKER = 'https://tracker.example.com/browse/'
const TITLE = 'Sign in to your account'

const wire = (url: string) => ({
  url,
  title: TITLE,
  description: 'Login page.',
  site_name: 'SSO',
  domain: 'sso.example.com',
  icon: 'data:image/png;base64,AAAA',
  icon_dark: '',
  fetched_at: 1770000000,
})

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  __resetLinkMetaForTests()
  fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://localhost').searchParams.get('url') ?? ''
    return { ok: true, status: 200, json: async () => wire(url) } as unknown as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  setConfigAutolinkRules([{ pattern: '\\bPROJ-\\d+\\b', url: `${TRACKER}{match}` }])
})

afterEach(() => {
  resetAutolinkRulesForTest()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

/** A link to another site in the same message: its title appearing proves the
 *  unfurl effects have run, so "the pattern link was not fetched" is checked
 *  after the renderer had every chance to fetch it. */
const CONTROL = 'https://other.example.com/x'
const CONTROL_LINE = `\n\nAlso [this page](${CONTROL}) here.`

/** Every URL the renderer asked `/api/link-meta` about. */
const requestedUrls = () => fetchMock.mock.calls.map(
  ([input]) => new URL(String(input), 'http://localhost').searchParams.get('url'),
)

const patternLink = (name: string) => screen.getAllByRole('link').find(a => a.textContent === name)

describe('config link patterns with link previews on', () => {
  it('keeps the matched text on a prose match and fetches nothing for it', async () => {
    render(<MarkdownRenderer content={`see PROJ-123 for details${CONTROL_LINE}`} linkPreviews />)
    await screen.findAllByText(TITLE)
    expect(requestedUrls()).toEqual([CONTROL])
    expect(patternLink('PROJ-123')).toHaveAttribute('href', `${TRACKER}PROJ-123`)
  })

  it('keeps a paragraph that is only a match as a link, not a card', async () => {
    render(<MarkdownRenderer content={`PROJ-7${CONTROL_LINE}`} linkPreviews />)
    await screen.findAllByText(TITLE)
    expect(requestedUrls()).toEqual([CONTROL])
    expect(patternLink('PROJ-7')?.closest('p')).not.toBeNull()
  })

  it('keeps the same label for an authored link to the match expansion', async () => {
    render(<MarkdownRenderer content={`see [PROJ-9](${TRACKER}PROJ-9) now${CONTROL_LINE}`} linkPreviews />)
    await screen.findAllByText(TITLE)
    expect(requestedUrls()).toEqual([CONTROL])
    expect(patternLink('PROJ-9')).toHaveAttribute('href', `${TRACKER}PROJ-9`)
  })

  it('keeps a formatted lone link to the match expansion as a link, not a card', async () => {
    // The paragraph's card gate must read the label through `**…**` as the
    // anchor's own gate does; reading only direct text saw an empty label.
    render(<MarkdownRenderer content={`[**PROJ-9**](${TRACKER}PROJ-9)${CONTROL_LINE}`} linkPreviews />)
    await screen.findAllByText(TITLE)
    expect(requestedUrls()).toEqual([CONTROL])
    expect(patternLink('PROJ-9')?.closest('p')).not.toBeNull()
  })

  it('still unfurls a link whose text matches but whose href goes elsewhere', async () => {
    render(<MarkdownRenderer content={`see [PROJ-9](${CONTROL}) now`} linkPreviews />)
    await screen.findAllByText(TITLE)
    expect(requestedUrls()).toEqual([CONTROL])
    expect(patternLink('PROJ-9')).toBeUndefined()
  })

  it('never lets the preview check spend the pool a later block links a key with', () => {
    // Every scan costs 10ms on this clock, so the six preview checks alone
    // would spend the whole 50ms per-message pool. The links sit alone in
    // list items, so the prose pass scans no text before the fence; the only
    // prose scan is the key after it. It must still become a link.
    let t = 0
    vi.spyOn(performance, 'now').mockImplementation(() => (t += 10))
    const links = Array.from({ length: 6 }, (_, i) => `- [page ${i}](https://other.example.com/${i})`).join('\n')
    render(<MarkdownRenderer content={`${links}\n\n\`\`\`text\nx\n\`\`\`\n\nsee PROJ-5 here`} linkPreviews />)
    expect(patternLink('PROJ-5')).toHaveAttribute('href', `${TRACKER}PROJ-5`)
  })
})
