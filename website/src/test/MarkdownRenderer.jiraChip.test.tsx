// @vitest-environment happy-dom
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { JiraHostsCtx } from '../lib/jiraHosts'

// In-message Jira chips: a Jira issue URL in a message body renders as a chip
// (Jira glyph + issue key) built synchronously from the URL alone -- Jira
// instances sit behind auth so the unfurl path can never produce a preview for
// them. Cloud (*.atlassian.net) chips need no configuration; self-hosted
// instances are gated on the JiraHostsCtx operator allowlist, mirroring
// dashboard.jira_hosts everywhere else.

describe('MarkdownRenderer Jira chips', () => {
  it('renders an Atlassian Cloud issue URL as a key-labelled chip', () => {
    render(<MarkdownRenderer content="See https://acme.atlassian.net/browse/PROJ-123 for details" />)
    const link = screen.getByRole('link')
    expect(link).toHaveAttribute('href', 'https://acme.atlassian.net/browse/PROJ-123')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link.getAttribute('rel')).toBe('noopener noreferrer')
    // The chip labels itself with the issue key, not the raw URL.
    expect(screen.getByText('PROJ-123')).toBeTruthy()
    expect(screen.queryByText(/https:\/\//)).toBeNull()
  })

  it('keeps a plain anchor for a self-hosted Jira URL not in the allowlist', () => {
    render(<MarkdownRenderer content="https://jira.acme.internal/browse/CORE-5" />)
    // Fails closed: unrecognized host stays a normal link showing the URL.
    expect(screen.queryByText('CORE-5')).toBeNull()
    expect(screen.getByRole('link').textContent).toContain('jira.acme.internal')
  })

  it('chips a self-hosted Jira URL when the host is allowlisted via context', () => {
    render(
      <JiraHostsCtx.Provider value={['jira.acme.internal']}>
        <MarkdownRenderer content="https://jira.acme.internal/browse/CORE-5" />
      </JiraHostsCtx.Provider>,
    )
    expect(screen.getByText('CORE-5')).toBeTruthy()
    expect(screen.getByRole('link')).toHaveAttribute('href', 'https://jira.acme.internal/browse/CORE-5')
  })

  it('keeps the author label of a labelled Jira link, with the Jira mark beside it', () => {
    render(<MarkdownRenderer content="See [the login outage](https://acme.atlassian.net/browse/PROJ-123) first." />)
    const link = screen.getByRole('link')
    expect(link).toHaveAttribute('href', 'https://acme.atlassian.net/browse/PROJ-123')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link.getAttribute('rel')).toBe('noopener noreferrer')
    expect(link.textContent).toBe('the login outage')
    expect(link.querySelector('[data-testid="jira-provider-mark"]')).toBeTruthy()
    expect(link.closest('.mc-md-ref-chip')).toBeNull()
    expect(screen.queryByText('PROJ-123')).toBeNull()
  })

  it('keeps the label of a labelled self-hosted Jira link when the host is allowlisted', () => {
    render(
      <JiraHostsCtx.Provider value={['jira.acme.internal']}>
        <MarkdownRenderer content="See [the core bug](https://jira.acme.internal/browse/CORE-5)." />
      </JiraHostsCtx.Provider>,
    )
    const link = screen.getByRole('link')
    expect(link.textContent).toBe('the core bug')
    expect(link.querySelector('[data-testid="jira-provider-mark"]')).toBeTruthy()
  })

  it('points a labelled Jira link at the canonical URL, without credentials', () => {
    render(<MarkdownRenderer content="See [the outage](https://user:pass@acme.atlassian.net/browse/PROJ-9)." />)
    const link = screen.getByRole('link')
    expect(link.textContent).toBe('the outage')
    expect(link).toHaveAttribute('href', 'https://acme.atlassian.net/browse/PROJ-9')
  })

  it('chips an angle-bracket autolink and a label that repeats the URL as the issue key', () => {
    render(
      <JiraHostsCtx.Provider value={['jira.acme.internal']}>
        <MarkdownRenderer content="Read <https://acme.atlassian.net/browse/PROJ-1> and [jira.acme.internal/browse/CORE-5/](https://jira.acme.internal/browse/CORE-5)." />
      </JiraHostsCtx.Provider>,
    )
    const [cloud, selfHosted] = screen.getAllByRole('link')
    expect(cloud.textContent).toBe('PROJ-1')
    expect(selfHosted.textContent).toBe('CORE-5')
    expect(cloud.closest('.mc-md-ref-chip')).toBeTruthy()
    expect(selfHosted.closest('.mc-md-ref-chip')).toBeTruthy()
  })

  it('does not chip non-issue Atlassian URLs', () => {
    render(<MarkdownRenderer content="https://acme.atlassian.net/wiki/spaces/X" />)
    expect(screen.getByRole('link').textContent).toContain('wiki/spaces')
  })
})
