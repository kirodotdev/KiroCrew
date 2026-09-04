import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import FolderSuggestionCard from '../pages/chat/FolderSuggestionCard'
import reducer, { setFolderSuggestion, clearFolderSuggestion, ageFolderSuggestion, startLocalTurn, confirmOptimisticSend, FOLDER_SUGGESTION_MAX_TURNS } from '../store/chatSlice'

vi.mock('../i18n/t', () => ({
  i18nT: (key: string, vars?: Record<string, unknown>) =>
    key === 'components.folderSuggestionCard.move_to_folder_question'
      ? `Move this session into folder \u201c${vars?.folder}\u201d?`
      : key.split('.').pop() ?? key,
}))

function renderCard(over: Partial<React.ComponentProps<typeof FolderSuggestionCard>> = {}) {
  const onAccept = vi.fn()
  const onDecline = vi.fn()
  render(
    <FolderSuggestionCard
      folderName="feature"
      breadcrumb="Kiro Crew › feature"
      onAccept={onAccept}
      onDecline={onDecline}
      {...over}
    />,
  )
  return { onAccept, onDecline }
}

describe('FolderSuggestionCard', () => {
  it('asks about the suggested folder with the name interpolated, not concatenated', () => {
    renderCard()
    expect(screen.getByText('Move this session into folder \u201cfeature\u201d?')).toBeInTheDocument()
  })

  it('shows the breadcrumb as ancestry context when the folder is nested', () => {
    renderCard()
    expect(screen.getByText('Kiro Crew › feature')).toBeInTheDocument()
  })

  it('hides the breadcrumb for a root folder, where it only repeats the name', () => {
    renderCard({ folderName: 'Errands', breadcrumb: 'Errands' })
    // The question still renders the name; the redundant second line does not.
    expect(screen.getByText('Move this session into folder \u201cErrands\u201d?')).toBeInTheDocument()
    expect(screen.queryByTitle('Errands')).not.toBeInTheDocument()
  })

  it('keeps the whole question reachable on hover, since the line truncates', () => {
    // The question line is single-line `truncate`, and for a ROOT folder the
    // breadcrumb is suppressed — so this tooltip is the only way to recover a
    // folder name that got clipped.
    renderCard({ folderName: 'Errands', breadcrumb: 'Errands' })
    const question = screen.getByText('Move this session into folder \u201cErrands\u201d?')
    expect(question.getAttribute('title')).toBe('Move this session into folder \u201cErrands\u201d?')
  })

  it('calls onAccept once for the move button', async () => {
    const { onAccept, onDecline } = renderCard()
    await userEvent.click(screen.getByTestId('folder-suggestion-accept'))
    expect(onAccept).toHaveBeenCalledTimes(1)
    expect(onDecline).not.toHaveBeenCalled()
  })

  it('calls onDecline once for the dismiss button', async () => {
    const { onAccept, onDecline } = renderCard()
    await userEvent.click(screen.getByTestId('folder-suggestion-decline'))
    expect(onDecline).toHaveBeenCalledTimes(1)
    expect(onAccept).not.toHaveBeenCalled()
  })

  it('always renders the lucide glyph, never a folder emoji', () => {
    // The card takes no icon prop: an emoji is font-dependent (tofu box wherever
    // the platform has no emoji font) and would not inherit --accent.
    const { container } = render(
      <FolderSuggestionCard folderName="i18n" breadcrumb="Kiro Crew › i18n" onAccept={vi.fn()} onDecline={vi.fn()} />,
    )
    const svg = container.querySelector('svg')
    expect(svg).toBeTruthy()
    expect(svg?.getAttribute('aria-hidden')).toBe('true')
    // No stray emoji anywhere in the rendered text.
    expect(container.textContent ?? '').not.toMatch(/\p{Extended_Pictographic}/u)
  })
})

describe('folderSuggestions reducers', () => {
  const base = () => reducer(undefined, { type: '@@INIT' })

  const setAction = (over: Record<string, unknown> = {}) =>
    setFolderSuggestion({
      slot: 'dashboard_chat-1',
      folderId: 'f1',
      folderName: 'feature',
      breadcrumb: 'Kiro Crew › feature',
      ts: 100,
      ...over,
    } as Parameters<typeof setFolderSuggestion>[0])

  it('stores a card under its own slot key', () => {
    const s = reducer(base(), setAction())
    expect(s.folderSuggestions['dashboard_chat-1']).toMatchObject({ folderId: 'f1', folderName: 'feature', ts: 100 })
  })

  it('ignores a card with no slot, folder id, or folder name', () => {
    let s = reducer(base(), setAction({ slot: '' }))
    expect(Object.keys(s.folderSuggestions)).toHaveLength(0)
    s = reducer(base(), setAction({ folderId: '' }))
    expect(Object.keys(s.folderSuggestions)).toHaveLength(0)
    s = reducer(base(), setAction({ folderName: '' }))
    expect(Object.keys(s.folderSuggestions)).toHaveLength(0)
  })

  it('never indexes state with a prototype-polluting key', () => {
    const s = reducer(base(), setAction({ slot: '__proto__' }))
    expect(Object.keys(s.folderSuggestions)).toHaveLength(0)
  })

  it('clears the card the user acted on', () => {
    const s = reducer(reducer(base(), setAction()), clearFolderSuggestion({ slot: 'dashboard_chat-1', ts: 100 }))
    expect(s.folderSuggestions['dashboard_chat-1']).toBeUndefined()
  })

  it('keeps a NEWER card when a stale clear arrives for the one it replaced', () => {
    let s = reducer(base(), setAction({ ts: 100 }))
    s = reducer(s, setAction({ ts: 200, folderId: 'f2', folderName: 'other' }))
    // The click that started against ts=100 must not delete the ts=200 card the
    // user has not answered yet.
    s = reducer(s, clearFolderSuggestion({ slot: 'dashboard_chat-1', ts: 100 }))
    expect(s.folderSuggestions['dashboard_chat-1']).toMatchObject({ folderId: 'f2', ts: 200 })
  })

  it('clears unconditionally when no ts is supplied', () => {
    const s = reducer(reducer(base(), setAction({ ts: 100 })), clearFolderSuggestion({ slot: 'dashboard_chat-1' }))
    expect(s.folderSuggestions['dashboard_chat-1']).toBeUndefined()
  })

  it('keeps cards for two slots independent', () => {
    let s = reducer(base(), setAction({ slot: 'a', ts: 1 }))
    s = reducer(s, setAction({ slot: 'b', ts: 2, folderId: 'f2', folderName: 'other' }))
    s = reducer(s, clearFolderSuggestion({ slot: 'a', ts: 1 }))
    expect(s.folderSuggestions['a']).toBeUndefined()
    expect(s.folderSuggestions['b']).toMatchObject({ folderId: 'f2' })
  })
})

