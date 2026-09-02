import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* ── Mock api/client BEFORE the component imports ── */
const mockApi = vi.hoisted(() => ({
  skillTree: vi.fn(),
  skillFile: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

/* ── Heavy children: skip Monaco / markdown machinery in unit tests ── */
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
vi.mock('../components/CodeBlock', () => ({
  CodeBlock: ({ code, lang }: { code: string; lang: string }) => (
    <pre data-testid="code" data-lang={lang}>{code}</pre>
  ),
}))

import SkillDirectoryBrowser from '../components/SkillDirectoryBrowser'

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

beforeEach(() => {
  mockApi.skillTree.mockReset()
  mockApi.skillFile.mockReset()
})

describe('SkillDirectoryBrowser', () => {
  it('renders the file tree from the API response', async () => {
    mockApi.skillTree.mockResolvedValue({
      name: 'demo',
      root: '/some/path/demo',
      entries: [
        { path: 'SKILL.md', type: 'file', size: 256 },
        { path: 'helper.sh', type: 'file', size: 42 },
        { path: 'references', type: 'dir', size: 0 },
        { path: 'references/doc.md', type: 'file', size: 1024 },
      ],
    })
    mockApi.skillFile.mockResolvedValue({ name: 'demo', path: 'SKILL.md', content: '# Demo' })

    renderWithQuery(<SkillDirectoryBrowser skillKey="demo" />)

    // Wait for tree to load — helper.sh only appears once tree renders
    // (SKILL.md is rendered earlier as the file-viewer label, so testing
    // for it doesn't prove the tree resolved).
    await waitFor(() => expect(screen.getByText('helper.sh')).toBeInTheDocument())
    expect(screen.getByText('references')).toBeInTheDocument()
    // SKILL.md appears in two places (tree row + viewer label) — the tree
    // having loaded means the row is now present.
    expect(screen.getAllByText('SKILL.md').length).toBeGreaterThanOrEqual(1)

    // Default selection is SKILL.md → fetched and rendered as markdown
    await waitFor(() => expect(mockApi.skillFile).toHaveBeenCalledWith('demo', 'SKILL.md'))
    await waitFor(() => expect(screen.getByTestId('md')).toHaveTextContent('# Demo'))
  })

  it('renders non-markdown files via CodeBlock with the right language', async () => {
    mockApi.skillTree.mockResolvedValue({
      name: 'demo',
      root: '/x',
      entries: [
        { path: 'SKILL.md', type: 'file', size: 0 },
        { path: 'check.sh', type: 'file', size: 50 },
      ],
    })
    mockApi.skillFile.mockImplementation((_name: string, path: string) =>
      Promise.resolve({ content: path === 'check.sh' ? '#!/bin/sh\necho ok' : '' })
    )

    renderWithQuery(<SkillDirectoryBrowser skillKey="demo" />)

    await waitFor(() => expect(screen.getByText('check.sh')).toBeInTheDocument())
    fireEvent.click(screen.getByText('check.sh'))

    await waitFor(() => expect(mockApi.skillFile).toHaveBeenCalledWith('demo', 'check.sh'))
    const code = await screen.findByTestId('code')
    expect(code).toHaveTextContent('echo ok')
    expect(code).toHaveAttribute('data-lang', 'bash')
  })

  it('expands a folder when its row is clicked, exposing children', async () => {
    mockApi.skillTree.mockResolvedValue({
      name: 'demo',
      root: '/x',
      entries: [
        { path: 'SKILL.md', type: 'file', size: 0 },
        { path: 'references', type: 'dir', size: 0 },
        { path: 'references/inner.md', type: 'file', size: 100 },
      ],
    })
    mockApi.skillFile.mockResolvedValue({ content: '' })
    renderWithQuery(<SkillDirectoryBrowser skillKey="demo" />)

    // The nested file is initially hidden because the folder is collapsed.
    await waitFor(() => expect(screen.getByText('references')).toBeInTheDocument())
    expect(screen.queryByText('inner.md')).not.toBeInTheDocument()

    // Expand the folder.
    fireEvent.click(screen.getByLabelText(/Expand references/))
    await waitFor(() => expect(screen.getByText('inner.md')).toBeInTheDocument())
  })

  it('shows "(empty skill folder)" when the tree has no entries', async () => {
    mockApi.skillTree.mockResolvedValue({ name: 'empty', root: '/x', entries: [] })
    mockApi.skillFile.mockResolvedValue({ content: '' })
    renderWithQuery(<SkillDirectoryBrowser skillKey="empty" />)
    await waitFor(() => expect(screen.getByText(/empty skill folder/i)).toBeInTheDocument())
  })

  it('shows an error message when the tree request fails', async () => {
    mockApi.skillTree.mockRejectedValue(new Error('boom'))
    renderWithQuery(<SkillDirectoryBrowser skillKey="bad" />)
    await waitFor(() => expect(screen.getByText(/Failed to load tree/)).toBeInTheDocument())
  })

  it('shows an error message when a file load fails', async () => {
    mockApi.skillTree.mockResolvedValue({
      name: 'demo',
      root: '/x',
      entries: [{ path: 'SKILL.md', type: 'file', size: 0 }],
    })
    mockApi.skillFile.mockRejectedValue(new Error('forbidden'))
    renderWithQuery(<SkillDirectoryBrowser skillKey="demo" />)
    await waitFor(() => expect(screen.getByText(/forbidden/)).toBeInTheDocument())
  })

  it('marks the selected file with aria-current', async () => {
    mockApi.skillTree.mockResolvedValue({
      name: 'demo',
      root: '/x',
      entries: [
        { path: 'SKILL.md', type: 'file', size: 0 },
        { path: 'helper.sh', type: 'file', size: 0 },
      ],
    })
    mockApi.skillFile.mockResolvedValue({ content: '' })
    renderWithQuery(<SkillDirectoryBrowser skillKey="demo" />)

    // Default selection is SKILL.md.
    await waitFor(() => expect(screen.getByLabelText('Open SKILL.md')).toHaveAttribute('aria-current', 'true'))
    expect(screen.getByLabelText('Open helper.sh')).not.toHaveAttribute('aria-current')

    fireEvent.click(screen.getByLabelText('Open helper.sh'))
    await waitFor(() => expect(screen.getByLabelText('Open helper.sh')).toHaveAttribute('aria-current', 'true'))
  })

  it('passes the skill key through to the API', async () => {
    mockApi.skillTree.mockResolvedValue({ name: 'kiro-user/foo', root: '/x', entries: [] })
    mockApi.skillFile.mockResolvedValue({ content: '' })
    renderWithQuery(<SkillDirectoryBrowser skillKey="kiro-user/foo" />)
    await waitFor(() => expect(mockApi.skillTree).toHaveBeenCalledWith('kiro-user/foo'))
  })

  it('strips YAML frontmatter from markdown bodies before rendering', async () => {
    // Without stripping, ``---\nname:foo\n---`` is parsed as a setext H2
    // heading by remark, which is the bug this regression covers.
    mockApi.skillTree.mockResolvedValue({
      name: 'demo', root: '/x',
      entries: [{ path: 'SKILL.md', type: 'file', size: 0 }],
    })
    mockApi.skillFile.mockResolvedValue({
      content: '---\nname: demo\ndescription: hi\n---\n# Real heading\n\nbody text',
    })
    renderWithQuery(<SkillDirectoryBrowser skillKey="demo" />)

    const md = await screen.findByTestId('md')
    // Frontmatter keys must NOT appear in the rendered body.
    expect(md.textContent).not.toContain('name: demo')
    expect(md.textContent).not.toContain('description: hi')
    // Real heading + body are preserved.
    expect(md.textContent).toContain('Real heading')
    expect(md.textContent).toContain('body text')
  })

  it('renders a frontmatter strip with description, triggers, tags, and loaded_by_agents', async () => {
    mockApi.skillTree.mockResolvedValue({
      name: 'demo', root: '/x',
      entries: [{ path: 'SKILL.md', type: 'file', size: 0 }],
    })
    mockApi.skillFile.mockResolvedValue({
      content: '---\nname: demo\ntriggers: foo, bar\ntags: [alpha, beta]\n---\nbody',
    })
    renderWithQuery(
      <SkillDirectoryBrowser
        skillKey="demo"
        skill={{
          key: 'demo', name: 'demo',
          description: 'a tested skill',
          source: 'kirocrew',
          loaded_by_agents: ['agent-one', 'agent-two'],
        }}
      />,
    )

    // Wait for both the file fetch (drives triggers/tags) and the
    // skill prop (drives description/loaded_by_agents) to land.
    await waitFor(() =>
      expect(screen.getByTestId('frontmatter-strip')).toHaveTextContent('foo')
    )
    const strip = screen.getByTestId('frontmatter-strip')
    expect(strip).toHaveTextContent('a tested skill')
    expect(strip).toHaveTextContent('foo')
    expect(strip).toHaveTextContent('bar')
    expect(strip).toHaveTextContent('alpha')
    expect(strip).toHaveTextContent('beta')
    expect(strip).toHaveTextContent('agent-one')
    expect(strip).toHaveTextContent('agent-two')
  })

  it('keeps the strip stable (from SKILL.md) when navigating to another file', async () => {
    // Regression: triggers/tags must come from SKILL.md, not the selected
    // file — otherwise they vanish when the user opens a non-markdown file.
    mockApi.skillTree.mockResolvedValue({
      name: 'demo', root: '/x',
      entries: [
        { path: 'SKILL.md', type: 'file', size: 0 },
        { path: 'run.sh', type: 'file', size: 20 },
      ],
    })
    mockApi.skillFile.mockImplementation((_name: string, path: string) =>
      Promise.resolve({
        content: path === 'SKILL.md'
          ? '---\nname: demo\ntriggers: foo, bar\ntags: [alpha, beta]\n---\nbody'
          : '#!/bin/sh\necho hi',
      }),
    )
    renderWithQuery(
      <SkillDirectoryBrowser
        skillKey="demo"
        skill={{ key: 'demo', name: 'demo', description: 'desc', source: 'kirocrew' }}
      />,
    )

    // Strip shows triggers from SKILL.md initially.
    await waitFor(() => expect(screen.getByTestId('frontmatter-strip')).toHaveTextContent('foo'))

    // Navigate to the shell script.
    fireEvent.click(screen.getByText('run.sh'))
    await waitFor(() => expect(mockApi.skillFile).toHaveBeenCalledWith('demo', 'run.sh'))

    // Strip must STILL show the SKILL.md-derived triggers/tags + description.
    const strip = screen.getByTestId('frontmatter-strip')
    expect(strip).toHaveTextContent('desc')
    expect(strip).toHaveTextContent('foo')
    expect(strip).toHaveTextContent('bar')
    expect(strip).toHaveTextContent('alpha')
    expect(strip).toHaveTextContent('beta')
  })

  it('omits the frontmatter strip when there is nothing to show', async () => {
    mockApi.skillTree.mockResolvedValue({
      name: 'bare', root: '/x',
      entries: [{ path: 'SKILL.md', type: 'file', size: 0 }],
    })
    mockApi.skillFile.mockResolvedValue({ content: '# just a heading\n' })
    renderWithQuery(<SkillDirectoryBrowser skillKey="bare" />)
    await waitFor(() => expect(screen.getByTestId('md')).toBeInTheDocument())
    expect(screen.queryByTestId('frontmatter-strip')).not.toBeInTheDocument()
  })

  it('strip uses the Skill description when SKILL.md has none', async () => {
    // skill.description should win — backend's redacted/canonical value.
    mockApi.skillTree.mockResolvedValue({
      name: 'demo', root: '/x',
      entries: [{ path: 'SKILL.md', type: 'file', size: 0 }],
    })
    mockApi.skillFile.mockResolvedValue({
      content: '---\nname: demo\n---\nbody',  // no description in file
    })
    renderWithQuery(
      <SkillDirectoryBrowser
        skillKey="demo"
        skill={{ key: 'demo', name: 'demo', description: 'from-skill-prop', source: 'kirocrew' }}
      />,
    )
    const strip = await screen.findByTestId('frontmatter-strip')
    expect(strip).toHaveTextContent('from-skill-prop')
  })
})
