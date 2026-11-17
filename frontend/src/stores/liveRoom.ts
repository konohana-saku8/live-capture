/** 数据页的独立状态：日历、录像、按页追加的消息。

选中场次后默认先显示最新一页（直播 / 回放都一样），往上滑才向前追加更早的页；
直播 / 尾部场间的新消息继续追加到列尾。任何滚动加载都只往已有列表里加行，不整页替换。
*/
import { computed, reactive } from 'vue'

import { api, postJson } from '@/api/client'
import { dayKey } from '@/lib/calendar'
import { state as appState } from '@/stores/app'
import type { FeedKind, FeedRow, Segment } from '@/types'

const PAGE = 200
const KINDS: FeedKind[] = ['danmaku', 'sc', 'events']
const TITLES: Record<FeedKind, string> = { danmaku: '弹幕', sc: 'SC', events: '事件' }

const savedLayout = localStorage.getItem('lc-live-layout')
const savedFilters = (() => {
  try {
    const raw = JSON.parse(localStorage.getItem('lc-live-filters') || 'null')
    return raw && typeof raw === 'object' ? raw : null
  } catch {
    return null
  }
})()

export const liveState = reactive({
  roomId: 0,
  segments: [] as Segment[],
  selectedDate: '',
  selectedKey: '',
  layout: (savedLayout === 'stack' ? 'stack' : 'columns') as 'columns' | 'stack',
  filters: {
    danmaku: savedFilters?.danmaku !== false,
    sc: savedFilters?.sc !== false,
    events: savedFilters?.events !== false,
  } as Record<FeedKind, boolean>,
  q: '',
  rows: { danmaku: [], sc: [], events: [] } as Record<FeedKind, FeedRow[]>,
  /** 前面还有更早的消息：滚到列顶时继续向前追加 */
  older: { danmaku: false, sc: false, events: false } as Record<FeedKind, boolean>,
  loading: { danmaku: false, sc: false, events: false } as Record<FeedKind, boolean>,
  currentMs: 0,
  summary: null as null | {
    count: number
    total_bytes: number
    free_bytes: number
    warn: boolean
    by_status?: Record<string, number>
  },
  loadingCal: false,
  error: '',
})

export function segKey(s: Segment) {
  return s.kind + '-' + s.index
}

export const selectedSeg = computed(
  () => liveState.segments.find((s) => segKey(s) === liveState.selectedKey) || null,
)

export const recording = computed(() => selectedSeg.value?.recording || null)

/** 视频 0 点：优先录像真正开始的时间，没录像时退回开播时间。 */
export const anchorMs = computed(() => {
  const s = selectedSeg.value
  if (!s) return 0
  return (s.recording && s.recording.started_at) || s.start || 0
})

/** 正在进行中的直播场（场间即使 end=0 也不是直播）。 */
export const isLiveSeg = computed(
  () => !!selectedSeg.value && selectedSeg.value.kind === 'live' && !selectedSeg.value.end,
)

/** 还没结束的段：直播场，或下播后仍在收数据的尾部场间。 */
export const isOpenSeg = computed(() => !!selectedSeg.value && !selectedSeg.value.end)

export const canPlay = computed(() => {
  const r = recording.value
  if (!r) return false
  if (isLiveSeg.value && r.status === 'recording') return false
  return r.mp4_count > 0 || r.flv_count > 0
})

export const canSeek = computed(() => canPlay.value && !isLiveSeg.value)

export const dayMap = computed(() => {
  const out: Record<string, Segment[]> = {}
  for (const s of liveState.segments) {
    const key = dayKey(s.start)
    if (!out[key]) out[key] = []
    out[key].push(s)
  }
  return out
})

export const selectedDaySegs = computed(() => dayMap.value[liveState.selectedDate] || [])

/* ---------------- 日历 / 场次 ---------------- */

export function setRoom(id: number) {
  const rid = Number(id) || 0
  // 数据页的房间选择同时驱动「动态」标签，所以同步到全局状态
  if (appState.roomId !== rid) {
    appState.roomId = rid
    if (rid) localStorage.setItem('lc-room', String(rid))
    appState.dyn = null
  }
  if (liveState.roomId !== rid) {
    liveState.roomId = rid
    liveState.segments = []
    liveState.selectedDate = ''
    liveState.selectedKey = ''
    liveState.currentMs = 0
    liveState.summary = null
    resetRows()
  }
  return loadCalendar()
}

