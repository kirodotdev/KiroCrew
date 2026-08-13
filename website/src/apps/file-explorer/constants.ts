export const API_BASE = '/apps/file-explorer/api'
export const STORAGE_KEY = 'kc:file-explorer:state:v2'

export const IMAGE_EXTS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.ico', '.svg'])

export const LANG_BY_EXT: Record<string, string> = {
  '.ts': 'typescript', '.tsx': 'typescript', '.js': 'javascript', '.jsx': 'javascript',
  '.mjs': 'javascript', '.cjs': 'javascript',
  '.py': 'python', '.rb': 'ruby', '.go': 'go', '.rs': 'rust',
  '.java': 'java', '.kt': 'kotlin', '.scala': 'scala', '.cs': 'csharp',
  '.c': 'c', '.h': 'c', '.cpp': 'cpp', '.cc': 'cpp', '.hpp': 'cpp',
  '.php': 'php', '.swift': 'swift', '.lua': 'lua', '.r': 'r',
  '.sh': 'bash', '.bash': 'bash', '.zsh': 'bash', '.fish': 'shell',
  '.json': 'json', '.yml': 'yaml', '.yaml': 'yaml', '.toml': 'toml',
  '.xml': 'xml', '.html': 'xml', '.htm': 'xml', '.svg': 'xml',
  '.css': 'css', '.scss': 'scss', '.less': 'less',
  '.sql': 'sql', '.graphql': 'graphql', '.gql': 'graphql',
  '.dockerfile': 'dockerfile', '.makefile': 'makefile',
  '.gradle': 'groovy', '.groovy': 'groovy',
  '.smithy': 'smithy', '.proto': 'protobuf',
}
