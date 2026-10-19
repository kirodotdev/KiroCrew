import { i18nT } from '../i18n/t'

type TitledSession = { key: string; title?: string | null }

/**
 * Whether a session has no title of its own.
 *
 * A title equal to the key counts as none, as the sidebar row already treats
 * it: a dragged session reference and a restored draft carry the key as their
 * title when the session had none.
 */
export function isUntitledSession(session: TitledSession): boolean {
  return !session.title || session.title === session.key
}

/**
 * The label a session row or chip shows for a session.
 *
 * A titled session shows its title. An untitled one shows the localized
 * "Untitled session" label, never its slot key: the key is an internal id the
 * reader cannot recognise (#20907, #20908, #20909, #20910). The label says
 * "untitled", not "new", because these surfaces list sessions that already
 * exist. Callers keep using `key` for identity, navigation and actions.
 */
export function sessionDisplayTitle(session: TitledSession): string {
  return isUntitledSession(session) ? i18nT('pages.artifactDetailPage.untitled_session') : session.title!
}
