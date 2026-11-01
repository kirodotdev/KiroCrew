import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'

import { renderWithProviders } from '../test/helpers'

vi.mock('../api/client', () => ({
  api: {
    projectAwsProfile: vi.fn(),
    setProjectAwsProfile: vi.fn(),
  },
}))

import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import ProjectAwsProfileStatus, { ProjectAwsProfileSection } from './ProjectAwsProfile'

const read = api.projectAwsProfile as unknown as ReturnType<typeof vi.fn>
const write = api.setProjectAwsProfile as unknown as ReturnType<typeof vi.fn>

const PROJECT = '/home/me/src/billing'
const bound = (status: string, extra: Record<string, unknown> = {}) =>
  ({ path: PROJECT, profile: 'billing-prod', boundDir: PROJECT, profiles: ['billing-prod', 'dev'], status, ...extra })
const unbound = (profiles: string[] = ['dev', 'ops']) => ({ path: PROJECT, profile: null, boundDir: null, profiles, status: null })

describe('ProjectAwsProfileStatus (the pill segment)', () => {
  beforeEach(() => { read.mockReset(); write.mockReset() })

  it('shows the bound profile and is not a control', async () => {
    read.mockResolvedValue(bound('ok'))
    const { container } = renderWithProviders(<ProjectAwsProfileStatus project={PROJECT} />)
    await screen.findByText('billing-prod')
    const seg = container.querySelector('[data-aws-status]')!
    expect(seg.getAttribute('data-aws-status')).toBe('ok')
    expect(seg.getAttribute('title')).toBe('AWS profile for this project: billing-prod')
    expect(container.querySelector('button')).toBeNull()
  })

  it('turns amber and gives the sign-in command when the SSO session expired', async () => {
    read.mockResolvedValue(bound('expired'))
    const { container } = renderWithProviders(<ProjectAwsProfileStatus project={PROJECT} />)
    await screen.findByText('billing-prod')
    const seg = container.querySelector('[data-aws-status]')!
    expect(seg.className).toContain('text-warn')
    expect(seg.getAttribute('title')).toContain('aws sso login --profile billing-prod')
  })

  it('names the ancestor an inherited binding comes from', async () => {
    read.mockResolvedValue(bound('ok', { path: `${PROJECT}/infra` }))
    const { container } = renderWithProviders(<ProjectAwsProfileStatus project={`${PROJECT}/infra`} />)
    await screen.findByText('billing-prod')
    expect(container.querySelector('[data-aws-status]')!.getAttribute('title')).toContain(`Set on parent folder ${PROJECT}`)
  })

  it('renders nothing for an unbound project or a refused read', async () => {
    read.mockResolvedValue(unbound())
    const first = renderWithProviders(<ProjectAwsProfileStatus project={PROJECT} />)
    await waitFor(() => expect(read).toHaveBeenCalled())
    expect(first.container.querySelector('[data-aws-status]')).toBeNull()
    first.unmount()

    read.mockRejectedValue(new ApiError(403, 'dashboard owner required'))
    const second = renderWithProviders(<ProjectAwsProfileStatus project={PROJECT} />)
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2))
    expect(second.container.querySelector('[data-aws-status]')).toBeNull()
  })
})

