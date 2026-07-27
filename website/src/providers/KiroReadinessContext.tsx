import { createContext, useContext, type ReactNode } from 'react'

const KiroReadinessContext = createContext(true)

export function KiroReadinessProvider({
  ready,
  children,
}: {
  ready: boolean
  children: ReactNode
}) {
  return (
    <KiroReadinessContext.Provider value={ready}>
      {children}
    </KiroReadinessContext.Provider>
  )
}

export function useKiroSessionReady(): boolean {
  return useContext(KiroReadinessContext)
}
