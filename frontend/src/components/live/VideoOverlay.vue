<script setup lang="ts">
/**
 * 画面上的弹幕 / SC / 事件层。
 *
 * 独立于右侧列表：按播放头时间窗口向后端取整段消息，再用 currentMs 驱动
 * 每条弹幕的水平位置。播放暂停时 currentMs 不变，弹幕随之停住；回放 seek
 * 时播放头跳变，弹幕同步跳到对应时刻。右侧列表只管「浏览 / 搜索」，画面层
 * 只管「跟着画面演」，两者数据源分开，互不干扰。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { api } from '@/api/client'
import { GUARD, KIND } from '@/lib/format'
import { anchorMs, liveState } from '@/stores/liveRoom'
import type { FeedKind, FeedRow } from '@/types'
import RichText from '@/components/RichText.vue'

const props = defineProps<{ currentMs: number; live: boolean }>()

const KINDS: FeedKind[] = ['danmaku', 'sc', 'events']
const DM_LIFE = 12000          // 一条弹幕横穿画面的毫秒数
const SC_LIFE = 16000          // SC 卡片停留时间（最长，决定回看窗口）
const EVENT_LIFE = 7000        // 事件横幅停留时间
const WINDOW_BEFORE = 18000    // 覆盖最长停留时间，保证回看时有数据
const WINDOW_AFTER = 8000      // 末端前瞻缓冲：播放头接近末端时才补下一窗
const RELOAD_MARGIN = 4000     // 距末端小于这个值就补窗，避免空窗
const DM_LANES = 6

const show = ref(localStorage.getItem('lc-overlay') !== '0')
const frame = ref<HTMLElement | null>(null)
const width = ref(800)
let ro: ResizeObserver | undefined

// 直播时 currentMs 每 2 秒才刷新一次，弹幕会一格一格跳；用逐帧时钟让直播
// 弹幕连续滚动（Date.now() 与 anchorMs + currentMs 等价）。回放仍跟随播放头。
const rafTick = ref(0)
let raf = 0
function loop() {
  if (props.live && show.value) rafTick.value++
  raf = requestAnimationFrame(loop)
}

const now = computed(() => {
  void rafTick.value
  return props.live ? Date.now() : anchorMs.value + Math.max(0, props.currentMs)
})
const roomId = computed(() => liveState.roomId)
const sessionKey = computed(() => liveState.selectedKey)

const pool = ref<Record<FeedKind, FeedRow[]>>({ danmaku: [], sc: [], events: [] })
let loadedFrom = 0
let loadedTo = 0
let loading = false
let loadSeq = 0
let lastLoadAt = 0

function clearPool() {
  loadedFrom = 0
  loadedTo = 0
  loadSeq++
  pool.value = { danmaku: [], sc: [], events: [] }
}

/** 保证已加载区间覆盖播放头附近的窗口；节流，避免回放时每帧请求。 */
async function ensureWindow(force = false) {
  const t = Math.floor(now.value)
  if (!t || !roomId.value) return
  if (!force) {
    // 播放头在已取窗口内、且离末端还有缓冲时不重载；向前推进接近末端才补窗。
    if (loadedFrom && t >= loadedFrom && t <= loadedTo - RELOAD_MARGIN) return
    if (Date.now() - lastLoadAt < 900) return
  }
  if (loading && !force) return
  loading = true
  lastLoadAt = Date.now()
  const mySeq = ++loadSeq
  const from = Math.max(0, t - WINDOW_BEFORE)
  const to = t + WINDOW_AFTER
  try {
    const j = await api<any>('/api/feed?all=1&room_id=' + roomId.value +
      '&since=' + from + '&until=' + to + '&types=' + KINDS.join(','))
    if (mySeq !== loadSeq) return
    const items = (j && j.items) || {}
    pool.value = {
      danmaku: ((items.danmaku || []) as FeedRow[]),
      sc: ((items.sc || []) as FeedRow[]),
      events: ((items.events || []) as FeedRow[]),
    }
    loadedFrom = from
    loadedTo = to
  } catch {
    // 读不到就保留上一窗，下次播放头前进再试
  } finally {
    loading = false
  }
}

interface OverlayDanmaku extends FeedRow {
  age: number
  lane: number
}

const dms = computed<OverlayDanmaku[]>(() => {
  const n = now.value
  let lane = 0
  const out: OverlayDanmaku[] = []
  for (const r of pool.value.danmaku) {
    const age = n - (r.received_at || 0)
    if (age < -600 || age > DM_LIFE) continue
    out.push({ ...r, age, lane: lane++ % DM_LANES })
  }
  return out
})

