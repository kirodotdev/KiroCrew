import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { SecretsPanel } from './SecretsPanel'
import { rotationDue } from './RotateByNote'
import { __resetSecondClockForTests } from '../../hooks/useSecondClock'

// SettingsSubNav measures its container; a fixed wide width keeps every case in
// the two-pane layout, where the rail and the selected pane are both on screen.
vi.mock('../../hooks/useContainerWidth', () => ({
  useContainerWidth: () => [{ current: null }, 1200],
}))
vi.mock('../../hooks/useIsMobile', () => ({ useIsMobile: () => false, useIsNarrowViewport: () => false }))

/**
 * The rotate-by date on the list-detail Secrets panel: a field on the add and
 * edit forms, a "Rotation due" chip on the entry's row and in its open pane once
 * the date has passed, and the date sent with a save. The list endpoint is the
 * global `fetch` the shared `api` client issues, stubbed with the fields that
 * transport reads.
 */
type Call = { method: string; url: string; body?: unknown }

let calls: Call[] = []
let listBody: Record<string, unknown> = {}

function respond(url: string, body: unknown): Response {
  return {
    ok: true,
    status: 200,
    url,
    headers: { get: () => null },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as unknown as Response
}

/** A list answer: the names, their plain-secret rows, and the dates by name. */
function list(names: string[], rotateBy?: Record<string, string>, managed: unknown[] = [], items?: unknown[]) {
  return {
    names,
    items: items ?? names.map(name => ({ name, type: 'secret', revision: `r-${name}` })),
    managed,
    ...(rotateBy ? { rotate_by: rotateBy } : {}),
  }
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <MemoryRouter initialEntries={['/settings']}>
      <QueryClientProvider client={qc}>
        <SecretsPanel />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

/** The rail row for *name*, found by its label. */
async function rowOf(name: string) {
  return screen.findByRole('option', { name: new RegExp(`^${name}\\b`) })
}

// Fixed dates far from any run date, so no clock is read for the assertion.
const PAST = '2001-01-01'
const FUTURE = '2999-12-31'
// The same days as the viewer's language writes them.
const PAST_SHOWN = 'Jan 1, 2001'
const FUTURE_SHOWN = 'Dec 31, 2999'

beforeEach(() => {
  calls = []
  listBody = list([])
  localStorage.setItem('kiro_crew_token', 'test-token')
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    if (method === 'GET' && url === '/api/secrets') return respond(url, listBody)
    if (method === 'GET' && url === '/api/secrets/usage') return respond(url, { usage: {}, incomplete: [] })
    return respond(url, { ok: true })
  }))
})

afterEach(() => {
  vi.useRealTimers()
  __resetSecondClockForTests()
  vi.unstubAllGlobals()
  localStorage.clear()
})

