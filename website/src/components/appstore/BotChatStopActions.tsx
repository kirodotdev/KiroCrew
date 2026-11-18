/**
 * The two ways out of a Bot-chat stop that did not finish, shared by the
 * uninstall, disable and trust-removal notices: take the app's tools out of
 * the Bot's chat, which keeps its history, or restart the gateway.
 */
import { Link } from 'react-router-dom'

import { Btn } from '../ui'
import { i18nT } from '../../i18n/t'

export default function BotChatStopActions({
  app,
  bots,
  count,
  onRetry,
  retrying,
}: {
  /** The app whose tools the retry removes; omitted when turning off blanket
   *  trust, where the button names the untrusted apps together. */
  app?: string
  /** The Bots' display names, joined, for the button label. */
  bots: string
  count: number
  onRetry: () => void
  retrying: boolean
}) {
  const label = app === undefined
    ? i18nT('pages.appsPage.bot_chat_stop_retry_all', { count, bots })
    : i18nT('pages.appsPage.bot_chat_stop_retry', { count, bots, name: app })
  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-3">
        <Btn disabled={retrying} onClick={onRetry}>
          {retrying ? i18nT('pages.appsPage.bot_chat_stop_retrying') : label}
        </Btn>
        <Link to="/settings/system" className="text-sm font-medium underline">
          {i18nT('pages.appsPage.restart_gateway_link')}
        </Link>
      </div>
      <span className="text-[12px] text-muted">{i18nT('pages.appsPage.bot_chat_stop_retry_hint')}</span>
    </div>
  )
}
