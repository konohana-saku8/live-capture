/** 全局状态 + 数据加载。视图只渲染，取数的时序都收在这里。 */
import { computed, reactive } from 'vue'

import { api, postJson } from '@/api/client'
import { segKeyOf, segmentOf } from '@/lib/format'
import type {
  Account, ConfigPayload, DynConfig, Dynamics, FeedKind, FeedRow,
  LogRow, Room, Segment, ServiceInfo, SysInfo,
} from '@/types'

/** 弹幕一次取多少条：整场动辄两万条，全塞进 DOM 手机会卡。 */
export const FEED_PAGE = 400
/** 采集器清单每页 3 个。 */
export const ROOM_PAGE = 3
const SUBTABS = ['live', 'dyn'] as const

type HintKind = '' | 'ok' | 'err'
export interface Hint { msg: string; kind: HintKind }

const savedFilters = (() => {
  try {
    const raw = JSON.parse(localStorage.getItem('lc-filters') || 'null')
    return raw && typeof raw === 'object' ? raw : null
  } catch {
    return null
  }
})()

export const state = reactive({
  ready: false,
  gate: { open: false, mode: 'unlock' as 'unlock' | 'setup' },

  rooms: [] as Room[],
  active: 0,
  running: false,
  roomId: Number(localStorage.getItem('lc-room') || 0) || 0,
  roomPage: 0,

  subTab: (localStorage.getItem('lc-subtab') || 'live') as (typeof SUBTABS)[number],
  collectRoomId: 0,

  filters: {
    danmaku: savedFilters?.danmaku !== false,
    sc: savedFilters?.sc !== false,
    events: savedFilters?.events !== false,
  } as Record<FeedKind, boolean>,
  q: '',
  autoRefresh: true,

  segs: [] as Segment[],
  sessSel: '',
  sessStart: 0,
  sessEnd: 0,
  counts: {} as Record<FeedKind, number>,
  feedRows: { danmaku: [], sc: [], events: [] } as Record<FeedKind, FeedRow[]>,
  feedMore: { danmaku: false, sc: false, events: false } as Record<FeedKind, boolean>,
  loadingMore: { danmaku: false, sc: false, events: false } as Record<FeedKind, boolean>,
  feedEmpty: '',

  /** 有未保存内容时拦一下离开：动态译文、采集参数。 */
  dirty: { dyn: false, cfg: false },

  dyn: null as (null | {
    total: number; items: Dynamics[]; limit: number; offset: number
    config: DynConfig; status: Record<string, unknown>
  }),
  dynPage: 0,
  dynPageSize: 10,
  dynQuery: '',

  svc: null as ServiceInfo | null,
  cfg: null as ConfigPayload | null,
  sys: null as SysInfo | null,
  dash: null as null | {
    totals: Record<string, number>
    timeline: { labels: string[]; danmaku: number[]; sc: number[]; events: number[] }
    top_sc: any[]
    last_received_at: number
  },
  accounts: [] as Account[],
  serverLogs: [] as LogRow[],
  webLogs: [] as LogRow[],

  hints: {
    room: { msg: '', kind: '' } as Hint,
    capture: { msg: '', kind: '' } as Hint,
    collect: { msg: '', kind: '' } as Hint,
    dyn: { msg: '', kind: '' } as Hint,
    acct: { msg: '', kind: '' } as Hint,
    cfg: { msg: '', kind: '' } as Hint,
    add: { msg: '', kind: '' } as Hint,
  },
})

export const currentRoom = computed(
  () => state.rooms.find((r) => r.id === state.roomId) || null,
)

export const visibleRooms = computed(() => {
  const from = state.roomPage * ROOM_PAGE
  return state.rooms.slice(from, from + ROOM_PAGE)
})

export const roomPages = computed(() => Math.max(1, Math.ceil(state.rooms.length / ROOM_PAGE)))

export const currentSeg = computed(() => segmentOf(state.segs, state.sessSel))

/* ---------------- 通用 ---------------- */

