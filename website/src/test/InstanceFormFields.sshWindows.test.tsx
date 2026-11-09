import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  EMPTY_INSTANCE_FORM,
  InstanceFormFields,
  useInstanceFormState,
} from '../pages/settings/InstanceFormFields'

const platformEnv = vi.hoisted(() => ({ value: 'other' as 'darwin' | 'windows' | 'other' }))

vi.mock('../hooks/useGatewayPlatform', () => ({
  useGatewayPlatform: () => platformEnv.value,
}))

const device = vi.hoisted(() => ({ touch: false }))

vi.mock('../hooks/useIsTouchDevice', () => ({ useIsTouchDevice: () => device.touch }))

function Harness() {
  const form = useInstanceFormState(EMPTY_INSTANCE_FORM)
  return <InstanceFormFields idPrefix="t" form={form} />
}

function renderForm() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <Harness />
    </QueryClientProvider>,
  )
}

describe('InstanceFormFields ssh transport on a Windows gateway', () => {
  it('says the SSH tunnel is not supported on Windows', () => {
    platformEnv.value = 'windows'
    renderForm()
    expect(screen.getByText(/SSH tunnels are not yet supported when .* runs on Windows/)).toBeTruthy()
    expect(screen.queryByText(/Opens ssh -N -L to the host/)).toBeNull()
    // The badge row keeps the display label, not the raw option value.
    expect(screen.getByRole('combobox', { name: 'Connection method' }).textContent).toContain('SSH tunnel')
  })

  it('names the Windows limit on the SSH row of the touch (native) list', () => {
    platformEnv.value = 'windows'
    device.touch = true
    try {
      renderForm()
      const options = screen.getAllByRole('option').map(o => o.textContent)
      expect(options).toContain('SSH tunnel — Not supported on Windows')
      // Only the SSH row carries it.
      expect(options.filter(t => t?.includes('Not supported on Windows'))).toHaveLength(1)
    } finally {
      device.touch = false
    }
  })

  it('keeps the normal SSH hint elsewhere', () => {
    platformEnv.value = 'other'
    renderForm()
    expect(screen.getByText(/Opens ssh -N -L to the host/)).toBeTruthy()
    expect(screen.queryByText(/not yet supported when .* runs on Windows/)).toBeNull()
  })
})
