/** The store's connection flag and its context-free mirror, moved together
 *  or not at all.
 *
 *  `dashboardSlice.connected` drives store-connected UI; the seam in
 *  `utils/errorReport` drives AskAgentButton, which must stay mountable
 *  outside the Provider (ErrorBoundary fallbacks) and so cannot read the
 *  store. App embeds' push channel (`utils/appSlotPush.ts`) follows the same flag.
 *  A socket site that set one without the other would split the two
 *  truths, so these helpers are the only sanctioned way for the socket layer
 *  to move the flag (`test/gatewayConnectedPairing.test.ts` pins that). */
import type { AppDispatch } from '../../store'
import { sseConnected, sseDisconnected } from '../../store/dashboardSlice'
import { setGatewayConnected } from '../../utils/errorReport'
import { setAppSlotPushConnected } from '../../utils/appSlotPush'

export function markGatewayConnected(dispatch: AppDispatch): void {
  dispatch(sseConnected())
  setGatewayConnected(true)
  setAppSlotPushConnected(true)
}

export function markGatewayDisconnected(dispatch: AppDispatch): void {
  dispatch(sseDisconnected())
  setGatewayConnected(false)
  setAppSlotPushConnected(false)
}
