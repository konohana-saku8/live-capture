<script setup lang="ts">
/** 日志页：服务端日志 + 网页操作记录合并展示，最新 1000 条。 */
import { computed, nextTick, ref, watch } from 'vue'

import { fmtFull } from '@/lib/format'
import { loadLogs, state, uiLog } from '@/stores/app'
import type { LogRow } from '@/types'

const level = ref('all')
const follow = ref(true)
const box = ref<HTMLDivElement | null>(null)

const rows = computed(() => {
  const all: LogRow[] = [
    ...state.serverLogs.map((x) => ({ ts: x.ts, level: x.level, msg: x.msg })),
    ...state.webLogs.map((x) => ({ ts: x.ts, level: 'web', msg: x.msg })),
  ]
  all.sort((a, b) => a.ts - b.ts)
  return all.filter((x) => level.value === 'all' || x.level === level.value).slice(-600)
})

const countText = computed(
  () => '共 ' + rows.value.length + ' 条（服务端 ' + state.serverLogs.length +
    ' / 网页 ' + state.webLogs.length + '）',
)

watch(rows, async () => {
  const el = box.value
  if (!el || !follow.value) return
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40
  await nextTick()
  if (atBottom) el.scrollTop = el.scrollHeight
}, { flush: 'post' })

function reload() {
  loadLogs()
  uiLog('info', '手动刷新日志')
}
</script>

<template>
  <div class="page">
    <section class="card">
      <div class="sec-head">
        <h2><i>◆</i>全部日志</h2>
        <span class="more">{{ countText }}</span>
      </div>
      <div class="row">
        <select v-model="level" style="width:132px">
          <option value="all">全部级别</option>
          <option value="info">info</option>
          <option value="warn">warn</option>
          <option value="error">error</option>
          <option value="web">网页操作</option>
        </select>
        <button class="btn sm" @click="reload">刷新日志</button>
        <label style="display:flex;align-items:center;gap:6px;margin:0;color:var(--ink-2);font-size:12px">
          <input v-model="follow" type="checkbox">自动滚动
        </label>
      </div>
      <div ref="box" class="logbox">
        <div v-for="(x, i) in rows" :key="i" class="line" :class="x.level">
          <span class="ts">{{ fmtFull(x.ts) }}</span>
          <span class="lv">{{ x.level }}</span>
          <span class="txt">{{ x.msg }}</span>
        </div>
        <div v-if="!rows.length" class="empty">暂无日志</div>
      </div>
      <div class="hint">服务端运行日志 + 网页上的操作结果都集中在这里，最新 1000 条。</div>
    </section>
  </div>
</template>
