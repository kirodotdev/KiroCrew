import { render, screen } from '@testing-library/react'
import { beforeAll, describe, expect, it, vi } from 'vitest'
import { registerThemeBranding } from '../themeBranding'
import { ShellAside } from '../components/OnboardingChapterShell'

let activeTheme: string | null = 'unregistered-onboarding-theme'

vi.mock('../hooks/useTheme', () => ({
  useOptionalTheme: () => activeTheme ? { colorTheme: activeTheme } : null,
}))

const copy = {
  ariaLabel: 'Setup',
  panelHeadline: 'Bring your crew with you.',
  panelBody: 'Import supported setup.',
  panelFootnote: 'Credentials stay where they are.',
}

function EditionDecorations() {
  return <div data-testid="edition-onboarding-decorations" />
}

function ThrowingDecorations(): never {
  throw new Error('broken edition decorations')
}

describe('onboarding theme branding seam', () => {
  beforeAll(() => {
    registerThemeBranding({
      'edition-onboarding-theme': {
        logo: '/edition-mark.svg',
        onboardingDecorations: EditionDecorations,
      },
      'throwing-onboarding-theme': {
        onboardingDecorations: ThrowingDecorations,
      },
    })
  })

  it('retains stock branding when rendered without a theme provider', () => {
    activeTheme = null
    const { container } = render(<ShellAside copy={copy} />)

    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelectorAll('svg')).toHaveLength(5)
  })

  it('retains the stock mascot treatment when no onboarding branding is registered', () => {
    activeTheme = 'unregistered-onboarding-theme'
    const { container } = render(<ShellAside copy={copy} />)

    expect(container.querySelector('img')).toBeNull()
    expect(screen.queryByTestId('edition-onboarding-decorations')).toBeNull()
    expect(container.querySelectorAll('svg')).toHaveLength(5)
  })

  it('reuses the edition logo with its onboarding decorations', () => {
    activeTheme = 'edition-onboarding-theme'
    const { container } = render(<ShellAside copy={copy} />)

    expect(container.querySelector('img')).toMatchObject({
      src: expect.stringContaining('/edition-mark.svg'),
    })
    expect(container.querySelector('img')).toHaveClass('h-8', 'w-8')
    expect(screen.getByTestId('edition-onboarding-decorations')).toBeInTheDocument()
    expect(container.querySelectorAll('svg')).toHaveLength(0)
  })

  it('falls back to stock decorations when an edition decoration throws', () => {
    activeTheme = 'throwing-onboarding-theme'
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      const { container } = render(<ShellAside copy={copy} />)
      expect(container.querySelectorAll('svg')).toHaveLength(5)
    } finally {
      consoleError.mockRestore()
    }
  })
})
