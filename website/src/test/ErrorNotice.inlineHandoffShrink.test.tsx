/**
 * An inline ErrorNotice keeps Ask the agent inside its row.
 *
 * The hand-off button never shrinks, so a wrapper that shrank under it let the
 * link run past the row's edge when a long message filled it (an app page's
 * refusal at 750px). jsdom cannot lay the row out, so the pin reads the class;
 * the PR carries the rendered shot.
 */
import { describe, it, expect } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import ErrorNotice from '../components/ErrorNotice'

describe('ErrorNotice inline hand-off', () => {
  it('does not let the hand-off wrapper shrink below its button', () => {
    render(<ErrorNotice variant="inline" message="A long refusal that fills the row." askAgent testId="n" />)
    const button = within(screen.getByTestId('n')).getByRole('button', { name: /ask the agent/i })
    expect(button.parentElement!.className.split(/\s+/)).toContain('shrink-0')
  })
})