/** keep=true 用于直播中的定时刷新：保留当前选中的场次与已加载的消息。 */
export async function loadCalendar(keep = false) {
  const rid = liveState.roomId
  if (!rid) {
    liveState.segments = []
    resetRows()
    return
  }
  liveState.loadingCal = true
  try {
    const [sess, sum] = await Promise.all([
      api<any>('/api/sessions?room_id=' + rid + '&with_recordings=1'),
      api<any>('/api/recordings/summary?room_id=' + rid).catch(() => null),
    ])
    if (liveState.roomId !== rid) return
    liveState.segments = (sess.segments || []) as Segment[]
    if (sum && sum.summary) liveState.summary = sum.summary
    liveState.error = ''
    ensureSelection(keep)
  } catch (e: any) {
    liveState.error = '读取场次失败：' + e.message
  } finally {
    liveState.loadingCal = false
  }
}

function ensureSelection(keep: boolean) {
  if (!liveState.segments.length) {
    liveState.selectedDate = ''
    liveState.selectedKey = ''
    resetRows()
    return
  }
  const current = liveState.segments.find((s) => segKey(s) === liveState.selectedKey)
  if (current) {
    liveState.selectedDate = dayKey(current.start)
    if (!keep) maybeLoad()
    return
  }
  // 初次进入优先落在最近一场直播（有画面）；没有直播场才退回最后一段
  const last = [...liveState.segments].reverse()
    .find((s) => s.kind === 'live') || liveState.segments[liveState.segments.length - 1]
  liveState.selectedDate = dayKey(last.start)
  liveState.selectedKey = segKey(last)
  liveState.currentMs = last.end
    ? 0
    : Math.max(0, Date.now() - ((last.recording && last.recording.started_at) || last.start))
  resetRows()
  void loadWindow()
}

function maybeLoad() {
  const any = KINDS.some((k) => liveState.rows[k].length)
  if (!any && !KINDS.some((k) => liveState.loading[k])) void loadWindow()
}

export function selectDate(key: string) {
  const list = dayMap.value[key] || []
  if (!list.length) return
  liveState.selectedDate = key
  // 同一天既有直播场又有尾部场间时，优先选最近一场直播
  const live = [...list].reverse().find((s) => s.kind === 'live')
  selectSegment(live || list[list.length - 1])
}

export function selectSegment(s: Segment) {
  const key = segKey(s)
  if (key === liveState.selectedKey) return
  liveState.selectedKey = key
  liveState.selectedDate = dayKey(s.start)
  liveState.currentMs = s.end ? 0 : Math.max(0, Date.now() - anchorMs.value)
  liveState.error = ''
  resetRows()
  void loadWindow()
}

/** 删除一段（场 / 场间）的全部数据；场会连开播 / 下播标记一起删。 */
export async function deleteSegment(seg: Segment) {
  if (!liveState.roomId || !seg.start) throw new Error('没有选中段')
  const j = await postJson<any>('/api/session/delete', {
    room_id: liveState.roomId,
    start: seg.start,
    end: seg.end || 0,
    kind: seg.kind,
  })
  await loadCalendar()
  return j
}

/* ---------------- 消息：按页追加 ---------------- */

let gen = 0
let livePollAt = 0
let queryTimer: number | undefined

function resetRows() {
  gen++
  for (const k of KINDS) {
    liveState.rows[k] = []
    liveState.older[k] = false
    liveState.loading[k] = false
  }
  livePollAt = 0
}

function rowKey(kind: FeedKind, r: FeedRow) {
  return kind === 'sc' ? 's' + r.sc_id : kind === 'danmaku' ? 'd' + r.seq : 'e' + r.seq
}

