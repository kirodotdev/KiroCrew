/** The `speakMode` half of the voice config, as the playback owner last read
 *  it. Read aloud (`pages/chat/speakReply.ts`) runs outside the playback hook,
 *  in a click handler, so it reads the mode here instead of fetching the config
 *  on every click; the hook writes it whenever it re-reads the config or hears
 *  `voice-config-changed`. */

export type VoiceSpeakMode = 'full' | 'summary'

let current: VoiceSpeakMode = 'full'

/** Narrow a config value to a mode; anything unknown is `full`, today's behaviour. */
export function parseSpeakMode(value: unknown): VoiceSpeakMode {
  return value === 'summary' ? 'summary' : 'full'
}

export function getVoiceSpeakMode(): VoiceSpeakMode {
  return current
}

export function setVoiceSpeakMode(value: unknown): VoiceSpeakMode {
  current = parseSpeakMode(value)
  return current
}
