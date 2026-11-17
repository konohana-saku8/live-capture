<script setup lang="ts">
/** 月历索引：只列出有直播场次的日期。 */
import { computed, ref, watch } from 'vue'

import { WEEKDAYS, dayKey, keyParts, monthCells, monthLabel, shiftMonth, todayKey } from '@/lib/calendar'
import type { Segment } from '@/types'

const props = defineProps<{
  segments: Segment[]
  selectedDate: string
  loading?: boolean
}>()
const emit = defineEmits<{ (e: 'select', key: string): void }>()

const view = ref(keyParts(props.selectedDate))

watch(
  () => props.selectedDate,
  (key) => {
    if (key) view.value = keyParts(key)
  },
  { immediate: true },
)

const counts = computed(() => {
  const out: Record<string, number> = {}
  for (const s of props.segments) {
    const key = dayKey(s.start)
    out[key] = (out[key] || 0) + 1
  }
  return out
})

const cells = computed(() => monthCells(view.value.year, view.value.month))
const lastKey = computed(() => props.segments.length
  ? dayKey(props.segments[props.segments.length - 1].start)
  : '')

function shift(delta: number) {
  view.value = shiftMonth(view.value.year, view.value.month, delta)
}

function pick(cell: { key: string; inMonth: boolean }) {
  if (!counts.value[cell.key]) return
  emit('select', cell.key)
}

function goLatest() {
  if (!lastKey.value) return
  view.value = keyParts(lastKey.value)
  emit('select', lastKey.value)
}
</script>

<template>
  <div class="cal" :class="{ loading }">
    <div class="cal-head">
      <button class="cal-nav" title="上个月" @click="shift(-1)">‹</button>
      <span class="cal-title">{{ monthLabel(view.year, view.month) }}</span>
      <button class="cal-nav" title="下个月" @click="shift(1)">›</button>
      <button class="cal-latest" :disabled="!lastKey" @click="goLatest">最近</button>
    </div>
    <div class="cal-grid cal-week">
      <span v-for="w in WEEKDAYS" :key="w">{{ w }}</span>
    </div>
    <div class="cal-grid">
      <button v-for="c in cells" :key="c.key" class="cal-cell"
              :class="{ out: !c.inMonth, on: c.key === selectedDate,
                        today: c.key === todayKey(), has: !!counts[c.key] }"
              :disabled="!counts[c.key]" :title="counts[c.key] ? counts[c.key] + ' 段' : ''"
              @click="pick(c)">
        <span class="cal-d">{{ c.day }}</span>
        <span v-if="counts[c.key]" class="cal-n">{{ counts[c.key] }}</span>
      </button>
    </div>
    <div class="cal-foot">
      共 {{ segments.length }} 段 · {{ Object.keys(counts).length }} 天有记录
    </div>
  </div>
</template>
