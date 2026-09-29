import { describe, it, expect } from 'vitest'
import { createRef } from 'react'
import { render, screen } from '@testing-library/react'
import OnboardingChapterShell, { PANEL_CLASS, SECTION_CLASS } from './OnboardingChapterShell'

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
