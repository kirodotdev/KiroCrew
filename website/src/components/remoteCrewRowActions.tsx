/**
 * Row-action seam for crews a contributed remote provisioner created.
 *
 * `remoteProvisionerRenderers` lets an edition draw the form that LAUNCHES a
 * crew. Once launched, the crew is an ordinary row in the Remote Crew list, and
 * the core only knows how to run the lifecycle of the crews IT launched: Stop,
 * Start and Delete in a row's overflow menu exist for EC2, addressed by the
 * launch tag. A crew another provisioner made has no lifecycle there at all, so
 * the edition had to draw a second list of the same crews just to give them a
 * Start button. This registry lets it put those controls on the core's own row.
 *
 * A contribution is keyed by the row's `provisioner_id` stamp and supplies up to
 * two components, both handed the row:
 *
 * - `menuItems` renders extra `DropdownMenuItem`s in the row's overflow menu,
 *   between Edit and Remove.
 * - `detail` renders under the row header: the machine's own state, an action
 *   in progress, or the error it ended in.
 *
 * Four properties are deliberate.
 *
 * **Keyed by `provisioner_id`, the stamp on the row.** A launch registers its
 * crew with the provisioner's id, and that persisted stamp is the only thing a
 * row carries that says which lane owns the machine behind it. `kind` names a
 * launch FORM and never reaches the registry record.
 *
 * **The built-in provisioner cannot be claimed.** EC2's Stop / Start / Delete
 * are core actions that resolve the machine through the row's AWS coordinates;
 * a contribution there would put a second set of lifecycle controls on a
 * billing instance the core already drives.
 *
 * **A chained row gets nothing.** A row reached through another crew's hop
 * arrives in that crew's payload, so its `provisioner_id` is a value the
 * announcing pane chose, not one this gateway stamped. Letting it select a
 * contribution would let a remote pane put lifecycle controls on a row of its
 * choosing.
 *
 * **It only draws.** Every action a contribution offers goes through the
 * edition's own routes, which authorize the caller server-side; rendering a
 * button here grants nothing. Each component renders inside an ErrorBoundary, so
 * a throwing contribution costs the user its controls and not the row.
 *
 * Registration is module-load, like every other frontend seam. The core
 * registers nothing, so stock behavior is unchanged.
 */
import type { ComponentType } from 'react'
import type { InstanceView } from '../api/client'
import { BUILTIN_PROVISIONER_ID } from '../utils/remoteCrew'
import { reportSeamCollision } from '../apps/seamCollision'

/** What each contributed component is handed. */
export interface CrewRowActionProps {
  /** The registry row this crew is. `id` is the instance id the core's routes use. */
  inst: InstanceView
  /**
   * The row is busy with a core action (connect, a teardown, an armed confirm),
   * so a contribution should not start one of its own.
   */
  disabled: boolean
}

export interface CrewRowActions {
  /** The `provisioner_id` stamp this applies to. Must not be the built-in one. */
  provisionerId: string
  /** Extra overflow-menu items: render `DropdownMenuItem`s, or null for none. */
  menuItems?: ComponentType<CrewRowActionProps>
  /** A block under the row header, or null for none. */
  detail?: ComponentType<CrewRowActionProps>
}

const CONTRIBUTIONS = new Map<string, CrewRowActions>()

/**
 * Register the row actions for crews one provisioner created.
 *
 * Rejected through `reportSeamCollision` (throws in dev/test, warns and ignores
 * in production) when the id is not a usable string, is the built-in
 * provisioner, is already registered, or the contribution supplies no
 * component. First registration wins, as in every additive seam.
 */
export function registerCrewRowActions(actions: CrewRowActions): void {
  const id = typeof actions.provisionerId === 'string' ? actions.provisionerId : ''
  // Compared verbatim against the stamp the server sends, so a normalized key
  // would register an id that never matches.
  if (!id || id !== id.trim()) {
    reportSeamCollision(
      'remoteCrewRowActions',
      'row actions need a non-empty provisioner id with no surrounding whitespace',
    )
    return
  }
  if (id === BUILTIN_PROVISIONER_ID) {
    reportSeamCollision(
      'remoteCrewRowActions',
      `provisioner ${id} has built-in row actions; a contribution cannot claim it`,
    )
    return
  }
  if (!actions.menuItems && !actions.detail) {
    reportSeamCollision(
      'remoteCrewRowActions',
      `row actions for ${id} supply neither menuItems nor detail`,
    )
    return
  }
  if (CONTRIBUTIONS.has(id)) {
    reportSeamCollision(
      'remoteCrewRowActions',
      `provisioner ${id} already has row actions; ignoring the duplicate`,
    )
    return
  }
  CONTRIBUTIONS.set(id, { provisionerId: id, menuItems: actions.menuItems, detail: actions.detail })
}

/**
 * The contribution that applies to this row, or undefined.
 *
 * Undefined for a row with no stamp, a built-in stamp, or a chained row (see
 * the module comment), so the panel never has to repeat those checks.
 */
export function getCrewRowActions(inst: InstanceView): CrewRowActions | undefined {
  if (inst.via_instance_id) return undefined
  const id = inst.provisioner_id
  if (!id || id === BUILTIN_PROVISIONER_ID) return undefined
  return CONTRIBUTIONS.get(id)
}
