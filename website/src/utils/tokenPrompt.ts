const MAX_PROMPT_LENGTH = 4000
const MAX_CLAIM_LENGTH = 256

function decodePayload(token: string): Record<string, unknown> | null {
  try {
    const parts = token.split('.')
    if (parts.length < 2) return null
    return JSON.parse(atob(parts[0].replace(/-/g, '+').replace(/_/g, '/')))
  } catch {
    return null
  }
}

/** Extract a prompt from a presigned token's payload (channel challenge-and-redirect). */
export function extractPromptFromToken(token: string): string | null {
  const payload = decodePayload(token)
  if (!payload) return null
  const prompt = payload.prompt
  if (typeof prompt !== 'string' || prompt.length === 0) return null
  if (prompt.length > MAX_PROMPT_LENGTH) return null
  return prompt
}

export interface TokenSlackContext {
  /** Existing dashboard session key already linked to the Slack thread, if any. */
  sessionKey: string | null
  /** Originating Slack channel id. */
  channel: string | null
  /** Originating Slack thread_ts (root message ts). */
  threadTs: string | null
}

/**
 * Extract Slack-thread context from a token payload so the dashboard can
 * reconnect to (session_key) or auto-link (channel + thread_ts) the correct
 * session instead of always spawning a fresh, disconnected one.
 */
export function extractSlackContextFromToken(token: string): TokenSlackContext {
  const payload = decodePayload(token)
  const pick = (k: string): string | null => {
    const v = payload?.[k]
    return typeof v === 'string' && v.length > 0 && v.length <= MAX_CLAIM_LENGTH ? v : null
  }
  return {
    sessionKey: pick('session_key'),
    channel: pick('channel'),
    threadTs: pick('thread_ts'),
  }
}
