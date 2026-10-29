import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

/** A Crew Mode worker never reaches the person on its own (owner decisions A and B,
 *  edge review R1 X2/S2/S4): its approval raises no note, chime, banner or badge
 *  (the conductor's approval card shows it), and its FINISHED turn raises no chime
 *  or toast (its conductor reads it). A turn that paused on a question or errored
 *  still sounds, as it does under the mute: the worker has stopped, and nothing
 *  else yet says so. */
const read = (name: string) => readFileSync(path.join(__dirname, name), 'utf-8')

describe('Crew Mode workers stay quiet for the person', () => {
  it('an approval frame for a worker returns before any note, chime or badge', () => {
    const src = read('approvals.ts')
    const frame = src.slice(src.indexOf('onApprovalFrame(data, reconnecting)'))
    const early = frame.indexOf('isCrewWorkerSlot(data.slot)')
    expect(early).toBeGreaterThan(0)
    expect(early).toBeLessThan(frame.indexOf('dispatchMcNotification(APPROVAL_KIND)'))
    expect(early).toBeLessThan(frame.indexOf('dispatch(addNotification(approvalNote))'))
    // The reconnect reconcile writes the worker's row but adds no note for it.
    const reconcile = src.slice(src.indexOf('async reconcilePending'), src.indexOf('onApprovalFrame(data, reconnecting)'))
    expect(reconcile.indexOf('isCrewWorkerSlot(slot)')).toBeLessThan(reconcile.indexOf('dispatch(addNotification({'))
  })

  it("a worker's native permission row raises no chime or banner (chatStream)", () => {
    // An interactive chat's prompt arrives as a `permission` row, not an approval
    // frame, so the worker check has to sit on that row's chime gate too; the OS
    // banner rides the same gate.
    const src = read('chatStream.ts')
    const gate = src.indexOf("data.role === 'permission' && !isCrewWorkerSlot(")
    expect(gate).toBeGreaterThan(0)
    expect(gate).toBeLessThan(src.indexOf('dispatchMcNotification(APPROVAL_KIND)'))
    expect(gate).toBeLessThan(src.indexOf('postNativeNotification(slotTitle'))
  })

  it('a finished worker turn raises no turn-done chime or toast; a paused or errored one still does', () => {
    const src = read('turnCompletion.ts')
    expect(src).toContain('const crewWorkerQuiet = !!crewRootOf(soundSlot) && !completionNeedsInput && !turnErrored')
    expect(src).toMatch(/&& !muteSuppresses && !crewWorkerQuiet/)
  })
})
