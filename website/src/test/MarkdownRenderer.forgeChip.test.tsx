// @vitest-environment happy-dom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import {
  parseSourceLinkUrl,
  forgeChipLabel,
  registerSourceProvider,
  resetSourceProvidersForTests,
  type SourceProviderDescriptor,
} from '../utils/pullRequestLinks'

// In-message GitHub / GitLab chips (#2579): a forge issue / PR / MR URL in a
// message body renders as a chip (provider mark + reference) built
// synchronously from the URL alone — matching the Jira chips next door, so
// forge links get at-a-glance recognition in USER messages, in assistant
// messages, and with `link_previews` off, none of which the network unfurl
// path covers. Host matching is EXACT (github.com / gitlab.com via the shared
// pullRequestLinks parser); lookalike hosts and malformed paths fall through
// to the plain anchor unchanged, and the anchor keeps the AUTHORED href.

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  // No test in this file may cause a network request: the chip must render
  // from the URL alone, and a chipped href must never reach the unfurl path.
  fetchMock = vi.fn(async () => { throw new Error('unexpected fetch') })
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('MarkdownRenderer forge chips — the four URL shapes', () => {
  it('chips a GitHub pull request URL as owner/repo#N with the GitHub mark', () => {
    render(<MarkdownRenderer content="See https://github.com/kirodotdev/KiroCrew/pull/2579 for the fix" />)
    const link = screen.getByRole('link')
    expect(link).toHaveAttribute('href', 'https://github.com/kirodotdev/KiroCrew/pull/2579')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link.getAttribute('rel')).toBe('noopener noreferrer')
    expect(screen.getByText('kirodotdev/KiroCrew#2579')).toBeTruthy()
    expect(link.querySelector('[data-provider-mark="github"]')).toBeTruthy()
  })

  it('chips a GitHub issue URL', () => {
    render(<MarkdownRenderer content="https://github.com/kirodotdev/KiroCrew/issues/45" />)
    expect(screen.getByText('kirodotdev/KiroCrew#45')).toBeTruthy()
    expect(screen.getByRole('link').querySelector('[data-provider-mark="github"]')).toBeTruthy()
  })

  it('chips a GitLab merge request URL as group/project!N with the GitLab mark', () => {
    render(<MarkdownRenderer content="https://gitlab.com/acme/widgets/-/merge_requests/7" />)
    const link = screen.getByRole('link')
    expect(link).toHaveAttribute('href', 'https://gitlab.com/acme/widgets/-/merge_requests/7')
    expect(screen.getByText('acme/widgets!7')).toBeTruthy()
    expect(link.querySelector('[data-provider-mark="gitlab"]')).toBeTruthy()
  })

  it('chips a GitLab issue URL as group/project#N, subgroups included', () => {
    render(<MarkdownRenderer content="https://gitlab.com/acme/team/widgets/-/issues/9" />)
    expect(screen.getByText('acme/team/widgets#9')).toBeTruthy()
    expect(screen.getByRole('link').querySelector('[data-provider-mark="gitlab"]')).toBeTruthy()
  })
})

