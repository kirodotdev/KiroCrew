import { i18nT } from '../i18n/t'

/**
 * Wire identifiers for the sign-in providers the gateway accepts. Sent verbatim
 * in the POST body — never catalog values, because a translated identifier is
 * not a protocol token.
 */
export type KasLoginProvider = 'google' | 'github' | 'builder_id' | 'idc'

// Display name for the provider a sign-in is running under (the device view's
// eyebrow interpolates it). Routed through the catalog like every other label
// so locales that transliterate brand names can.
export function providerLabel(provider: KasLoginProvider): string {
  switch (provider) {
    case 'google':
      return i18nT('components.kasLogin.provider_google')
    case 'github':
      return i18nT('components.kasLogin.provider_github')
    case 'builder_id':
      return i18nT('components.kasLogin.provider_builder_id')
    case 'idc':
      return i18nT('components.kasLogin.provider_company_sso')
  }
}

/**
 * Display name for the provider a STORED sign-in reports. The gateway spells
 * it as the governance classification the token carries ('Google' | 'Github' |
 * 'BuilderId' | 'Enterprise' | 'ExternalIdp'), not as the chooser's wire
 * identifiers, so the mapping is by loose match; anything unrecognised is shown
 * verbatim rather than mislabelled as a provider it is not.
 *
 * This module holds no component, so the account modal in the main bundle can
 * share the mapping without pulling the sign-in flow in with it.
 */
export function providerLabelFromWire(stored: string | null | undefined): string {
  const key = (stored ?? '').toLowerCase()
  if (key === 'google') return providerLabel('google')
  if (key === 'github') return providerLabel('github')
  if (key === 'builderid' || key === 'builder_id') return providerLabel('builder_id')
  if (key === 'enterprise' || key === 'idc' || key === 'externalidp') return providerLabel('idc')
  return stored ?? ''
}

function humanizeProviderId(value: string): string {
  return value
    .replace(/[_-]+/g, ' ')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .trim()
}

/**
 * Display name for an account from its `kiro-cli whoami` kind (`accountType`) and,
 * for an organisation sign-in, its portal: "IAM Identity Center · example.awsapps.com".
 * `startUrl` is the portal's URL; a caller holding only the host passes it as a URL.
 * Shared by the account popup and the Kiro sign-in card, so one account is never
 * called two things.
 */
export function accountProviderLabel(accountType?: string, startUrl?: string): string {
  const rawType = accountType?.trim()
  if (rawType?.startsWith('Social')) {
    const socialProvider = humanizeProviderId(rawType.slice('Social'.length))
    return socialProvider || i18nT('app.social_login')
  }

  const accountKind = rawType === 'IamIdentityCenter'
    ? i18nT('app.iam_identity_center')
    : rawType === 'BuilderId'
      ? i18nT('app.builder_id')
      : rawType?.toLowerCase().replace(/[^a-z0-9]/g, '') === 'externalidp'
        // The organization's own sign-in: the sign-in flow's plain-language
        // name for it ("Company SSO"), never the protocol's.
        ? providerLabelFromWire('externalidp')
        : rawType
          ? humanizeProviderId(rawType)
          : undefined

  let issuerHost: string | undefined
  if (startUrl) {
    try { issuerHost = new URL(startUrl).host } catch { issuerHost = undefined }
  }
  return [accountKind, issuerHost].filter(Boolean).join(' · ')
}
