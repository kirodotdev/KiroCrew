/** The `/api/ws` wire envelope.
 *
 *  Every server push is one JSON text frame `{ type, data, ...siblings }`
 *  (`slots` carries its side channels beside `data`). Nothing validates a
 *  frame as a whole: the router switches on `type`, and each handler reads
 *  the fields it needs — some check them and drop a malformed frame, others
 *  hand the payload to its reducer as it came. A throw anywhere in a handler
 *  ends that frame's effects; the router swallows it. */

/** A decoded frame, or one of its fields, before a handler has checked it. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- untyped wire JSON: each handler checks the fields it reads
export type FrameData = any

export interface DecodedFrame {
  type: FrameData
  data: FrameData
  /** The whole envelope, for the frames that carry fields beside `data`. */
  msg: FrameData
}

/** Parse one text frame. Throws on malformed JSON or a `null` envelope; the
 *  router swallows that, so a malformed frame is dropped without effects. */
export function decodeFrame(raw: string): DecodedFrame {
  const msg = JSON.parse(raw)
  const { type, data } = msg
  return { type, data, msg }
}
