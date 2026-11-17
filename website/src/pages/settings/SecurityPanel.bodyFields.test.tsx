/**
 * Message body fields (Settings -> Security).
 *
 * A view over `/api/security/mcp-body-fields`. Pinned here: a field the gate
 * refused for size is offered with one "Allow this field from now on" button that
 * sends exactly that row; an owner-added row can be removed; the defaults are
 * read-only; and a failed read shows an error, never an empty list.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import type { McpBodyFieldsData } from '../../api/client'
import { ApiError } from '../../api/apiError'

vi.mock('../../api/client', async () => ({
  ApiError: (await vi.importActual<typeof import('../../api/apiError')>('../../api/apiError')).ApiError,
  api: {
    deniedCommands: vi.fn(),
    kirocrewConfig: vi.fn(),
    tailnetStatus: vi.fn(),
    mcpBodyFields: vi.fn(),
    allowMcpBodyField: vi.fn(),
    removeMcpBodyField: vi.fn(),
  },
}))

// framer keeps `layoutId` off the DOM; this mock writes it as `data-layout-id`
// so a test can see that the refused row and the allowed row are one element.
vi.mock('framer-motion', async importOriginal => {
  const actual = await importOriginal<typeof import('framer-motion')>()
  const React = await import('react')
  const div = React.forwardRef<HTMLDivElement, Record<string, unknown> & { children?: React.ReactNode }>(
    ({ layoutId, layout: _layout, transition: _transition, children, ...rest }, ref) =>
      React.createElement('div', { ...rest, 'data-layout-id': layoutId, ref }, children),
  )
  const cache = new Map<PropertyKey, unknown>()
  return {
    ...actual,
    motion: new Proxy(actual.motion, {
      get: (t, tag) => {
        if (!cache.has(tag)) cache.set(tag, tag === 'div' ? div : Reflect.get(t, tag))
        return cache.get(tag)
      },
    }),
  }
})

import { api } from '../../api/client'
import { SecurityPanel } from './SecurityPanel'

const DEFAULTS = [{ server: 'gmail', tool: 'send_email', field: 'body' }]
const REFUSED = { server: 'mail', tool: 'send', field: 'html', refused_at: '2026-11-05T20:00:00+00:00' }
const OWNER = { server: 'docs', tool: 'create_page', field: 'markdown', added_at: '2026-11-04T20:00:00+00:00' }

function mocked(fn: unknown) {
  return fn as ReturnType<typeof vi.fn>
}

async function renderFields(data: McpBodyFieldsData) {
  mocked(api.mcpBodyFields).mockResolvedValue(data)
  renderWithProviders(<SecurityPanel />, { route: '/?section=bodyFields', queryDefaults: { retryDelay: 0 } })
  await screen.findByTestId('body-fields-defaults')
}

describe('SecurityPanel - message body fields', () => {
  beforeEach(() => {
    cleanup()
    vi.clearAllMocks()
    mocked(api.deniedCommands).mockResolvedValue({
      builtins: [], user_added: [], disable_all: false, effective_count: 0, governance_locked: false,
    })
    mocked(api.kirocrewConfig).mockResolvedValue({})
    mocked(api.tailnetStatus).mockResolvedValue({
      enabled: false, governance_pinned: false, host: '', origin: '', resolved_at: 0, state: 'off',
    })
  })

  it('says a body field is limited by size and a credential or data-out link is only logged', async () => {
    await renderFields({ defaults: DEFAULTS, owner: [], refused: [] })
    const desc = screen.getByText(/up to 256 KB/)
    expect(desc.textContent).toMatch(/logged as a warning, not blocked/)
    expect(desc.textContent).not.toMatch(/checked for sensitive paths, credentials/)
  })

  it('offers a refused field and allows exactly that row', async () => {
    const after = { defaults: DEFAULTS, owner: [{ server: 'mail', tool: 'send', field: 'html', added_at: '' }], refused: [] }
    mocked(api.allowMcpBodyField).mockResolvedValue(after)
    await renderFields({ defaults: DEFAULTS, owner: [], refused: [REFUSED] })
    const refused = screen.getByTestId('body-fields-refused')
    expect(within(refused).getByText('@mail/send')).toBeInTheDocument()
    mocked(api.mcpBodyFields).mockResolvedValue(after)
    fireEvent.click(within(refused).getByTestId('body-fields-allow'))
    await waitFor(() => expect(api.allowMcpBodyField).toHaveBeenCalledWith({ server: 'mail', tool: 'send', field: 'html' }))
    await screen.findByTestId('body-fields-refused-empty')
  })

  it('an allowed field moves from Refused to Allowed as one row (shared layoutId)', async () => {
    const after = { defaults: DEFAULTS, owner: [{ server: 'mail', tool: 'send', field: 'html', added_at: '' }], refused: [] }
    mocked(api.allowMcpBodyField).mockResolvedValue(after)
    await renderFields({ defaults: DEFAULTS, owner: [], refused: [REFUSED] })
    const refusedRow = within(screen.getByTestId('body-fields-refused')).getByText('@mail/send').closest('[data-layout-id]')
    const id = refusedRow?.getAttribute('data-layout-id')
    expect(id).toBeTruthy()
    mocked(api.mcpBodyFields).mockResolvedValue(after)
    fireEvent.click(within(screen.getByTestId('body-fields-refused')).getByTestId('body-fields-allow'))
    const owner = await screen.findByTestId('body-fields-owner')
    expect(within(owner).getByText('@mail/send').closest('[data-layout-id]')?.getAttribute('data-layout-id')).toBe(id)
  })

  it('says next to Allow what it allows, that the refused message is not re-sent, and how to undo it', async () => {
    await renderFields({ defaults: DEFAULTS, owner: [], refused: [REFUSED] })
    const refused = screen.getByTestId('body-fields-refused')
    const hint = within(refused).getByTestId('body-fields-allow-hint')
    expect(hint.textContent).toMatch(/up to 256 KB/)
    expect(hint.textContent).toMatch(/sensitive paths/)
    expect(hint.textContent).toMatch(/not sent again/)
    expect(hint.textContent).toMatch(/Stop allowing/)
    expect(hint.closest('[data-layout-id]')).toBe(within(refused).getByTestId('body-fields-allow').closest('[data-layout-id]'))
  })

  it('stacks field identities above full-width actions on narrow panes', async () => {
    await renderFields({ defaults: DEFAULTS, owner: [OWNER], refused: [REFUSED] })
    const refusedRow = within(screen.getByTestId('body-fields-refused')).getByText('@mail/send').closest('[data-layout-id]') as HTMLElement
    const actions = within(refusedRow).getByTestId('body-fields-allow-hint').parentElement as HTMLElement
    expect(refusedRow).toHaveClass('flex-col', 'items-stretch', 'sm:flex-row', 'sm:items-center', 'sm:justify-between')
    expect(actions).toHaveClass('w-full', 'sm:w-auto', 'sm:max-w-[280px]', 'sm:shrink-0', 'sm:items-end')
    expect(actions.className.split(/\s+/)).not.toContain('max-w-[280px]')
    expect(actions.className.split(/\s+/)).not.toContain('shrink-0')

    const ownerRow = within(screen.getByTestId('body-fields-owner')).getByText('@docs/create_page').closest('[data-layout-id]')
    expect(ownerRow).toHaveClass('flex-col', 'items-stretch', 'sm:flex-row', 'sm:items-center', 'sm:justify-between')
  })

  it('names the undo button "Stop allowing" and words the empty refused list without the gateway', async () => {
    await renderFields({ defaults: DEFAULTS, owner: [OWNER], refused: [] })
    expect(within(screen.getByTestId('body-fields-owner')).getByTestId('body-fields-remove').textContent).toBe('Stop allowing')
    expect(screen.getByTestId('body-fields-refused-empty').textContent).toBe('No field has been refused for size yet.')
  })

  it('removes an owner-added row and keeps the defaults read-only', async () => {
    const after = { defaults: DEFAULTS, owner: [], refused: [] }
    mocked(api.removeMcpBodyField).mockResolvedValue(after)
    await renderFields({ defaults: DEFAULTS, owner: [OWNER], refused: [] })
    mocked(api.mcpBodyFields).mockResolvedValue(after)
    expect(within(screen.getByTestId('body-fields-defaults')).queryByRole('button')).toBeNull()
    fireEvent.click(within(screen.getByTestId('body-fields-owner')).getByTestId('body-fields-remove'))
    await waitFor(() => expect(api.removeMcpBodyField).toHaveBeenCalledWith({ server: 'docs', tool: 'create_page', field: 'markdown' }))
    await screen.findByTestId('body-fields-owner-empty')
  })

  it('reads the list again on return, under the app-wide Infinity staleTime', async () => {
    mocked(api.mcpBodyFields).mockResolvedValue({ defaults: DEFAULTS, owner: [], refused: [] })
    const { rerender } = renderWithProviders(<SecurityPanel />, {
      route: '/?section=bodyFields', queryDefaults: { retryDelay: 0, staleTime: Infinity },
    })
    await screen.findByTestId('body-fields-refused-empty')
    mocked(api.mcpBodyFields).mockResolvedValue({ defaults: DEFAULTS, owner: [], refused: [REFUSED] })
    rerender(<div />)
    rerender(<SecurityPanel />)
    await waitFor(() => expect(within(screen.getByTestId('body-fields-refused')).getByText('@mail/send')).toBeInTheDocument())
    expect(api.mcpBodyFields).toHaveBeenCalledTimes(2)
  })

  it('a refused read shows an error, no lists, and no retry', async () => {
    mocked(api.mcpBodyFields).mockRejectedValue(new ApiError(403, 'owner required', '{"code":"dashboard_owner_required"}'))
    renderWithProviders(<SecurityPanel />, { route: '/?section=bodyFields', queryDefaults: { retryDelay: 0 } })
    await screen.findByTestId('body-fields-read-failed')
    expect(api.mcpBodyFields).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('body-fields-defaults')).toBeNull()
  })
})
