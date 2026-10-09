import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { NavigationLeaveGuardProvider, useMayLeaveForNavigation } from '../../components/NavigationLeaveGuard'
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
      createKirocrewAgent: vi.fn(() => Promise.resolve({ ok: true, name: 'Scout' })),
    },
  }
})

let mayLeave: () => boolean = () => true
function Probe() {
  mayLeave = useMayLeaveForNavigation()
  return null
}
const unloadPrevented = () => {
  const ev = new Event('beforeunload', { cancelable: true })
  window.dispatchEvent(ev)
  return ev.defaultPrevented
}
const props = { onClose: vi.fn(), onCreated: vi.fn(), existingNames: [] as string[] }
const typeName = (v: string) => fireEvent.change(screen.getByRole('textbox', { name: 'Name' }), { target: { value: v } })

describe('NewCrewmateDialog', () => {
  beforeEach(() => {
    props.onClose = vi.fn()
    props.onCreated = vi.fn()
    vi.mocked(api.createKirocrewAgent).mockClear()
  })
  afterEach(() => { vi.restoreAllMocks() })

  it('standalone: still a modal dialog', async () => {
    renderWithProviders(<NewCrewmateDialog open {...props} />)
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toContainElement(screen.getByTestId('crewmate-create-form'))
    expect(screen.queryByTestId('crewmate-create-embedded')).toBeNull()
  })

  it('embedded: the same complete form in a labelled page region, no dialog, no Escape dismissal', async () => {
    const { container } = renderWithProviders(<NewCrewmateDialog open embedded {...props} />)
    const region = await screen.findByRole('region', { name: 'New crewmate' })
    expect(container.contains(region)).toBe(true)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(region).toContainElement(screen.getByTestId('crewmate-create-form'))
    expect(region).toContainElement(screen.getByTestId('crewmate-create-submit'))
    // The expert fields are still there behind Advanced.
    fireEvent.click(screen.getByTestId('crewmate-create-advanced-toggle'))
    expect(screen.getByTestId('crewmate-create-advanced')).toBeInTheDocument()
    const esc = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    document.dispatchEvent(esc)
    expect(props.onClose).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(props.onClose).toHaveBeenCalledTimes(1)
  })

  it('embedded: renders nothing while closed and submits the same create', async () => {
    const { rerender } = renderWithProviders(<NewCrewmateDialog open={false} embedded {...props} />)
    expect(screen.queryByTestId('crewmate-create-embedded')).toBeNull()
    rerender(<NewCrewmateDialog open embedded {...props} />)
    await screen.findByTestId('crewmate-create-embedded')
    typeName('Scout')
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api.createKirocrewAgent).mock.calls[0][0]).toMatchObject({ name: 'Scout', kiro_agent: 'kirocrew' })
    await waitFor(() => expect(props.onCreated).toHaveBeenCalledWith({ name: 'Scout', job: '' }), { timeout: 4000 })
  })

  it.each([false, true])('a typed draft asks before leaving and before unload (embedded=%s); a clean form does not', async (embedded) => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderWithProviders(
      <NavigationLeaveGuardProvider>
        <Probe />
        <NewCrewmateDialog open embedded={embedded} {...props} />
      </NavigationLeaveGuardProvider>,
    )
    await screen.findByTestId('crewmate-create-form')
    expect(mayLeave()).toBe(true)
    expect(unloadPrevented()).toBe(false)
    typeName('Scout')
    expect(unloadPrevented()).toBe(true)
    expect(mayLeave()).toBe(false)
    expect(confirm).toHaveBeenCalledTimes(1)
  })
})
