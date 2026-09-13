/**
 * Ratchet: `meta.sendIds` is a persisted field on server rows, and the two
 * readers below are its designated consumers -- the row-identity set that
 * decides which optimistic bubbles a server row stands for, and the
 * `_merged_send_ids` retirement pass. A third direct reader would fork that
 * decision, so a row could be retired against one reader's view of the ids and
 * kept against another's.
 *
 * Scoped to reads, not mentions: the field name legitimately appears in the
 * writers that populate it and in comments naming the contract, so the pattern
 * matches property ACCESS (`.sendIds` not followed by an assignment) and the
 * ceiling is per-file rather than tree-wide.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '..')

// A read, not a write: `x.sendIds =` and `sendIds:` (object literal) are excluded.
const READ = /\.sendIds(?!\s*=[^=])/g

const count = (rel: string) =>
  (readFileSync(resolve(SRC, rel), 'utf-8').match(READ) ?? []).length

describe('meta.sendIds reader ratchet', () => {
  // The designated readers live here: rowIdentities and the _merged_send_ids pass.
  it('keeps at most the two designated readers in the store', () => {
    expect(count('store/chatSlice.ts')).toBeLessThanOrEqual(2)
  })

  it.each([
    'pages/ChatPage.tsx',
    'pages/chat/UserMessage.tsx',
    'components/ChatPane.tsx',
    'hooks/useQueuedMessageActions.ts',
  ])('%s never reads meta.sendIds directly', rel => {
    expect(count(rel)).toBe(0)
  })
})
