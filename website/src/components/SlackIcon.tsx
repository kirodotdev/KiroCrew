import slackLogoUrl from '../assets/slack-logo.svg'

/**
 * Official Slack logo mark (2019), full color. Importing the asset lets Vite
 * emit a hashed URL under /assets, which the production gateway serves.
 */
export function SlackIcon({ size = 16 }: { size?: number }) {
  return <img src={slackLogoUrl} width={size} height={size} alt="" aria-hidden="true" />
}
