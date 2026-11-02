import { describe, it, expect, vi, beforeEach, onTestFinished } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { WeixinPanel } from '../pages/settings/WeixinPanel'
import { api, type WeixinConfigData } from '../api/client'

const CONFIG: WeixinConfigData = {
  connected: false,
  connect_error: '',
  configured: false,
  read_only: false,
  credential_set: false,
  enabled: true,
  account_id: '',
  dm_policy: 'open',
  allowed_user_ids: [],
}

const QR_TTL_MS = 5 * 60 * 1000
// Chain: QR start mutation -> 'waiting' phase -> status query enabled with
// refetchInterval -> effect maps the polled status to the phase. Each step runs
// after the one before it, so the waits on this chain need a longer timeout.
const QR_STATUS_CHAIN = { timeout: 4000 }

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(api, 'getWeixinConfig').mockResolvedValue({ ...CONFIG })
})

async function startSignIn() {
  vi.spyOn(api, 'weixinQrStart').mockResolvedValue({
    session_id: 'sess-1',
    qrcode_img_content: 'data:image/png;base64,AAAA',
  })
  renderWithProviders(<WeixinPanel />)
  fireEvent.click(await screen.findByTestId('weixin-connect'))
  await waitFor(() => expect(screen.getByText('Waiting for scan…')).toBeInTheDocument(), QR_STATUS_CHAIN)
}

describe('WeixinPanel — QR sign-in outcomes', () => {
  it('reports a confirmed scan and reloads the saved config', async () => {
    const status = vi
      .spyOn(api, 'weixinQrStatus')
      .mockResolvedValue({ status: 'confirmed', connected: true, account_id: 'a5ace6fd482e@im.bot' })
    await startSignIn()

    await waitFor(() => expect(screen.getByTestId('weixin-confirmed')).toBeInTheDocument(), QR_STATUS_CHAIN)
    expect(status).toHaveBeenCalledWith('sess-1')
    // Confirming invalidates the config query, so the panel re-reads the saved
    // config (a second call beyond the initial load).
    await waitFor(() => expect(api.getWeixinConfig).toHaveBeenCalledTimes(2), QR_STATUS_CHAIN)
    expect(screen.queryByText('Waiting for scan…')).toBeNull()
  })

  it('treats connected: true as signed in even while the status still says waiting', async () => {
    vi.spyOn(api, 'weixinQrStatus').mockResolvedValue({ status: 'waiting', connected: true })
    await startSignIn()
    await waitFor(() => expect(screen.getByTestId('weixin-confirmed')).toBeInTheDocument(), QR_STATUS_CHAIN)
  })

  it('shows the scanned prompt while the code waits for confirmation in WeChat', async () => {
    vi.spyOn(api, 'weixinQrStatus').mockResolvedValue({ status: 'scaned' })
    await startSignIn()
    await waitFor(() => expect(screen.getByText(/confirm in wechat/i)).toBeInTheDocument(), QR_STATUS_CHAIN)
    expect(screen.queryByTestId('weixin-confirmed')).toBeNull()
    expect(screen.getByTestId('weixin-connect')).toBeDisabled()
  })

  it('says the code expired when the service reports it expired', async () => {
    vi.spyOn(api, 'weixinQrStatus').mockResolvedValue({ status: 'expired' })
    await startSignIn()
    await waitFor(() => expect(screen.getByTestId('weixin-expired')).toBeInTheDocument(), QR_STATUS_CHAIN)
    expect(screen.getByText('The code expired. Try again.')).toBeInTheDocument()
    expect(screen.queryByText('Waiting for scan…')).toBeNull()
    expect(screen.getByTestId('weixin-connect')).toBeEnabled()
  })

  it('expires an unscanned code once its five-minute window passes', async () => {
    vi.spyOn(api, 'weixinQrStatus').mockResolvedValue({ status: 'wait' })
    // The expiry timer is scheduled when the phase becomes 'waiting', so the
    // fake clock must be installed before the sign-in starts. shouldAdvanceTime
    // keeps the clock moving for the waits inside startSignIn.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    onTestFinished(() => vi.useRealTimers())
    await startSignIn()

    // Move the wall clock past the code's deadline, then let the pending expiry
    // timer fire. The timer's own due time sits exactly on the deadline, so the
    // clock has to be ahead of it when the timer runs.
    vi.setSystemTime(Date.now() + QR_TTL_MS + 1000)
    act(() => {
      vi.advanceTimersByTime(QR_TTL_MS)
    })
    await waitFor(() => expect(screen.getByTestId('weixin-expired')).toBeInTheDocument())

    expect(screen.getByTestId('weixin-expired')).toBeInTheDocument()
    expect(screen.queryByText('Waiting for scan…')).toBeNull()
  })
})

describe('WeixinPanel — allowlist editor', () => {
  it('saves the allowed user IDs the operator adds to the list', async () => {
    vi.spyOn(api, 'getWeixinConfig').mockResolvedValue({ ...CONFIG, dm_policy: 'allowlist', allowed_user_ids: [] })
    const save = vi
      .spyOn(api, 'saveWeixinConfig')
      .mockResolvedValue({ ok: true, restart_required: false })
    renderWithProviders(<WeixinPanel />)

    const allowlist = await screen.findByTestId('weixin-allowlist')
    const input = allowlist.querySelector('input')!
    fireEvent.change(input, { target: { value: 'wxid_alice' } })
    fireEvent.click(screen.getByRole('button', { name: /add/i }))

    await waitFor(() => expect(save).toHaveBeenCalledWith({ allowed_user_ids: ['wxid_alice'] }))
  })
})
