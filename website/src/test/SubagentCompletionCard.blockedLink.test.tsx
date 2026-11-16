import { describe, it, expect, afterEach } from 'vitest'
import { render, cleanup, fireEvent } from '@testing-library/react'
import SubagentCompletionCard from '../pages/chat/SubagentCompletionCard'

/** A subagent completion's card draws its row's blocked-link records: a link the
 *  server blocked is its chip there, never a live anchor. */

const LINK = 'https://collect.example.com/c?q=' + 'A'.repeat(250)
const record = { url: LINK, domain: 'collect.example.com', rule: 'exfil_query_length', query_chars: 252, withheld: null }

afterEach(() => cleanup())

describe('a blocked link in a subagent completion card', () => {
  it('is drawn as its chip once the card is open', () => {
    const message = {
      role: 'user',
      ts: '1',
      content: `[Subagent completion event]\nAgent \`a1\` (kirocrew) completed ✅\n\n[report](${LINK})`,
      meta: { link_blocks: [record] },
    } as never
    const { container, getAllByRole } = render(<SubagentCompletionCard message={message} disclosureKey="k" />)
    for (const b of getAllByRole('button')) if (b.getAttribute('aria-expanded') === 'false') fireEvent.click(b)
    const live = Array.from(container.querySelectorAll('a')).filter(a => a.getAttribute('href') === LINK)
    expect(live).toEqual([])
  })
})
