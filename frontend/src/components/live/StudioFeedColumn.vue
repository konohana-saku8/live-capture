<script setup lang="ts">
/** 右侧一列：弹幕 / SC / 事件。默认停在最早一条；向下滚到底追加更新，向上滚到顶追加更早。 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'

import { GUARD, KIND, fmt, fmtOffset } from '@/lib/format'
import type { FeedKind, FeedRow } from '@/types'
import RichText from '@/components/RichText.vue'

const props = defineProps<{
  kind: FeedKind
  rows: FeedRow[]
  older: boolean
  newer: boolean
  loading: boolean
  anchorMs: number
  currentMs: number
  relative: boolean
  seekable: boolean
  sessionKey: string
  emptyHint: string
}>()
const emit = defineEmits<{
  (e: 'older'): void
  (e: 'newer'): void
  (e: 'pick', at: number): void
}>()

const box = ref<HTMLDivElement | null>(null)
const showJump = ref(false)
/** 每列独立：默认停在最早一条，用户点「跟随」后才自动跟播放头 / 最新。 */
const follow = ref(false)
/** 程序自己滚动时短暂标记，避免 scroll 事件把「跟随」状态改掉。 */
let autoScrollUntil = 0
/** 用户最近一次滚轮 / 触摸：用来区分「程序滚动」与「用户主动滚动」。 */
let userGestureAt = 0
/** 上次 scrollTop：分页只认「朝那个方向滑到边」，避免刚打开时轻轻一滑就加载。 */
let lastScrollTop = 0
/** 首屏 / 换场次后，等消息到位再把视图放到最早一条。 */
let pendingInitial = true
/** 初始定位 / 手动触发补页后，短暂抑制连锁的边沿触发。
 *  新页追加时保持原滚动位置，不再强制跳到扩展后的底部。 */
let suppressNewerUntil = 0
let suppressOlderUntil = 0

const TITLES: Record<FeedKind, string> = { danmaku: '弹幕', sc: 'Super Chat', events: '直播间事件' }
const title = computed(() => TITLES[props.kind])

function rowTime(r: FeedRow) {
  return r.received_at || r.start_time || 0
}

function rowKey(r: FeedRow) {
  return props.kind === 'sc' ? 's' + r.sc_id : props.kind === 'danmaku' ? 'd' + r.seq : 'e' + r.seq
}

function guardLabel(level?: number) {
  return level ? GUARD[level] || '舰长' : ''
}

function offsetLabel(r: FeedRow) {
  const ms = rowTime(r) - props.anchorMs
  return ms >= 0 ? fmtOffset(ms) : ''
}

function onRow(r: FeedRow) {
  if (props.seekable) emit('pick', rowTime(r))
}

const lastKey = computed(
  () => (props.rows.length ? rowKey(props.rows[props.rows.length - 1]) : ''),
)

/** 当前播放头之前最后一条消息。 */
const activeKey = computed(() => {
  if (!props.relative || !props.rows.length) return ''
  const target = props.anchorMs + Math.max(0, props.currentMs)
  let lo = 0
  let hi = props.rows.length - 1
  let hit = -1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (rowTime(props.rows[mid]) <= target) {
      hit = mid
      lo = mid + 1
    } else {
      hi = mid - 1
    }
  }
  return hit >= 0 ? rowKey(props.rows[hit]) : ''
})

/* ---------------- 跟随最新 ---------------- */

function isAtBottom(el: HTMLElement) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 48
}

function markAutoScroll() {
  autoScrollUntil = Date.now() + 220
}

function syncScrollTop() {
  const el = box.value
  if (el) lastScrollTop = el.scrollTop
}

function scrollToTop() {
  const el = box.value
  if (!el) return
  markAutoScroll()
  el.scrollTop = 0
  lastScrollTop = 0
}

function scrollToBottom() {
  const el = box.value
  if (!el) return
  markAutoScroll()
  el.scrollTop = el.scrollHeight
  syncScrollTop()
}

function setActiveRow() {
  const el = box.value
  if (!el) return
  const prev = el.querySelector('.studio-line.active') as HTMLElement | null
  if (prev) prev.classList.remove('active')
  if (!activeKey.value) return
  const target = el.querySelector(
    '[data-key="' + activeKey.value + '"]') as HTMLElement | null
  if (target) target.classList.add('active')
}

