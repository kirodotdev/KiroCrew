/**
 * Fire-and-forget usage count for clicks on agent-rendered controls.
 *
 * Sends only which KIND of control was clicked (`UiControl`), never what it
 * said, which turn it was on or which artifact it named. The gateway records
 * `kirocrew.ui.control.clicked` and answers 204; with telemetry consent off it
 * records nothing. A failure is swallowed here, so reporting can never change
 * what the click does.
 */

import { api } from '../api/client'
import type { UiControl } from '../api/client/telemetry'

export type { UiControl }

export function reportUiControlClick(control: UiControl): void {
  try {
    void Promise.resolve(api.reportUiControlClick(control)).catch(() => {})
  } catch {
    // Never let the usage count affect the click.
  }
}
