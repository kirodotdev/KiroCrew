import { useCallback, useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { PIN_PREVIEW_INPUT_MAX_CHARS, pinsApi, type ChatPin, type PinMessageBody } from '../api/pins'

const pinQueryKey = (slotKey: string | undefined) => ['chat-pins', slotKey] as const

type UnpinMutation = { id: string; slotKey: string }

/**
 * Hook to manage chat message pins for a given slot.
 * Uses React Query with optimistic updates – eliminates stale-closure race
 * conditions when the user switches slots quickly (each slot has its own
 * query key, so a late response for slot A never overwrites slot B's cache).
 */
export function useChatPins(slotKey: string | undefined) {
  const qc = useQueryClient()
  const queryKey = useMemo(() => pinQueryKey(slotKey), [slotKey])
  const [error, setError] = useState<'pin' | 'unpin' | null>(null)
  const clearError = useCallback(() => setError(null), [])

  const { data: pins = [], isLoading: loading } = useQuery<ChatPin[]>({
    queryKey,
    queryFn: async () => {
      const res = await pinsApi.list(slotKey!)
      return res.pins
    },
    enabled: !!slotKey,
  })

  const { mutateAsync: pinMessageAsync } = useMutation({
    mutationFn: (body: PinMessageBody) => pinsApi.create(body),
    onMutate: async (body: PinMessageBody) => {
      setError(null)
      const mutationQueryKey = pinQueryKey(body.slot_key)
      await qc.cancelQueries({ queryKey: mutationQueryKey })
      const prev = qc.getQueryData<ChatPin[]>(mutationQueryKey)
      const optimistic: ChatPin = {
        id: `temp-${crypto.randomUUID()}`,
        slot_key: body.slot_key,
        message_ts: body.message_ts,
        role: body.role,
        preview: body.preview,
        pinned_at: new Date().toISOString(),
      }
      qc.setQueryData<ChatPin[]>(mutationQueryKey, old => [...(old ?? []), optimistic])
      return { prev, optimisticId: optimistic.id, queryKey: mutationQueryKey }
    },
    onError: (_err, _body, ctx) => {
      if (ctx?.prev) qc.setQueryData(ctx.queryKey, ctx.prev)
      setError('pin')
    },
    onSuccess: (real, _body, ctx) => {
      if (!ctx) return
      // Replace the temp entry with the server-confirmed pin in its originating slot.
      qc.setQueryData<ChatPin[]>(ctx.queryKey, old =>
        (old ?? []).map(p => p.id === ctx.optimisticId ? real : p),
      )
    },
    onSettled: (_data, _error, body) => {
      qc.invalidateQueries({ queryKey: pinQueryKey(body.slot_key) })
    },
  })

  const { mutateAsync: unpinMessageAsync } = useMutation({
    mutationFn: ({ id }: UnpinMutation) => pinsApi.remove(id),
    onMutate: async ({ id, slotKey }: UnpinMutation) => {
      setError(null)
      const mutationQueryKey = pinQueryKey(slotKey)
      await qc.cancelQueries({ queryKey: mutationQueryKey })
      const prev = qc.getQueryData<ChatPin[]>(mutationQueryKey)
      qc.setQueryData<ChatPin[]>(mutationQueryKey, old =>
        (old ?? []).filter(p => p.id !== id),
      )
      return { prev, queryKey: mutationQueryKey }
    },
    onError: (_err, _mutation, ctx) => {
      if (ctx?.prev) qc.setQueryData(ctx.queryKey, ctx.prev)
      setError('unpin')
    },
    onSettled: (_data, _error, mutation) => {
      qc.invalidateQueries({ queryKey: pinQueryKey(mutation.slotKey) })
    },
  })

  const isPinned = useCallback(
    (messageTs: string) => pins.some(p => p.message_ts === messageTs),
    [pins],
  )

  const pinMessage = useCallback(
    async (body: Omit<PinMessageBody, 'slot_key'>) => {
      if (!slotKey) return
      await pinMessageAsync({
        ...body,
        slot_key: slotKey,
        preview: body.preview.slice(0, PIN_PREVIEW_INPUT_MAX_CHARS),
      })
    },
    [slotKey, pinMessageAsync],
  )

  const unpinMessage = useCallback(
    async (messageTs: string) => {
      if (!slotKey) return
      const pin = pins.find(p => p.message_ts === messageTs)
      if (!pin) return
      await unpinMessageAsync({ id: pin.id, slotKey: pin.slot_key })
    },
    [slotKey, pins, unpinMessageAsync],
  )

  const unpinById = useCallback(
    async (id: string) => {
      if (!slotKey) return
      const pin = pins.find(candidate => candidate.id === id)
      await unpinMessageAsync({ id, slotKey: pin?.slot_key ?? slotKey })
    },
    [slotKey, pins, unpinMessageAsync],
  )

  const refresh = useCallback(() => {
    qc.invalidateQueries({ queryKey })
  }, [qc, queryKey])

  return {
    pins,
    loading,
    error,
    clearError,
    isPinned,
    pinMessage,
    unpinMessage,
    unpinById,
    refresh,
  }
}
