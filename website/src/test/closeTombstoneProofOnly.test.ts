import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const source = readFileSync(resolve(__dirname, '../store/chatSlice.ts'), 'utf-8')
const sweep = source.slice(source.indexOf('const retireCloseTombstone'))

describe('the close tombstone releases only on proof, never on activity', () => {
  it('has no activity-based release helper', () => {
    expect(source).not.toMatch(/slotIsServingPastClose/)
  })

  it('never consults running or last_ts to release the hold', () => {
    const branch = sweep.slice(0, sweep.indexOf('.catch('))

    expect(branch).not.toMatch(/running === true/)
    expect(branch).not.toMatch(/last_ts/)
  })

  it('keeps the three proof signals: omission, a different incarnation, a re-read otherwise', () => {
    expect(sweep).toMatch(/if \(!listed\)/)
    expect(sweep).toMatch(/listed\.incarnation !== closingInstance/)
    expect(sweep).toMatch(/again\(\)/)
  })
})
