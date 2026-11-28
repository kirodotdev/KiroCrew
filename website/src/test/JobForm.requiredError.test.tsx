/** A "required" refusal clears as soon as its field has a value, rather than
 *  staying on screen until the next submit. */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders } from './helpers'
import JobForm from '../components/JobForm'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    updateCron: vi.fn(),
    createCron: vi.fn(),
    models: vi.fn().mockResolvedValue([]),
    chatFolders: vi.fn().mockResolvedValue([]),
  },
}))

describe('JobForm required-field errors', () => {
  it('clears "Name is required" once a name is typed', async () => {
    renderWithProviders(<JobForm agents={[]} defaultAgent="" onSaved={() => {}} />)
    await userEvent.click(screen.getByRole('button', { name: 'Add' }))
    expect(await screen.findByText('Name is required')).toBeInTheDocument()
    await userEvent.type(screen.getByRole('textbox', { name: 'Name' }), 'slp')
    await waitFor(() => expect(screen.queryByText('Name is required')).not.toBeInTheDocument())
    expect(api.createCron).not.toHaveBeenCalled()
  })

  it('clears "Message is required" once a message is typed', async () => {
    renderWithProviders(<JobForm agents={[]} defaultAgent="" onSaved={() => {}} />)
    await userEvent.type(screen.getByRole('textbox', { name: 'Name' }), 'slp')
    await userEvent.click(screen.getByRole('button', { name: 'Add' }))
    expect(await screen.findByText('Message is required')).toBeInTheDocument()
    await userEvent.type(screen.getByRole('textbox', { name: 'Message' }), 'go')
    await waitFor(() => expect(screen.queryByText('Message is required')).not.toBeInTheDocument())
  })

  it('keeps a different error when the name is edited', async () => {
    renderWithProviders(<JobForm agents={[]} defaultAgent="" onSaved={() => {}} />)
    await userEvent.type(screen.getByRole('textbox', { name: 'Name' }), 'slp')
    await userEvent.click(screen.getByRole('button', { name: 'Add' }))
    expect(await screen.findByText('Message is required')).toBeInTheDocument()
    await userEvent.type(screen.getByRole('textbox', { name: 'Name' }), 'x')
    expect(screen.getByText('Message is required')).toBeInTheDocument()
  })
})