describe('SecretsPanel rotate-by dates', () => {
  it('flags an entry whose date has passed on its row and in its open pane', async () => {
    const user = userEvent.setup()
    listBody = list(['OLD_KEY'], { OLD_KEY: PAST })
    mount()
    const row = await rowOf('OLD_KEY')
    expect(within(row).getByText('Rotation due')).toBeInTheDocument()
    await user.click(row)
    expect(await screen.findByText(`Rotate by ${PAST_SHOWN}`)).toBeInTheDocument()
    const chip = screen.getByRole('note', {
      name: `Rotation due for OLD_KEY: the rotate-by date ${PAST_SHOWN} has passed`,
    })
    expect(chip).toHaveTextContent('Rotation due')
  })

  it('shows a future date in the pane, with no chip on the row or in the pane', async () => {
    const user = userEvent.setup()
    listBody = list(['NEW_KEY'], { NEW_KEY: FUTURE })
    mount()
    const row = await rowOf('NEW_KEY')
    await user.click(row)
    expect(await screen.findByText(`Rotate by ${FUTURE_SHOWN}`)).toBeInTheDocument()
    expect(screen.queryByText('Rotation due')).not.toBeInTheDocument()
  })

  it('renders an entry with no date exactly as before', async () => {
    const user = userEvent.setup()
    listBody = list(['PLAIN_KEY'])
    mount()
    await user.click(await rowOf('PLAIN_KEY'))
    await screen.findByRole('button', { name: 'Edit' })
    expect(screen.queryByText(/Rotate by/)).not.toBeInTheDocument()
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
    expect(screen.queryByText('Rotation due')).not.toBeInTheDocument()
  })

  it('flags a managed credential on its row and in its pane', async () => {
    const user = userEvent.setup()
    listBody = list(
      ['JIRA_API_TOKEN'],
      { JIRA_API_TOKEN: PAST },
      [{ name: 'JIRA_API_TOKEN', kind: 'jira_api_token' }],
    )
    mount()
    const row = await rowOf('JIRA_API_TOKEN')
    expect(within(row).getByText('Rotation due')).toBeInTheDocument()
    await user.click(row)
    expect(
      await screen.findByRole('note', {
        name: `Rotation due for JIRA_API_TOKEN: the rotate-by date ${PAST_SHOWN} has passed`,
      }),
    ).toBeInTheDocument()
  })

  it('reads a date only from the entry’s own record, never from an inherited member', async () => {
    // Object.prototype names: a plain lookup on the date map finds a member the
    // owner never set and would print it as a date.
    const user = userEvent.setup()
    listBody = list(['constructor', '__proto__', 'toString'], { OTHER: PAST })
    mount()
    await user.click(await rowOf('constructor'))
    await screen.findByRole('button', { name: 'Edit' })
    expect(screen.queryAllByText(/Rotate by/)).toHaveLength(0)
    expect(screen.queryAllByText('Rotation due')).toHaveLength(0)
    expect(screen.queryAllByRole('note')).toHaveLength(0)
  })

  it('does the same on a managed credential row', async () => {
    const user = userEvent.setup()
    listBody = list(['constructor'], undefined, [{ name: 'constructor', kind: 'jira_api_token' }])
    mount()
    await user.click(await rowOf('constructor'))
    await screen.findAllByText('constructor')
    expect(screen.queryAllByText(/Rotate by/)).toHaveLength(0)
    expect(screen.queryAllByText('Rotation due')).toHaveLength(0)
  })

  it('still shows a real date for a name that is also an Object.prototype member', async () => {
    const user = userEvent.setup()
    listBody = list(['toString'], { toString: PAST })
    mount()
    const row = await rowOf('toString')
    expect(within(row).getByText('Rotation due')).toBeInTheDocument()
    await user.click(row)
    expect(await screen.findByText(`Rotate by ${PAST_SHOWN}`)).toBeInTheDocument()
  })

  it('shows the chip on the row once local midnight passes the date, with no remount', async () => {
    // Only the date is faked: the list still loads on real timers, and the
    // shared one-second clock ticks for real while the system day moves on.
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date(2026, 5, 15, 23, 59, 59))
    listBody = list(['EDGE_KEY'], { EDGE_KEY: '2026-06-15' })
    mount()
    const row = await rowOf('EDGE_KEY')
    expect(within(row).queryByText('Rotation due')).not.toBeInTheDocument()

    vi.setSystemTime(new Date(2026, 5, 16, 0, 0, 1))
    await waitFor(() => expect(within(row).getByText('Rotation due')).toBeInTheDocument(), { timeout: 4000 })
  })

  it('tells the owner what the date does and that changing or removing it needs the value entered again', async () => {
    const user = userEvent.setup()
    mount()
    await user.click(await screen.findByRole('option', { name: /Add secret/ }))
    expect(
      await screen.findByText(
        'Shows “Rotation due” after this date. To change or remove the date later, enter the value again and save.',
      ),
    ).toBeInTheDocument()
  })

  it('sends the date the owner picks with a new entry', async () => {
    const user = userEvent.setup()
    mount()
    await user.type(await screen.findByLabelText('Secret name'), 'DATED_KEY')
    await user.type(screen.getByLabelText('Secret value'), 'synthetic-value')
    fireEvent.change(screen.getByLabelText('Rotate by (optional)'), { target: { value: FUTURE } })
    expect(screen.getByText(`Rotate by ${FUTURE_SHOWN}`)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => {
      expect(calls.find(c => c.method === 'POST')?.body).toEqual({
        name: 'DATED_KEY',
        type: 'secret',
        fields: { value: 'synthetic-value' },
        rotate_by: FUTURE,
      })
    })
  })

  it('sends no date with a new entry when the field is left empty', async () => {
    const user = userEvent.setup()
    mount()
    expect(await screen.findByLabelText('Rotate by (optional)')).toHaveValue('')
    await user.type(screen.getByLabelText('Secret name'), 'PLAIN_KEY')
    await user.type(screen.getByLabelText('Secret value'), 'synthetic-value')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => {
      expect(calls.find(c => c.method === 'POST')?.body).toEqual({
        name: 'PLAIN_KEY',
        type: 'secret',
        fields: { value: 'synthetic-value' },
      })
    })
  })

  it('previews only the dated note under the field, with no chip, for a date that has already passed', async () => {
    mount()
    fireEvent.change(await screen.findByLabelText('Rotate by (optional)'), { target: { value: PAST } })
    expect(await screen.findByText(`Rotate by ${PAST_SHOWN}`)).toBeInTheDocument()
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
    expect(screen.queryByText('Rotation due')).not.toBeInTheDocument()
  })

  it('keeps Save off when only the date of a plain secret changed, and the hint names why', async () => {
    const user = userEvent.setup()
    listBody = list(['PLAIN_KEY'], { PLAIN_KEY: FUTURE })
    mount()
    await user.click(await rowOf('PLAIN_KEY'))
    await user.click(await screen.findByRole('button', { name: 'Edit' }))
    fireEvent.change(screen.getByLabelText('Rotate by (optional)'), { target: { value: '2998-01-02' } })
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    expect(screen.getByText(/enter the value again and save/)).toBeInTheDocument()
    expect(screen.queryByText('Rotation due')).not.toBeInTheDocument()
    await user.type(screen.getByLabelText('Secret value'), 'synthetic-value')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled())
  })

  it('starts an edit on the stored date and sends it back when the owner leaves it', async () => {
    const user = userEvent.setup()
    listBody = list(
      ['MEMO'],
      { MEMO: FUTURE },
      [],
      [{ name: 'MEMO', type: 'note', revision: 'r1' }],
    )
    mount()
    await user.click(await rowOf('MEMO'))
    await user.click(await screen.findByRole('button', { name: 'Edit' }))
    expect(screen.getByLabelText('Rotate by (optional)')).toHaveValue(FUTURE)
    await user.type(screen.getByLabelText('Text'), 'new text')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => {
      expect(calls.find(c => c.method === 'POST')?.body).toEqual({
        name: 'MEMO',
        type: 'note',
        fields: { text: 'new text' },
        rotate_by: FUTURE,
      })
    })
  })

  it('sends no date when the owner clears the stored one, which removes the reminder', async () => {
    const user = userEvent.setup()
    listBody = list(
      ['MEMO'],
      { MEMO: PAST },
      [],
      [{ name: 'MEMO', type: 'note', revision: 'r1' }],
    )
    mount()
    await user.click(await rowOf('MEMO'))
    await user.click(await screen.findByRole('button', { name: 'Edit' }))
    fireEvent.change(screen.getByLabelText('Rotate by (optional)'), { target: { value: '' } })
    await user.type(screen.getByLabelText('Text'), 'new text')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => {
      expect(calls.find(c => c.method === 'POST')?.body).toEqual({
        name: 'MEMO',
        type: 'note',
        fields: { text: 'new text' },
      })
    })
  })

  it('compares dates by day in the viewer’s own calendar', () => {
    expect(rotationDue('2026-11-14', '2026-11-15')).toBe(true)
    expect(rotationDue('2026-11-15', '2026-11-15')).toBe(false)
    expect(rotationDue('2026-11-16', '2026-11-15')).toBe(false)
  })
})
