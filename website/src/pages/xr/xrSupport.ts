/**
 * Whether this browser can open an immersive session, and the switch that
 * shows the XR entry on a desk browser for previews and tests.
 */
import { useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

export type XrSupport = 'ar' | 'vr' | 'none'

export async function detectXr(): Promise<XrSupport> {
  const xr = typeof navigator === 'undefined' ? undefined : navigator.xr
  if (!xr) return 'none'
  if (await xr.isSessionSupported('immersive-ar').catch(() => false)) return 'ar'
  if (await xr.isSessionSupported('immersive-vr').catch(() => false)) return 'vr'
  return 'none'
}

/** Set to '1' in localStorage to show the XR entry on a browser with no headset. */
export const XR_PREVIEW_KEY = 'kirocrew:xr-preview'

export function xrPreviewForced(): boolean {
  try {
    return window.localStorage.getItem(XR_PREVIEW_KEY) === '1'
  } catch {
    return false
  }
}

const XR_SUPPORT_KEY = ['xr-support'] as const

/** The session kind this browser offers. Re-read when a headset is plugged in or removed. */
export function useXrSupport(): XrSupport {
  const client = useQueryClient()
  const { data } = useQuery({ queryKey: XR_SUPPORT_KEY, queryFn: detectXr, staleTime: Infinity })
  useEffect(() => {
    const xr = navigator.xr
    if (!xr) return
    const again = () => { void client.invalidateQueries({ queryKey: XR_SUPPORT_KEY }) }
    xr.addEventListener('devicechange', again)
    return () => xr.removeEventListener('devicechange', again)
  }, [client])
  return data ?? 'none'
}
