/**
 * Route element for /apps/detail/:name.
 *
 * The router reuses one element across every app name, so without a key the
 * detail page kept the previous app's state (a success message, the install
 * log, a local-switch notice, an open confirm) when the nav rail or a
 * migration banner jumped straight to another app. Keying it by name gives
 * each app a fresh page, the same as leaving the page and coming back.
 */
import { useParams } from 'react-router-dom'
import AppDetailPage from './AppDetailPage'

export default function AppDetailRoute() {
  const { name } = useParams<{ name: string }>()
  return <AppDetailPage key={name} />
}
