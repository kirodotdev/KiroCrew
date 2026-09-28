import { i18nT } from '../i18n/t'

/** Folder color palette — the identity mark a user picks for a folder in
 *  the config modal. Shares the Artifacts page's FOLDER_COLORS hues so the
 *  two folder systems speak one visual language. KEEP IN SYNC with
 *  `_FOLDER_COLOR_PALETTE` in src/kiro_crew/dashboard/chat_folders.py.
 *  Labels are thunks with literal keys (not `i18nT(`…${name}`)`) so every
 *  reference stays statically resolvable by the i18n key checker. */
export const FOLDER_COLOR_PALETTE: { value: string; label: () => string }[] = [
  { value: '#ef4444', label: () => i18nT('components.folderColorNames.red') },
  { value: '#f97316', label: () => i18nT('components.folderColorNames.orange') },
  { value: '#f59e0b', label: () => i18nT('components.folderColorNames.amber') },
  { value: '#84cc16', label: () => i18nT('components.folderColorNames.lime') },
  { value: '#22c55e', label: () => i18nT('components.folderColorNames.green') },
  { value: '#14b8a6', label: () => i18nT('components.folderColorNames.teal') },
  { value: '#06b6d4', label: () => i18nT('components.folderColorNames.cyan') },
  { value: '#3b82f6', label: () => i18nT('components.folderColorNames.blue') },
  { value: '#6366f1', label: () => i18nT('components.folderColorNames.indigo') },
  { value: '#8b5cf6', label: () => i18nT('components.folderColorNames.violet') },
  { value: '#ec4899', label: () => i18nT('components.folderColorNames.pink') },
  { value: '#94a3b8', label: () => i18nT('components.folderColorNames.gray') },
]
