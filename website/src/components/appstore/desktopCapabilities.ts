import { i18nT } from '../../i18n/t'

/**
 * `permissions.desktopCapabilities`: what an app's desktop windows may ask the
 * desktop app for. Mirrors `CAPABILITIES` in `apps/desktop_capabilities.py`; an
 * entry not listed here grants nothing, so it is never shown as if it did.
 */
export const DESKTOP_CAPABILITIES = ['screenCapture', 'localFiles', 'gatewayChoice'] as const

export type DesktopCapability = (typeof DESKTOP_CAPABILITIES)[number]

/** The known capabilities in a declared or approved list, in the one fixed order. */
export function knownDesktopCapabilities(value: unknown): DesktopCapability[] {
  return Array.isArray(value) ? DESKTOP_CAPABILITIES.filter(cap => value.includes(cap)) : []
}

/**
 * The one sentence for a capability, shared by the trust prompt and the app page,
 * so an owner reads the same words wherever they approve it.
 */
export function desktopCapabilityLine(cap: DesktopCapability): string {
  switch (cap) {
    case 'screenCapture': return i18nT('components.appstore.trustAppModal.desktop_capability_screen_capture')
    case 'localFiles': return i18nT('components.appstore.trustAppModal.desktop_capability_local_files')
    case 'gatewayChoice': return i18nT('components.appstore.trustAppModal.desktop_capability_gateway_choice')
  }
}