describe('ProjectAwsProfileStatus read failure', () => {
  beforeEach(() => { read.mockReset(); write.mockReset() })

  it('shows a failed read through the error notice instead of looking unbound, with no hand-off', async () => {
    read.mockRejectedValue(new ApiError(500, 'failed to read config file'))
    const { container } = renderWithProviders(<ProjectAwsProfileStatus project={PROJECT} />)
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain("Could not read this project's AWS profile")
    expect(container.querySelector('[data-aws-status]')!.getAttribute('data-aws-status')).toBe('read-failed')
    // The pill sits in the chat composer, so the hand-off stays off over its unsent draft.
    expect(screen.queryByRole('button', { name: /ask the agent/i })).toBeNull()
    expect(container.querySelector('button')).toBeNull()
  })

  it('says the gateway is busy when the read times out', async () => {
    read.mockRejectedValue(Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' }))
    renderWithProviders(<ProjectAwsProfileStatus project={PROJECT} />)
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('The gateway is busy and did not answer')
  })
})

describe('ProjectAwsProfileSection (the project picker row)', () => {
  beforeEach(() => { read.mockReset(); write.mockReset() })

  const open = async () => {
    fireEvent.click(await screen.findByRole('button', { name: /AWS profile for/ }))
    return screen.getByRole('radiogroup', { name: /AWS profile for/ })
  }

  it('starts collapsed, naming the current binding', async () => {
    read.mockResolvedValue(bound('expired'))
    renderWithProviders(<ProjectAwsProfileSection project={PROJECT} />)
    const toggle = await screen.findByRole('button', { name: /AWS profile for/ })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(toggle.textContent).toContain('billing-prod')
    expect(toggle.getAttribute('title')).toContain('aws sso login --profile billing-prod')
    expect(screen.queryByRole('radiogroup')).toBeNull()
  })

  it('opens on the chat project and says how to sign in again when the SSO session expired', async () => {
    read.mockResolvedValue(bound('expired'))
    renderWithProviders(<ProjectAwsProfileSection project={PROJECT} />)
    await open()
    expect(screen.getByText(PROJECT)).toBeTruthy()
    expect(screen.getByText(/Run aws sso login --profile billing-prod/)).toBeTruthy()
  })

  it('binds a picked profile and goes back to the default chain', async () => {
    read.mockResolvedValue(unbound())
    write.mockResolvedValue(bound('ok', { profile: 'ops' }))
    renderWithProviders(<ProjectAwsProfileSection project={PROJECT} />)
    await open()
    expect(screen.getByRole('radio', { name: "No profile (use the gateway's AWS sign-in)" }).getAttribute('aria-checked')).toBe('true')
    read.mockResolvedValue(bound('ok', { profile: 'ops', profiles: ['dev', 'ops'] }))
    fireEvent.click(screen.getByRole('radio', { name: 'ops' }))
    await waitFor(() => expect(write).toHaveBeenCalledWith(PROJECT, 'ops'))
    await waitFor(() => expect(screen.getByRole('radio', { name: 'ops' }).getAttribute('aria-checked')).toBe('true'))

    write.mockResolvedValue(unbound())
    fireEvent.click(screen.getByRole('radio', { name: "No profile (use the gateway's AWS sign-in)" }))
    await waitFor(() => expect(write).toHaveBeenLastCalledWith(PROJECT, ''))
  })

  it('binds a typed name and refuses one outside the charset', async () => {
    read.mockResolvedValue(unbound(['dev']))
    write.mockResolvedValue(bound('ok', { profile: 'sso.Admin+ro' }))
    renderWithProviders(<ProjectAwsProfileSection project={PROJECT} />)
    await open()
    const input = screen.getByRole('textbox', { name: 'Other profile name' })
    const bind = screen.getByRole('button', { name: 'Use profile' }) as HTMLButtonElement

    fireEvent.change(input, { target: { value: 'bad name; rm' } })
    expect(bind.disabled).toBe(true)
    expect(screen.getByText(/does not start with a hyphen/)).toBeTruthy()

    fireEvent.change(input, { target: { value: 'sso.Admin+ro' } })
    fireEvent.click(bind)
    await waitFor(() => expect(write).toHaveBeenCalledWith(PROJECT, 'sso.Admin+ro'))
  })

  it('does not offer the default chain for an inherited binding', async () => {
    read.mockResolvedValue(bound('ok', { path: `${PROJECT}/infra` }))
    renderWithProviders(<ProjectAwsProfileSection project={`${PROJECT}/infra`} />)
    await open()
    expect((screen.getByRole('radio', { name: "No profile (use the gateway's AWS sign-in)" }) as HTMLButtonElement).disabled).toBe(true)
    const note = screen.getByText(new RegExp(`Set on parent folder ${PROJECT}`))
    // Above the choices, so it is read at the moment of choice rather than below the fold.
    expect(note.compareDocumentPosition(screen.getByRole('radiogroup')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('reports a refused save through the error notice', async () => {
    read.mockResolvedValue(unbound())
    write.mockRejectedValue(new ApiError(400, 'aws.project_profiles refuses the entry'))
    renderWithProviders(<ProjectAwsProfileSection project={PROJECT} />)
    await open()
    fireEvent.click(screen.getByRole('radio', { name: 'dev' }))
    const alert = await screen.findByRole('alert')
    // A list pick: there is no typed name to check.
    expect(alert.textContent).toContain('Could not save this AWS profile. Try again.')
    expect(alert.textContent).not.toContain('Check the name')
    // The server's own wording stays one hover away rather than in the sentence.
    expect(alert.textContent).not.toContain('aws.project_profiles refuses the entry')
    expect(alert.querySelector('[title="aws.project_profiles refuses the entry"]')).not.toBeNull()
  })

  it('asks to check the name only when the failed save was typed', async () => {
    read.mockResolvedValue(unbound())
    write.mockRejectedValue(new ApiError(400, 'refused'))
    renderWithProviders(<ProjectAwsProfileSection project={PROJECT} />)
    await open()
    fireEvent.change(screen.getByRole('textbox', { name: 'Other profile name' }), { target: { value: 'typo-prod' } })
    fireEvent.click(screen.getByRole('button', { name: 'Use profile' }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('Could not save this AWS profile. Check the name and try again.')
  })

  it('says what a pick does above the choices, and shortens the unbound value', async () => {
    read.mockResolvedValue(unbound())
    renderWithProviders(<ProjectAwsProfileSection project={PROJECT} />)
    const toggle = await screen.findByRole('button', { name: /AWS profile for/ })
    expect(toggle.textContent).toContain('No profile')
    expect(toggle.textContent).not.toContain("gateway's AWS sign-in")
    const group = await open()
    const line = screen.getByText(/Picking one runs nothing now/)
    // Before the radio group in document order, not under the text field.
    expect(line.compareDocumentPosition(group) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('names the bound directory in the row header', async () => {
    read.mockResolvedValue(bound('ok', { path: `${PROJECT}/infra` }))
    renderWithProviders(<ProjectAwsProfileSection project={`${PROJECT}/infra`} />)
    const toggle = await screen.findByRole('button', { name: /AWS profile for infra/ })
    expect(toggle.textContent).toContain('billing-prod')
    // A long folder name truncates instead of squeezing the bound profile to nothing.
    const [title, value] = Array.from(toggle.querySelectorAll(':scope > span'))
    expect(title.className).toContain('truncate')
    expect(title.className).not.toContain('shrink-0')
    expect(value.className).toContain('shrink-0')
    expect(value.className).toMatch(/max-w-/)
  })

  it.each([
    ['our deadline', Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })],
    ['a dropped connection', new TypeError('Failed to fetch')],
    ['a proxy timeout', new ApiError(504, 'gateway timeout')],
  ])('says the gateway is busy, not that the profile is unreadable, on %s', async (_label, failure) => {
    read.mockRejectedValue(failure)
    renderWithProviders(<ProjectAwsProfileSection project={PROJECT} />)
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('The gateway is busy and did not answer')
    expect(alert.textContent).not.toContain("Could not read this project's AWS profile")
  })

  it('reports a failed read through the error notice, but stays silent on the owner-only refusal', async () => {
    read.mockRejectedValue(new ApiError(500, 'gateway error'))
    const first = renderWithProviders(<ProjectAwsProfileSection project={PROJECT} />)
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain("Could not read this project's AWS profile")
    first.unmount()

    read.mockRejectedValue(new ApiError(403, 'dashboard owner required'))
    const second = renderWithProviders(<ProjectAwsProfileSection project={PROJECT} />)
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2))
    expect(second.container.querySelector('[data-testid="pp-aws-profile"]')).toBeNull()
  })

  it('renders nothing for an unbound project with no known profile', async () => {
    read.mockResolvedValue(unbound([]))
    const { container } = renderWithProviders(<ProjectAwsProfileSection project={PROJECT} />)
    await waitFor(() => expect(read).toHaveBeenCalled())
    expect(container.querySelector('[data-testid="pp-aws-profile"]')).toBeNull()
  })
})
