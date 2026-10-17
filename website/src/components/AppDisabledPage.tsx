import { useNavigate } from 'react-router-dom'
import { ArrowLeft, PowerOff } from 'lucide-react'
import { Btn, PageHeader } from './ui'
import ErrorNotice from './ErrorNotice'
import { reportForError } from '../utils/errorReport'
import { i18nT } from '../i18n/t'

/** The way from a disabled-app notice to the Apps page: the arrow and "Apps" that `AppHost` ends each
 *  of its states with. The one control wherever the notice shows, in place of a view or beside an
 *  input, so the same notice does not lead to Apps by two different controls. */
export function AppsButton() {
  const navigate = useNavigate()
  return <Btn onClick={() => navigate('/apps')}><ArrowLeft size={14} /> {i18nT('components.appHost.apps')}</Btn>
}

/**
 * What a user meets in place of an app that is switched off: a power-off mark, the line that says
 * to enable the app from the Apps page, and the way there. `AppDisabledPage` puts it under the page
 * header; a builtin page whose first call is refused with `app_not_enabled`
 * (`isAppNotEnabledError`) renders it alone, in place of the view that call feeds, and keeps its own
 * header and view switch so its other views stay reachable. A refusal of a later call over input
 * the user typed is shown beside that input instead, because the button here leaves the page.
 *
 * `refusal` is that refused call. Without one, nothing failed (`AppHost` reads the app's own
 * record) and the line is plain text. With one it is an error, so it renders through `ErrorNotice`
 * with the agent hand-off on, titled with the app's state because the page header around it says
 * something else. The caller renders it in place of a view that holds nothing typed, so the
 * navigation the hand-off makes has nothing to destroy.
 */
export function AppDisabledNotice({ refusal }: { refusal?: unknown }) {
  const enableText = i18nT('components.appHost.enable_this_app_from_apps_to_use_it')
  return (
    <div className="flex-1 flex items-center justify-center p-8">
      <div className="text-center max-w-md">
        <PowerOff size={48} className="text-muted mx-auto mb-4 opacity-30" />
        {refusal == null
          ? <p className="text-sm text-muted mb-4">{enableText}</p>
          : <ErrorNotice
              title={i18nT('components.appHost.this_app_is_disabled')}
              message={enableText}
              report={reportForError(refusal)}
              askAgent
              className="mb-4 text-left"
            />}
        <AppsButton />
      </div>
    </div>
  )
}

/** The page `AppHost` shows for an installed app that is known to be off: the page's own title, the
 *  disabled subtitle, and the notice. */
export default function AppDisabledPage({ title }: { title: string }) {
  return (
    <>
      <PageHeader title={title} subtitle={i18nT('components.appHost.this_app_is_disabled')} />
      <AppDisabledNotice />
    </>
  )
}
