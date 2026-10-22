/**
 * A crewmate's own change, drawn as one row with an Undo: the row names the
 * crewmate by its display name, draws the faces only for a face change, posts
 * the undo to the member's own route, reads a refusal's code off the error the
 * transport throws, and takes the button away when the refusal is final.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { store as reduxStore } from '../store'
import { http, HttpResponse } from 'msw'
import { server } from '../../integration/mocks/server'
import '../api/client'
import { i18nT } from '../i18n/t'
import { fmtList } from '../i18n/format'
import ConversationCard, { readSelfChangeRef } from './ConversationCard'
import type { ChatMessage } from '../types'

const L = (k: string, v?: Record<string, unknown>) => i18nT(`components.selfChange.${k}`, v)
const SLOT = 'member-scout'

const GHOST = { kind: 'ghost', traits: { eyes: 'sparkle', brows: 'none', mouth: 'grin', accessory: 'cap', prop: 'term', blush: false, flip: false, tile: '#9046ff' } }

function row(card: Record<string, unknown>): ChatMessage {
  return {
    role: 'card',
    content: 'scout changed its avatar',
    cls: 'msg msg-card',
    ts: '2026-10-12T10:00:00Z',
    meta: { mid: `m-${String(card.id)}`, card: { surface: 'self', id: 'ch1', slot: SLOT, member: 'scout', name: 'Pebble', slug: 'scout', fields: ['avatar'], before: { avatar: {} }, after: { avatar: GHOST }, status: 'applied', ...card } },
  }
}

function renderRow(message: ChatMessage) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={reduxStore}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ConversationCard message={message} slot={SLOT} />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

let undoCalls: string[]

beforeEach(() => {
  undoCalls = []
})

describe('reading the row', () => {
  it('reads a self row and nothing else', () => {
    const ref = readSelfChangeRef(row({}))
    expect(ref).not.toBeNull()
    expect(ref?.fields).toEqual(['avatar'])
    expect(ref?.name).toBe('Pebble')
    expect(ref?.undoable).toBe(true)
    expect(readSelfChangeRef({ role: 'card', meta: { card: { surface: 'change', id: 'x' } } })).toBeNull()
    expect(readSelfChangeRef(row({ fields: ['workspace'] }))).toBeNull()
    expect(readSelfChangeRef(row({ fields: [] }))).toBeNull()
  })

  it('keeps only the fields that changed from before/after', () => {
    const ref = readSelfChangeRef(row({ fields: ['description'], before: { description: 'a', avatar: {} }, after: { description: 'b', avatar: GHOST } }))
    expect(ref?.before).toEqual({ description: 'a' })
    expect(ref?.after).toEqual({ description: 'b' })
  })
})

describe('the row', () => {
  it('names the mate by its display name and draws the faces for a face change', () => {
    renderRow(row({}))
    const el = screen.getByTestId('self-change-row')
    expect(el).toHaveAttribute('data-self-status', 'applied')
    expect(el).toHaveTextContent(L('changed_avatar', { name: 'Pebble' }))
    expect(el.querySelectorAll('img')).toHaveLength(2)
    expect(screen.getByTestId('self-change-undo')).toHaveTextContent(L('undo'))
  })

  it('draws no faces for a name or description change, and names the fields when several changed', () => {
    renderRow(row({ fields: ['description'], before: { description: '' }, after: { description: 'I watch builds.' } }))
    expect(screen.getByTestId('self-change-row').querySelectorAll('img')).toHaveLength(0)
    expect(screen.getByTestId('self-change-row')).toHaveTextContent(L('changed_description', { name: 'Pebble' }))
    renderRow(row({ id: 'ch2', fields: ['display_name', 'avatar'], before: { display_name: 'Scout', avatar: {} }, after: { display_name: 'Rock', avatar: GHOST } }))
    const rows = screen.getAllByTestId('self-change-row')
    expect(rows[1]).toHaveTextContent(L('changed_fields', { name: 'Rock', fields: fmtList([L('field_display_name'), L('field_avatar')]) }))
  })

  it('says undone once the change is taken back, keeps the row name, and offers no Undo', () => {
    renderRow(row({ status: 'undone', fields: ['display_name'], before: { display_name: 'Scout' }, after: { display_name: 'Pebble' } }))
    const el = screen.getByTestId('self-change-row')
    expect(el).toHaveAttribute('data-self-status', 'undone')
    // The row is the same note it was before the undo; only the badge changes.
    expect(el).toHaveTextContent(L('changed_display_name', { name: 'Pebble' }))
    expect(el).toHaveTextContent(L('undone'))
    expect(screen.queryByTestId('self-change-undo')).toBeNull()
  })

  it('offers no Undo when the gateway kept no record', () => {
    renderRow(row({ undoable: false }))
    expect(screen.queryByTestId('self-change-undo')).toBeNull()
  })

  it('offers no Undo when the page has no conversation to bind it to', () => {
    // The gateway refuses an unbound undo, so the button is not drawn.
    render(
      <Provider store={reduxStore}>
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <MemoryRouter>
            <ConversationCard message={row({})} slot={null} />
          </MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
    expect(screen.getByTestId('self-change-row')).toBeInTheDocument()
    expect(screen.queryByTestId('self-change-undo')).toBeNull()
  })
})

describe('Undo', () => {
  it("posts to the member's own undo route, naming the page's conversation, and leaves the row to the gateway's update", async () => {
    server.use(
      http.post('/api/members/:slug/self-changes/:id/undo', async ({ params, request }) => {
        // The conversation comes from the page, not from the row's own data.
        const body = await request.json() as { slot?: string }
        undoCalls.push(`${String(params.slug)}/${String(params.id)}@${body.slot}`)
        return HttpResponse.json({ ok: true, member: 'scout', changed: ['avatar'], status: 'undone' })
      }),
    )
    renderRow(row({}))
    fireEvent.click(screen.getByTestId('self-change-undo'))
    await waitFor(() => expect(undoCalls).toEqual([`scout/ch1@${SLOT}`]))
    expect(screen.queryByTestId('self-change-undo-error')).toBeNull()
    // The row flips through the gateway's row update, not locally.
    expect(screen.getByTestId('self-change-row')).toHaveAttribute('data-self-status', 'applied')
  })

  it('reads a 410 off the thrown error, says where the value is set, and takes the button away', async () => {
    server.use(
      http.post('/api/members/:slug/self-changes/:id/undo', () =>
        HttpResponse.json({ error: 'this change can no longer be undone here', code: 'undo_unavailable' }, { status: 410 })),
    )
    renderRow(row({}))
    fireEvent.click(screen.getByTestId('self-change-undo'))
    const notice = await screen.findByTestId('self-change-undo-error')
    expect(notice).toHaveTextContent(L('undo_unavailable', { name: 'Pebble' }))
    expect(screen.queryByTestId('self-change-undo')).toBeNull()
  })

  it('says the field was changed again on a stale undo', async () => {
    server.use(
      http.post('/api/members/:slug/self-changes/:id/undo', () =>
        HttpResponse.json({ error: 'changed again since', code: 'stale_undo' }, { status: 409 })),
    )
    renderRow(row({}))
    fireEvent.click(screen.getByTestId('self-change-undo'))
    expect(await screen.findByTestId('self-change-undo-error')).toHaveTextContent(L('undo_stale', { name: 'Pebble' }))
    expect(screen.queryByTestId('self-change-undo')).toBeNull()
  })

  it('keeps the button on any other failure and shows the message', async () => {
    server.use(
      http.post('/api/members/:slug/self-changes/:id/undo', () =>
        HttpResponse.json({ error: 'this mate was removed', code: 'not_a_crewmate' }, { status: 403 })),
    )
    renderRow(row({}))
    fireEvent.click(screen.getByTestId('self-change-undo'))
    await screen.findByTestId('self-change-undo-error')
    expect(screen.getByTestId('self-change-undo')).toBeInTheDocument()
  })
})