export function hint(key: keyof typeof state.hints, msg: string, kind: HintKind = '') {
  state.hints[key] = { msg: msg || '', kind }
  if (msg) uiLog(kind === 'err' ? 'error' : 'info', key + '：' + msg)
}

export function uiLog(level: string, msg: string) {
  state.webLogs.push({ ts: Date.now(), level: 'web', msg: '[' + level + '] ' + String(msg) })
  if (state.webLogs.length > 300) state.webLogs = state.webLogs.slice(-300)
}

export function setBadgeRooms(payload: { items?: Room[]; active?: number; running?: boolean }) {
  state.rooms = payload.items || []
  state.active = payload.active || 0
  state.running = !!payload.running
}

/* ---------------- 仪表盘 / 系统 / 状态 ---------------- */

export async function loadDashboard() {
  try {
    const j = await api('/api/dashboard')
    state.dash = j
    state.svc = j.service
    state.cfg = j.config
    setBadgeRooms(j.rooms || {})
  } catch { /* 轮询里静默 */ }
}

export async function loadSystem() {
  try {
    state.sys = (await api('/api/system')).system
  } catch { /* ignore */ }
}

export async function loadStatus() {
  try {
    const j = await api('/api/status')
    state.cfg = j.config
    state.svc = j.service
    state.active = j.capture?.active || 0
    state.running = !!j.capture?.running
  } catch { /* ignore */ }
}

/* ---------------- 采集器 ---------------- */

export async function loadRooms() {
  try {
    setBadgeRooms(await api('/api/rooms'))
    normalizeSelection()
  } catch { /* ignore */ }
}

/** 选中的采集器可能已被删掉：回落到第一个，并把清单翻到它所在页。 */
function normalizeSelection() {
  if (!state.rooms.length) {
    state.roomId = 0
    state.roomPage = 0
    return
  }
  if (!state.rooms.some((r) => r.id === state.roomId)) {
    state.roomId = state.rooms[0].id
    localStorage.setItem('lc-room', String(state.roomId))
    jumpRoomPage(state.roomId)
  }
  state.roomPage = Math.min(Math.max(0, state.roomPage), roomPages.value - 1)
}

export function jumpRoomPage(id: number) {
  const i = state.rooms.findIndex((r) => r.id === id)
  if (i >= 0) state.roomPage = Math.floor(i / ROOM_PAGE)
}

export async function selectRoom(id: number) {
  state.roomId = id
  localStorage.setItem('lc-room', String(id))
  jumpRoomPage(id)
  // 切采集器时场次回到最近一段，并清掉上一家的动态
  state.sessSel = ''
  state.sessStart = 0
  state.sessEnd = 0
  state.collectRoomId = 0
  state.dyn = null
  await loadRooms()
  await loadSessions()
}

export async function addRoom(uid: number, label: string) {
  const j = await postJson('/api/rooms', { uid, label })
  state.roomId = j.room.id
  localStorage.setItem('lc-room', String(state.roomId))
  state.roomPage = 1e9
  await loadRooms()
  await loadSessions()
  return j
}

export async function removeRoom(room: Room) {
  const j = await postJson('/api/rooms/remove', { room_id: room.id })
  setBadgeRooms(j.rooms || {})
  normalizeSelection()
  return j
}

export async function saveCollect(room: Room, params: Record<string, unknown>) {
  const j = await postJson('/api/rooms/settings', { room_id: room.id, params })
  state.collectRoomId = 0
  if (j.rooms) setBadgeRooms(j.rooms)
  return j
}

export async function refreshRooms() {
  const j = await postJson('/api/rooms/refresh', {})
  if (j.rooms) setBadgeRooms(j.rooms)
  return j
}

export async function controlCapture(roomId: number, action: 'start' | 'stop') {
  const j = await postJson('/api/capture/' + action, { room_id: roomId })
  if (j.rooms) setBadgeRooms(j.rooms)
  return j
}

export async function stopAll() {
  const j = await postJson('/api/capture/stop_all', {})
  if (j.rooms) setBadgeRooms(j.rooms)
  return j
}

