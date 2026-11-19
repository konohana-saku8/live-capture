/** 接口返回的实体类型：只写页面真正用到的字段。 */

export interface RoomTotal {
  danmaku: number
  sc: number
  events: number
  sc_amount: number
  sessions: number
  sessions_approx?: boolean
  viewers: number
  interactions: number
  sc_peak?: number
  last_at?: number
}

export interface Room {
  id: number
  uid?: number
  label?: string
  uname?: string
  face?: string
  title?: string
  running: boolean
  waiting?: boolean
  live_status?: number
  live_only?: boolean
  auto_record?: boolean
  recording?: boolean
  record_started_at?: number
  record_bytes?: number
  record_path?: string
  record_error?: string
  record_qn?: number
  record_qn_actual?: number
  record_status?: string
  collect_live?: boolean
  collect_dynamics?: boolean
  record_danmaku?: boolean
  record_sc?: boolean
  record_events?: boolean
  record_guard?: boolean
  record_gift?: boolean
  record_red_pocket?: boolean
  record_enter?: boolean
  record_interact?: boolean
  real_room_id?: number
  live_time?: number
  live_started_at?: number
  online?: number
  popularity?: number
  last_packet_at?: number
  started_at?: number
  last_error?: string
  warning?: string
  counts?: Record<string, number>
  live_stats?: {
    danmaku: number
    sc: number
    events: number
    sc_amount: number
    since: number
  } | null
  dyns?: number
  dyn_last_at?: number
  dyn_schedule?: string
  dyn_auto?: boolean
  dyn_last_ts?: number
  last_live_at?: number
  totals?: RoomTotal
}

export interface RecordingPart {
  index: number
  started_at: number
  bytes: number
  duration_ms: number
}

export interface RecordingInfo {
  id: number
  room_id: number
  label?: string
  live_started_at?: number
  started_at: number
  stopped_at?: number
  status: string
  requested_qn?: number
  actual_qn?: number
  bytes?: number
  duration_ms?: number
  error?: string
  flv_count: number
  mp4_count: number
  has_video: boolean
  mp4_parts: RecordingPart[]
  flv_parts?: RecordingPart[]
  merge_status?: string
  merge_error?: string
  merge_seconds?: number
  merged?: { bytes: number; duration_ms: number } | null
}

export interface Segment {
  kind: 'live' | 'gap'
  index: number
  start: number
  end?: number
  single?: boolean
  lead?: boolean
  sc_amount?: number
  viewers?: number
  danmaku?: number
  sc?: number
  interactions?: number
  recording?: RecordingInfo | null
}

export type FeedKind = 'danmaku' | 'sc' | 'events'

export interface FeedRow {
  seq?: number
  sc_id?: number
  received_at?: number
  start_time?: number
  uid?: number
  uname?: string
  text?: string
  message?: string
  message_trans?: string
  price?: number
  guard_level?: number
  medal_name?: string
  medal_level?: number
  is_admin?: boolean
  kind?: string
  detail?: string
  emote_map?: string
  is_emoticon?: boolean
  emote_url?: string
  emote_px?: number
}

export interface Dynamics {
  dyn_id: string
  pub_ts?: number
  text?: string
  text_trans?: string
  pics?: string[]
  jump_url?: string
}

export interface DynConfig {
  dyn_auto: boolean
  dyn_hour: number
  dyn_minute: number
  dyn_last_ts: number
  collect_dynamics?: boolean
}

export interface AccountProfile {
  uname?: string
  face?: string
  uid?: number
}

export interface Account {
  id: string
  label: string
  active?: boolean
  error?: string
  uid?: number
  sessdata_mask?: string
  cookie_count?: number
  file_name?: string
  profile?: AccountProfile
}

export interface ServiceInfo {
  started_at: number
  host: string
  port: number
  lan_ip: string
  data_dir: string
  websockets: boolean
}

export interface SysInfo {
  ts?: number
  cpu?: { percent?: number; cores?: number; scope?: string }
  memory?: { percent?: number; used?: number; total?: number }
  disk?: { percent?: number; used?: number; total?: number; free?: number }
  network?: { online?: boolean; lan_ip?: string }
  load?: Record<string, number>
  uptime?: number
  db_size?: number
}

export interface LogRow {
  ts: number
  level: string
  msg: string
}

export interface ConfigPayload {
  host: string
  port: number
  auto_start: boolean
  protover: number
  heartbeat_seconds: number
  stale_seconds: number
  record_danmaku: boolean
  record_sc: boolean
  record_events: boolean
  record_guard?: boolean
  record_gift?: boolean
  record_red_pocket?: boolean
  record_enter?: boolean
  record_interact?: boolean
  token_set?: boolean
  token?: string
}
