/**
 * Which update affordance an install may be offered.
 *
 * Two facts decide it and they are independent. AVAILABILITY says a newer build
 * exists; CAPABILITY says whether the gateway can install it in-process. Reading
 * only availability is what put an "Update now" button in front of installs where
 * `POST /api/update` answers 400/409 — it is git fetch + reset, so a wheel
 * install (the `cli.sh` managed venv) and a desktop bundle both refuse it.
 *
 * Availability is also NULLABLE on the wire: `null` means no verdict — a check
 * that never ran, or one that failed — and must never be read as either "an
 * update is waiting" or "you are up to date".
 */
export type UpdateAffordance = 'apply' | 'command' | 'none'

export function updateAffordance(input: {
  /** The gateway's verdict. `null`/`undefined` = no verdict. */
  updateAvailable: boolean | null | undefined
  /** Can the gateway replace its own code in-process? */
  canApply: boolean | undefined
  /** Copyable installer command, when one applies to this shape. */
  command: string | undefined
}): UpdateAffordance {
  if (input.updateAvailable !== true) return 'none'
  if (input.canApply === true) return 'apply'
  return input.command ? 'command' : 'none'
}
