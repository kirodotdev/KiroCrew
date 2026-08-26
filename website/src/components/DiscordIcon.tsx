import discordLogoUrl from '../assets/discord-logo.svg'

/**
 * Official Discord logo mark (blurple Clyde symbol). Importing the asset lets
 * Vite emit a hashed URL under /assets, which the production gateway serves
 * (same treatment as slack-logo.svg — see vite.config.ts).
 */
export function DiscordIcon({ size = 16 }: { size?: number }) {
  return <img src={discordLogoUrl} width={size} height={size} alt="" aria-hidden="true" />
}
