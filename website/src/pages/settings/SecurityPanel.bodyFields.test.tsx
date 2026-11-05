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

  it('a refused read shows an error, no lists, and no retry', async () => {
    mocked(api.mcpBodyFields).mockRejectedValue(new ApiError(403, 'owner required', '{"code":"dashboard_owner_required"}'))
    renderWithProviders(<SecurityPanel />, { route: '/?section=bodyFields', queryDefaults: { retryDelay: 0 } })
    await screen.findByTestId('body-fields-read-failed')
    expect(api.mcpBodyFields).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('body-fields-defaults')).toBeNull()
  })
})
