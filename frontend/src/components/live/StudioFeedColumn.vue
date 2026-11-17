<script setup lang="ts">
/** 右侧一列：弹幕 / SC / 事件。滚到底追加下一页，滚到顶追加更早页。 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'

import { GUARD, KIND, fmt, fmtOffset } from '@/lib/format'
import type { FeedKind, FeedRow } from '@/types'
import RichText from '@/components/RichText.vue'

const props = defineProps<{
  kind: FeedKind
  rows: FeedRow[]
  older: boolean
  loading: boolean
  anchorMs: number
  relative: boolean
  seekable: boolean
  sessionKey: string
  emptyHint: string
}>()
const emit = defineEmits<{
  (e: 'older'): void
  (e: 'pick', at: number): void
}>()

const box = ref<HTMLDivElement | null>(null)
const showJump = ref(false)
/** 每列独立：加载 / 换场次时默认跟随，用户自己滑动后暂停。 */
const follow = ref(true)
/** 程序自己滚动时短暂标记，避免 scroll 事件把「跟随」关掉。 */
let autoScrollUntil = 0
/** 用户最近一次滚轮 / 触摸：用来区分「程序滚动」与「用户主动滚动」。 */
let userGestureAt = 0

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

/* ---------------- 跟随最新（列表只跟最新，不跟播放头） ---------------- */

function isAtBottom(el: HTMLElement) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 48
}

function markAutoScroll() {
  autoScrollUntil = Date.now() + 220
}

function scrollToBottom() {
  const el = box.value
  if (!el) return
  markAutoScroll()
  el.scrollTop = el.scrollHeight
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
  if (Date.now() < autoScrollUntil && Date.now() - userGestureAt > 300) return
  if (isAtBottom(el)) return
  // 用户主动滑离底部：这一列暂停跟随，露出「跟随最新」按钮
  if (follow.value) {
    follow.value = false
    updateJump()
  }
  // 默认看最新一页；只有滑到列顶才继续向前加载
  if (el.scrollTop < 24 && props.older && !props.loading) emit('older')
}

async function applyFollowPosition() {
  await nextTick()
  scrollToBottom()
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
  await nextTick()
  const body = box.value
  if (prepended && body) {
    body.scrollTop = oldTop + (body.scrollHeight - oldHeight)
  } else if (follow.value) {
    scrollToBottom()
  }
  prevFirst = first
  prevLast = last
  updateJump()
})

watch(follow, async (on) => {
  if (on) await applyFollowPosition()
  else updateJump()
})

// 换场次时这一列重新默认跟随；其它列的跟随状态不受影响
watch(() => props.sessionKey, () => { follow.value = true })

watch(() => props.loading, async (v) => {
  if (v) return
  await nextTick()
  updateJump()
})

onMounted(() => { void applyFollowPosition() })
</script>

<template>
  <section class="studio-col">
    <h3>{{ title }} <span class="n">{{ rows.length }}</span></h3>
    <div ref="box" class="studio-feed-body" @scroll.passive="onScroll"
         @wheel.passive="onUserGesture" @touchmove.passive="onUserGesture">
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
