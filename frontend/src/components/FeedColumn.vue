<script setup lang="ts">
/** 一类内容的一列：正序显示，顶部可往前追更早的分页。 */
import { computed, nextTick, ref, watch } from 'vue'

import { FEED_PAGE } from '@/stores/app'
import { GUARD, KIND, escapeHtml, fmt, isHttpUrl } from '@/lib/format'
import type { FeedKind, FeedRow } from '@/types'
import RichText from './RichText.vue'

const props = defineProps<{
  kind: FeedKind
  rows: FeedRow[]
  more: boolean
  loading: boolean
  emptyHint: string
}>()
const emit = defineEmits<{ (e: 'more'): void }>()

const box = ref<HTMLDivElement | null>(null)
let prevFirstKey = ''

const title = computed(() => ({ danmaku: '弹幕', sc: 'Super Chat', events: '直播间事件' }[props.kind]))

function rowKey(r: FeedRow) {
  return props.kind === 'sc' ? 's' + r.sc_id : props.kind === 'danmaku' ? 'd' + r.seq : 'e' + r.seq
}

function guardLabel(level?: number) {
  return level ? GUARD[level] || '舰长' : ''
}

function faceUrl(url?: string) {
  return isHttpUrl(url) ? url : ''
}

watch(
  () => props.rows,
  async () => {
    const el = box.value
    if (!el) return
    const firstKey = props.rows.length ? rowKey(props.rows[0]) : ''
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40
    const prevHeight = el.scrollHeight
    const prevTop = el.scrollTop
    const prepended = firstKey !== prevFirstKey && prevFirstKey !== ''
    await nextTick()
    if (prepended) {
      // 往前追了一页：保持当前视线不动
      el.scrollTop = prevTop + (el.scrollHeight - prevHeight)
    } else if (atBottom || !prevFirstKey) {
      el.scrollTop = el.scrollHeight
    }
    prevFirstKey = firstKey
  },
  { flush: 'post' },
)
</script>

<template>
  <section class="feed">
    <h3>{{ title }} <span class="n">{{ rows.length }}</span></h3>
    <div ref="box" class="feed-body">
      <button v-if="more" class="feed-more" :disabled="loading" @click="emit('more')">
        {{ loading ? '正在载入…' : '↑ 载入更早的 ' + FEED_PAGE + ' 条' }}
      </button>
      <template v-if="rows.length">
        <template v-for="r in rows" :key="rowKey(r)">
          <!-- SC：单条卡片，带价格与舰长标签 -->
          <div v-if="kind === 'sc'" class="sc-card">
            <span class="t">{{ fmt(r.received_at || r.start_time) }}</span>
            <span class="price">¥{{ r.price }}</span>
            <span v-if="r.guard_level" class="guard">[{{ guardLabel(r.guard_level) }}]</span>
            <a v-if="r.uid" class="uname" :href="'https://space.bilibili.com/' + r.uid"
               target="_blank" rel="noopener">{{ r.uname || '—' }}</a>
            <span v-else>{{ r.uname || '—' }}</span>
            <template v-if="Number(r.price || 0) >= 100"> ★</template>
            <div class="msg"><RichText :text="r.message" :row="r" /></div>
            <span v-if="r.message_trans" class="trans">译：{{ r.message_trans }}</span>
          </div>

          <!-- 弹幕：时间 + 牌子 + 昵称 + 正文 -->
          <div v-else-if="kind === 'danmaku'" class="line">
            <span class="t">{{ fmt(r.received_at) }}</span>
            <span v-if="r.medal_name" class="tag">[{{ r.medal_name }} {{ r.medal_level || 0 }}]</span>
            <span v-if="r.guard_level" class="guard">[{{ guardLabel(r.guard_level) }}]</span>
            <span v-if="r.is_admin" class="tag">[房管]</span>
            <a v-if="r.uid" class="uname" :href="'https://space.bilibili.com/' + r.uid"
               target="_blank" rel="noopener">{{ r.uname || '—' }}</a>
            <span v-else>{{ r.uname || '—' }}</span>
            ：<span class="msg"><RichText :text="r.text" :row="r" /></span>
          </div>

          <!-- 事件：礼物 / 上舰 / 直播状态 -->
          <div v-else class="line">
            <span class="t">{{ fmt(r.received_at) }}</span>
            <span class="tag">[{{ KIND[r.kind || ''] || r.kind || '事件' }}]</span>
            <span>{{ r.uname || '—' }}</span>　{{ r.detail || '' }}
          </div>
        </template>
      </template>
      <div v-else class="empty">{{ emptyHint }}</div>
    </div>
  </section>
</template>
