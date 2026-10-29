import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

/** A Crew Mode worker's FINISHED turn raises no chime or toast for the person:
 *  its conductor reads it (owner decisions A and B, edge review R1 S2/S4). A
 *  turn that paused on a question or errored still sounds, as it does under the
 *  mute: the worker has stopped, and nothing else yet says so. Its tool approvals
 *  keep notifying here; the part that adds the conductor's approval card is the
 *  one that routes them there. */
const read = (name: string) => readFileSync(path.join(__dirname, name), 'utf-8')

describe('Crew Mode workers stay quiet for the person', () => {
  it('a finished worker turn raises no turn-done chime or toast; a paused or errored one still does', () => {
    const src = read('turnCompletion.ts')
    expect(src).toContain('const crewWorkerQuiet = !!soundSlot?.crew_root && !completionNeedsInput && !turnErrored')
    expect(src).toMatch(/&& !muteSuppresses && !crewWorkerQuiet/)
  })
})
