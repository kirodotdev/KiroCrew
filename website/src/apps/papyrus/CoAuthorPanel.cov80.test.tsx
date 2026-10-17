/**
 * CoAuthorPanel — the embedded Papyrus co-author chat.
 *
 * The panel's own logic is the three-way body (mounted chat / creating spinner /
 * empty invitation), the header buttons, and the switchSlot effect that must
 * re-dispatch when the project changes but NOT on an unrelated re-render — that
 * guard is what keeps a re-render from yanking the user back to another paper's
 * session. ChatPage is stubbed: mounting the real one would pull the whole chat
 * tree into a test about this panel.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, screen } from '@testing-library/react'
import { renderWithProviders, createTestStore } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import CoAuthorPanel, { type CoAuthorPanelProps } from './CoAuthorPanel'

vi.mock('../../pages/ChatPage', () => ({
  default: (props: Record<string, unknown>) => (
    <div data-testid="zzq-chatpage">{`zzq:${props.embedMode}:${String(props.noUrlSync)}`}</div>
  ),
}))

const switchSlot = vi.hoisted(() => vi.fn((key: string) => ({ type: 'zzq/switchSlot', payload: key })))
vi.mock('../../store/chatSlice', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  switchSlot,
}))

function setup(props: Partial<CoAuthorPanelProps> = {}) {
  const handlers = {
    onStartSession: vi.fn(),
    onOpenFull: vi.fn(),
    onClose: vi.fn(),
  }
  const store = createTestStore()
  const utils = renderWithProviders(
    <CoAuthorPanel slotKey={null} creating={false} {...handlers} {...props} />,
    { store },
  )
  return { ...utils, ...handlers }
}

beforeEach(() => {
  switchSlot.mockClear()
})

describe('CoAuthorPanel body', () => {
  it('mounts the embedded chat page for a project with a session', () => {
    setup({ slotKey: 'zzq-slot-1' })
    expect(screen.getByTestId('zzq-chatpage').textContent).toBe('zzq:chat:true')
  })

  it('shows a progress status while a session is being created', () => {
    setup({ creating: true })
    expect(screen.getByRole('status').textContent).toContain(
      i18nT('apps.papyrus.coAuthor.starting_session'),
    )
    expect(screen.queryByTestId('zzq-chatpage')).not.toBeInTheDocument()
  })

  it('invites the user to start a session when none exists', () => {
    const { onStartSession } = setup()
    expect(screen.getByTestId('papyrus-co-author').textContent).toContain(
      i18nT('apps.papyrus.coAuthor.no_co_author_session_for_this_paper_yet'),
    )
    fireEvent.click(
      screen.getByRole('button', { name: i18nT('apps.papyrus.coAuthor.start_a_session') }),
    )
    expect(onStartSession).toHaveBeenCalledTimes(1)
  })

  it('prefers the mounted chat over the creating spinner when both apply', () => {
    setup({ slotKey: 'zzq-slot-1', creating: true })
    expect(screen.getByTestId('zzq-chatpage')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
})

describe('CoAuthorPanel header', () => {
  it('offers the open-in-chat-page shortcut only with a session', () => {
    const { onOpenFull } = setup({ slotKey: 'zzq-slot-1' })
    fireEvent.click(
      screen.getByRole('button', { name: i18nT('apps.papyrus.coAuthor.open_in_chat_page') }),
    )
    expect(onOpenFull).toHaveBeenCalledTimes(1)
  })

  it('hides that shortcut when there is no session', () => {
    setup()
    expect(
      screen.queryByRole('button', { name: i18nT('apps.papyrus.coAuthor.open_in_chat_page') }),
    ).not.toBeInTheDocument()
  })

  it('closes the panel', () => {
    const { onClose } = setup({ slotKey: 'zzq-slot-1' })
    fireEvent.click(
      screen.getByRole('button', {
        name: i18nT('apps.papyrus.coAuthor.close_co_author_panel'),
      }),
    )
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})

describe('CoAuthorPanel slot activation', () => {
  it('activates the project session on mount', () => {
    setup({ slotKey: 'zzq-slot-1' })
    expect(switchSlot).toHaveBeenCalledWith('zzq-slot-1')
  })

  it('does not activate anything when the project has no session', () => {
    setup()
    expect(switchSlot).not.toHaveBeenCalled()
  })

  it('re-activates only when the project changes', () => {
    const { rerender } = setup({ slotKey: 'zzq-slot-1' })
    rerender(
      <CoAuthorPanel
        slotKey="zzq-slot-1"
        creating={false}
        onStartSession={() => {}}
        onOpenFull={() => {}}
        onClose={() => {}}
      />,
    )
    expect(switchSlot).toHaveBeenCalledTimes(1)

    rerender(
      <CoAuthorPanel
        slotKey="zzq-slot-2"
        creating={false}
        onStartSession={() => {}}
        onOpenFull={() => {}}
        onClose={() => {}}
      />,
    )
    expect(switchSlot).toHaveBeenCalledTimes(2)
    expect(switchSlot).toHaveBeenLastCalledWith('zzq-slot-2')
  })
})
