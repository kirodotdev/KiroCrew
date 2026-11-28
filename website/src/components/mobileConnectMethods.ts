import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { readAfterRenewal } from '../api/readAfterRenewal'

/**
 * The phone-connect methods this deployment offers, as
 * `/api/mobile-connect/methods` answers them: the edition's list, less what
 * policy denies. The endpoint is the authority, so a surface that mints for a
 * method reads it here and is drawn only when the method is listed. An empty
 * answer, a failed read and a read still in flight all offer none.
 */
export function useMobileConnectMethods() {
  return useQuery({
    queryKey: ['mobile-connect-methods'],
    // No retry, so a read refused at a lapsed sign-in would offer nothing until
    // the window next gets focus: it is read once more after the renewal.
    queryFn: () => readAfterRenewal(() => api.mobileConnectMethods()),
    staleTime: 5 * 60_000,
    retry: false,
  })
}