/* ---------------- 场次 ---------------- */

function applySegments(list: Segment[]) {
  state.segs = list || []
  if (!state.segs.length) {
    state.sessSel = ''
    state.sessStart = 0
    state.sessEnd = 0
    return false
  }
  // 只按场次看：首次进来或原来那段没了，就跳到最近一段
  if (!segmentOf(state.segs, state.sessSel)) {
    applySeg(state.segs[state.segs.length - 1])
    return true
  }
  return false
}

export function applySeg(s: Segment) {
  state.sessSel = segKeyOf(s)
  state.sessStart = s.start || 0
  state.sessEnd = s.end || 0
}

export async function loadSessions(opts: { tail?: boolean } = {}) {
  let changed = false
  if (!state.roomId) {
    changed = applySegments([])
  } else {
    try {
      const j = await api('/api/sessions?room_id=' + state.roomId)
      changed = applySegments(j.segments || [])
    } catch {
      changed = applySegments([])
    }
  }
  await loadFeed(opts.tail && !changed ? { tail: true } : {})
  return changed
}

export async function selectSession(key: string) {
  const s = segmentOf(state.segs, key)
  if (!s) return
  applySeg(s)
  await loadFeed()
}

/** 删除一个场次 / 场间：后端按时间段删数据，返回各类删除条数。 */
export async function deleteSession(seg?: Segment) {
  const s = seg || segmentOf(state.segs, state.sessSel)
  if (!s || !state.roomId) throw new Error('没有选中场次')
  return postJson('/api/session/delete', {
    room_id: state.roomId,
    start: s.start || 0,
    end: s.end || 0,
    kind: s.kind,
  })
}

/* ---------------- 弹幕 ---------------- */

function feedRowKey(k: FeedKind, r: FeedRow) {
  return k === 'sc' ? 's' + r.sc_id : k === 'danmaku' ? 'd' + r.seq : 'e' + r.seq
}

/** 增量刷新把新到的一页并进已有列表：按主键去重再按时间正序排。 */
export function mergeFeed(k: FeedKind, old: FeedRow[], fresh: FeedRow[]) {
  const seen: Record<string, boolean> = {}
  const out: FeedRow[] = []
  for (const r of old.concat(fresh || [])) {
    const key = feedRowKey(k, r)
    if (seen[key]) continue
    seen[key] = true
    out.push(r)
  }
  out.sort((a, b) => {
    const d = (a.received_at || 0) - (b.received_at || 0)
    return d || ((a.seq || 0) - (b.seq || 0))
  })
  return out
}

function applyFeed(j: any, tail: boolean) {
  const kinds: FeedKind[] = ['danmaku', 'sc', 'events']
  for (const k of kinds) {
    const rows: FeedRow[] = (j.items && j.items[k]) || []
    if (tail && state.feedRows[k].length) {
      state.feedRows[k] = mergeFeed(k, state.feedRows[k], rows)
      state.feedMore[k] = !!j.more?.[k] || state.feedMore[k]
    } else {
      state.feedRows[k] = rows
      state.feedMore[k] = !!j.more?.[k]
    }
  }
  state.counts = j.counts || {}
}

export async function loadFeed(opts: { tail?: boolean } = {}) {
  if (!state.roomId) {
    state.feedEmpty = '请先添加采集器'
    return
  }
  if (!state.sessSel) {
    state.feedEmpty = '先在左侧选一个场次'
    return
  }
  state.feedEmpty = ''
  const types = (Object.keys(state.filters) as FeedKind[]).filter((k) => state.filters[k])
  const lim = opts.tail ? 120 : FEED_PAGE
  let qs = '/api/feed?paged=1&room_id=' + state.roomId + '&limit=' + lim +
    '&types=' + types.join(',') + '&q=' + encodeURIComponent(state.q)
  if (state.sessStart) qs += '&since=' + state.sessStart
  if (state.sessEnd) qs += '&until=' + state.sessEnd
  try {
    applyFeed(await api(qs), !!opts.tail)
  } catch (e: any) {
    hint('capture', '读取内容失败：' + e.message, 'err')
  }
}

