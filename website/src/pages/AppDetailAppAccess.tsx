/**
 * The app detail page's two access cards that follow the Bot grants card: the pages the
 * app's agent may browse, then the app's dashboard access. They sit in one module so the
 * page file keeps one import and one line for both (the page is at its line cap).
 */
import AppDetailBrowserSites from './AppDetailBrowserSites'
import AppDetailDashboardAccess from './AppDetailDashboardAccess'

interface Props {
  appName: string
  displayName?: string
  onChanged: () => void
  onUpdate?: () => Promise<void>
}

export default function AppDetailAppAccess({ appName, displayName, onChanged, onUpdate }: Props) {
  return (
    <>
      <AppDetailBrowserSites appName={appName} onChanged={onChanged} />
      <AppDetailDashboardAccess appName={appName} displayName={displayName} onUpdate={onUpdate} />
    </>
  )
}