function scrollToActive() {
  const el = box.value
  if (!el || !activeKey.value) return
  const target = el.querySelector('[data-key="' + activeKey.value + '"]') as HTMLElement | null
  if (!target) return
  markAutoScroll()
  // 跟随播放时把当前消息放在这一栏的第一行：手机高度有限，后面的内容往下排
  el.scrollTop = Math.max(0, target.offsetTop - 4)
  syncScrollTop()
}

function updateJump() {
  const el = box.value
  if (!el) return
  showJump.value = !follow.value && !isAtBottom(el) && props.rows.length > 0
}

function onUserGesture() {
  userGestureAt = Date.now()
}

function onScroll() {
  const el = box.value
  if (!el) return
  updateJump()
  // 用户刚动过滚轮 / 手指时，即使程序刚滚过也要按用户位置判断
  const now = Date.now()
  if (now < autoScrollUntil && now - userGestureAt > 300) return
  const delta = el.scrollTop - lastScrollTop
  lastScrollTop = el.scrollTop
  if (isAtBottom(el)) {
    // 只有「向下滑到底」才补下一页：停在底部向上轻滑不算，避免打开页面就连锁加载
    if (delta > 0 && props.newer && !props.loading && now >= suppressNewerUntil) {
      if (follow.value) { follow.value = false; updateJump() }
      // 抑制放在触发这一刻：新页追加后不跳到底，用户仍停在旧页末尾继续往下读
      suppressNewerUntil = now + 400
      emit('newer')
    }
    return
  }
  // 用户主动滑离底部：这一列暂停跟随，露出「跟随最新」按钮
  if (follow.value) {
    follow.value = false
    updateJump()
  }
  // 只有「向上滑到顶」才继续向前加载更早的；直播取的是最新一页，打开时已经在顶，
  // 这时要用列首的「载入更早」按钮显式触发，不能靠初始定位自动加载。
  if (delta < 0 && el.scrollTop < 24 && props.older && !props.loading && now >= suppressOlderUntil) {
    suppressOlderUntil = now + 400
    emit('older')
  }
}

function onOlderClick() {
  if (!props.older || props.loading) return
  suppressOlderUntil = Date.now() + 400
  emit('older')
}

async function applyFollowPosition() {
  await nextTick()
  setActiveRow()
  // 跟随模式：当前播放位置对应的消息已加载就跟到栏顶，否则跟最新
  if (props.seekable && activeKey.value) scrollToActive()
  else scrollToBottom()
  updateJump()
}

async function applyInitialPosition() {
  await nextTick()
  if (!props.rows.length) return
  pendingInitial = false
  setActiveRow()
  // 深链带播放位置（如 ?t=）时仍按原逻辑对齐播放头并跟随
  if (props.seekable && props.currentMs > 0 && activeKey.value) {
    follow.value = true
    scrollToActive()
  } else {
    follow.value = false
    scrollToTop()
  }
  const now = Date.now()
  suppressNewerUntil = now + 400
  suppressOlderUntil = now + 400
  updateJump()
}

function jumpToLatest() {
  follow.value = true
  void applyFollowPosition()
}

let prevFirst = ''
let prevLast = ''

watch([() => props.rows.length, lastKey], async () => {
  const el = box.value
  const oldHeight = el ? el.scrollHeight : 0
  const oldTop = el ? el.scrollTop : 0
  const first = props.rows.length ? rowKey(props.rows[0]) : ''
  const last = props.rows.length ? rowKey(props.rows[props.rows.length - 1]) : ''
  // 前面追加了更早的页：保持当前视线不动
  const prepended = !!prevFirst && first !== prevFirst && last === prevLast
  if (!props.rows.length) pendingInitial = true
  await nextTick()
  const body = box.value
  setActiveRow()
  if (pendingInitial && props.rows.length) {
    await applyInitialPosition()
  } else if (prepended && body) {
    body.scrollTop = oldTop + (body.scrollHeight - oldHeight)
    syncScrollTop()
  } else if (follow.value) {
    if (props.seekable && activeKey.value) scrollToActive()
    else scrollToBottom()
  }
  prevFirst = first
  prevLast = last
  updateJump()
})

