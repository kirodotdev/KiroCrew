import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import MigrationBanner from './MigrationBanner'

const navigate = vi.fn()
vi.mock('react-router-dom', async importOriginal => {
  const mod = await importOriginal<typeof import('react-router-dom')>()
  return { ...mod, useNavigate: () => navigate }
})

describe('MigrationBanner', () => {
  beforeEach(() => navigate.mockReset())

  it('strips the registry prefix when routing to the app detail page', () => {
    renderWithProviders(<MigrationBanner appName="Zzq App" migratedTo="registry:zzq-pkg" />)
    fireEvent.click(screen.getByRole('button'))
    expect(navigate).toHaveBeenCalledWith('/apps/detail/zzq-pkg')
  })

  it('keeps every segment after the first colon', () => {
    renderWithProviders(<MigrationBanner appName="Zzq App" migratedTo="standalone:a:b" />)
    fireEvent.click(screen.getByRole('button'))
    expect(navigate).toHaveBeenCalledWith('/apps/detail/a%3Ab')
  })

  it('uses the bare value when there is no prefix', () => {
    renderWithProviders(<MigrationBanner appName="Zzq App" migratedTo="zzq-bare" />)
    fireEvent.click(screen.getByRole('button'))
    expect(navigate).toHaveBeenCalledWith('/apps/detail/zzq-bare')
  })

  it('names the app being deprecated', () => {
    renderWithProviders(<MigrationBanner appName="Zzq App" migratedTo="registry:zzq-pkg" />)
    expect(screen.getByText(/Zzq App/)).toBeInTheDocument()
  })
})
