import { describe, expect, it, vi } from 'vitest'
import { act, render } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { createTestStore } from '../../test/helpers'
import { setActiveId } from '../../store/instancesSlice'
import { NavigationLeaveGuardProvider, useRegisterNavigationLeaveGuard } from '../../components/NavigationLeaveGuard'
import { useOpenInAppPath } from './openInAppPath'

// The crew-link hook is a unit of its own (`lib/crewDeepLink.test.tsx`); here
// only its place in the order matters, so it answers for any crew link.
const openCrewLink = vi.fn((path: string) => path.includes('crew='))
vi.mock('../../lib/crewDeepLink', async importOriginal => ({
  ...(await importOriginal<typeof import('../../lib/crewDeepLink')>()),
  useCrewDeepLink: () => openCrewLink,
}))

let open: (path: string) => void = () => {}
let here = ''

/** A page holding a draft: its guard refuses to be left. */
function Page({ guard }: { guard: () => boolean }) {
  useRegisterNavigationLeaveGuard(guard)
  open = useOpenInAppPath()
  const location = useLocation()
  here = location.pathname + location.search
  return null
}

function mount(at: string) {
  const guard = vi.fn(() => false)
  render(
    <Provider store={createTestStore()}>
      <MemoryRouter initialEntries={[at]}>
        <NavigationLeaveGuardProvider><Page guard={guard} /></NavigationLeaveGuardProvider>
      </MemoryRouter>
    </Provider>,
  )
  return guard
}

describe('useOpenInAppPath', () => {
  it('does not ask the page about the address already on screen', () => {
    const guard = mount('/schedule?job=nightly')
    act(() => { open('/schedule?job=nightly') })
    expect(guard).not.toHaveBeenCalled()
    expect(here).toBe('/schedule?job=nightly')
  })

  it('brings the local Crew forward for an app detail link before asking the page', () => {
    const store = createTestStore()
    store.dispatch(setActiveId('remote-1'))
    const guard = vi.fn(() => {
      // The leave question is asked with the local Crew already on screen.
      expect(store.getState().instances.activeId).toBeNull()
      return true
    })
    render(
      <Provider store={store}>
        <MemoryRouter initialEntries={['/schedule']}>
          <NavigationLeaveGuardProvider><Page guard={guard} /></NavigationLeaveGuardProvider>
        </MemoryRouter>
      </Provider>,
    )
    act(() => { open('/apps/detail/demo-app') })
    expect(guard).toHaveBeenCalledTimes(1)
    expect(store.getState().instances.activeId).toBeNull()
    expect(here).toBe('/apps/detail/demo-app')
  })

  it('hands a crew link to the crew-link handler before asking the page', () => {
    openCrewLink.mockClear()
    const guard = mount('/schedule')
    act(() => { open('/chat?crew=dev-box&crewSid=chat-3-1785870239') })
    expect(openCrewLink).toHaveBeenCalledWith('/chat?crew=dev-box&crewSid=chat-3-1785870239')
    expect(guard).not.toHaveBeenCalled()
    expect(here).toBe('/schedule')
  })

  it('fills the Remote Crew add form on the local Crew for an add link, after the page agrees', () => {
    const store = createTestStore()
    store.dispatch(setActiveId('remote-1'))
    const guard = vi.fn(() => {
      expect(store.getState().instances.activeId).toBeNull()
      return true
    })
    render(
      <Provider store={store}>
        <MemoryRouter initialEntries={['/schedule']}>
          <NavigationLeaveGuardProvider><Page guard={guard} /></NavigationLeaveGuardProvider>
        </MemoryRouter>
      </Provider>,
    )
    act(() => { open('/settings/instances?host=me%40devbox.example.com&port=5476&name=Dev%20box') })
    expect(guard).toHaveBeenCalledTimes(1)
    expect(store.getState().instances.crewForms?.addLink).toEqual({ host: 'me@devbox.example.com', port: '5476', name: 'Dev box' })
    expect(here).toBe('/settings/instances')
  })

  it('does not ask about a same-page link that only adds a fragment', () => {
    const guard = mount('/settings/display?tab=theme')
    act(() => { open('/settings/display?tab=theme#accent') })
    expect(guard).not.toHaveBeenCalled()
  })

  it('asks the page about another address, and a refusal keeps it', () => {
    const guard = mount('/schedule')
    act(() => { open('/settings/security') })
    expect(guard).toHaveBeenCalledTimes(1)
    expect(here).toBe('/schedule')
  })

  it('asks when only the query differs, because a pane mounts on the query', () => {
    const guard = mount('/schedule?job=a')
    act(() => { open('/schedule?job=b') })
    expect(guard).toHaveBeenCalledTimes(1)
    expect(here).toBe('/schedule?job=a')
  })

  it('does not ask about the session already on screen', () => {
    const guard = mount('/chat?sid=chat-4-1793224442')
    act(() => { open('/chat?sid=chat-4-1793224442') })
    expect(guard).not.toHaveBeenCalled()
  })
})
