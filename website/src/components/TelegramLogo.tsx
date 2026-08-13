import telegramLogoUrl from '../assets/telegram-logo.svg'

/**
 * Official Telegram logo mark (blue gradient circle + paper plane). Importing
 * the asset lets Vite emit a hashed URL under /assets, which the production
 * gateway serves (same treatment as slack-logo.svg — see vite.config.ts).
 */
export function TelegramLogo({ size = 16 }: { size?: number }) {
  return <img src={telegramLogoUrl} width={size} height={size} alt="" aria-hidden="true" />
}
