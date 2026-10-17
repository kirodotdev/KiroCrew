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