describe('MarkdownRenderer forge links — labelled vs. bare', () => {
  const PR = 'https://github.com/modelcontextprotocol/transports-wg/pull/57'

  it('keeps the author label of a labelled GitHub link, with the GitHub mark beside it', () => {
    render(<MarkdownRenderer content={`Read the [Definition Tags proposal](${PR}) first.`} />)
    const link = screen.getByRole('link')
    expect(link).toHaveAttribute('href', PR)
    expect(link).toHaveAttribute('target', '_blank')
    expect(link.getAttribute('rel')).toBe('noopener noreferrer')
    expect(link.textContent).toBe('Definition Tags proposal')
    expect(link.querySelector('[data-provider-mark="github"]')).toBeTruthy()
    expect(link.closest('.mc-md-ref-chip')).toBeNull()
    expect(screen.queryByText('modelcontextprotocol/transports-wg#57')).toBeNull()
  })

  it('keeps the author label of a labelled GitLab link, with the GitLab mark beside it', () => {
    render(<MarkdownRenderer content="See [the widgets MR](https://gitlab.com/acme/widgets/-/merge_requests/7)." />)
    const link = screen.getByRole('link')
    expect(link.textContent).toBe('the widgets MR')
    expect(link.querySelector('[data-provider-mark="gitlab"]')).toBeTruthy()
    expect(screen.queryByText('acme/widgets!7')).toBeNull()
  })

  it('never fetches a labelled forge link inside prose, even with link previews on', () => {
    render(<MarkdownRenderer content={`Read the [Definition Tags proposal](${PR}) first.`} linkPreviews />)
    expect(screen.getByRole('link').textContent).toBe('Definition Tags proposal')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('chips a bare URL as owner/repo#N', () => {
    render(<MarkdownRenderer content={`Read ${PR} first.`} />)
    const link = screen.getByRole('link')
    expect(link.closest('.mc-md-ref-chip')).toBeTruthy()
    expect(link.textContent).toBe('modelcontextprotocol/transports-wg#57')
  })

  it('chips an angle-bracket autolink as owner/repo#N', () => {
    render(<MarkdownRenderer content={`Read <${PR}> first.`} />)
    const link = screen.getByRole('link')
    expect(link.closest('.mc-md-ref-chip')).toBeTruthy()
    expect(link.textContent).toBe('modelcontextprotocol/transports-wg#57')
  })

  it('chips a labelled link whose label is the URL itself, with or without its scheme', () => {
    render(<MarkdownRenderer content={`[${PR}](${PR}) and [github.com/o/r/pull/3](https://github.com/o/r/pull/3)`} />)
    const [full, schemeless] = screen.getAllByRole('link')
    expect(full.textContent).toBe('modelcontextprotocol/transports-wg#57')
    expect(schemeless.textContent).toBe('o/r#3')
    expect(full.closest('.mc-md-ref-chip')).toBeTruthy()
    expect(schemeless.closest('.mc-md-ref-chip')).toBeTruthy()
  })

  it('chips a URL label that differs from the href only by a trailing slash or the host case', () => {
    render(<MarkdownRenderer content={`[${PR}/](${PR}) and [https://GitHub.com/o/r/pull/3](https://github.com/o/r/pull/3)`} />)
    const [slash, hostCase] = screen.getAllByRole('link')
    expect(slash.textContent).toBe('modelcontextprotocol/transports-wg#57')
    expect(hostCase.textContent).toBe('o/r#3')
  })

  it('chips a URL label written as inline code', () => {
    render(<MarkdownRenderer content={`[\`${PR}\`](${PR})`} />)
    const link = screen.getByRole('link')
    expect(link.closest('.mc-md-ref-chip')).toBeTruthy()
    expect(link.textContent).toBe('modelcontextprotocol/transports-wg#57')
  })

  it('keeps a formatted label: inline code and emphasis inside the text', () => {
    render(<MarkdownRenderer content={`See [the \`Definition Tags\` proposal](${PR}) and [**Definition** Tags](https://github.com/o/r/pull/3).`} />)
    const [code, bold] = screen.getAllByRole('link')
    expect(code.textContent).toBe('the Definition Tags proposal')
    expect(code.querySelector('code')).toBeTruthy()
    expect(bold.textContent).toBe('Definition Tags')
    expect(bold.querySelector('strong')).toBeTruthy()
    for (const link of [code, bold]) {
      expect(link.closest('.mc-md-ref-chip')).toBeNull()
      expect(link.querySelector('[data-provider-mark="github"]')).toBeTruthy()
    }
  })
})

describe('MarkdownRenderer forge chips — security and fallthrough', () => {
  it('does NOT chip a lookalike hostname', () => {
    render(<MarkdownRenderer content="https://evil-github.com.attacker.test/kirodotdev/KiroCrew/pull/1" />)
    const link = screen.getByRole('link')
    expect(link.textContent).toContain('attacker.test')
    expect(link.querySelector('[data-provider-mark]')).toBeNull()
  })

  it('does NOT chip a credential-bearing URL, even on the real host', () => {
    render(<MarkdownRenderer content="https://user:pass@github.com/kirodotdev/KiroCrew/pull/1" />)
    expect(screen.queryByText('kirodotdev/KiroCrew#1')).toBeNull()
    expect(screen.getByRole('link').querySelector('[data-provider-mark]')).toBeNull()
  })

  it('falls through to a plain anchor for malformed forge paths', () => {
    render(
      <MarkdownRenderer content={'https://github.com/kirodotdev/KiroCrew/pull/abc and https://github.com/kirodotdev/KiroCrew and https://gitlab.com/acme/widgets/merge_requests/7'} />,
    )
    for (const link of screen.getAllByRole('link')) {
      expect(link.querySelector('[data-provider-mark]')).toBeNull()
    }
  })

  it('keeps the authored href — fragments and query params are not rewritten', () => {
    const authored = 'https://github.com/kirodotdev/KiroCrew/pull/2579?diff=split#issuecomment-1'
    render(<MarkdownRenderer content={authored} />)
    expect(screen.getByRole('link')).toHaveAttribute('href', authored)
  })
})

describe('MarkdownRenderer forge chips — render contexts', () => {
  it('chips with link previews OFF and issues no network request', () => {
    // No `linkPreviews` prop = the default context, exactly how user-message
    // markdown renders (ChatPage renders user content without linkPreviews).
    render(<MarkdownRenderer content="https://github.com/kirodotdev/KiroCrew/pull/2579" softBreaks compactImages />)
    expect(screen.getByText('kirodotdev/KiroCrew#2579')).toBeTruthy()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('chips with link previews ON and still never fetches the forge URL', () => {
    render(<MarkdownRenderer content="Merged https://gitlab.com/acme/widgets/-/merge_requests/7 today" linkPreviews />)
    expect(screen.getByText('acme/widgets!7')).toBeTruthy()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('forgeChipLabel', () => {
  const parse = (url: string) => {
    const link = parseSourceLinkUrl(url)
    expect(link).not.toBeNull()
    return link!
  }

  it('labels GitHub pulls and issues as owner/repo#N', () => {
    expect(forgeChipLabel(parse('https://github.com/o/r/pull/12'))).toBe('o/r#12')
    expect(forgeChipLabel(parse('https://github.com/o/r/issues/3'))).toBe('o/r#3')
  })

  it('labels GitLab MRs with ! and issues with #, keeping subgroup paths', () => {
    expect(forgeChipLabel(parse('https://gitlab.com/g/p/-/merge_requests/5'))).toBe('g/p!5')
    expect(forgeChipLabel(parse('https://gitlab.com/g/sub/p/-/issues/8'))).toBe('g/sub/p#8')
  })

  it('returns null for Jira links (they label themselves with the issue key)', () => {
    expect(forgeChipLabel(parse('https://acme.atlassian.net/browse/PROJ-9'))).toBeNull()
  })
})

describe('MarkdownRenderer forge chips — a registered provider', () => {
  const descriptor = (icon?: SourceProviderDescriptor['icon']): SourceProviderDescriptor => ({
    id: 'acme',
    displayName: 'Acme Review',
    parse(url) {
      const match = url.hostname === 'review.acme.example' ? /^\/cr\/(\d+)$/.exec(url.pathname) : null
      return match
        ? { url: url.href, provider: 'acme', number: Number(match[1]), repo: 'acme', kind: 'change' }
        : null
    },
    chipLabel: link => `CR-${link.number}`,
    refLabel: n => `CR-${n}`,
    capabilities: { checks: false, mergeState: false, resolveThreads: false, comment: false },
    ...(icon ? { icon } : {}),
  })

  afterEach(() => resetSourceProvidersForTests())

  it('draws the one neutral mark the sidebar chip and Changes tab use when it ships no icon', () => {
    registerSourceProvider(descriptor())
    render(<MarkdownRenderer content="See https://review.acme.example/cr/123 for the change" />)
    const link = screen.getByRole('link')
    expect(link.textContent).toContain('CR-123')
    const mark = link.querySelector('[data-provider-mark="neutral"]')
    expect(mark).not.toBeNull()
    expect(mark!.getAttribute('class')).toContain('lucide-link-2')
    expect(mark!.getAttribute('width')).toBe('12')
    expect(link.querySelector('.lucide-git-pull-request')).toBeNull()
  })

  it('draws the provider its own icon when its descriptor ships one', () => {
    const AcmeGlyph = ({ size }: { size?: number }) => <span data-testid="acme-glyph" data-size={size} />
    registerSourceProvider(descriptor(AcmeGlyph))
    render(<MarkdownRenderer content="See https://review.acme.example/cr/123 for the change" />)
    const link = screen.getByRole('link')
    expect(link.querySelector('[data-testid="acme-glyph"]')?.getAttribute('data-size')).toBe('12')
    expect(link.querySelector('[data-provider-mark="neutral"]')).toBeNull()
  })
})
