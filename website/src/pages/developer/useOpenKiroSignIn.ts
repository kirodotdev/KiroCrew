import { useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAppDispatch, useAppSelector } from '../../store'
import { localCovered, setActiveId } from '../../store/instancesSlice'
import { useGuardedLeave } from '../../components/NavigationLeaveGuard'
import { KIRO_SIGN_IN_PATH } from './kiroSignInLink'

/**
 * Opens Settings > Kiro account from a control that can show while a relay or
 * remote Crew pane covers the local one (the switcher, the top bar, the account
 * popup). The local Crew is brought forward first whenever a Crew pane or a
 * relay pane covers it (`localCovered`), as a crew link's refusal does, so
 * the leave guard's confirmation and the sign-in card are both visible; a
 * navigation alone would only move the hidden local route.
 * `before` runs once the leave is allowed (a popup closing itself).
 */
export function useOpenKiroSignIn(): (before?: () => void) => void {
  const dispatch = useAppDispatch()
  const covered = useAppSelector(localCovered)
  const navigate = useNavigate()
  const leave = useGuardedLeave()
  return useCallback((before?: () => void) => {
    if (covered) dispatch(setActiveId(null))
    leave(() => {
      before?.()
      navigate(KIRO_SIGN_IN_PATH)
    }, KIRO_SIGN_IN_PATH)
  }, [covered, dispatch, leave, navigate])
}
