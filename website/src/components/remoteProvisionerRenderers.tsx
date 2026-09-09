/**
 * Renderer seam for remote-instance provisioner KINDS.
 *
 * The backend decides WHICH provisioners a deployment offers: `GET
 * /api/cloud/provisioners` returns `{id, kind, label, posix_only, steps}` rows,
 * and the stock build returns exactly one — `aws_ec2`, the EC2-in-your-own-
 * account launcher the "Set up a new one" tab already draws. A row only NAMES a
 * provisioner, though: the form that collects its inputs and posts
 * `/api/cloud/launch` lives here. This registry is where a downstream edition
 * supplies that form, so a provisioner the backend contributes has something to
 * draw it.
 *
 * It is also the SINGLE definition of the renderable set, read by both
 * consumers: `canRenderRemoteProvisionerKind()` filters the rows the setup tab
 * offers and `getRemoteProvisionerRenderer()` supplies the form for the selected
 * one. One definition with two readers cannot disagree the way two copies of a
 * string literal can.
 *
 * Four properties are deliberate.
 *
 * **Keyed by `kind`, not by `id`.** That is the row's own split: `id` is the
 * identifier `POST /api/cloud/launch` resolves a provisioner by (and the server
 * rejects an unknown one with `unknown_provisioner`), while `kind` names the
 * frontend renderer. A deployment may offer several rows of one kind — two
 * DevSpace pools, one form — so the renderer is per-kind and a form that needs
 * its own row reads the `provisioner` prop it is handed.
 *
 * **A built-in kind cannot be claimed.** `aws_ec2` is drawn by the panel's own
 * prerequisites card and launch form, whose preflight the core audits, so
 * registering over it is a collision, not an override: silently replacing it
 * would let a composition step redirect a launch into someone else's AWS
 * account while the core still believes it owns that form.
 *
 * **It cannot widen what exists.** A renderer only draws a provisioner the
 * server already listed, so a renderer for a kind the deployment does not offer
 * draws nothing at all, and the backend `LaunchEngine` runs its OWN preflight on
 * every launch — a form here cannot skip a check by not rendering it. An
 * edition's own provisioner enforces its own authorization server-side.
 *
 * **Registration is module-load, like every other frontend seam.** This registry
 * is not reactive; the edition registers during composition, before the shell
 * renders. The core registers nothing, so stock behavior is unchanged.
 */
import type { ComponentType } from 'react'
import type { LaunchJob, RemoteProvisioner } from '../api/client'
import { reportSeamCollision } from '../apps/seamCollision'

/**
 * Provisioner kinds the core draws itself, in `RemoteCrewPanel`.
 *
 * Every member must have a built-in form on that panel's "Set up a new one"
 * tab — a member without one reports as drawable, offers its row in the
 * selector, and then renders nothing when picked, which is the outcome this seam
 * exists to avoid.
 */
export const BUILTIN_REMOTE_PROVISIONER_KINDS = ['aws_ec2'] as const

/**
 * What a registered form is handed. Everything a launch needs and nothing the
 * form could not have asked for itself.
 */
export interface RemoteProvisionerFormProps {
  /** The row this form is drawing, `id` included — that is what `launch()` posts. */
  provisioner: RemoteProvisioner
  /**
   * Start the launch. The panel supplies `provider_id` from `provisioner.id` and
   * fills the omitted fields with '', so a provisioner with no AWS profile or
   * region concept passes `size_key` alone.
   */
  launch: (input: { profile?: string; region?: string; size_key: string }) => void
  /** A launch this form started is in flight. */
  launching: boolean
  /** The panel is disabled (the instances feature is off, or the list failed). */
  disabled: boolean
  /**
   * The launch job the panel is polling, or null. Shared: the panel renders the
   * progress card and the status error notice below whichever form shows, so a
   * form does not draw its own — it is here for a form that wants to gate its
   * inputs while a job of its own is still running.
   */
  activeJob: LaunchJob | null
}

export interface RemoteProvisionerRenderer {
  /**
   * The `RemoteProvisioner.kind` this draws. Must not be a built-in kind.
   */
  kind: string
  /** The form, rendered on the setup tab with `RemoteProvisionerFormProps`. */
  component: ComponentType<RemoteProvisionerFormProps>
}

const RENDERERS = new Map<string, RemoteProvisionerRenderer>()

const isBuiltin = (kind: string): boolean =>
  (BUILTIN_REMOTE_PROVISIONER_KINDS as readonly string[]).includes(kind)

/**
 * Register the form that drives one remote-provisioner kind.
 *
 * Rejected through `reportSeamCollision` (throws in dev/test, warns and ignores
 * in production) when the kind is not a usable string, is a core-drawn built-in,
 * or is already registered — core and first registration win, as in every
 * additive seam.
 */
export function registerRemoteProvisionerRenderer(renderer: RemoteProvisionerRenderer): void {
  // A non-string survives from untyped JS, and reaching for `.trim()` on it
  // would throw a raw TypeError at composition — in production too, where every
  // other rejection here degrades to warn-and-ignore. Route it to the same
  // rejection instead of breaking that contract.
  const kind = typeof renderer.kind === 'string' ? renderer.kind : ''
  // Surrounding whitespace is rejected rather than trimmed away: the row is
  // compared verbatim by both readers, so storing a normalized key would
  // register a kind that can never match the one the server sends.
  if (!kind || kind !== kind.trim()) {
    reportSeamCollision(
      'remoteProvisionerRenderers',
      'a renderer needs a non-empty provisioner kind with no surrounding whitespace',
    )
    return
  }
  if (isBuiltin(kind)) {
    reportSeamCollision(
      'remoteProvisionerRenderers',
      `kind ${kind} is drawn by a built-in form; a renderer cannot claim it`,
    )
    return
  }
  if (RENDERERS.has(kind)) {
    reportSeamCollision(
      'remoteProvisionerRenderers',
      `kind ${kind} already has a renderer; ignoring the duplicate`,
    )
    return
  }
  RENDERERS.set(kind, { kind, component: renderer.component })
}

/**
 * The renderer for `kind`, or undefined when nothing registered one.
 *
 * Undefined for a built-in kind too: the core draws those itself, in the panel,
 * so there is no component here to hand back.
 */
export function getRemoteProvisionerRenderer(kind: string): RemoteProvisionerRenderer | undefined {
  return RENDERERS.get(kind)
}

/**
 * Whether this frontend can draw a provisioner of `kind` — a built-in form or a
 * registered renderer.
 *
 * This is what the setup tab filters the server's list on. A kind nothing can
 * draw stays filtered out, so its row is never offered rather than selecting
 * into an empty tab: the seam adds a way to draw a provisioner, it does not
 * remove that guard.
 */
export function canRenderRemoteProvisionerKind(kind: string): boolean {
  return isBuiltin(kind) || RENDERERS.has(kind)
}