const scs = computed<FeedRow[]>(() => {
  const n = now.value
  return pool.value.sc.filter(
    (r) => n - (r.received_at || r.start_time || 0) >= -600 &&
           n - (r.received_at || r.start_time || 0) <= SC_LIFE,
  ).slice(-3)
})

const events = computed<FeedRow[]>(() => {
  const n = now.value
  return pool.value.events.filter(
    (r) => n - (r.received_at || 0) >= -600 && n - (r.received_at || 0) <= EVENT_LIFE,
  ).slice(-2)
})

function rowTime(r: FeedRow) {
  return r.received_at || r.start_time || 0
}

function eventLabel(r: FeedRow) {
  return KIND[r.kind || ''] || r.kind || '事件'
}

function dmStyle(v: OverlayDanmaku) {
  // age=0 从画面右侧外进入，age=DM_LIFE 从左侧离开
  const p = Math.min(1.25, Math.max(0, v.age / DM_LIFE))
  const x = width.value * (1 - p * 1.35)
  return { transform: 'translateX(' + x.toFixed(1) + 'px)', top: (5 + v.lane * 11) + '%' }
}

function scStyle(r: FeedRow) {
  const age = now.value - rowTime(r)
  const fade = age > SC_LIFE - 2500 ? Math.max(0.25, (SC_LIFE - age) / 2500) : 1
  return { opacity: fade }
}

function evStyle(r: FeedRow) {
  const age = now.value - rowTime(r)
  const fade = age > EVENT_LIFE - 1800 ? Math.max(0.25, (EVENT_LIFE - age) / 1800) : 1
  return { opacity: fade }
}

function toggle() {
  show.value = !show.value
  localStorage.setItem('lc-overlay', show.value ? '1' : '0')
}

watch(now, () => { void ensureWindow(false) })
watch([roomId, sessionKey], () => {
  clearPool()
  void ensureWindow(true)
})

onMounted(() => {
  if (frame.value) {
    width.value = frame.value.clientWidth || 800
    if (typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver(() => {
        if (frame.value) width.value = frame.value.clientWidth || width.value
      })
      ro.observe(frame.value)
    }
  }
  raf = requestAnimationFrame(loop)
  void ensureWindow(true)
})

onBeforeUnmount(() => {
  if (raf) cancelAnimationFrame(raf)
  ro?.disconnect()
})
</script>

<template>
  <div ref="frame" class="ov-root" :class="{ off: !show }">
    <button class="ov-switch" :class="{ on: show }" type="button"
            :title="show ? '关闭画面弹幕' : '开启画面弹幕'" @click.stop="toggle">弹</button>
    <template v-if="show">
      <div class="ov-dm-layer" aria-hidden="true">
        <div v-for="(v, i) in dms" :key="'d' + (v.seq || rowTime(v)) + '-' + i"
             class="ov-dm" :style="dmStyle(v)">
          <span v-if="v.uname" class="ov-dm-name">{{ v.uname }}：</span><RichText :text="v.text" :row="v" />
        </div>
      </div>
      <div class="ov-evt-layer" aria-hidden="true">
        <div v-for="(r, i) in events" :key="'e' + (r.seq || rowTime(r)) + '-' + i"
             class="ov-evt" :class="'evt-' + (r.kind || '')" :style="evStyle(r)">
          <span class="ov-evt-tag">{{ eventLabel(r) }}</span>
          <span v-if="r.uname">{{ r.uname }}</span>{{ r.detail || '' }}
        </div>
      </div>
      <div class="ov-sc-layer" aria-hidden="true">
        <div v-for="(r, i) in scs" :key="'s' + (r.sc_id || rowTime(r)) + '-' + i"
             class="ov-sc" :style="scStyle(r)">
          <div class="ov-sc-head">
            <span class="ov-sc-price">¥{{ r.price }}</span>
            <span class="ov-sc-name">{{ r.uname || '—' }}</span>
            <span v-if="r.guard_level" class="ov-sc-guard">[{{ GUARD[r.guard_level] || '舰长' }}]</span>
          </div>
          <div class="ov-sc-msg"><RichText :text="r.message" :row="r" /></div>
          <div v-if="r.message_trans" class="ov-sc-trans">{{ r.message_trans }}</div>
        </div>
      </div>
    </template>
  </div>
</template>