/** 「载入更早」：以当前最老一条的 received_at 为游标，含同一毫秒，前端去重。 */
export async function loadMore(kind: FeedKind) {
  const rows = state.feedRows[kind]
  if (!rows.length || state.loadingMore[kind] || !state.sessSel) return
  state.loadingMore[kind] = true
  let qs = '/api/feed?paged=1&room_id=' + state.roomId + '&types=' + kind +
    '&limit=' + FEED_PAGE + '&before=' + rows[0].received_at + '&incl=1' +
    '&q=' + encodeURIComponent(state.q)
  if (state.sessStart) qs += '&since=' + state.sessStart
  if (state.sessEnd) qs += '&until=' + state.sessEnd
  try {
    const j = await api(qs)
    state.feedRows[kind] = mergeFeed(kind, rows, (j.items && j.items[kind]) || [])
    state.feedMore[kind] = !!j.more?.[kind]
  } catch (e: any) {
    hint('capture', '载入更早内容失败：' + e.message, 'err')
  } finally {
    state.loadingMore[kind] = false
  }
}

/* ---------------- 动态 ---------------- */

export async function loadDynamics(keepPage = false) {
  const room = currentRoom.value
  if (!room) {
    state.dyn = null
    return
  }
  const q = state.dynQuery
  if (!keepPage) state.dynPage = 0
  const offset = state.dynPage * state.dynPageSize
  try {
    state.dyn = await api(
      '/api/dynamics?limit=' + state.dynPageSize + '&offset=' + offset +
      '&room_id=' + state.roomId + '&mid=' + (room.uid || 0) +
      (q ? '&q=' + encodeURIComponent(q) : ''),
    )
  } catch (e: any) {
    hint('dyn', '读取动态失败：' + e.message, 'err')
  }
}

export async function fetchDynamics() {
  const room = currentRoom.value
  if (!room) {
    hint('dyn', '请先选择采集器', 'err')
    return
  }
  hint('dyn', '正在从 B 站拉取动态…')
  try {
    const j = await postJson('/api/dynamics/fetch', {
      mid: room.uid || 0, room_id: room.id, limit: 20,
    })
    hint('dyn', j.message, j.ok ? 'ok' : 'err')
    if (j.ok) {
      state.dynPage = 0
      await loadDynamics()
      await loadRooms()
    }
  } catch (e: any) {
    hint('dyn', '拉取失败：' + e.message, 'err')
  }
}

export async function saveDynConfig(room: Room, dynAuto: boolean, hour: number, minute: number) {
  const j = await postJson('/api/dynamics/config', {
    room_id: room.id, dyn_auto: dynAuto, dyn_hour: hour, dyn_minute: minute,
  })
  if (state.dyn) state.dyn.config = j.config
  if (j.rooms) setBadgeRooms(j.rooms)
  return j
}

export async function saveDynTranslation(dynId: string, text: string) {
  return postJson('/api/dynamics/trans', { dyn_id: dynId, text })
}

/* ---------------- 账号 ---------------- */

export async function loadAccounts() {
  try {
    const j = await api('/api/accounts')
    state.accounts = j.items || []
  } catch (e: any) {
    hint('acct', '读取账号失败：' + e.message, 'err')
  }
}

export async function uploadAccount(name: string, csv: string) {
  const j = await postJson('/api/accounts', { name, csv, activate: true })
  state.accounts = j.accounts || []
  await loadStatus()
  return j
}

export async function accountAction(action: 'remove' | 'activate', id: string) {
  const j = await postJson('/api/accounts/' + action, { id })
  state.accounts = j.accounts || []
  return j
}

/* ---------------- 日志 ---------------- */

export async function loadLogs() {
  try {
    state.serverLogs = (await api('/api/logs?limit=500')).items || []
  } catch { /* ignore */ }
}
