import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { CrewLetterBadge, crewBadgeLetters } from './crewStateMark'

describe('crewBadgeLetters', () => {
  it('gives each crew its first letter while no other crew shares it', () => {
    expect(crewBadgeLetters(['devdesk', 'prod-us-east-1', 'Cloud One'])).toEqual(['D', 'P', 'C'])
  })

  it('lengthens only the colliding names, to the shortest prefix that tells them apart', () => {
    expect(crewBadgeLetters(['staging', 'sandbox', 'devdesk'])).toEqual(['St', 'Sa', 'D'])
  })

  it('compares without case and shows the first character upper-case, the second lower-case', () => {
    expect(crewBadgeLetters(['STAGING', 'Sandbox'])).toEqual(['St', 'Sa'])
    expect(crewBadgeLetters(['dev', 'DOCS'])).toEqual(['De', 'Do'])
  })

  it('caps at two characters when two names still match there', () => {
    expect(crewBadgeLetters(['prod-us', 'prod-eu', 'qa'])).toEqual(['Pr', 'Pr', 'Q'])
  })

  it('takes the whole name when it is shorter than the prefix its neighbour needs', () => {
    expect(crewBadgeLetters(['a', 'ab'])).toEqual(['A', 'Ab'])
  })

  it('skips leading punctuation and spaces, and gives a name with no letter or digit no badge', () => {
    expect(crewBadgeLetters(['  @prod', '-dev', '---'])).toEqual(['P', 'D', ''])
  })

  it('reads a non-Latin name by code point', () => {
    expect(crewBadgeLetters(['東京', '大阪'])).toEqual(['東', '大'])
  })

  it('keeps a first letter whose capital would be two letters', () => {
    expect(crewBadgeLetters(['ßeta', 'beta'])).toEqual(['ß', 'B'])
  })
})

const OUTLINE_STYLES = ['border-solid', 'border-dashed', 'border-dotted']

/** The badge's non-colour cue: its outline style, or `filled` when it has no border. */
function shapeCue(badge: HTMLElement): string {
  const outline = OUTLINE_STYLES.filter(c => badge.classList.contains(c))
  if (outline.length > 1) throw new Error(`badge has more than one outline style: ${outline.join(' ')}`)
  return outline[0] ?? (badge.classList.contains('border') ? 'outline-without-style' : 'filled')
}

function renderBadge(state?: string) {
  render(<CrewLetterBadge letters="D" state={state} />)
  const badge = screen.getByTestId('crew-letter-badge')
  expect(badge).toHaveTextContent('D')
  expect(badge).toHaveAttribute('aria-hidden', 'true')
  return badge
}

describe('CrewLetterBadge', () => {
  it('fills the disc while the tunnel is live, with no border', () => {
    const badge = renderBadge('connected')
    expect(badge).toHaveAttribute('data-state', 'connected')
    expect(badge).toHaveClass('bg-[var(--ok)]')
    expect(badge).not.toHaveClass('border')
    expect(shapeCue(badge)).toBe('filled')
  })

  it('draws a dashed 1px outline for a tunnel error', () => {
    const badge = renderBadge('error')
    expect(badge).toHaveClass('border', 'border-current', 'border-dashed', 'text-[var(--danger)]')
    expect(badge).not.toHaveClass('bg-[var(--ok)]')
    expect(shapeCue(badge)).toBe('border-dashed')
  })

  it('draws a dotted 1px outline while connecting', () => {
    const badge = renderBadge('connecting')
    expect(badge).toHaveClass('border', 'border-current', 'border-dotted', 'text-[var(--warn)]')
    expect(shapeCue(badge)).toBe('border-dotted')
  })

  it('draws a solid 1px outline for a stopped tunnel', () => {
    const badge = renderBadge('stopped')
    expect(badge).toHaveClass('border', 'border-current', 'border-solid', 'text-muted')
    expect(shapeCue(badge)).toBe('border-solid')
  })

  it('reads a missing state as disconnected, with a solid 1px outline', () => {
    const badge = renderBadge(undefined)
    expect(badge).toHaveAttribute('data-state', 'disconnected')
    expect(badge).toHaveClass('border', 'border-current', 'border-solid', 'text-muted')
    expect(shapeCue(badge)).toBe('border-solid')
  })

  it('gives live, error, connecting and stopped four different shapes, so colour is not the only cue', () => {
    const cues = ['connected', 'error', 'connecting', 'stopped'].map(state => {
      const { unmount } = render(<CrewLetterBadge letters="D" state={state} />)
      const cue = shapeCue(screen.getByTestId('crew-letter-badge'))
      unmount()
      return cue
    })
    expect(new Set(cues).size).toBe(4)
  })

  it('marks an error with the Lucide CircleAlert and a stopped tunnel with Square, and no other state', () => {
    const marks = ['connected', 'error', 'connecting', 'stopped', 'disconnected', undefined].map(state => {
      const { unmount } = render(<CrewLetterBadge letters="D" state={state} />)
      const mark = screen.queryByTestId('crew-badge-mark')
      if (mark) expect(mark).toHaveAttribute('aria-hidden', 'true')
      const kind = mark?.getAttribute('data-mark') ?? null
      const icon = mark
        ? ['lucide-circle-alert', 'lucide-square'].find(c => mark.classList.contains(c)) ?? 'other'
        : null
      unmount()
      return [kind, icon]
    })
    expect(marks).toEqual([
      [null, null],
      ['error', 'lucide-circle-alert'],
      [null, null],
      ['stopped', 'lucide-square'],
      [null, null],
      [null, null],
    ])
  })
})
