<script setup lang="ts">
/** 直播间内容：场次侧栏 + 三列纵向分区（无表格、无整库视图）。 */
import { computed, ref } from 'vue'

import { download } from '@/api/client'
import { segRange, segTitle } from '@/lib/format'
import {
  currentRoom, currentSeg, deleteSession, hint, loadFeed, loadMore,
  loadSessions, selectSession, state,
} from '@/stores/app'
import type { FeedKind, Segment } from '@/types'
import FeedColumn from './FeedColumn.vue'

const KINDS: FeedKind[] = ['danmaku', 'sc', 'events']
const expType = ref<'danmaku' | 'sc' | 'events'>('danmaku')
let qTimer: number | undefined

/** 侧栏倒序：最近一段在最上面。 */
const segsDesc = computed(() => [...state.segs].reverse())

const feedInfo = computed(() => {
  const room = currentRoom.value
  const s = currentSeg.value
  const label = s ? segTitle(s) + ' · ' + segRange(s) : '未选场次'
  return (room ? (room.uname || room.label) + ' · ' : '') + label
})

const emptyHint = computed(() => state.feedEmpty || '这一段没有该类数据')

function segBadge(s: Segment) {
  if (s.kind === 'live') return '场'
  if (s.single) return '未分段'
  return s.lead ? '更早' : '场间'
}

function isOn(s: Segment) {
  return s.kind + '-' + s.index === state.sessSel
}

function toggleFilter(kind: FeedKind) {
  state.filters[kind] = !state.filters[kind]
  localStorage.setItem('lc-filters', JSON.stringify(state.filters))
  loadFeed()
}

function onQueryInput() {
  window.clearTimeout(qTimer)
  qTimer = window.setTimeout(() => loadFeed(), 300)
}

function reload() {
  loadSessions()
}

/** 删除指定场次：确认一次再提交，删完刷新场次列表与内容。 */
async function delSeg(s: Segment) {
  const label = segTitle(s) + ' · ' + segRange(s)
  const marks = s.kind === 'live' ? '，含开播 / 下播标记' : '（保留开播 / 下播标记）'
  const detail = '弹幕 ' + (s.danmaku || 0) + '、SC ' + (s.sc || 0) + marks
  if (!window.confirm('确认删除「' + label + '」？\n' + detail +
      '\n删除后不可恢复。')) return
  try {
    const j: any = await deleteSession(s)
    const d = (j && j.deleted) || {}
    hint('capture', '已删除：弹幕 ' + (d.danmaku || 0) + '、SC ' + (d.sc || 0) +
      '、事件 ' + (d.events || 0), 'ok')
  } catch (e: any) {
    hint('capture', '删除失败：' + e.message, 'err')
    return
  }
  await loadSessions()
}

async function exportData(fmt: 'json' | 'csv') {
  if (!state.roomId) {
    hint('capture', '请先选择采集器', 'err')
    return
  }
  const url = '/api/export?type=' + expType.value + '&format=' + fmt +
    '&room_id=' + state.roomId + '&q=' + encodeURIComponent(state.q)
  try {
    await download(url, 'room-' + state.roomId + '-' + expType.value + '.' + fmt)
  } catch (e: any) {
    hint('capture', '导出失败：' + e.message, 'err')
  }
}
</script>

<template>
  <div class="room-block" data-panel="live">
    <div class="block-meta">{{ feedInfo }}</div>

    <div class="filters">
      <label v-for="k in KINDS" :key="k">
        <input type="checkbox" :checked="state.filters[k]" @change="toggleFilter(k)">
        {{ k === 'danmaku' ? '弹幕' : k === 'sc' ? 'Super Chat' : '直播间事件' }}
      </label>
      <label>
        <input v-model="state.autoRefresh" type="checkbox">
        自动刷新
      </label>
    </div>

    <div class="row">
      <input v-model="state.q" type="text" placeholder="搜索内容 / 昵称"
             style="flex:1 1 160px" @input="onQueryInput"
             @keydown.enter="loadFeed()">
      <select v-model="expType" style="width:112px">
        <option value="danmaku">弹幕</option>
        <option value="sc">SC</option>
        <option value="events">事件</option>
      </select>
      <button class="btn sm" @click="reload">刷新</button>
      <button class="btn sm" @click="exportData('json')">导出 JSON</button>
      <button class="btn sm" @click="exportData('csv')">导出 CSV</button>
    </div>

    <div class="content-wrap">
      <aside class="session-pane">
        <h4>场次 / 场间</h4>
        <div>
          <div v-for="s in segsDesc" :key="s.kind + '-' + s.index"
               class="session-row">
            <button class="session-item" :class="{ on: isOn(s) }"
                    @click="selectSession(s.kind + '-' + s.index)">
              <span class="st-t">
                {{ segRange(s) }}
                <i :class="s.kind === 'live' ? 'live' : 'gap'">{{ segBadge(s) }}</i>
                <template v-if="!s.end"> ●</template>
              </span>
              <span class="st-m">
                {{ segTitle(s) }} · 流水 ¥{{ s.sc_amount || 0 }}
                · 观众 {{ s.viewers || 0 }} · 弹幕 {{ s.danmaku || 0 }}
              </span>
            </button>
            <button class="st-del" title="删除这一段（含全部数据，不可恢复）"
                    @click="delSeg(s)">✕</button>
          </div>
          <div v-if="!state.segs.length" class="empty"
               style="font-size:11.5px;padding:6px 4px">
            暂无场次记录<br>（采集到开播 / 下播事件后自动分段）
          </div>
          <div v-else-if="state.segs.length === 1 && state.segs[0].single"
               class="st-m"
               style="padding:6px 6px 0;color:var(--dim-2);font-size:10px">
            还没有开播记录，采集到的内容先归为一段，下次开播自动切开
          </div>
        </div>
      </aside>

      <div class="feeds">
        <FeedColumn v-for="k in KINDS" :key="k" :kind="k" :rows="state.feedRows[k]"
                    :more="state.feedMore[k]" :loading="state.loadingMore[k]"
                    :empty-hint="emptyHint" @more="loadMore(k)" />
      </div>
    </div>

    <div class="hint">
      弹幕只按场次查看：左边选一段，右边就是那一段的全部内容。「场」＝开播到下播；「场间」＝上一次下播到这次开播，期间收到的弹幕同样完整保留。还没有开过播时，采集到的内容先归为「未分段」，下次开播自动切开。弹幕按时间正序，越上方越早；一段太长时先显示最新的 400 条，点列表顶部的按钮往前追。
    </div>
  </div>
</template>
