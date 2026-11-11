import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { ComputerUseSetupAction, ComputerUseSetupDoc } from '../api/client'

/** One cache entry for the setup document, shared by the Settings status card
 *  and the setup dialog, so an action taken in one shows in the other. */
export const COMPUTER_USE_SETUP_QK = ['computer-use-setup'] as const

/** Read the setup document. `refetchInterval` is the caller's bounded poll; the
 *  status card passes none and reads once. */
export function useComputerUseSetupQuery(options: {
  enabled?: boolean
  refetchInterval?: (doc: ComputerUseSetupDoc | undefined) => number | false
} = {}) {
  const { enabled = true, refetchInterval } = options
  return useQuery<ComputerUseSetupDoc>({
    queryKey: COMPUTER_USE_SETUP_QK,
    queryFn: api.getComputerUseSetup,
    enabled,
    refetchInterval: refetchInterval ? q => refetchInterval(q.state.data) : false,
    // A failed read is shown with its own Check again; an automatic retry storm
    // against a wedged driver would only delay that.
    retry: false,
  })
}

/** Run one setup action. The answer is the document the action left behind, and
 *  it replaces the cached one directly, so the view moves without a second read. */
export function useComputerUseSetupAction() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (action: ComputerUseSetupAction) => api.runComputerUseSetupAction(action),
    onSuccess: doc => {
      qc.setQueryData<ComputerUseSetupDoc>(COMPUTER_USE_SETUP_QK, doc)
    },
  })
}
