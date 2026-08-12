/**
 * RepoSwitcher — the repo picker pinned to the top of Issue Radar's rail.
 *
 * Locks the #3047 fix: with deeply nested GitLab group paths
 * (`acme-corp/infra/cloud/modules/terraform-cloud-networking`), the old
 * single-span right-side `truncate` cut off exactly the distinguishing part —
 * the repo name — so sibling repos under one parent group rendered identically.
 * The contract now is:
 *   - the repo name sits in its OWN span that never yields width to the owner
 *     path (the owner span carries the huge flex-shrink and does all the
 *     shrinking),
 *   - the owner path truncates from the LEFT (`dir="rtl"` moves the CSS
 *     ellipsis to the left edge; LRM sentinels in aria-hidden select-none
 *     spans keep character order without polluting selection/clipboard),
 *   - the full `owner/repo` path is recoverable via a `title` tooltip,
 * in BOTH the trigger button and every dropdown row.
 *
 * happy-dom does no layout, so these tests pin the mechanism (element
 * structure, direction, shrink behaviour, tooltip) rather than measuring
 * pixels.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

const ctx = { value: {} as Record<string, unknown> }
vi.mock('../apps/issue-radar/context', () => ({
  useIssueRadar: () => ctx.value,
}))

const RepoSwitcher = (await import('../apps/issue-radar/components/RepoSwitcher')).default

const LRM = '\u200E'
const GROUP = 'acme-corp/infra/cloud/modules'
const NESTED = [
  { owner: GROUP, repo: 'terraform-cloud-networking', provider: 'gitlab', permissions: { push: true } },
  { owner: GROUP, repo: 'terraform-cloud-compute', provider: 'gitlab', permissions: { push: true } },
]

const switchRepo = vi.fn()

beforeEach(() => {
  vi.clearAllMocks()
  ctx.value = {
    repos: NESTED,
    active: { owner: GROUP, repo: 'terraform-cloud-networking', provider: 'gitlab' },
    switchRepo,
  }
})

/** Radix opens on keyboard activation — the path happy-dom handles, unlike the
 * PointerEvent-driven mouse open. */
function openMenu(container: HTMLElement) {
  fireEvent.keyDown(container.querySelector('button')!, { key: 'Enter' })
}

/** The repo-name span of one rendered path label. */
function repoSpanOf(label: HTMLElement): HTMLElement {
  // Last element child: [owner span, repo span].
  return label.lastElementChild as HTMLElement
}
function ownerSpanOf(label: HTMLElement): HTMLElement {
  return label.firstElementChild as HTMLElement
}

describe('RepoSwitcher — nested group paths (#3047)', () => {
  it('renders the repo name in its own non-yielding span on the trigger', () => {
    const { container } = render(<RepoSwitcher />)
    const label = container.querySelector('button [data-testid="repo-path-label"]') as HTMLElement
    expect(label).not.toBeNull()
    const repoSpan = repoSpanOf(label)
    // The repo name is whole, prefixed by its separator, in a span of its own —
    // a sibling of the owner path, never inside the truncating owner span.
    expect(repoSpan.textContent).toBe('/terraform-cloud-networking')
    expect(ownerSpanOf(label).textContent).not.toContain('terraform-cloud-networking')
  })

  it('left-truncates the owner path: rtl ellipsis + LRM order sentinels', () => {
    const { container } = render(<RepoSwitcher />)
    const label = container.querySelector('button [data-testid="repo-path-label"]') as HTMLElement
    const ownerSpan = ownerSpanOf(label)
    // dir="rtl" is what moves the CSS ellipsis to the LEFT edge, keeping the
    // tail (most specific group) visible instead of the head.
    expect(ownerSpan.getAttribute('dir')).toBe('rtl')
    expect(ownerSpan.className).toContain('truncate')
    // LRM sentinels pin logical character order inside the rtl span. Each one
    // sits in an aria-hidden select-none span, so a selection/copy of the
    // visible path yields the clean owner string with no invisible characters,
    // and the accessible name stays clean.
    const sentinels = [...ownerSpan.querySelectorAll('span[aria-hidden="true"]')] as HTMLElement[]
    expect(sentinels.length).toBe(2)
    for (const s of sentinels) {
      expect(s.textContent).toBe(LRM)
      expect(s.className).toContain('select-none')
    }
    expect(ownerSpan.textContent).toBe(`${LRM}${GROUP}${LRM}`)
    // The owner span absorbs ALL width pressure before the repo span shrinks.
    // Deliberately NO min-width floor: at the rail's width a floor re-truncates
    // the repo name (the #3047 defect) to show an ellipsis carrying less
    // information than the repo characters it displaced.
    expect(ownerSpan.className).toContain('[flex-shrink:9999]')
    expect(ownerSpan.className).toContain('min-w-0')
    expect(repoSpanOf(label).className).not.toContain('flex-shrink-0')
  })

  it('recovers the full path via a title tooltip on the trigger', () => {
    const { container } = render(<RepoSwitcher />)
    const label = container.querySelector('button [data-testid="repo-path-label"]') as HTMLElement
    expect(label.getAttribute('title')).toBe(`${GROUP}/terraform-cloud-networking`)
  })

  it('gives every dropdown row the same contract, so sibling repos are tellable apart', () => {
    const { container } = render(<RepoSwitcher />)
    openMenu(container)
    const items = screen.getAllByRole('menuitem')
    expect(items.length).toBe(2)
    const repoNames = items.map((it) => {
      const label = it.querySelector('[data-testid="repo-path-label"]') as HTMLElement
      return repoSpanOf(label).textContent
    })
    // The distinguishing part is intact per row — the old single-span truncate
    // rendered these two rows identically.
    expect(repoNames).toEqual(['/terraform-cloud-networking', '/terraform-cloud-compute'])
    for (const it of items) {
      const label = it.querySelector('[data-testid="repo-path-label"]') as HTMLElement
      expect(ownerSpanOf(label).getAttribute('dir')).toBe('rtl')
      expect(label.getAttribute('title')).toMatch(new RegExp(`^${GROUP}/terraform-cloud-`))
    }
  })

  it('still switches repos on the full identity', () => {
    const { container } = render(<RepoSwitcher />)
    openMenu(container)
    fireEvent.click(screen.getAllByRole('menuitem')[1])
    expect(switchRepo).toHaveBeenCalledWith({
      owner: GROUP, repo: 'terraform-cloud-compute', provider: 'gitlab', host: undefined,
    })
  })
})
