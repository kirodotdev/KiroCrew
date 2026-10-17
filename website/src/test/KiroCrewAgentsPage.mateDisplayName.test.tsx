import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { DisplayNameField, displayNameFallback } from '../pages/KiroCrewAgentsPage'
import type { KiroCrewAgent } from '../components/AgentSelector'

const mate = { name: 'mate', kiro_agent: 'kirocrew', display_name: '' } as KiroCrewAgent
const ordinary = { name: 'pr-buddy', kiro_agent: 'kirocrew', display_name: '' } as KiroCrewAgent

describe('Mate display name in the crew editor', () => {
  it('falls back to the default Mate name, never the member key', () => {
    expect(displayNameFallback(mate, 'mate')).toBe('Mate')
    expect(displayNameFallback(ordinary, 'pr-buddy')).toBe('pr-buddy')
    expect(displayNameFallback(undefined, 'pr-buddy')).toBe('pr-buddy')
  })

  it('an empty Mate field shows Mate, and the hint never names the key', () => {
    const { container } = render(
      <DisplayNameField value="" onChange={() => {}} fallback={displayNameFallback(mate, 'mate')} />,
    )
    expect(screen.getByTestId('display-name-input')).toHaveAttribute('placeholder', 'Mate')
    expect(container.textContent).not.toContain('mate')
  })

  it('a renamed Mate shows the rename', () => {
    render(<DisplayNameField value="Skipper" onChange={() => {}} fallback="Mate" />)
    expect(screen.getByTestId('display-name-input')).toHaveValue('Skipper')
  })
})