describe('folderSuggestions age out by rendered, confirmed user send', () => {
  const base = () => reducer(undefined, { type: '@@INIT' })
  const seed = (slot = 'dashboard_chat-1') =>
    reducer(base(), setFolderSuggestion({ slot, folderId: 'f1', folderName: 'feature', breadcrumb: 'feature', ts: 100 }))
  // Aging is an explicit action dispatched ONLY by the render site that showed
  // the card (ChatPage, active slot) after the server confirmed delivery. It is
  // deliberately NOT baked into startLocalTurn (failed sends must not count) or
  // confirmOptimisticSend (ChatPane surfaces confirm sends without rendering
  // the card, and an unseen card must never age).
  const aged = (slot: string, ts = 100) => ageFolderSuggestion({ slot, ts })

  it('survives FOLDER_SUGGESTION_MAX_TURNS aged sends and is gone on the next one', () => {
    let s = seed()
    for (let n = 1; n <= FOLDER_SUGGESTION_MAX_TURNS; n++) {
      s = reducer(s, aged('dashboard_chat-1'))
      expect(s.folderSuggestions['dashboard_chat-1']).toMatchObject({ turns: n })
    }
    s = reducer(s, aged('dashboard_chat-1'))
    expect(s.folderSuggestions['dashboard_chat-1']).toBeUndefined()
  })

  it('a FAILED or unrendered send does not age the card — neither shared send reducer touches it', () => {
    // startLocalTurn (the optimistic dispatch, fired even for sends that then
    // fail) and confirmOptimisticSend (fired by ChatPane surfaces that never
    // render the card) must both leave the card untouched, however often they
    // run. Only the explicit ageFolderSuggestion dispatch counts.
    let s = seed()
    for (let n = 0; n <= FOLDER_SUGGESTION_MAX_TURNS + 2; n++) {
      s = reducer(s, startLocalTurn('dashboard_chat-1'))
      s = reducer(s, confirmOptimisticSend({ slot: 'dashboard_chat-1', sendId: `send-${n}` }))
    }
    expect(s.folderSuggestions['dashboard_chat-1']).toMatchObject({ turns: 0 })
  })

  it('a stale ts does not age a replacement card that landed mid-flight', () => {
    // The send left while the ts=100 card was visible; a ts=200 replacement
    // arrived before the response. The confirmed send may only age the
    // generation the user actually saw, which no longer exists.
    let s = seed()
    s = reducer(s, setFolderSuggestion({ slot: 'dashboard_chat-1', folderId: 'f2', folderName: 'other', breadcrumb: 'other', ts: 200 }))
    s = reducer(s, aged('dashboard_chat-1', 100))
    expect(s.folderSuggestions['dashboard_chat-1']).toMatchObject({ folderId: 'f2', turns: 0 })
  })

  it('ages only the slot that sent — a card parked in another session is untouched', () => {
    let s = seed('a')
    s = reducer(s, setFolderSuggestion({ slot: 'b', folderId: 'f2', folderName: 'other', breadcrumb: 'other', ts: 200 }))
    for (let n = 0; n <= FOLDER_SUGGESTION_MAX_TURNS; n++) s = reducer(s, aged('a'))
    expect(s.folderSuggestions['a']).toBeUndefined()
    expect(s.folderSuggestions['b']).toMatchObject({ folderId: 'f2', turns: 0 })
  })

  it('does not mutate Object.prototype when a send names a polluting slot key', () => {
    const s = reducer(seed(), ageFolderSuggestion({ slot: '__proto__', ts: 100 }))
    expect(s.folderSuggestions['dashboard_chat-1']).toMatchObject({ turns: 0 })
    expect((Object.prototype as Record<string, unknown>).turns).toBeUndefined()
  })

  it('starts the count from a card that predates the field', () => {
    // A slice preloaded from an older shape (or a hand-built test fixture) can
    // have no `turns`; the first aged send must count as one, not NaN.
    const s = reducer(
      { ...base(), folderSuggestions: { a: { folderId: 'f', folderName: 'F', breadcrumb: 'F', ts: 1 } } } as never,
      ageFolderSuggestion({ slot: 'a', ts: 1 }),
    )
    expect(s.folderSuggestions['a']).toMatchObject({ turns: 1 })
  })
})
