/**
 * Human-readable display names for the model ids the backend serves.
 *
 * kiro-cli hands the dashboard bare ids (`claude-opus-4.8`, `gpt-5.6-sol`,
 * `minimax-m2.5`), which read as identifiers rather than product names. The
 * picker, the composer chip and the effort notes show the vendor's own spelling
 * where this table knows it, and fall back to the id VERBATIM otherwise — a new
 * model must never be dressed up with an invented name. Ids stay the value that
 * is persisted, filtered on and sent to the backend; only the rendering changes.
 */

const DISPLAY_NAMES: Readonly<Record<string, string>> = {
  auto: 'Auto',
  'claude-opus-5': 'Claude Opus 5',
  'claude-opus-4.8': 'Claude Opus 4.8',
  'claude-opus-4.7': 'Claude Opus 4.7',
  'claude-opus-4.6': 'Claude Opus 4.6',
  'claude-opus-4.5': 'Claude Opus 4.5',
  'claude-sonnet-5': 'Claude Sonnet 5',
  'claude-sonnet-4.6': 'Claude Sonnet 4.6',
  'claude-sonnet-4.5': 'Claude Sonnet 4.5',
  'claude-sonnet-4': 'Claude Sonnet 4',
  'claude-haiku-4.5': 'Claude Haiku 4.5',
  'claude-fable-5.1': 'Claude Fable 5.1',
  'claude-fable-5': 'Claude Fable 5',
  'gpt-5.6-sol': 'GPT-5.6 Sol',
  'gpt-5.6-terra': 'GPT-5.6 Terra',
  'gpt-5.6-luna': 'GPT-5.6 Luna',
  'gpt-5.5': 'GPT-5.5',
  'deepseek-3.2': 'DeepSeek-V3.2',
  'minimax-m2.5': 'MiniMax-M2.5',
  'minimax-m2.1': 'MiniMax-M2.1',
  'glm-5': 'GLM-5',
  'qwen3-coder-next': 'Qwen3-Coder-Next',
  'agi-nova-beta-1m': 'AGI Nova Beta 1M',
}

/** Display name for a model id; the id itself when none is known. */
export function modelDisplayName(id: string | undefined | null): string {
  if (!id) return ''
  return Object.prototype.hasOwnProperty.call(DISPLAY_NAMES, id) ? DISPLAY_NAMES[id] : id
}

/** True when the id has a display name distinct from itself. */
export function hasModelDisplayName(id: string | undefined | null): boolean {
  return !!id && Object.prototype.hasOwnProperty.call(DISPLAY_NAMES, id)
}
