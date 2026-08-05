export type ID = string

export interface Page<T> {
  items: T[]
  page: number
  page_size: number
  total: number
}

export interface TaskRef {
  jobId: string
  status: TaskStatus
  deduplicated: boolean
  kind?: string
  href?: string
}

export type TaskStatus = 'queued' | 'leased' | 'running' | 'succeeded' | 'failed' | 'skipped' | 'effect_unknown' | 'cancelled'

export interface Bootstrap {
  csrf_token: string
  api_version: string
  features: Record<string, boolean>
  worker: { status?: string; connected?: boolean; [key: string]: unknown }
}

export interface Folder {
  id: ID
  parent_id: ID | null
  name: string
  sort_order: number
}

export interface Tag {
  id: ID
  name: string
  color?: string | null
  icon?: string | null
  sort_order?: number
}

export interface SiteType {
  id: ID
  name: string
  slug: string
  color?: string | null
  icon?: string | null
  sort_order: number
  enabled: boolean
  is_system: boolean
  is_default: boolean
  site_count: number
  created_at?: string
  updated_at?: string
}

export interface Site {
  id: ID
  folder_id: ID | null
  site_type_id?: ID | null
  site_type?: SiteType | null
  name: string
  url: string
  default_endpoint_id?: ID
  canonical_host?: string
  kind?: string
  status: string
  description?: string | null
  note?: string | null
  icon?: string | null
  pinned: boolean
  overview_hidden?: boolean
  sort_order?: number
  visit_count?: number
  last_visited_at?: string | null
  created_at?: string
  updated_at?: string
  tags?: Tag[]
  capabilities?: string[]
  account_count?: number
  unread_count?: number
  adapter?: string
  config?: Record<string, unknown>
  configured?: boolean
  configured_secret_fields?: string[]
}

export type SiteRecommendationReason = 'pinned' | 'frequent' | 'recent' | 'starter'

export interface SiteQuickAccess extends Site {
  recommendation_reason: SiteRecommendationReason
}

export interface OverviewSites {
  frequent: SiteQuickAccess[]
  recent: SiteQuickAccess[]
  has_usage: boolean
}

export type AuthState = 'not_configured' | 'unverified' | 'opening' | 'awaiting_user' | 'verifying' | 'ready' | 'expired' | 'error'

export interface SiteAccount {
  id: ID
  site_id: ID
  label: string
  enabled: boolean
  auth_state: AuthState
  configured: boolean
  configured_secret_fields: string[]
  adapter?: string
  config?: Record<string, unknown>
  config_json?: Record<string, unknown>
  last_verified_at?: string | null
  sort_order?: number
  schedule_override?: AccountSchedule | null
  profile?: {
    relative_path: string
    state: string
    manifest_sha256?: string | null
    file_count?: number | null
    total_bytes?: number | null
    last_verified_at?: string | null
    verification_error?: string | null
  } | null
}

export type AccountSchedule =
  | { mode: 'fixed_time'; time: string; timezone?: string }
  | { mode: 'custom_window'; start: string; end: string; timezone?: string }

export interface Product {
  id: ID
  site_id: ID
  site_name?: string
  category_id?: ID | null
  category_name?: string | null
  scraped_category_id?: ID | null
  scraped_category_name?: string | null
  category_overridden?: boolean
  external_key?: string | null
  name: string
  image_url?: string | null
  url?: string | null
  buy_endpoint_id?: ID | null
  price?: number | null
  original_price?: number | null
  currency?: string
  stock?: number | null
  availability_status?: string
  availability_reason?: string | null
  description?: string | null
  raw_tags?: string[]
  tags?: Tag[]
  last_observed_at?: string | null
  updated_at?: string
  price_history_summary?: PriceHistorySummary
}

export interface ProductCategory {
  id: ID
  site_id: ID
  site_name: string
  name: string
  source_name?: string | null
  sort_order: number
  product_count: number
  created_at?: string
  updated_at?: string
}

export interface PriceObservation {
  id: ID
  product_id: ID
  price: number | null
  stock: number | null
  availability_status: string
  observed_at: string
  source_run_id?: ID | null
}

export interface PriceHistorySummary {
  observation_count: number
  min_price: number | null
  max_price: number | null
  first_observed_at: string | null
  last_observed_at: string | null
}

export interface InboxEvent {
  id: ID
  type: string
  severity: 'info' | 'success' | 'warning' | 'error' | string
  title?: string
  message?: string
  entity_type?: string
  entity_id?: ID | null
  run_id?: ID | null
  payload_json?: Record<string, unknown>
  occurred_at: string
  read_at?: string | null
  dismissed_at?: string | null
}

export interface NotificationChannelConfig {
  event_types: string[]
  min_severity: 'success' | 'info' | 'warning' | 'danger'
}

export interface NotificationChannel {
  id: ID
  name: string
  channel_type: 'webhook'
  enabled: boolean
  endpoint_configured: boolean
  config: NotificationChannelConfig
  delivery: {
    pending: number
    sent: number
    failed: number
  }
  created_at: string
  updated_at: string
}

export interface NotificationChannelPage {
  items: NotificationChannel[]
  total: number
}

export interface Job {
  id: ID
  kind?: string
  site_id?: ID
  site_name?: string
  account_label?: string
  business_date?: string
  planned_at?: string | null
  available_at?: string | null
  status: TaskStatus | string
  attempt?: number
  max_attempts?: number
  message?: string | null
  error_message?: string | null
  progress?: number | null
  created_at?: string
  updated_at?: string
}

export interface Run {
  id: ID
  job_id?: ID | null
  site_id?: ID
  site_name?: string
  account_label?: string
  kind?: string
  status: string
  reason_code?: string | null
  message?: string | null
  started_at?: string | null
  finished_at?: string | null
  duration_ms?: number | null
  result_json?: Record<string, unknown>
}

export interface JsonSchema {
  type?: 'object' | 'string' | 'number' | 'integer' | 'boolean' | 'array'
  title?: string
  description?: string
  properties?: Record<string, JsonSchema>
  required?: string[]
  enum?: Array<string | number | boolean>
  default?: unknown
  format?: string
  minimum?: number
  maximum?: number
  minLength?: number
  maxLength?: number
  items?: JsonSchema
}

export interface UiField {
  label?: string
  help?: string
  placeholder?: string
  widget?: 'text' | 'textarea' | 'select' | 'password' | 'number' | 'switch'
  order?: number
  section?: string
  hidden?: boolean
}

export interface AdapterManifest {
  id: string
  name?: string
  version: string
  description?: string
  capabilities: string[]
  site_config_schema?: JsonSchema
  account_config_schema?: JsonSchema
  ui_schema?: Record<string, UiField>
  secret_fields: string[]
  defaults?: Record<string, unknown>
}

export interface ApiErrorBody {
  error: { code: string; message: string; field?: string; context?: Record<string, unknown> }
  request_id?: string
}
