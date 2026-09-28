import { describe, it, expect, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { resetAutolinkRulesForTest, setConfigAutolinkRules, drainConfigScanBudget } from '../utils/autolinkRules'

// Operator link rules (dashboard.link_patterns) in the transcript renderer:
// ChatPage feeds the config into the autolink registry, `remarkAutolinkRules`
// links prose matches on the mdast walk, and an inline-code span whose WHOLE
// text matches becomes a link chip instead of the copy-only chip (inlineCode
// is opaque to the remark plugin by design). With no rules the renderer is
// byte-identical — the plugin returns before walking on an empty registry.

const RULES = [{ pattern: '\\bPROJ-\\d+\\b', url: 'https://tracker.example.com/browse/{match}' }]

afterEach(() => resetAutolinkRulesForTest())

function renderWithRules(content: string) {
  setConfigAutolinkRules(RULES)
  return render(<MarkdownRenderer content={content} />)
}

describe('MarkdownRenderer with config autolink rules', () => {
  it('renders a prose match as a link to the resolved template', () => {
    renderWithRules('see PROJ-123 for details')
    const link = screen.getByRole('link')
    expect(link).toHaveAttribute('href', 'https://tracker.example.com/browse/PROJ-123')
    expect(link.textContent).toBe('PROJ-123')
  })

  it('renders an inline-code whole match as a link chip, not a copy chip', () => {
    renderWithRules('run `PROJ-42` now')
    const link = screen.getByRole('link')
    expect(link).toHaveAttribute('href', 'https://tracker.example.com/browse/PROJ-42')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link.getAttribute('rel')).toBe('noopener noreferrer')
    // The chip keeps the code element inside the anchor.
    expect(link.querySelector('code')?.textContent).toBe('PROJ-42')
  })

  it('keeps the copy chip for an inline-code span that only partially matches', () => {
    renderWithRules('`deploy PROJ-42 now`')
    expect(screen.queryByRole('link')).toBeNull()
    expect(screen.getByText('deploy PROJ-42 now')).toBeTruthy()
  })

  it('does not rewrite matches inside fenced code or existing links', () => {
    renderWithRules('```\nPROJ-7\n```\n\n[PROJ-8](https://other.example)')
    const links = screen.getAllByRole('link')
    expect(links).toHaveLength(1)
    expect(links[0]).toHaveAttribute('href', 'https://other.example')
  })

  it('renders plain text unchanged when no rules are configured', () => {
    render(<MarkdownRenderer content="see PROJ-123 for details" />)
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('re-arms the scan budget per message render: a spent pool never starves the next message', () => {
    // The budget rearm lives HERE, in the top-level renderer, not at the
    // plugin's tree entry (one message = many trees; a per-tree rearm is the
    // round-21 multi-block bypass). The other half of the contract is that a
    // pool a previous message exhausted must not bleed forward: this render
    // must start from a full pool or linkification dies permanently.
    setConfigAutolinkRules(RULES)
    drainConfigScanBudget(10_000) // a previous message spent everything
    render(<MarkdownRenderer content="see PROJ-321 for details" />)
    const link = screen.getByRole('link')
    expect(link).toHaveAttribute('href', 'https://tracker.example.com/browse/PROJ-321')
  })

  it('caps hits per text node so a match-packed paragraph cannot mint unbounded anchors', () => {
    // 250 matches inside a node UNDER the 2000-char subject cap (7 chars per
    // token = 1750): the walk stops collecting at 200 and the rest of the
    // node stays plain text.
    const tokens = Array.from({ length: 250 }, () => 'PROJ-1').join(' ')
    expect(tokens.length).toBeLessThan(2000)
    renderWithRules(tokens)
    expect(screen.getAllByRole('link')).toHaveLength(200)
  })

  it('skips oversized text nodes for config rules', () => {
    // One node over the 2000-char subject cap: the config rule must not scan
    // it, so the embedded token stays plain text.
    renderWithRules(`${'x'.repeat(2100)} PROJ-9`)
    expect(screen.queryByRole('link')).toBeNull()
  })
})
