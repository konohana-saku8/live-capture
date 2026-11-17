<script setup lang="ts">
/** 右侧数据栏：三列并排或单栏三段堆叠，供浏览与搜索（弹幕上屏由 VideoOverlay 负责）。 */
import { computed } from 'vue'

import type { FeedKind } from '@/types'
import {
  liveState, loadOlder, setFilter, setLayout, setQuery,
} from '@/stores/liveRoom'
import StudioFeedColumn from './StudioFeedColumn.vue'

const props = defineProps<{
  anchorMs: number
  relative: boolean
  seekable: boolean
}>()
const emit = defineEmits<{ (e: 'pick', at: number): void }>()

const KINDS: FeedKind[] = ['danmaku', 'sc', 'events']
const SHORT: Record<FeedKind, string> = { danmaku: '弹幕', sc: 'SC', events: '事件' }
const TITLES: Record<FeedKind, string> = { danmaku: '弹幕', sc: 'Super Chat', events: '直播间事件' }

const total = computed(() => KINDS.reduce((n, k) => n + liveState.rows[k].length, 0))
const anyOn = computed(() => KINDS.some((k) => liveState.filters[k]))

function emptyHint(kind: FeedKind) {
  if (!liveState.selectedKey) return '先在左侧选一个场次'
  return '这场没有' + TITLES[kind]
}

function onFilter(kind: FeedKind, event: Event) {
  setFilter(kind, (event.target as HTMLInputElement).checked)
}

function onSearch(event: Event) {
  setQuery((event.target as HTMLInputElement).value)
}
</script>

<template>
  <div class="studio-feed">
    <div class="studio-tools">
      <div class="layout-switch">
        <button :class="{ on: liveState.layout === 'columns' }"
                @click="setLayout('columns')">三列</button>
        <button :class="{ on: liveState.layout === 'stack' }"
                @click="setLayout('stack')">单栏</button>
      </div>
      <label v-for="k in KINDS" :key="k">
        <input type="checkbox" :checked="liveState.filters[k]" @change="onFilter(k, $event)">
        {{ SHORT[k] }}
      </label>
      <input class="studio-search" :value="liveState.q" placeholder="搜索内容 / 昵称"
             @input="onSearch">
      <span class="muted">{{ total }} 条</span>
    </div>

    <div class="studio-feed-main" :class="liveState.layout">
      <template v-if="anyOn">
        <StudioFeedColumn v-for="k in KINDS" v-show="liveState.filters[k]" :key="k"
                          :kind="k" :rows="liveState.rows[k]"
                          :older="liveState.older[k]"
                          :loading="liveState.loading[k]" :anchor-ms="props.anchorMs"
                          :relative="props.relative"
                          :seekable="props.seekable" :session-key="liveState.selectedKey"
                          :empty-hint="emptyHint(k)"
                          @older="loadOlder(k)"
                          @pick="emit('pick', $event)" />
      </template>
      <div v-else class="empty">三类内容都关了，勾选上面的类型查看</div>
    </div>
  </div>
</template>
