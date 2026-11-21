/** 全局状态 + 数据加载。视图只渲染，取数的时序都收在这里。 */
import { computed, reactive } from 'vue'

import { api, postJson } from '@/api/client'
import type {
  Account, ConfigPayload, DataRoom, DataStatus, DynConfig, Dynamics, LogDiagnostics,
  LogRow, LogStats, Room, ServiceInfo, SysInfo,
} from '@/types'

/** 采集器清单每页 3 个。 */
export const ROOM_PAGE = 3

type HintKind = '' | 'ok' | 'err'
export interface Hint { msg: string; kind: HintKind }

export const state = reactive({
  ready: false,
  gate: { open: false, mode: 'unlock' as 'unlock' | 'setup' },

  rooms: [] as Room[],
  /** 数据页的房间：采集器 + 只剩数据的房间，顺序按最近有数据的时间倒序。 */
  dataRooms: [] as DataRoom[],
  active: 0,
  running: false,
  roomId: Number(localStorage.getItem('lc-room') || 0) || 0,
  roomPage: 0,

  collectRoomId: 0,

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
    data_status: DataStatus | null
    last_received_at: number
  },
  accounts: [] as Account[],
  serverLogs: [] as LogRow[],
  webLogs: [] as LogRow[],
  logStats: null as LogStats | null,
  logDiag: null as LogDiagnostics | null,

  hints: {
    room: { msg: '', kind: '' } as Hint,
    capture: { msg: '', kind: '' } as Hint,
    collect: { msg: '', kind: '' } as Hint,
    sched: { msg: '', kind: '' } as Hint,
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

/** 只删某个录像里的一个分片；录像记录与其它分片保留。 */
export async function deleteVideoFile(kind: 'mp4' | 'flv', recId: number, index: number) {
  const j = await postJson<any>('/api/recordings/' + recId + '/file/delete',
    { kind, index })
  return j.deleted
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

/** 数据页的房间清单：采集器删掉后数据还在的房间也要能选到。 */
export async function loadDataRooms() {
  try {
    const j = await api<{ items?: DataRoom[] }>('/api/data/rooms')
    state.dataRooms = j.items || []
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
  // 切采集器时清掉上一家的动态，面板会按新 up 主自己取数
  state.collectRoomId = 0
  state.dyn = null
  await loadRooms()
}

export async function addRoom(uid: number, label: string) {
  const j = await postJson('/api/rooms', { uid, label })
  state.roomId = j.room.id
  localStorage.setItem('lc-room', String(state.roomId))
  state.roomPage = 1e9
  state.dyn = null
  await loadRooms()
  return j
}

export async function removeRoom(room: Room) {
  const j = await postJson('/api/rooms/remove', { room_id: room.id })
  state.dyn = null
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

export async function saveDynConfig(room: Room, collectDyn: boolean,
                                    dynAuto: boolean, hour: number, minute: number) {
  const j = await postJson('/api/dynamics/config', {
    room_id: room.id, collect_dynamics: collectDyn,
    dyn_auto: dynAuto, dyn_hour: hour, dyn_minute: minute,
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

/** `/accounts` 的增删启用返回的是 {ok, active_id, items}，不是数组。 */
function accountItems(payload: any): Account[] {
  return (payload && payload.items) || []
}

export async function uploadAccount(name: string, csv: string) {
  const j = await postJson('/api/accounts', { name, csv, activate: true })
  state.accounts = accountItems(j.accounts)
  await loadStatus()
  return j
}

export async function accountAction(action: 'remove' | 'activate', id: string) {
  const j = await postJson('/api/accounts/' + action, { id })
  state.accounts = accountItems(j.accounts)
  return j
}

/* ---------------- 日志 ---------------- */

export async function loadLogs() {
  try {
    const j = await api<any>('/api/logs?limit=2000')
    state.serverLogs = j.items || []
    state.logStats = j.stats || null
    state.logDiag = j.diagnostics || null
  } catch { /* ignore */ }
}
