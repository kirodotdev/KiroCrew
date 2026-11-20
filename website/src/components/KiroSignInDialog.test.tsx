import { fireEvent, screen } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import { KiroSignInStrip } from './KiroSignInDialog'
import { copyCode } from '../utils/clipboard'

vi.mock('../utils/clipboard', async importOriginal => {
  const mod = await importOriginal<typeof import('../utils/clipboard')>()
  return { ...mod, copyCode: vi.fn() }
})

const copy = vi.mocked(copyCode)
const ID = '69c7830a-1b2c-4d5e'

describe('KiroSignInStrip request id copy', () => {
  beforeEach(() => copy.mockReset())

  it('a copy that fails says so and shows the id to select by hand', async () => {
    copy.mockResolvedValue(false)
    renderWithProviders(<KiroSignInStrip title="Sign in to Kiro to keep chatting" requestId={ID} />)
    fireEvent.click(screen.getByTestId('kiro-sign-in-copy-request-id'))
    // The copy answered false: the failure and the id appear.
    expect(await screen.findByTestId('kiro-sign-in-copy-request-id-failed')).toHaveTextContent(ID)
  })

  it('a copy that works shows no failure', async () => {
    copy.mockResolvedValue(true)
    renderWithProviders(<KiroSignInStrip title="Sign in to Kiro to keep chatting" requestId={ID} />)
    fireEvent.click(screen.getByTestId('kiro-sign-in-copy-request-id'))
    // The copy answered true.
    await vi.waitFor(() => expect(copy).toHaveBeenCalledWith(ID))
    expect(screen.queryByTestId('kiro-sign-in-copy-request-id-failed')).toBeNull()
  })
})
