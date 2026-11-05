import { i18nT } from '../i18n/t'

type TitledSession = { key: string; title?: string | null }

/**
 * Whether a title is the placeholder the backend sends for an open, untitled
 * slot (`NEW_SESSION_TITLE` in `state_parts/slot_session.py`), in either
 * ellipsis spelling. It is English text, not a title, so no surface shows it
 * (#29748). An exact match, so "New Session Planning" stays a real title.
 * This is the one copy of the check; the command palette imports it too.
 */
export function isBackendPlaceholderTitle(title: string): boolean {
  return title === 'New Session…' || title === 'New Session...'
}

/**
 * Whether a session has no title of its own.
 *
 * A title equal to the key counts as none, as the sidebar row already treats
 * it: a dragged session reference and a restored draft carry the key as their
 * title when the session had none. The backend's placeholder counts as none
 * too, so an open untitled slot reads like a closed one.
 */
export function isUntitledSession(session: TitledSession): boolean {
  return !session.title || session.title === session.key || isBackendPlaceholderTitle(session.title)
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
