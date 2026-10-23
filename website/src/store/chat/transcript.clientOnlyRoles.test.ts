import { describe, it, expect } from 'vitest'

import { isDurableRow, serverRowCount } from './transcript'
import type { ChatMessage } from '../../types'

const row = (role: string): ChatMessage => ({ role, content: role, cls: '' }) as ChatMessage

describe('client-only transcript roles', () => {
  it('an elicitation row is client-only, like a permission row: it never shifts a paging offset', () => {
    // The gateway keeps an elicitation in its live tail but never writes it to
    // history (`_TRANSIENT_ROLES`), so counting it would move the cursor past a
    // persisted row.
    expect(isDurableRow({ role: 'elicitation' })).toBe(false)
    expect(isDurableRow({ role: 'permission' })).toBe(false)
    expect(serverRowCount([row('user'), row('elicitation'), row('assistant')])).toBe(2)
  })
})
