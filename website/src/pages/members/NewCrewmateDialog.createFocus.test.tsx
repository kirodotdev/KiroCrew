// The New crewmate dialog keeps keyboard focus on the control that started the
// create while the request is out.
//
// The form locks while the POST is in flight so no edit can land after the body
// was sent. Native `disabled` on the pressed control blurs it to <body>, outside
// the dialog, so a keyboard or screen reader user loses their place until the
// create answers, and on a failure the error notice renders in a dialog that no
// longer holds focus. The pressed control is held with aria-disabled instead,
// fields refuse edits while held, and focus stays inside the dialog.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from '../../test/helpers'
import { api } from '../../api/client'
import NewCrewmateDialog from './NewCrewmateDialog'

vi.mock('../../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      agentCatalog: vi.fn(() => Promise.resolve({ agents: [], default_agent: 'kirocrew' })),
      workspaces: vi.fn(() => Promise.resolve({ workspaces: [{ name: 'default' }] })),
      availableModels: vi.fn(() => Promise.resolve({ models: [] })),
      members: vi.fn(() => Promise.resolve({ members: [] })),
      createKirocrewAgent: vi.fn(),
    },
  }
})

const props = { onClose: vi.fn(), onCreated: vi.fn(), existingNames: [] as string[] }

/** A create whose promise the test holds open, so the dialog sits busy. */
function deferredCreate() {
  let settle!: (v: unknown) => void
  let reject!: (e: unknown) => void
  const p = new Promise((res, rej) => { settle = res; reject = rej })
  vi.mocked(api.createKirocrewAgent).mockReturnValue(p as ReturnType<typeof api.createKirocrewAgent>)
  return { settle, reject }
}

const dialog = () => screen.getByRole('dialog')
const nameField = () => screen.getByRole('textbox', { name: 'Name' })
const submitBtn = () => screen.getByTestId('crewmate-create-submit')

describe('NewCrewmateDialog keeps focus on the control that started the create', () => {
  beforeEach(() => {
    props.onClose = vi.fn()
    props.onCreated = vi.fn()
    vi.mocked(api.createKirocrewAgent).mockReset()
  })
  afterEach(() => { vi.restoreAllMocks() })

  it('Enter in the Name field: the field keeps focus, held with aria-disabled, not native disabled', async () => {
    deferredCreate()
    const user = userEvent.setup()
    renderWithProviders(<NewCrewmateDialog open {...props} />)
    await screen.findByTestId('crewmate-create-form')

    const name = nameField()
    await user.click(name)
    await user.keyboard('Scout{Enter}')

    await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))
    // The field that submitted keeps focus, inside the dialog, held as
    // unavailable but never blurred to <body>.
    expect(document.activeElement).toBe(name)
    expect(dialog()).toContainElement(document.activeElement as HTMLElement)
    expect(name.getAttribute('aria-disabled')).toBe('true')
    expect(name.hasAttribute('disabled')).toBe(false)
  })

  it('Create button pressed: the button keeps focus, held with aria-disabled and aria-busy', async () => {
    deferredCreate()
    const user = userEvent.setup()
    renderWithProviders(<NewCrewmateDialog open {...props} />)
    await screen.findByTestId('crewmate-create-form')

    await user.type(nameField(), 'Scout')
    const create = submitBtn()
    create.focus()
    await user.keyboard('{Enter}')

    await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))
    expect(document.activeElement).toBe(create)
    expect(dialog()).toContainElement(document.activeElement as HTMLElement)
    expect(create.getAttribute('aria-disabled')).toBe('true')
    expect(create.getAttribute('aria-busy')).toBe('true')
    expect(create.hasAttribute('disabled')).toBe(false)
  })

  it('Enter in an Advanced text field (Job) keeps focus on that field, held not disabled', async () => {
    deferredCreate()
    const user = userEvent.setup()
    renderWithProviders(<NewCrewmateDialog open startExpanded {...props} />)
    await screen.findByTestId('crewmate-create-form')

    // A name is required for the submit to start the create.
    await user.type(nameField(), 'Scout')
    const job = screen.getByRole('textbox', { name: 'Goal' }) as HTMLInputElement
    await user.click(job)
    await user.keyboard('watch CI{Enter}')

    await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))
    // Enter in Job implicitly submitted the form; the field it submitted from
    // keeps focus inside the dialog, held rather than blurred to <body>.
    expect(document.activeElement).toBe(job)
    expect(dialog()).toContainElement(document.activeElement as HTMLElement)
    expect(job.getAttribute('aria-disabled')).toBe('true')
    expect(job.readOnly).toBe(true)
    expect(job.hasAttribute('disabled')).toBe(false)
  })

  it('refuses an edit to the Name field while the create is out', async () => {
    deferredCreate()
    const user = userEvent.setup()
    renderWithProviders(<NewCrewmateDialog open {...props} />)
    await screen.findByTestId('crewmate-create-form')

    const name = nameField() as HTMLInputElement
    await user.type(name, 'Scout')
    submitBtn().focus()
    await user.keyboard('{Enter}')
    await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))

    // Typing more while the request is out changes nothing.
    await user.click(name)
    await user.keyboard('X')
    expect(name.value).toBe('Scout')
    // A second submit is refused while the first is still out.
    submitBtn().focus()
    await user.keyboard('{Enter}')
    expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1)
  })

  it('on a failure the error notice shows and focus stays on the control inside the dialog', async () => {
    const { reject } = deferredCreate()
    const user = userEvent.setup()
    renderWithProviders(<NewCrewmateDialog open {...props} />)
    await screen.findByTestId('crewmate-create-form')

    await user.type(nameField(), 'Scout')
    const create = submitBtn()
    create.focus()
    await user.keyboard('{Enter}')
    await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))

    reject(new Error('network down'))

    // The notice renders, the lock releases, and focus never left the dialog.
    await screen.findByTestId('crewmate-create-error')
    expect(dialog()).toContainElement(document.activeElement as HTMLElement)
    await waitFor(() => expect(submitBtn().getAttribute('aria-disabled')).not.toBe('true'))
    expect(nameField().getAttribute('aria-disabled')).not.toBe('true')
  })
})
