import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { SecretsPanelLegacy } from './SecretsPanelLegacy'
import { rotationDue } from './RotateByNote'

/**
 * The rotate-by date on the single-page Secrets panel: shown under an entry that
 * carries one, flagged once it has passed, and sent with a new value when the
 * owner picks one. The list endpoint is the global `fetch` the shared `api`
 * client issues, stubbed with the fields that transport reads.
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

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <SecretsPanelLegacy />
    </QueryClientProvider>,
  )
}

// Fixed dates far from any run date, so no clock is read for the assertion.
const PAST = '2001-01-01'
const FUTURE = '2999-12-31'
// The same days as the viewer's language writes them.
const PAST_SHOWN = '1/1/2001'
const FUTURE_SHOWN = '12/31/2999'

beforeEach(() => {
  calls = []
  listBody = { names: [], managed: [] }
  localStorage.setItem('kiro_crew_token', 'test-token')
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    return respond(url, method === 'GET' && url === '/api/secrets' ? listBody : { ok: true })
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})

describe('SecretsPanelLegacy rotate-by dates', () => {
  it('flags an entry whose rotate-by date has passed, with an accessible label', async () => {
    listBody = { names: ['OLD_KEY'], managed: [], rotate_by: { OLD_KEY: PAST } }
    mount()
    await screen.findByText('OLD_KEY')
    expect(screen.getByText(`Rotate by ${PAST_SHOWN}`)).toBeInTheDocument()
    const chip = screen.getByRole('note', {
      name: `Rotation due for OLD_KEY: the rotate-by date ${PAST_SHOWN} has passed`,
    })
    expect(chip).toHaveTextContent('Rotation due')
  })

  it('shows a future date without the chip', async () => {
    listBody = { names: ['NEW_KEY'], managed: [], rotate_by: { NEW_KEY: FUTURE } }
    mount()
    await screen.findByText('NEW_KEY')
    expect(screen.getByText(`Rotate by ${FUTURE_SHOWN}`)).toBeInTheDocument()
    expect(screen.queryByText('Rotation due')).not.toBeInTheDocument()
  })

  it('renders an entry with no date exactly as before', async () => {
    listBody = { names: ['PLAIN_KEY'], managed: [] }
    mount()
    await screen.findByText('PLAIN_KEY')
    expect(screen.queryByText(/Rotate by/)).not.toBeInTheDocument()
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
  })

  it('flags a managed credential too', async () => {
    listBody = {
      names: ['JIRA_API_TOKEN'],
      managed: [{ name: 'JIRA_API_TOKEN', kind: 'jira_api_token' }],
      rotate_by: { JIRA_API_TOKEN: PAST },
    }
    mount()
    expect(
      await screen.findByRole('note', {
        name: `Rotation due for JIRA_API_TOKEN: the rotate-by date ${PAST_SHOWN} has passed`,
      }),
    ).toBeInTheDocument()
  })

  it('reads a date only from the entry’s own record, never from an inherited member', async () => {
    // Object.prototype names: a plain lookup on the date map finds a member the
    // owner never set and would print it as a date.
    listBody = { names: ['constructor', '__proto__', 'toString'], managed: [], rotate_by: { OTHER: PAST } }
    mount()
    await screen.findByText('constructor')
    expect(screen.getByText('toString')).toBeInTheDocument()
    expect(screen.queryAllByText(/Rotate by/)).toHaveLength(0)
    expect(screen.queryAllByRole('note')).toHaveLength(0)
  })

  it('does the same on a managed credential row', async () => {
    listBody = {
      names: ['constructor'],
      managed: [{ name: 'constructor', kind: 'jira_api_token' }],
    }
    mount()
    await screen.findAllByText('constructor')
    expect(screen.queryAllByText(/Rotate by/)).toHaveLength(0)
    expect(screen.queryAllByRole('note')).toHaveLength(0)
  })

  it('still shows a real date for a name that is also an Object.prototype member', async () => {
    listBody = { names: ['toString'], managed: [], rotate_by: { toString: PAST } }
    mount()
    await screen.findByText('toString')
    expect(screen.getByText(`Rotate by ${PAST_SHOWN}`)).toBeInTheDocument()
  })

  it('tells the owner what the date does, how to rotate and what clears the reminder', async () => {
    const user = userEvent.setup()
    mount()
    await screen.findByText('No secrets stored yet.')
    await user.click(screen.getByRole('button', { name: 'Add secret' }))
    expect(
      screen.getByText(
        'Shows “Rotation due” after this date. To rotate, save a new value under the same name here. Saving without a date removes the reminder.',
      ),
    ).toBeInTheDocument()
  })

  it('sends the date the owner picks with the new value, and none when left empty', async () => {
    const user = userEvent.setup()
    mount()
    await screen.findByText('No secrets stored yet.')
    await user.click(screen.getByRole('button', { name: 'Add secret' }))
    await user.type(screen.getByLabelText('Secret name'), 'DATED_KEY')
    await user.type(screen.getByLabelText('Secret value'), 'synthetic-value')
    fireEvent.change(screen.getByLabelText('Rotate by (optional)'), { target: { value: FUTURE } })
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => {
      expect(calls.find(c => c.method === 'POST')?.body).toEqual({
        name: 'DATED_KEY',
        value: 'synthetic-value',
        rotate_by: FUTURE,
      })
    })

    calls = []
    await user.click(await screen.findByRole('button', { name: 'Add secret' }))
    expect(screen.getByLabelText('Rotate by (optional)')).toHaveValue('')
    await user.type(screen.getByLabelText('Secret name'), 'PLAIN_KEY')
    await user.type(screen.getByLabelText('Secret value'), 'synthetic-value')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => {
      expect(calls.find(c => c.method === 'POST')?.body).toEqual({
        name: 'PLAIN_KEY',
        value: 'synthetic-value',
      })
    })
  })

  it('compares dates by day in the viewer’s own calendar', () => {
    expect(rotationDue('2026-11-14', '2026-11-15')).toBe(true)
    expect(rotationDue('2026-11-15', '2026-11-15')).toBe(false)
    expect(rotationDue('2026-11-16', '2026-11-15')).toBe(false)
  })
})
