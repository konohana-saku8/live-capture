<script setup lang="ts">
/** 采集器清单里的一行：采谁、在采什么、采了多少。 */
import { computed } from 'vue'

import { fmtFull, num } from '@/lib/format'
import type { Room } from '@/types'
import CdnImg from './CdnImg.vue'

const props = defineProps<{ room: Room; selected: boolean }>()
defineEmits<{ (e: 'select'): void; (e: 'remove'): void }>()

const name = computed(() => props.room.uname || props.room.label || '')
const totals = computed(() => props.room.totals || {})
const collectLive = computed(() => props.room.collect_live !== false)
const collectDyn = computed(() => props.room.collect_dynamics !== false)
</script>

<template>
  <div class="collector" :class="{ on: selected }" @click="$emit('select')">
    <CdnImg :src="room.face" kind="cface" alt="up 主头像" />
    <span v-if="!room.face" class="cface"></span>
    <div class="cmain">
      <div class="crow">
        <b>{{ name }}</b>
        <span class="dot" :class="{ on: room.running }"
              :title="room.running ? (room.waiting ? '已启动，等开播' : '采集中') : '未采集'">
          <i></i>{{ room.running ? (room.waiting ? '等开播' : '采集中') : '未采集' }}
        </span>
        <span class="cid">UID {{ room.uid || '—' }} · ROOM {{ room.id }}</span>
        <button class="cx" title="删除采集器（保留已采集数据）"
                @click.stop="$emit('remove')">✕</button>
      </div>
      <div class="cgoals">
        <span v-if="collectLive" class="goal live">
          <em>直播间</em>累计开播 <b>{{ num(totals.sessions) }}</b> 次
          <span v-if="totals.sessions_approx" class="off" title="历史数据没有开播事件，按采集批次统计">近似</span>
          · 最近开播 <b>{{ room.last_live_at ? fmtFull(room.last_live_at) : '—' }}</b>
          <template v-if="room.running">
            · <b>{{ room.waiting ? '等开播' : '采集中' }}</b>
          </template>
          <template v-if="room.live_only"> · <span class="off">只在开播时采</span></template>
        </span>
        <span v-else class="goal live"><em>直播间</em><span class="off">已关闭</span></span>

        <span v-if="collectDyn" class="goal dyn">
          <em>动态</em>累计 <b>{{ num(room.dyns) }}</b> 条 ·
          <template v-if="room.dyn_auto">每天 <b>{{ room.dyn_schedule || '—' }}</b> 定时拉取</template>
          <template v-else>定时拉取已关</template>
          <template v-if="room.dyn_last_ts || room.dyn_last_at">
            · 上次 <b>{{ fmtFull(room.dyn_last_ts || room.dyn_last_at) }}</b>
          </template>
          <template v-else> · 还没拉过</template>
        </span>
        <span v-else class="goal dyn"><em>动态</em><span class="off">已关闭</span></span>
      </div>
    </div>
  </div>
</template>
