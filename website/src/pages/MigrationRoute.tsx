/**
 * Route element for /apps/migrate/:name. The router reuses one element across
 * app names, so the page is keyed by name: each migrating app opens fresh,
 * without the previous app's notice, cleanup card or install step.
 */
import { useParams } from 'react-router-dom'
import MigrationPage from './MigrationPage'

export default function MigrationRoute() {
  const { name } = useParams<{ name: string }>()
  return <MigrationPage key={name} />
}
