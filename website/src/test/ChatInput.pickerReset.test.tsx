import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useState } from 'react'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { SlotProvider } from '../providers/SlotContext'

/* ── A trigger picker must not survive a value change the composer did not
 *    originate: a parent-driven clear (send) or a slot switch. ── */
const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  skillTrust: vi.fn(),
  grantSkillTrust: vi.fn(),
  fileSearch: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import ChatInput from '../components/ChatInput'

const SKILLS = [
  { key: 'peer-review-walk', name: 'peer-review-walk', description: 'Walk peer CRs', source: 'kirocrew' },
  { key: 'grill', name: 'grill', description: 'Questioning', source: 'kirocrew' },
]

beforeEach(() => {
  vi.restoreAllMocks()
  vi.clearAllMocks()
  localStorage.clear()
  mockApi.skills.mockResolvedValue(SKILLS)
  mockApi.skillTrust.mockResolvedValue({ project: '/work/p', project_key: '/work/p' })
  mockApi.grantSkillTrust.mockResolvedValue({ trusted: true })
  mockApi.fileSearch.mockResolvedValue({ results: [] })
})

/** Clears the draft the way send does: set it to '' with no DOM change event.
 *  `slot` feeds the seam ChatInput actually reads (useSlotId), not a prop. */
function Host({ slot = 'chat-1' }: { slot?: string }) {
  const [val, setVal] = useState('')
  return (
    <SlotProvider slotId={slot}>
      <button onClick={() => setVal('')}>parent-clear</button>
      <ChatInput value={val} onChange={setVal} onSend={vi.fn()} />
    </SlotProvider>
  )
}

function typeInto(value: string) {
  fireEvent.change(screen.getByLabelText('Message input'), { target: { value } })
}

/** Scoped to the menu: the composer mirrors the draft into a PasteHighlightLayer,
 *  so a bare getByText also matches the typed text. */
function row(name: string) {
  return within(screen.getByRole('listbox')).getByText(name)
}

describe('ChatInput — trigger pickers close on a non-user value change', () => {
  it('closes the skill picker when the parent clears the draft (send)', async () => {
    renderWithProviders(<Host />)
    typeInto('$peer')
    expect(await screen.findByRole('listbox')).toBeInTheDocument()
    await waitFor(() => expect(row('$peer-review-walk')).toBeInTheDocument())

    fireEvent.click(screen.getByText('parent-clear'))

    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
  })

  it('closes the skill picker when the slot changes', async () => {
    const { rerender } = renderWithProviders(<Host slot="chat-1" />)
    typeInto('$peer')
    expect(await screen.findByRole('listbox')).toBeInTheDocument()

    // Same ChatInput instance, different slot — a tab switch.
    rerender(<Host slot="chat-2" />)

    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
  })

  it('NEGATIVE CONTROL: keeps the picker open while a token is still under the caret', async () => {
    renderWithProviders(<Host />)
    typeInto('$peer')
    expect(await screen.findByRole('listbox')).toBeInTheDocument()

    // A keystroke must never close the menu.
    typeInto('$peer-review')
    typeInto('$peer-review-walk')

    expect(screen.getByRole('listbox')).toBeInTheDocument()
    expect(row('$peer-review-walk')).toBeInTheDocument()
  })

  it('NEGATIVE CONTROL: keeps the picker open when the parent never applies onChange', async () => {
    // With a no-op onChange `value` stays '', so a close derived from value
    // CONTENT would shut the menu on the keystroke that opened it.
    renderWithProviders(
      <SlotProvider slotId="chat-1">
        <ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />
      </SlotProvider>,
    )
    typeInto('hello $peer')
    expect(await screen.findByRole('listbox')).toBeInTheDocument()
    await waitFor(() => expect(row('$peer-review-walk')).toBeInTheDocument())
  })
})
