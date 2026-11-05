import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import LinkOpenChoices from './LinkOpenChoices'
import { rememberChoice, rememberedChoice } from './linkOpenRegistry'

beforeEach(() => window.localStorage.clear())
afterEach(() => cleanup())

describe('LinkOpenChoices: the "Always" answers of one app', () => {
  it('renders nothing when the app has no remembered answer', () => {
    rememberChoice('other:open', 'tickets.example.com', 'app')
    const { container } = render(<LinkOpenChoices appName="tickets" label="Tickets" />)
    expect(container.firstChild).toBeNull()
  })

  it('lists each host with where it opens, and Forget removes that one answer', () => {
    rememberChoice('tickets:open', 'tickets.example.com', 'app')
    rememberChoice('tickets:open', 'docs.example.com', 'browser')
    render(<LinkOpenChoices appName="tickets" label="Tickets" />)
    expect(screen.getByTestId('link-open-choices')).toBeTruthy()
    expect(screen.getByText('tickets.example.com')).toBeTruthy()
    expect(screen.getByText('Opens in Tickets')).toBeTruthy()
    expect(screen.getByText('Opens in your browser')).toBeTruthy()
    fireEvent.click(screen.getAllByRole('button', { name: 'Forget' })[0])
    expect(rememberedChoice('tickets:open', 'tickets.example.com')).toBeNull()
    expect(rememberedChoice('tickets:open', 'docs.example.com')).toBe('browser')
    expect(screen.queryByText('tickets.example.com')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Forget' }))
    expect(screen.queryByTestId('link-open-choices')).toBeNull()
  })
})
