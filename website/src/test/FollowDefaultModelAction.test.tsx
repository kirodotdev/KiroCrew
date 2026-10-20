/**
 * Settings > Chat > Models: the button that makes chats keeping a model of their
 * own follow the default again.
 *
 * Nothing changes without the press, and the press only counts: the clear is sent
 * after the confirm, carrying the count's digest, so the number the dialog showed
 * is the set the gateway clears. Every expected string is read from the shipped
 * English catalog.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { api, ApiError } from '../api/client'
import type { FollowDefaultCount } from '../api/client/chatSlotSettings'
import { retryPolicy } from '../api/queryClient'
import enManual from '../i18n/locales/en.manual.json'
import { i18nT } from '../i18n/t'
import { FollowDefaultModelAction } from '../pages/settings/FollowDefaultModelAction'
import { renderWithProviders } from './helpers'

function renderAction() {
  return renderWithProviders(<FollowDefaultModelAction />, {
    queryDefaults: { retry: retryPolicy, retryDelay: 0, staleTime: Infinity },
  })
}

const copy = enManual.pages.settings.chatPanel
const DIGEST = 'a'.repeat(64)

function withCount(text: string, count: number): string {
  return text.replace('{{count}}', String(count))
}

const OLD = 'claude-fable-5'
const PICK = 'claude-sonnet-5'
const DEFAULT = 'claude-opus-5'

/** What the count answers for *count* chats, all on one kept model and untitled
 *  unless *extra* says otherwise. */
function answer(count: number, extra: Partial<FollowDefaultCount> = {}): FollowDefaultCount {
  return { count, digest: DIGEST, default_model: DEFAULT, models: [{ model: OLD, count }], titles: [], ...extra }
}

function stub(
  count: number,
  clear?: () => Promise<{ ok: boolean; changed: number }>,
  extra: Partial<FollowDefaultCount> = {},
) {
  const counted = vi.spyOn(api, 'chatSlotsFollowDefaultCount').mockResolvedValue(answer(count, extra))
  const cleared = vi.spyOn(api, 'chatSlotsFollowDefault').mockImplementation(
    clear ?? (async () => ({ ok: true, changed: count })),
  )
  return { counted, cleared }
}

/** The text a `<model/>` line renders with *model* in its slot. */
function withModel(text: string, model: string): string {
  return text.replace('<model/>', model)
}

async function dialogFor(count: number, extra: Partial<FollowDefaultCount>) {
  stub(count, undefined, extra)
  await press()
  return screen.findByRole('dialog')
}