function mergeRows(kind: FeedKind, oldRows: FeedRow[], fresh: FeedRow[]) {
  const seen: Record<string, boolean> = {}
  const out: FeedRow[] = []
  for (const r of oldRows.concat(fresh)) {
    const key = rowKey(kind, r)
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

function segQuery(kinds: FeedKind[]) {
  const seg = selectedSeg.value
  let qs = '&types=' + kinds.join(',') + '&q=' + encodeURIComponent(liveState.q)
  if (seg && seg.start) qs += '&since=' + seg.start
  if (seg && seg.end) qs += '&until=' + seg.end
  return qs
}

/** 已结束段的向后游标：after 之后的新消息。 */
function afterUrl(kind: FeedKind, afterMs: number) {
  return '/api/feed?paged=1&asc=1&room_id=' + liveState.roomId +
    '&limit=' + PAGE + '&after=' + Math.max(0, afterMs) + '&incl=1' +
    segQuery([kind])
}

/** 直播 / 尾部场间的向前游标：before 之前的更早消息。 */
function beforeUrl(kind: FeedKind, beforeMs: number) {
  return '/api/feed?paged=1&room_id=' + liveState.roomId +
    '&limit=' + PAGE + '&before=' + Math.max(0, beforeMs) + '&incl=1' +
    segQuery([kind])
}

/** 第一页：默认取最新一页；往上滑再向前追加更早的页。 */
export async function loadWindow() {
  if (!selectedSeg.value) {
    resetRows()
    return
  }
  gen++
  const myGen = gen
  for (const k of KINDS) {
    liveState.rows[k] = []
    liveState.older[k] = false
    liveState.loading[k] = true
  }
  const qs = '/api/feed?paged=1&room_id=' + liveState.roomId +
    '&limit=' + PAGE + segQuery(KINDS)
  try {
    const j = await api<any>(qs)
    if (myGen !== gen) return
    for (const k of KINDS) {
      liveState.rows[k] = ((j.items && j.items[k]) || []) as FeedRow[]
      liveState.older[k] = !!(j.more && j.more[k])
    }
  } catch (e: any) {
    if (myGen === gen) liveState.error = '读取数据失败：' + e.message
  } finally {
    if (myGen === gen) {
      for (const k of KINDS) liveState.loading[k] = false
    }
  }
}

/** 滚到列顶：向前追加更早的一页。 */
export function loadOlder(kind: FeedKind) {
  if (!liveState.older[kind] || liveState.loading[kind]) return
  const list = liveState.rows[kind]
  if (!list.length) return
  void fetchOlder(kind, list[0].received_at || 0, gen)
}

async function fetchOlder(kind: FeedKind, before: number, myGen: number) {
  const seg = selectedSeg.value
  if (!seg || !liveState.roomId) return
  liveState.loading[kind] = true
  try {
    const j = await api<any>(beforeUrl(kind, before))
    if (myGen !== gen) return
    const fresh = ((j.items && j.items[kind]) || []) as FeedRow[]
    if (fresh.length) {
      liveState.rows[kind] = mergeRows(kind, fresh, liveState.rows[kind])
    }
    liveState.older[kind] = !!(j.more && j.more[kind])
  } catch (e: any) {
    if (myGen === gen) liveState.error = '读取' + TITLES[kind] + '失败：' + e.message
  } finally {
    if (myGen === gen) liveState.loading[kind] = false
  }
}

/** 只取游标之后的新消息，追加到列尾（直播 / 尾部场间）。 */
async function fetchTail(kind: FeedKind, myGen: number) {
  const seg = selectedSeg.value
  if (!seg) return
  const list = liveState.rows[kind]
  const after = list.length
    ? list[list.length - 1].received_at || 0
    : Math.max(0, (seg.start || 0) - 1)
  try {
    const j = await api<any>(afterUrl(kind, after))
    if (myGen !== gen) return
    const fresh = ((j.items && j.items[kind]) || []) as FeedRow[]
    if (fresh.length) liveState.rows[kind] = mergeRows(kind, list, fresh)
  } catch (e: any) {
    if (myGen === gen) liveState.error = '读取' + TITLES[kind] + '失败：' + e.message
  }
}

function pollTail() {
  const now = Date.now()
  if (now - livePollAt < 3500) return
  livePollAt = now
  KINDS.forEach((k) => {
    if (liveState.loading[k]) return
    void fetchTail(k, gen)
  })
}

/** 直播中：跟随墙上时间，每 3.5 秒把新增消息补进来。 */
export function tickLive() {
  if (!isLiveSeg.value) return
  liveState.currentMs = Math.max(0, Date.now() - anchorMs.value)
  pollTail()
}

/** 下播后的尾部场间：仍可能有弹幕 / 礼物，定时追加即可。 */
export function tickGap() {
  if (!isOpenSeg.value || isLiveSeg.value) return
  pollTail()
}

export function setCurrent(ms: number) {
  liveState.currentMs = Math.max(0, ms)
}

/* ---------------- 工具条 ---------------- */

export function setLayout(layout: 'columns' | 'stack') {
  liveState.layout = layout
  localStorage.setItem('lc-live-layout', layout)
}

/** 过滤器只控制显示；已加载的页不丢。 */
export function setFilter(kind: FeedKind, on: boolean) {
  liveState.filters[kind] = on
  localStorage.setItem('lc-live-filters', JSON.stringify(liveState.filters))
}

export function setQuery(value: string) {
  liveState.q = value
  window.clearTimeout(queryTimer)
  queryTimer = window.setTimeout(() => {
    resetRows()
    void loadWindow()
  }, 300)
}

export function disposeLiveRoom() {
  window.clearTimeout(queryTimer)
  resetRows()
}
