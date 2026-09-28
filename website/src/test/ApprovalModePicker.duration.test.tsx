
vi.mock("@radix-ui/react-dropdown-menu", async () => await import("./__mocks__/@radix-ui/react-dropdown-menu"))
vi.mock('../api/client', () => ({
  api: { chatMode: vi.fn().mockResolvedValue({}) },
}))

import { render, screen, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import ApprovalModePicker from '../components/ApprovalModePicker'
import { sseStatus } from '../store/dashboardSlice'
import type { StatusData } from '../types'
import { createTestStore } from './helpers'

/** The HTTP `/api/status` reply is the only status source that carries the
 *  configured ad-hoc duration; the 5-second WebSocket `dashboard` frame is
 *  built from the shared `status_snapshot` and has no `yolo_duration` key. */
const HTTP_STATUS = {
  uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
  yolo: false, yolo_duration: '1h', yolo_until_shutdown_permitted: true,
} as StatusData
const WS_FRAME = {
  uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
} as StatusData

function openYoloConfirm(store: ReturnType<typeof createTestStore>) {
  render(
    <Provider store={store}>
      <MemoryRouter>
        <ApprovalModePicker mode="normal" slotKey="dashboard:1" />
      </MemoryRouter>
    </Provider>,
  )
  fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
  fireEvent.click(screen.getAllByRole('menuitem')[3]) // YOLO
}

describe('ApprovalModePicker YOLO confirm copy names the configured duration', () => {
  beforeEach(() => localStorage.clear())

  it('reads the duration from the HTTP status', () => {
    const store = createTestStore()
    store.dispatch(sseStatus(HTTP_STATUS))
    openYoloConfirm(store)
    expect(screen.getByText('Turns itself off after 1 hour, then asks again.')).toBeInTheDocument()
  })

  it('keeps the configured duration after a WebSocket status frame lands', () => {
    const store = createTestStore()
    store.dispatch(sseStatus(HTTP_STATUS))
    store.dispatch(sseStatus(WS_FRAME))
    openYoloConfirm(store)
    expect(screen.getByText('Turns itself off after 1 hour, then asks again.')).toBeInTheDocument()
    expect(screen.queryByText(/6 hours/)).not.toBeInTheDocument()
  })

  it('keeps until_shutdown copy after a WebSocket status frame lands', () => {
    const store = createTestStore()
    store.dispatch(sseStatus({ ...HTTP_STATUS, yolo_duration: 'until_shutdown' }))
    store.dispatch(sseStatus(WS_FRAME))
    openYoloConfirm(store)
    expect(screen.queryByText(/6 hours/)).not.toBeInTheDocument()
    expect(store.getState().dashboard.status?.yolo_duration).toBe('until_shutdown')
  })
})
