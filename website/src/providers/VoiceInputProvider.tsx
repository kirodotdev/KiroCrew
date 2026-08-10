import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { useVoiceInput } from '../hooks/useVoiceInput'

type VoiceOptions = NonNullable<Parameters<typeof useVoiceInput>[1]>
type VoiceHandlers = {
  onText: Parameters<typeof useVoiceInput>[0]
  onPartial?: NonNullable<VoiceOptions>['onPartial']
  onEndpoint?: NonNullable<VoiceOptions>['onEndpoint']
}

interface VoiceInputContextValue {
  voice: ReturnType<typeof useVoiceInput>
  configure: (options: VoiceOptions, handlers: VoiceHandlers) => void
  unregister: (handlers: VoiceHandlers) => void
}

const VoiceInputContext = createContext<VoiceInputContextValue | null>(null)

export function VoiceInputProvider({ children }: { children: React.ReactNode }) {
  const [options, setOptions] = useState<VoiceOptions>({})
  const handlersRef = useRef<VoiceHandlers | null>(null)
  const pendingTextRef = useRef<Array<{ text: string; sessionId: string | null }>>([])
  const voice = useVoiceInput(
    (text, sessionId) => {
      if (handlersRef.current) handlersRef.current.onText(text, sessionId)
      else pendingTextRef.current.push({ text, sessionId })
    },
    {
      ...options,
      onPartial: (text, sessionId) => handlersRef.current?.onPartial?.(text, sessionId),
      onEndpoint: () => handlersRef.current?.onEndpoint?.(),
    },
  )

  const configure = useCallback((next: VoiceOptions, handlers: VoiceHandlers) => {
    handlersRef.current = handlers
    const pending = pendingTextRef.current
    pendingTextRef.current = []
    pending.forEach(({ text, sessionId }) => handlers.onText(text, sessionId))
    setOptions(previous => {
      if (previous.streaming === next.streaming && previous.sessionId === next.sessionId) {
        return previous
      }
      return next
    })
  }, [])

  const unregister = useCallback((handlers: VoiceHandlers) => {
    if (handlersRef.current === handlers) handlersRef.current = null
  }, [])

  return (
    <VoiceInputContext.Provider value={{ voice, configure, unregister }}>
      {children}
    </VoiceInputContext.Provider>
  )
}

export function usePersistentVoiceInput(
  onText: Parameters<typeof useVoiceInput>[0],
  options: VoiceOptions = {},
) {
  const context = useContext(VoiceInputContext)
  if (!context) {
    // Direct page mounts remain compatible with the hook's pre-provider API.
    // eslint-disable-next-line react-hooks/rules-of-hooks
    return useVoiceInput(onText, options)
  }

  // The provider branch owns the long-lived hook instance.
  // eslint-disable-next-line react-hooks/rules-of-hooks
  return useProviderVoiceInput(context, onText, options)
}

function useProviderVoiceInput(
  context: VoiceInputContextValue,
  onText: Parameters<typeof useVoiceInput>[0],
  options: VoiceOptions,
) {
  const { configure, unregister } = context
  const handlers = {
    onText,
    onPartial: options.onPartial,
    onEndpoint: options.onEndpoint,
  }
  useEffect(() => {
    configure(options, handlers)
    return () => unregister(handlers)
  }, [configure, handlers, options, unregister])

  return context.voice
}
