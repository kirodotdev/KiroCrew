export interface KnowledgeItem {
  id: string
  title: string
  summary?: string
  item_type: string
  status: string
  tags?: string
  content?: string
  created_at: string
  updated_at: string
  namespace?: string
  source_id?: string
  entities?: Entity[]
  relations?: Relation[]
  source_locations?: SourceLocation[]
  _score?: number
  _match_type?: string
  _file_path?: string
}

export interface Entity {
  id: string
  name: string
  entity_type: string
  description?: string
  mention_count?: number
}

export interface Relation {
  id: string
  source_id: string
  target_id: string
  relation_type: string
  weight?: number
  source_name?: string
  target_name?: string
}

export interface SourceLocation {
  item_id: string
  source_id: string
  section_title?: string
  chunk_range?: string
}

export interface SourceSummary {
  topic?: string
  themes?: string[]
  generated_at?: string
}

export interface Source {
  id: string
  name: string
  source_type: string
  uri?: string
  sync_status: string
  last_synced?: string
  item_count?: number
  properties?: string | Record<string, unknown>
  summary_topic?: string
  summary_themes?: string
}

export interface SourceFileInfo {
  file_path: string
  status: 'pending' | 'scanning' | 'done' | 'failed' | 'skipped'
  error_message?: string | null
  mtime: number
  item_count: number
}

export interface SourceFilesResponse {
  files: SourceFileInfo[]
  total: number
  done: number
  failed: number
  skipped: number
}

export interface Stats {
  items: number
  entities: number
  relations: number
  sources: number
}

export interface GraphData {
  nodes: { id: string; name: string; type: string }[]
  edges: { source: string; target: string; type: string; weight?: number }[]
}

export interface NamespaceInfo {
  name: string
  count: number
}

export interface IngestionJob {
  name: string
  status: string
}
