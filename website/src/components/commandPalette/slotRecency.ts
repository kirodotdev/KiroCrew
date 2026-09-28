export type SlotTimestamp = string | number

export interface SlotRecencyInput {
  last_activity_ts?: SlotTimestamp | null
  last_ts?: SlotTimestamp | null
  created?: SlotTimestamp | null
}

export interface SlotRecency {
  timestamp: SlotTimestamp | undefined
  epoch: number
}

function timestampEpoch(timestamp: SlotTimestamp | null | undefined): number | undefined {
  if (typeof timestamp === 'number') {
    return Number.isFinite(timestamp) ? timestamp * 1000 : undefined
  }
  if (!timestamp) return undefined
  const epoch = new Date(timestamp).getTime()
  return Number.isNaN(epoch) ? undefined : epoch
}

/** Select the latest parseable activity or message timestamp.
 * Creation time is a fallback only when neither message-derived field parses. */
export function slotRecency(slot: SlotRecencyInput): SlotRecency {
  const activityEpoch = timestampEpoch(slot.last_activity_ts)
  const messageEpoch = timestampEpoch(slot.last_ts)

  if (activityEpoch !== undefined || messageEpoch !== undefined) {
    if (messageEpoch !== undefined && (activityEpoch === undefined || messageEpoch >= activityEpoch)) {
      return { timestamp: slot.last_ts ?? undefined, epoch: messageEpoch }
    }
    return { timestamp: slot.last_activity_ts ?? undefined, epoch: activityEpoch ?? 0 }
  }

  const createdEpoch = timestampEpoch(slot.created)
  return {
    timestamp: createdEpoch === undefined ? undefined : (slot.created ?? undefined),
    epoch: createdEpoch ?? 0,
  }
}
