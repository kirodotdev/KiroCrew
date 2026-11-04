// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import JobCreator from './JobCreator'

afterEach(cleanup)

describe('JobCreator', () => {
  it('shows the guest by display name', () => {
    render(<JobCreator creator="Ada" />)
    expect(screen.getByTestId('job-creator').textContent).toBe('Ada')
  })

  it('falls back to "Guest" when no one has a name for them', () => {
    render(<JobCreator creator="" />)
    expect(screen.getByTestId('job-creator').textContent).toBe('Guest')
  })

  it('shows nothing for any other creator', () => {
    const { container } = render(<JobCreator creator={null} />)
    expect(container.firstChild).toBeNull()
  })
})