async function press() {
  const user = userEvent.setup()
  renderAction()
  await user.click(screen.getByRole('button', { name: copy.follow_default_button }))
  return user
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('FollowDefaultModelAction', () => {
  it('sits under its own label and explains what it changes', async () => {
    const user = userEvent.setup()
    renderAction()
    expect(screen.getByText(copy.follow_default_label)).toBeInTheDocument()
    expect(screen.getByText(copy.follow_default_description)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: i18nT('components.infoTip.more_information') }))
    expect(await screen.findByText(copy.follow_default_hint)).toBeInTheDocument()
  })

  it('names the Default Model the chats land on, in monospace', async () => {
    const dialog = await dialogFor(3, { default_model: DEFAULT })
    const target = within(dialog).getByTestId('follow-default-target')
    expect(target).toHaveTextContent(withModel(copy.follow_default_confirm_target, DEFAULT))
    expect(within(target).getByText(DEFAULT)).toHaveClass('font-mono')
  })

  it('says what an Auto default does instead of naming a default of the default', async () => {
    const dialog = await dialogFor(3, { default_model: '' })
    const target = within(dialog).getByTestId('follow-default-target')
    expect(target).toHaveTextContent(copy.follow_default_confirm_target_auto)
    expect(target).not.toHaveTextContent(copy.default_auto)
  })

  it('lists each model the chats leave with how many keep it', async () => {
    const dialog = await dialogFor(3, { models: [{ model: OLD, count: 2 }, { model: PICK, count: 1 }] })
    const list = within(dialog).getByRole('list', { name: copy.follow_default_models_heading })
    const rows = within(list).getAllByRole('listitem')
    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveTextContent(OLD)
    expect(rows[0]).toHaveTextContent(withCount(copy.follow_default_model_chats_other, 2))
    expect(rows[1]).toHaveTextContent(PICK)
    expect(rows[1]).toHaveTextContent(withCount(copy.follow_default_model_chats_one, 1))
    expect(within(rows[0]).getByText(OLD)).toHaveClass('font-mono')
  })

  it('lists the chats that change, an untitled one by name, and how many more', async () => {
    const long = 'A very long chat title '.repeat(12)
    const dialog = await dialogFor(7, { titles: ['Plan the trip', '', long] })
    const list = within(dialog).getByRole('list', { name: copy.follow_default_titles_heading })
    const rows = within(list).getAllByRole('listitem')
    expect(rows.map(row => row.textContent)).toEqual(['Plan the trip', copy.follow_default_untitled, long])
    expect(rows[2]).toHaveClass('truncate')
    expect(within(dialog).getByTestId('follow-default-more')).toHaveTextContent(
      withCount(copy.follow_default_more_other, 4),
    )
  })

  it('says no more when every chat that changes is listed', async () => {
    const dialog = await dialogFor(2, { titles: ['One', 'Two'] })
    const list = within(dialog).getByRole('list', { name: copy.follow_default_titles_heading })
    expect(within(list).getAllByRole('listitem')).toHaveLength(2)
    expect(within(dialog).queryByTestId('follow-default-more')).not.toBeInTheDocument()
  })

  it('counts only the chats you started, in the words the rest of the row uses', () => {
    expect(copy.follow_default_hint).toMatch(/\bchats\b/)
    expect(copy.follow_default_hint).not.toMatch(/session/i)
  })

  it('leaves out the chats others started, in the same words as the description', async () => {
    // The description's sentence about the chats that keep their model, up to
    // what happens to them: "Chats that crewmates, … started".
    const senders = copy.follow_default_description
      .split('. ')
      .find(sentence => sentence.endsWith(' keep their model'))
      ?.replace(/ keep their model$/, '')
    expect(senders).toBeTruthy()
    const dialog = await dialogFor(3, {})
    const caveat = within(dialog).getByTestId('follow-default-caveat')
    expect(caveat).toHaveTextContent(copy.follow_default_confirm_body)
    expect(copy.follow_default_confirm_body).toContain(`${senders} are not included.`)
    expect(copy.follow_default_confirm_body).not.toMatch(/\bkept\b/)
  })

  it('says it is counting while the count loads, and keeps its label while the dialog is open', async () => {
    let release!: (value: FollowDefaultCount) => void
    vi.spyOn(api, 'chatSlotsFollowDefaultCount').mockImplementation(
      () => new Promise(resolve => { release = resolve }),
    )
    const user = userEvent.setup()
    renderAction()
    await user.click(screen.getByRole('button', { name: copy.follow_default_button }))

    const counting = await screen.findByRole('button', { name: copy.follow_default_counting })
    expect(counting).toBeDisabled()

    release(answer(3))
    await screen.findByRole('dialog')
    const button = screen.getByTestId('follow-default-button')
    expect(button).toHaveTextContent(copy.follow_default_button)
    expect(button).toBeDisabled()
  })

  it('says it is updating while the confirmed change runs', async () => {
    let finish!: (value: { ok: boolean; changed: number }) => void
    stub(3, () => new Promise(resolve => { finish = resolve }))
    const user = await press()
    await screen.findByRole('dialog')
    await user.click(screen.getByRole('button', { name: copy.follow_default_confirm }))

    const updating = await screen.findByRole('button', { name: copy.follow_default_updating })
    expect(updating).toBeDisabled()

    finish({ ok: true, changed: 3 })
    expect(await screen.findByRole('status')).toHaveTextContent(withCount(copy.follow_default_done_other, 3))
    expect(screen.getByRole('button', { name: copy.follow_default_button })).toBeEnabled()
  })

  it('keeps the note that a model can still be picked in any chat, last', async () => {
    const dialog = await dialogFor(3, { titles: ['One'] })
    const caveat = within(dialog).getByTestId('follow-default-caveat')
    expect(caveat).toHaveTextContent(copy.follow_default_confirm_body)
    const target = within(dialog).getByTestId('follow-default-target')
    expect(target.compareDocumentPosition(caveat) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('asks with the counted number, and the confirm clears what was counted', async () => {
    const { cleared } = stub(3)
    const user = await press()

    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent(withCount(copy.follow_default_confirm_title_other, 3))
    expect(dialog).toHaveTextContent(copy.follow_default_confirm_body)
    expect(cleared).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: copy.follow_default_confirm }))
    await waitFor(() => expect(cleared).toHaveBeenCalledWith(DIGEST))
    expect(await screen.findByRole('status')).toHaveTextContent(
      withCount(copy.follow_default_done_other, 3),
    )
  })

  it('paints the confirm as the next step, like the sidebar bulk switch, never as a danger', async () => {
    stub(3)
    await press()
    await screen.findByRole('dialog')
    const confirmButton = screen.getByRole('button', { name: copy.follow_default_confirm })
    expect(confirmButton).toHaveClass('bg-accent', 'text-accent-fg')
    expect(confirmButton).not.toHaveClass('text-danger')
  })

  it('names one chat in the singular', async () => {
    stub(1)
    await press()
    expect(await screen.findByRole('dialog')).toHaveTextContent(
      withCount(copy.follow_default_confirm_title_one, 1),
    )
  })

  it('changes nothing when the confirm is cancelled', async () => {
    const { cleared } = stub(3)
    const user = await press()
    await screen.findByRole('dialog')

    await user.click(screen.getByRole('button', { name: i18nT('components.confirmDialog.cancel') }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(cleared).not.toHaveBeenCalled()
    expect(screen.queryByTestId('follow-default-status')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: copy.follow_default_button })).toBeEnabled()
  })

  it('says so and asks nothing when every chat already follows the default', async () => {
    const { cleared } = stub(0)
    await press()
    expect(await screen.findByRole('status')).toHaveTextContent(copy.follow_default_none)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(cleared).not.toHaveBeenCalled()
  })

  it('reports the number the gateway changed, not the number it was asked to', async () => {
    const user = (stub(3, async () => ({ ok: true, changed: 2 })), await press())
    await screen.findByRole('dialog')
    await user.click(screen.getByRole('button', { name: copy.follow_default_confirm }))
    expect(await screen.findByRole('status')).toHaveTextContent(
      withCount(copy.follow_default_done_other, 2),
    )
  })

  it('asks for a fresh count when the chats moved between the count and the confirm', async () => {
    const changed = new ApiError(
      409,
      'the chats changed since they were counted',
      JSON.stringify({ error: 'the chats changed since they were counted', code: 'chats_changed', count: 4, digest: 'b'.repeat(64) }),
    )
    const user = (stub(3, async () => { throw changed }), await press())
    await screen.findByRole('dialog')
    await user.click(screen.getByRole('button', { name: copy.follow_default_confirm }))
    expect(await screen.findByTestId('follow-default-error')).toHaveTextContent(copy.follow_default_changed)
    expect(screen.queryByTestId('follow-default-status')).not.toBeInTheDocument()
  })

  it('reads a confirm that could not save some chats as a failure, never as done', async () => {
    const notSaved = new ApiError(
      500,
      'some chats could not be saved',
      JSON.stringify({ error: 'some chats could not be saved', code: 'chats_not_saved', changed: 2, failed: 1 }),
    )
    const user = (stub(3, async () => { throw notSaved }), await press())
    await screen.findByRole('dialog')
    await user.click(screen.getByRole('button', { name: copy.follow_default_confirm }))
    expect(await screen.findByTestId('follow-default-error')).toHaveTextContent(copy.follow_default_failed)
    expect(screen.queryByTestId('follow-default-status')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: copy.follow_default_button })).toBeEnabled()
  })

  it('retries a throttled count through the shared retry ladder before opening the dialog', async () => {
    const { counted, cleared } = stub(3)
    counted.mockRejectedValueOnce(new ApiError(429, '', ''))
    await press()

    expect(await screen.findByRole('dialog')).toHaveTextContent(
      withCount(copy.follow_default_confirm_title_other, 3),
    )
    expect(counted).toHaveBeenCalledTimes(2)
    expect(cleared).not.toHaveBeenCalled()
    expect(screen.queryByTestId('follow-default-error')).not.toBeInTheDocument()
  })

  it('shows a failed count and lets the press be tried again', async () => {
    vi.spyOn(api, 'chatSlotsFollowDefaultCount').mockRejectedValue(new ApiError(500, '', ''))
    const cleared = vi.spyOn(api, 'chatSlotsFollowDefault')
    await press()
    expect(await screen.findByTestId('follow-default-error')).toHaveTextContent(copy.follow_default_failed)
    expect(cleared).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: copy.follow_default_button })).toBeEnabled()
  })
})
