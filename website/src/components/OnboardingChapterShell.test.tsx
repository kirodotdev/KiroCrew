import { describe, it, expect } from 'vitest'
import { createRef } from 'react'
import type React from 'react'
import { render, screen } from '@testing-library/react'
import OnboardingChapterShell, {
  EMBEDDED_PANEL_CLASS,
  EMBEDDED_SECTION_CLASS,
  OnboardingShellHost,
  PANEL_CLASS,
  SECTION_CLASS,
} from './OnboardingChapterShell'

/**
 * On a phone the aside stacks above the section and the scrim scrolls, so the
 * step's navigation must stay reachable without scrolling and clear the
 * browser toolbar / home indicator. jsdom cannot lay out, so these pin the
 * classes that make that true; the rendered proof is in the PR evidence.
 */
describe('OnboardingChapterShell narrow-viewport footer', () => {
  it('pins the footer to the bottom of the scrim with a safe-area inset', () => {
    render(
      <OnboardingChapterShell
        ariaLabel="Chapter"
        panelHeadline="Headline"
        panelBody="Body"
        panelFootnote="Footnote"
        eyebrow="STEP · 1 OF 2"
        dialogRef={createRef<HTMLDivElement>()}
        header={<h1>Title</h1>}
        footer={<button type="button">Next</button>}
      >
        <p>Content</p>
      </OnboardingChapterShell>,
    )
    const footer = screen.getByRole('button', { name: 'Next' }).closest('footer')
    expect(footer).not.toBeNull()
    const cls = footer!.className.split(/\s+/)
    expect(cls).toContain('sticky')
    expect(cls).toContain('bottom-0')
    expect(cls).toContain('bg-card')
    expect(footer!.className).toContain('env(safe-area-inset-bottom)')
  })

  it('keeps floating mascots out of the narrow text column', () => {
    render(
      <OnboardingChapterShell
        ariaLabel="Chapter"
        panelHeadline="Own a goal. Follow it through."
        panelBody="Work across chats, dashboards and notes."
        panelFootnote=""
        eyebrow="STEP · 1 OF 4"
        dialogRef={createRef<HTMLDivElement>()}
        header={null}
        footer={<button type="button">Next</button>}
      >
        <p>Content</p>
      </OnboardingChapterShell>,
    )
    const mascots = screen.getByRole('dialog', { name: 'Chapter' }).querySelectorAll('aside .pointer-events-none')
    expect(mascots).toHaveLength(4)
    for (const mascot of mascots) {
      expect(mascot.classList.contains('hidden')).toBe(true)
    }
  })

  it('never sizes against the large viewport or clips with overflow-hidden', () => {
    // 100vh is the LARGE viewport on iOS Safari (URL bar hidden), which pushed
    // the footer under the toolbar; overflow-hidden would stop the sticky footer.
    for (const cls of [PANEL_CLASS, SECTION_CLASS]) {
      const mobile = cls.split(/\s+/).filter(c => !c.startsWith('sm:'))
      expect(mobile.join(' ')).not.toMatch(/100vh|min-h-screen/)
      expect(mobile).not.toContain('overflow-hidden')
    }
  })
})

describe('OnboardingChapterShell embedded', () => {
  const renderEmbedded = (wrap?: (node: React.ReactElement) => React.ReactElement) => {
    const node = (
      <OnboardingChapterShell
        embedded
        ariaLabel="Chapter"
        panelHeadline="Headline"
        panelBody="Body"
        panelFootnote=""
        eyebrow="STEP · 1 OF 2"
        dialogRef={createRef<HTMLDivElement>()}
        headerAction={<button type="button">Back</button>}
        footer={<button type="button">Next</button>}
      >
        <p>Content</p>
      </OnboardingChapterShell>
    )
    return render(wrap ? wrap(node) : node)
  }

  it('renders in place as a region: no portal, no dialog, no scrim, no viewport sizing', () => {
    const { container } = renderEmbedded()
    expect(screen.queryByRole('dialog')).toBeNull()
    const region = screen.getByRole('region', { name: 'Chapter' })
    expect(container.contains(region)).toBe(true)
    expect(region.className).toBe(EMBEDDED_PANEL_CLASS)
    for (const cls of [EMBEDDED_PANEL_CLASS, EMBEDDED_SECTION_CLASS]) {
      expect(cls).not.toMatch(/\bfixed\b|100vh|100svh|min-h-svh|min-h-screen/)
      expect(cls.split(/\s+/)).not.toContain('overflow-hidden')
    }
    expect(screen.getByText('Headline').closest('aside')).not.toBeNull()
    expect(screen.getByRole('button', { name: 'Back' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Next' }).closest('footer')!.className).toContain('sticky')
  })

  it('keeps the original four-ghost composition and fills its host region', () => {
    renderEmbedded()
    const region = screen.getByRole('region', { name: 'Chapter' })
    expect(region).toHaveClass('flex-1', 'sm:h-full')
    expect(region).not.toHaveClass('sm:max-h-[760px]', 'sm:max-w-6xl')
    const ghosts = Array.from(region.querySelectorAll('aside .pointer-events-none'))
    expect(ghosts).toHaveLength(4)
    expect(ghosts[0]).toHaveClass('-left-8', 'top-[24%]', 'xl:block')
    expect(ghosts[1]).toHaveClass('-right-5', 'top-5', 'xl:block')
    expect(ghosts[2]).toHaveClass('bottom-[-6.5rem]', 'right-[-10rem]', 'xl:block')
    expect(ghosts[3]).toHaveClass('-top-20', 'left-[40%]', 'xl:block')
    expect(region.querySelector('aside')).toHaveClass('xl:w-[415px]')
  })

  it('ignores an enclosing modal host', () => {
    const { container } = renderEmbedded(node => <OnboardingShellHost>{node}</OnboardingShellHost>)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(container.contains(screen.getByRole('region', { name: 'Chapter' }))).toBe(true)
  })
})
