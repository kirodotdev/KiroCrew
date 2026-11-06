// A long unbroken title must shrink and wrap so the message and dismiss stay in view.
import { it, expect, beforeEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import ErrorNotice from '../components/ErrorNotice'
import { __resetErrorJournalForTests, __resetNavSeamForTests } from '../utils/errorReport'

beforeEach(() => {
  __resetErrorJournalForTests()
  __resetNavSeamForTests()
})

it('makes a long unbroken inline title shrinkable and wrapping', () => {
  const title = 'x'.repeat(200)
  render(<ErrorNotice variant="inline" title={title} message="boom" testId="n" />)
  const strong = within(screen.getByTestId('n')).getByText(title, { selector: 'strong' })
  expect(strong).toHaveClass('min-w-0')
  expect(strong).toHaveStyle({ overflowWrap: 'anywhere' })
})
