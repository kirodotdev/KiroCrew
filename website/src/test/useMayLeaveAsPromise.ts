import { useMayLeaveNow } from '../components/NavigationLeaveGuard'

/** Tests read the leave answer as a promise whether or not a guard prompted. */
export function useMayLeaveAsPromise(): () => Promise<boolean> {
  const askNow = useMayLeaveNow()
  return () => Promise.resolve(askNow())
}
