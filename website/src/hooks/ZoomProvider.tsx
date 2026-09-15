import { createContext, useContext } from 'react'
import { useZoom } from './useZoom'

type ZoomCtx = ReturnType<typeof useZoom>
const Ctx = createContext<ZoomCtx | null>(null)

export function ZoomProvider({ children }: { children: React.ReactNode }) {
  return <Ctx.Provider value={useZoom()}>{children}</Ctx.Provider>
}

export function useZoomCtx(): ZoomCtx {
  const v = useContext(Ctx)
  if (!v) throw new Error('useZoomCtx must be used within ZoomProvider')
  return v
}
