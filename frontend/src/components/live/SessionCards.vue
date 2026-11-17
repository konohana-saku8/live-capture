<script setup lang="ts">
/** 某个日期下的场次卡片：场 / 场间、时长、统计、录像状态、删除。 */
import { computed } from 'vue'

import { bytes as fmtBytes, fmtDuration, fmtHM, num, qnLabel, segTitle } from '@/lib/format'
import { segKey } from '@/stores/liveRoom'
import type { RecordingInfo, Segment } from '@/types'

const props = defineProps<{
  segments: Segment[]
  selectedKey: string
}>()
const emit = defineEmits<{
  (e: 'select', seg: Segment): void
  (e: 'delete', seg: Segment): void
}>()

const cards = computed(() => props.segments.map((s) => ({
  seg: s,
  key: segKey(s),
  live: s.kind === 'live',
  duration: (s.end || Date.now()) - s.start,
  rec: s.recording || null,
})))

function recText(rec: RecordingInfo | null) {
  if (!rec) return '无录像'
  if (rec.status === 'recording') return '录制中'
  if (rec.status === 'pending' || rec.status === 'remuxing') return '转封装中'
  if (rec.status === 'ready') return 'MP4 · ' + rec.mp4_count + ' 段'
  if (rec.status === 'flv') return 'FLV · ' + rec.flv_count + ' 段'
  if (rec.status === 'failed') return '转封装失败'
  if (rec.status === 'interrupted') return '录像中断'
  return rec.status || '录像'
}

function recClass(rec: RecordingInfo | null) {
  if (!rec) return 'none'
  if (rec.status === 'recording') return 'live'
  if (rec.status === 'ready') return 'ok'
  if (rec.status === 'failed' || rec.status === 'interrupted') return 'bad'
  return 'wait'
}

function delTitle(s: Segment) {
  return s.kind === 'live'
    ? '删除这一场（含开播 / 下播标记，不可恢复）'
    : '删除这段场间内容（保留开播 / 下播标记）'
}
</script>

<template>
  <div class="sess-cards">
    <div v-for="c in cards" :key="c.key" class="sess-row">
      <button class="sess-card" :class="{ on: c.key === selectedKey }"
              @click="emit('select', c.seg)">
        <span class="sess-top">
          <b>{{ fmtHM(c.seg.start) }}<template v-if="c.seg.end"> - {{ fmtHM(c.seg.end) }}</template><template v-else> - 至今</template></b>
          <i v-if="c.live && !c.seg.end" class="live-dot">直播中</i>
          <span class="sess-idx">{{ c.live ? '第 ' + c.seg.index + ' 场' : segTitle(c.seg) }}</span>
        </span>
        <span class="sess-meta">
          {{ fmtDuration(c.duration) }} · 弹幕 {{ num(c.seg.danmaku) }} ·
          SC {{ num(c.seg.sc) }}<template v-if="c.seg.sc_amount"> (¥{{ c.seg.sc_amount }})</template>
          · 观众 {{ num(c.seg.viewers) }}
        </span>
        <span v-if="c.live" class="sess-rec" :class="recClass(c.rec)">
          {{ recText(c.rec) }}
          <template v-if="c.rec">
            <template v-if="c.rec.actual_qn"> · {{ qnLabel(c.rec.actual_qn) }}</template>
            <template v-if="c.rec.bytes"> · {{ fmtBytes(c.rec.bytes) }}</template>
          </template>
        </span>
        <span v-else class="sess-rec none">场间数据 · 无录像</span>
      </button>
      <button class="sess-del" :title="delTitle(c.seg)"
              @click.stop="emit('delete', c.seg)">✕</button>
    </div>
    <div v-if="!cards.length" class="empty">这一天没有记录</div>
  </div>
</template>
