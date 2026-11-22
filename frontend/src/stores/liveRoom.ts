/** 数据页的独立状态：日历、录像、按页追加的消息。

选中场次后：直播 / 尾部场间默认先显示最新一页，新消息追加到列尾；已结束的回放
则从最早一页开始，播放头推进时向后补页——这样三栏的 active 高亮能跟画面走。
滚动加载只往已有列表里加行，不整页替换。
*/
import { computed, reactive } from 'vue'

import { api, postJson } from '@/api/client'
import { dayKey } from '@/lib/calendar'
import { loadDashboard, state as appState } from '@/stores/app'
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
  /** 后面还有更新的消息：回放从最早加载，播放头前进时向后补页 */
  newer: { danmaku: false, sc: false, events: false } as Record<FeedKind, boolean>,
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
  void loadDashboard()
  return j
}

/* ---------------- 消息：按页追加 ---------------- */

let gen = 0
let livePollAt = 0
let lastFollowAt = 0
let queryTimer: number | undefined

/** 播放头距已加载尾部不到这个时长就补下一页（弹幕约 40-60 秒/页）。 */
const FOLLOW_AHEAD = 60000

function resetRows() {
  gen++
  for (const k of KINDS) {
    liveState.rows[k] = []
    liveState.older[k] = false
    liveState.newer[k] = false
    liveState.loading[k] = false
  }
  livePollAt = 0
  lastFollowAt = 0
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

/**
 * 第一页：
 * - 回放（已结束的场 / 场间）：从最早一页开始（asc=1），播放头从头推进时再向后补页，
 *   这样任一时刻的 active 高亮都能在已加载数据里找到，三栏才跟得上画面。
 * - 直播 / 仍在收数据的尾部场间：仍取最新一页，新消息走 pollTail 追加。
 */
export async function loadWindow() {
  const seg = selectedSeg.value
  if (!seg) {
    resetRows()
    return
  }
  const replay = !!seg.end                 // 已结束 = 回放，按播放头时间跟
  gen++
  const myGen = gen
  for (const k of KINDS) {
    liveState.rows[k] = []
    liveState.older[k] = false
    liveState.newer[k] = false
    liveState.loading[k] = true
  }
  const asc = replay ? '&asc=1&after=0' : ''
  const qs = '/api/feed?paged=1&room_id=' + liveState.roomId +
    '&limit=' + PAGE + asc + segQuery(KINDS)
  try {
    const j = await api<any>(qs)
    if (myGen !== gen) return
    for (const k of KINDS) {
      liveState.rows[k] = ((j.items && j.items[k]) || []) as FeedRow[]
      if (replay) liveState.newer[k] = !!(j.more && j.more[k])
      else liveState.older[k] = !!(j.more && j.more[k])
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

/** 手动滚到底部时补下一页；和播放头推进走同一条补页逻辑。 */
export function loadNewer(kind: FeedKind) {
  if (!liveState.newer[kind] || liveState.loading[kind]) return
  const tail = lastTime(kind)
  void fetchNewer(kind, tail, gen)
}

/** 向后补一页（回放：从最早开始，播放头推进时追着补）。 */
async function fetchNewer(kind: FeedKind, after: number, myGen: number): Promise<boolean> {
  if (!liveState.roomId) return false
  liveState.loading[kind] = true
  try {
    const j = await api<any>(afterUrl(kind, after))
    if (myGen !== gen) return false
    const fresh = ((j.items && j.items[kind]) || []) as FeedRow[]
    if (fresh.length) liveState.rows[kind] = mergeRows(kind, liveState.rows[kind], fresh)
    liveState.newer[kind] = !!(j.more && j.more[kind])
    return fresh.length > 0
  } catch (e: any) {
    if (myGen === gen) liveState.error = '读取' + TITLES[kind] + '失败：' + e.message
    return false
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

function lastTime(kind: FeedKind): number {
  const list = liveState.rows[kind]
  return list.length ? (list[list.length - 1].received_at || 0) : 0
}

/**
 * 回放时播放头（视频时间 + 录像起点 = 绝对时刻）推进，逼近已加载数据的尾部就
 * 向后补页。seek 到中段会连续补多页直到覆盖播放头，保证 active 高亮不断档。
 * 直播不在这里处理，仍由 pollTail 追最新。
 */
function followPlayback() {
  const seg = selectedSeg.value
  if (!seg || !seg.end || isLiveSeg.value) return
  const now = Date.now()
  if (now - lastFollowAt < 350) return
  const myGen = gen
  const target = anchorMs.value + liveState.currentMs
  const need = KINDS.filter((k) =>
    liveState.newer[k] && !liveState.loading[k] &&
    target >= lastTime(k) - FOLLOW_AHEAD)
  if (!need.length) return
  lastFollowAt = now
  void (async () => {
    for (const k of need) {
      // 一轮只补 200 条；长场（上万条弹幕）需要 60 轮以上才能追到播放头，
      // 上限太低会让尾部几百条永远补不上，所以留足余量。
      for (let guard = 0; guard < 200 && myGen === gen && liveState.newer[k]; guard++) {
        const tail = lastTime(k)
        // 播放头后面已留出足够一屏的余量，停止补页
        if (tail > 0 && target < tail - FOLLOW_AHEAD) break
        const got = await fetchNewer(k, tail, myGen)
        if (!got) break
      }
    }
  })()
}

/**
 * 播放头大幅跳跃（拖动进度条）时，别从最早一页连补几十页慢慢追——直接按目标
 * 时刻开窗重取一页，一次定位。小步推进仍走 followPlayback 顺序补页，保持连续。
 */
async function seekWindow(target: number, myGen: number) {
  const seg = selectedSeg.value
  if (!seg || !liveState.roomId) return
  lastFollowAt = Date.now()
  const after = Math.max(0, target - FOLLOW_AHEAD / 2)
  // 开窗后前面还有内容就保留「载入更早」入口，否则拖动进度条后
  // 只能看到这一页，滚到列顶也补不出更早的弹幕 / SC。
  const hasOlder = after > (seg.start || 0)
  for (const k of KINDS) {
    liveState.loading[k] = true
    liveState.rows[k] = []
    liveState.older[k] = hasOlder
  }
  await Promise.all(KINDS.map(async (k) => {
    try {
      const j = await api<any>(afterUrl(k, after))
      if (myGen !== gen) return
      liveState.rows[k] = ((j.items && j.items[k]) || []) as FeedRow[]
      liveState.newer[k] = !!(j.more && j.more[k])
    } catch (e: any) {
      if (myGen === gen) liveState.error = '读取' + TITLES[k] + '失败：' + e.message
    } finally {
      if (myGen === gen) liveState.loading[k] = false
    }
  }))
}

export function setCurrent(ms: number) {
  const prev = liveState.currentMs
  liveState.currentMs = Math.max(0, ms)
  const seg = selectedSeg.value
  if (!seg || !seg.end || isLiveSeg.value) return
  // 前后跳变超过两页的量视为拖动进度条：直接按目标时刻重开窗
  if (Math.abs(liveState.currentMs - prev) > FOLLOW_AHEAD * 2) {
    void seekWindow(anchorMs.value + liveState.currentMs, gen)
    return
  }
  followPlayback()
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
