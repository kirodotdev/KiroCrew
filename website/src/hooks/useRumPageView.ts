import { useEffect } from 'react'
import { useLocation } from 'react-router-dom'
import { recordEvent } from '../rum'

/**
 * Track page views on route change. Drop this into App and every
 * navigation will emit a `page_view` custom event to RUM.
 */
export function useRumPageView(): void {
  const { pathname } = useLocation()

  useEffect(() => {
    recordEvent('page_view', { page: pathname })
  }, [pathname])
}
