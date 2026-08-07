/**
 * The Workflows author toolbar's "Load example…" control.
 *
 * It migrated off a native <select> whose first <option> was a disabled
 * placeholder header. The replacement is deliberately NOT value-bound: nothing
 * on the page records a "current example", the script is editable the instant it
 * lands, and holding the value at '' is what lets the SAME example be loaded
 * twice — which the native select could not do, because a select never re-fires
 * `change` for the option already selected.
 *
 * Real Radix Select, no mock: it opens on click in happy-dom (the "Should not
 * already be working." wall only bites Radix-inside-Radix-Dialog, and this
 * toolbar sits on the page).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import WorkflowsPage from '../apps/workflows/WorkflowsPage'

const EXAMPLES = [
  { name: 'fan-out', description: 'parallel agents', source: '# fan-out source' },
  { name: 'sequential', description: 'one after another', source: '# sequential source' },
]

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(<QueryClientProvider client={qc}><WorkflowsPage /></QueryClientProvider>)
}

/** The trigger, once the /examples query has resolved and revealed it. */
function loader() {
  return screen.findByRole('combobox', { name: /Load example/ })
}

const editor = () => screen.getByRole('textbox', { name: 'workflow source' })

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (url: unknown) => ({
    ok: true,
    json: async () => (String(url).endsWith('/examples') ? EXAMPLES : {}),
  })))
})

afterEach(() => { vi.unstubAllGlobals() })

describe('Workflows example loader', () => {
  it('loads the picked example into the editor', async () => {
    renderPage()
    fireEvent.click(await loader())
    fireEvent.click(await screen.findByRole('option', { name: 'fan-out' }))
    await waitFor(() => expect(editor()).toHaveValue('# fan-out source'))
  })

  it('holds its command label instead of displaying the pick as a value', async () => {
    renderPage()
    const trigger = await loader()
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('option', { name: 'sequential' }))
    await waitFor(() => expect(editor()).toHaveValue('# sequential source'))
    // A value-bound select would now read "sequential" — and go stale the moment
    // the operator edits the script.
    expect(trigger).toHaveTextContent(/Load example/)
  })

  it('re-loads the same example after the script was edited', async () => {
    renderPage()
    const trigger = await loader()
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('option', { name: 'fan-out' }))
    await waitFor(() => expect(editor()).toHaveValue('# fan-out source'))

    fireEvent.change(editor(), { target: { value: 'broken by hand' } })
    expect(editor()).toHaveValue('broken by hand')

    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('option', { name: 'fan-out' }))
    await waitFor(() => expect(editor()).toHaveValue('# fan-out source'))
  })
})
