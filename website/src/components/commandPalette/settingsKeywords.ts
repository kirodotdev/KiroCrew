/**
 * Settings keyword synonyms (Search Everywhere — Settings provider).
 *
 * Manual overlay of additional search terms that are NOT in the label or
 * description. Merged into the search corpus by the settings provider so
 * queries like "dark mode" find the "Mode" setting on the Display tab.
 *
 * Keys must be setting ids that exist in SETTINGS_REGISTRY — enforced by
 * settingsKeywords.test.ts, which fails CI on unknown ids.
 */
export const SETTINGS_KEYWORDS: Record<string, string[]> = {
  // Display
  'display.mode': ['dark mode', 'light mode', 'theme', 'appearance', 'dark', 'light', 'night'],
  'display.theme': ['theme', 'appearance', 'skin', 'palette'],
  'display.zoom-level': ['magnification', 'scale', 'bigger', 'smaller', 'bigger text', 'smaller text', 'accessibility'],
  'display.font-family': ['typeface', 'monospace', 'sans-serif'],
  'display.interface': ['chat mode', 'cli mode', 'bubbles', 'terminal'],

  // Chat
  'chat.fallback-model': ['model', 'llm', 'opus', 'sonnet', 'haiku', 'gpt', 'default model', 'fallback model', 'switch model', 'which model'],
  'chat.default-reasoning-effort': ['reasoning', 'thinking', 'effort', 'thinking depth', 'xhigh', 'reasoning effort'],
  'chat.auto-compact-threshold': ['context window', 'compaction', 'memory', 'conversation length'],
  'chat.show-timestamps': ['time', 'clock', 'message time'],
  'chat.merge-queued-messages': ['queue', 'batch', 'combine messages'],
  'chat.quick-send': ['fast send', 'enter to send', 'hotkey'],

  // Voice
  'voice.enabled': ['tts', 'speak', 'read aloud', 'narrate', 'text-to-speech'],
  'voice.auto-speak-responses': ['automatic speech', 'auto read'],
  'voice.language': ['stt', 'dictation', 'microphone', 'whisper', 'transcribe', 'speech-to-text'],

  // Developer
  'developer.developer-mode': ['dev mode', 'debug', 'advanced', 'power user'],

  // Notifications
  'notifications.play-sound-on-new-notifications': ['alert', 'audio', 'mute', 'silent'],

  // Browser
  'browser.enable-browser-mode': ['playwright', 'headless', 'web automation', 'chromium', 'firefox', 'webkit'],
  'browser.attach-to-my-running-browser': ['extension', 'chrome', 'edge', 'brave', 'arc', 'opera', 'attach'],

  // Computer use
  'computer-use.enable-computer-use': ['accessibility', 'desktop', 'click', 'keyboard', 'screenshot', 'a11y', 'automation', 'gui'],
  'computer-use.attach-screenshots': ['screen capture', 'screenshot', 'pixels', 'window image'],

  // General navigation
  'chat.split-view-session-grid': ['split pane', 'multi session', 'grid view'],
}
