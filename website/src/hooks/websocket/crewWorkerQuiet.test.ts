import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

/** A Crew Mode crew member's FINISHED turn raises no chime or toast for the
 *  person: its conductor reads it (owner decisions A and B, edge review R1 S2/S4).
 *  A turn that paused on a question or errored still sounds, as it does under the
 *  mute: the crew member has stopped, and nothing else yet says so. Its tool
 *  approvals always alert (crewMemberApprovalNotify.test.ts). */
const read = (name: string) => readFileSync(path.join(__dirname, name), 'utf-8')

describe('Crew Mode crew members stay quiet for the person when a turn finishes', () => {
  it('a finished crew member turn raises no turn-done chime or toast; a paused or errored one still does', () => {
    const src = read('turnCompletion.ts')
    expect(src).toContain('const crewWorkerQuiet = !!soundSlot?.crew_root && !completionNeedsInput && !turnErrored')
    expect(src).toMatch(/&& !muteSuppresses && !crewWorkerQuiet/)
  })
})
