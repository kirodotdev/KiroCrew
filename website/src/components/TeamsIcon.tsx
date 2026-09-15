import teamsLogoUrl from '../assets/teams-logo.svg'

/**
 * Official Microsoft Teams logo mark, full color. Importing the asset lets Vite
 * emit a hashed URL under /assets, which the production gateway serves — same
 * treatment as slack-logo.svg / webex-logo.svg (see vite.config.ts).
 */
export function TeamsIcon({ size = 16 }: { size?: number }) {
  return <img src={teamsLogoUrl} width={size} height={size} alt="" aria-hidden="true" />
}
