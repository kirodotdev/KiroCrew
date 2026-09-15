import whatsappLogoUrl from '../assets/whatsapp-logo.svg'

/**
 * Official WhatsApp mark — the green speech bubble with the phone glyph.
 * Importing the asset lets Vite emit a hashed URL under /assets, which the
 * production gateway serves (same treatment as slack-logo.svg — see
 * vite.config.ts and {@link SlackIcon}).
 *
 * This is the personal-account channel (WhatsApp Web / QR pairing via neonize),
 * not the Meta Cloud Business API.
 */
export function WhatsAppLogo({ size = 16 }: { size?: number }) {
  return <img src={whatsappLogoUrl} width={size} height={size} alt="" aria-hidden="true" />
}
