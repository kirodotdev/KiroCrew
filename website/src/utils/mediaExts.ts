/** Extensions the dashboard plays as media rather than showing as an image or
 *  a file card. Shared by the file viewer (`detectFileType`) and the chat
 *  renderer (`ImgWithFallback`), so a file that plays in one surface plays in
 *  the other.
 *
 *  .ogg goes to audio: the extension is overwhelmingly audio in practice and
 *  the .ogv variant exists for video. .opus is an audio-only Ogg mapping, so it
 *  follows the same compact-player route. */
export const VIDEO_EXTS = new Set(['.mp4', '.m4v', '.webm', '.mov', '.mkv', '.ogv'])
export const AUDIO_EXTS = new Set(['.mp3', '.wav', '.m4a', '.flac', '.ogg', '.oga', '.opus'])

/** `'video'` / `'audio'` when the last path segment ends in a media extension,
 *  otherwise `null`. Pass a URL's pathname, not the whole URL, so a query or
 *  fragment never decides the kind. */
export function mediaKindOf(path: string): 'video' | 'audio' | null {
  const name = path.slice(Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\')) + 1)
  const dot = name.lastIndexOf('.')
  if (dot < 0) return null
  const ext = name.slice(dot).toLowerCase()
  if (VIDEO_EXTS.has(ext)) return 'video'
  if (AUDIO_EXTS.has(ext)) return 'audio'
  return null
}
