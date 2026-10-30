import type { ComponentProps } from 'react'
import SessionActionsMenu from '../components/SessionActionsMenu'
import { useSessionActions } from '../hooks/useSessionActions'

/** A session menu that owns its own actions, for tests that render one menu
 *  alone. Production owners pass `actions` and render the confirmation above
 *  the menu content. */
export default function OwnedSessionActionsMenu({ mode, ...props }: Omit<ComponentProps<typeof SessionActionsMenu>, 'actions'> & { mode?: string }) {
  const actions = useSessionActions(mode)
  return <><SessionActionsMenu {...props} actions={actions} />{actions.confirmDialog}</>
}
