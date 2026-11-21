/**
 * The managed-environment lane's two dashboard seams, registered by the stock build itself.
 *
 * The form draws only when the server lists the `kiro_managed_env` provisioner, which it does
 * only while the `crew_client` feature preview is on; the row actions draw only on rows stamped
 * with that provisioner id. With the preview off neither shows, and nothing here calls a route.
 * Imported once from `main.tsx`, after the edition's composition root.
 */
import { registerCrewRowActions } from '../remoteCrewRowActions'
import { registerRemoteProvisionerRenderer } from '../remoteProvisionerRenderers'
import SandboxLaunchForm, { SANDBOX_PROVISIONER_KIND } from './SandboxLaunchForm'
import { SANDBOX_PROVISIONER_ID, SandboxCrewRowDetail, SandboxCrewRowMenuItems } from './SandboxCrewRowActions'

registerRemoteProvisionerRenderer({ kind: SANDBOX_PROVISIONER_KIND, component: SandboxLaunchForm })

registerCrewRowActions({
  provisionerId: SANDBOX_PROVISIONER_ID,
  menuItems: SandboxCrewRowMenuItems,
  detail: SandboxCrewRowDetail,
})
