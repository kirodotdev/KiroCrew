/**
 * Renderer seam for phone-connection METHOD KINDS.
 *
 * The backend `mobile_connect` CPP seam decides WHICH phone-connection methods a
 * deployment offers: `MobileConnectProvider.connect_methods()` returns
 * `{id, kind}` descriptors and `capabilities.mobile_connect` governs which of
 * them survive. A descriptor only NAMES a method, though — the credential is
 * minted on the method's own endpoint, and the UI that drives that endpoint
 * lives here. This registry is where a downstream edition supplies that UI, so a
 * method the backend contributes has something to draw it.
 *
 * It is also the SINGLE definition of the renderable set, read by both
 * consumers: `canRenderMobileConnectKind()` answers for the nav rail's row and
 * `getMobileConnectRenderers()` supplies the dialog's sections. One definition
 * with two readers cannot disagree the way two copies of a string literal can.
 *
 * Four properties are deliberate.
 *
 * **Keyed by `kind`, not by `id`.** That is the descriptor's own split: `id` is
 * the GOVERNED identifier the `methods` ruleset narrows on, while `kind` names
 * the frontend renderer. Two methods may legitimately share a kind (two tunnels,
 * one drawing), so the renderer is per-kind and a component that needs the ids
 * reads `/api/mobile-connect/methods` itself.
 *
 * **A built-in kind cannot be claimed.** `tailnet_qr` and `login_link` are drawn
 * by core sections whose mint endpoints core audits, so registering over one is a
 * collision, not an override: silently replacing them would let a composition
 * step redirect a credential mint the core still believes it owns.
 *
 * **It cannot widen governance.** A renderer only draws a method the server
 * already listed: the endpoint filters every id through
 * `capabilities.mobile_connect` before the dialog sees a kind, and each built-in
 * mint endpoint re-runs that decision (`mint_denied_reason`) because the filtered
 * list is presentation, never the control. A renderer for a kind the deployment
 * does not offer draws nothing at all. An edition's OWN mint endpoint is outside
 * this seam and enforces its own authorization.
 *
 * **Registration is module-load, like every other frontend seam.** This registry
 * is not reactive; the edition registers during composition, before the shell
 * renders. The core registers nothing, so stock behavior is unchanged.
 */
import type { ComponentType } from 'react'
import { reportSeamCollision } from '../apps/seamCollision'

/**
 * Method kinds the core draws itself, in `MobileConnectModal`.
 *
 * Every member must have a built-in section in that dialog — a member without
 * one reports as drawable, shows the nav row, and then opens a dialog with an
 * empty body, which is the outcome this seam exists to avoid.
 * `MobileConnectModal.test.tsx` pins that each member draws.
 */
export const BUILTIN_MOBILE_CONNECT_KINDS = ['tailnet_qr', 'login_link'] as const

export interface MobileConnectRenderer {
  /**
   * The `MobileConnectMethod.kind` this draws. Must not be a built-in kind.
   */
  kind: string
  /**
   * The section. Rendered inside the dialog with only `onClose`, matching the
   * built-in sections: a section owner reads whatever else it needs itself, and
   * dismisses the dialog when it navigates away.
   */
  component: ComponentType<{ onClose: () => void }>
}

const RENDERERS = new Map<string, MobileConnectRenderer>()

const isBuiltin = (kind: string): boolean =>
  (BUILTIN_MOBILE_CONNECT_KINDS as readonly string[]).includes(kind)

/**
 * Register the section that draws one phone-connection method kind.
 *
 * Rejected through `reportSeamCollision` (throws in dev/test, warns and ignores
 * in production) when the kind is not a usable string, is a core-drawn built-in,
 * or is already registered — core and first registration win, as in every
 * additive seam.
 */
export function registerMobileConnectRenderer(renderer: MobileConnectRenderer): void {
  // A non-string survives from untyped JS, and reaching for `.trim()` on it
  // would throw a raw TypeError at composition — in production too, where every
  // other rejection here degrades to warn-and-ignore. Route it to the same
  // rejection instead of breaking that contract.
  const kind = typeof renderer.kind === 'string' ? renderer.kind : ''
  // Surrounding whitespace is rejected rather than trimmed away: the descriptor
  // is compared verbatim by both readers, so storing a normalized key would
  // register a kind that can never match the one the server sends.
  if (!kind || kind !== kind.trim()) {
    reportSeamCollision(
      'mobileConnectRenderers',
      'a renderer needs a non-empty method kind with no surrounding whitespace',
    )
    return
  }
  if (isBuiltin(kind)) {
    reportSeamCollision(
      'mobileConnectRenderers',
      `kind ${kind} is drawn by a built-in section; a renderer cannot claim it`,
    )
    return
  }
  if (RENDERERS.has(kind)) {
    reportSeamCollision(
      'mobileConnectRenderers',
      `kind ${kind} already has a renderer; ignoring the duplicate`,
    )
    return
  }
  RENDERERS.set(kind, { kind, component: renderer.component })
}

/**
 * Registered renderers in registration order, or `[]` when none is registered.
 *
 * Order is the registrant's, so a deployment contributing several methods
 * controls how they stack rather than inheriting endpoint or map ordering.
 */
export function getMobileConnectRenderers(): MobileConnectRenderer[] {
  return [...RENDERERS.values()]
}

/**
 * Whether this frontend can draw a method of `kind` — a built-in section or a
 * registered renderer.
 *
 * This is what the nav rail filters on. A kind nothing can draw stays filtered
 * out, so the row is hidden rather than opening a dialog with an empty body: the
 * seam adds a way to draw a method, it does not remove that guard.
 */
export function canRenderMobileConnectKind(kind: string): boolean {
  return isBuiltin(kind) || RENDERERS.has(kind)
}
