import { describe, expect, it, vi } from 'vitest'
import { act, render } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { createTestStore } from '../../test/helpers'
import { NavigationLeaveGuardProvider, useRegisterNavigationLeaveGuard } from '../../components/NavigationLeaveGuard'
import { useOpenInAppPath } from './openInAppPath'

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