watch(activeKey, async () => {
  // 只查一次 class，不让 1.6 万行的 v-for 跟着播放头每 250ms 重渲染
  await nextTick()
  setActiveRow()
  if (!follow.value || !props.seekable) return
  scrollToActive()
  updateJump()
})

watch(follow, async (on) => {
  if (on) await applyFollowPosition()
  else updateJump()
})

// 换场次时这一列重新默认停在最早；其它列的状态不受影响
watch(() => props.sessionKey, async () => {
  pendingInitial = true
  follow.value = false
  const now = Date.now()
  suppressNewerUntil = now + 400
  suppressOlderUntil = now + 400
  await nextTick()
  if (props.rows.length) void applyInitialPosition()
})

watch(() => props.loading, async (v) => {
  if (v) return
  await nextTick()
  updateJump()
})

onMounted(() => { void applyInitialPosition() })
</script>

<template>
  <section class="studio-col">
    <h3>{{ title }} <span class="n">{{ rows.length }}</span></h3>
    <div ref="box" class="studio-feed-body" @scroll.passive="onScroll"
         @wheel.passive="onUserGesture" @touchmove.passive="onUserGesture">
      <button v-if="older && !loading" class="feed-older" @click="onOlderClick">载入更早 ↑</button>
      <div v-if="loading && older" class="feed-loading">正在载入更早…</div>
      <template v-if="rows.length">
        <template v-for="r in rows" :key="rowKey(r)">
          <div v-if="kind === 'sc'" class="studio-line sc" :data-key="rowKey(r)"
               :class="{ pickable: seekable }"
               @click="onRow(r)">
            <span class="t">{{ fmt(rowTime(r)) }}</span>
            <span v-if="relative && offsetLabel(r)" class="rel">{{ offsetLabel(r) }}</span>
            <span class="price">¥{{ r.price }}</span>
            <span v-if="r.guard_level" class="guard">[{{ guardLabel(r.guard_level) }}]</span>
            <a v-if="r.uid" class="uname" :href="'https://space.bilibili.com/' + r.uid"
               target="_blank" rel="noopener" @click.stop>{{ r.uname || '—' }}</a>
            <span v-else>{{ r.uname || '—' }}</span>
            <div class="msg"><RichText :text="r.message" :row="r" /></div>
            <span v-if="r.message_trans" class="trans">译：{{ r.message_trans }}</span>
          </div>

          <div v-else-if="kind === 'danmaku'" class="studio-line" :data-key="rowKey(r)"
               :class="{ pickable: seekable }"
               @click="onRow(r)">
            <span class="t">{{ fmt(rowTime(r)) }}</span>
            <span v-if="relative && offsetLabel(r)" class="rel">{{ offsetLabel(r) }}</span>
            <span v-if="r.medal_name" class="tag">[{{ r.medal_name }} {{ r.medal_level || 0 }}]</span>
            <span v-if="r.guard_level" class="guard">[{{ guardLabel(r.guard_level) }}]</span>
            <span v-if="r.is_admin" class="tag">[房管]</span>
            <a v-if="r.uid" class="uname" :href="'https://space.bilibili.com/' + r.uid"
               target="_blank" rel="noopener" @click.stop>{{ r.uname || '—' }}</a>
            <span v-else>{{ r.uname || '—' }}</span>
            ：<span class="msg"><RichText :text="r.text" :row="r" /></span>
          </div>

          <div v-else class="studio-line evt" :data-key="rowKey(r)"
               :class="['evt-' + (r.kind || 'event'), { pickable: seekable }]"
               @click="onRow(r)">
            <span class="t">{{ fmt(rowTime(r)) }}</span>
            <span v-if="relative && offsetLabel(r)" class="rel">{{ offsetLabel(r) }}</span>
            <span class="tag">[{{ KIND[r.kind || ''] || r.kind || '事件' }}]</span>
            <span>{{ r.uname || '—' }}</span>　{{ r.detail || '' }}
          </div>
        </template>
      </template>
      <div v-else class="empty">{{ emptyHint }}</div>
      <div v-if="loading && !older" class="feed-loading">正在载入…</div>
      <button v-if="showJump" class="feed-jump"
              :title="seekable ? '跳到播放头并恢复跟随' : '回到最新并恢复跟随'"
              @click="jumpToLatest">{{ seekable ? '跟随播放 ↓' : '跟随最新 ↓' }}</button>
    </div>
  </section>
</template>
