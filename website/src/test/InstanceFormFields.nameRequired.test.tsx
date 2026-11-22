/**
 * An empty Name says why the Add button stays disabled (Bug Bash row 68).
 *
 * The Name field's placeholder is an example name ("Crew 1") that reads as a
 * value, so an otherwise complete form showed a disabled button and no reason.
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import '../i18n'
import { i18nT } from '../i18n/t'
import {
  EMPTY_INSTANCE_FORM,
  InstanceFormFields,
  useInstanceFormState,
} from '../pages/settings/InstanceFormFields'

vi.mock('../hooks/useGatewayPlatform', () => ({ useGatewayPlatform: () => 'other' }))
vi.mock('../hooks/useIsTouchDevice', () => ({ useIsTouchDevice: () => false }))

function Harness() {
  const form = useInstanceFormState(EMPTY_INSTANCE_FORM)
  return <InstanceFormFields idPrefix="t" form={form} />
}

describe('InstanceFormFields Name', () => {
  it('marks Name required and explains an empty one until a name is typed', () => {
    const { container } = render(
      <QueryClientProvider client={new QueryClient()}>
        <Harness />
      </QueryClientProvider>,
    )
    const name = container.querySelector<HTMLInputElement>('#t-name')!
    const hint = i18nT('pages.settings.instancesPanel.name_required_hint')
    expect(name.required).toBe(true)
    expect(screen.getByText(hint)).toBeTruthy()
    expect(name.getAttribute('aria-describedby')).toBe('t-name-hint')

    fireEvent.change(name, { target: { value: 'build box' } })
    expect(screen.queryByText(hint)).toBeNull()
    expect(name.getAttribute('aria-describedby')).toBeNull()
  })
})
