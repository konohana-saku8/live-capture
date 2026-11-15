<script setup lang="ts">
/** 单个采集器的详情面板：锚点信息 + 采集开关 + 直播间 / 动态两个局部标签。 */
import { computed, ref, watch } from 'vue'

import { fmtFull, isHttpUrl, num } from '@/lib/format'
import {
  controlCapture, hint, loadDynamics, loadSessions, saveCollect, state,
} from '@/stores/app'
import type { Room } from '@/types'
import CdnImg from './CdnImg.vue'
import DynamicsPanel from './DynamicsPanel.vue'
import LiveFeedPanel from './LiveFeedPanel.vue'

const props = defineProps<{ room: Room }>()

const colLive = ref(true)
const colDyn = ref(true)
const colOnly = ref(false)
const busy = ref(false)

const name = computed(() => props.room.uname || props.room.label || '')
const liveText = computed(() =>
  ({ 0: '未开播', 1: '直播中', 2: '轮播' } as Record<number, string>)[props.room.live_status ?? 0] || '未知')

const liveSince = computed(() => {
  if (props.room.live_status !== 1) return 0
  return props.room.live_started_at || (props.room.live_time || 0) * 1000
})

const metaList = computed(() => {
  const r = props.room
  const out: string[] = []
  out.push('人气 <b>' + (r.online || '—') + '</b>')
  out.push('房间号 <b>' + r.id + '</b>')
  out.push('动态 <b>' + num(r.dyns || 0) + '</b>')
  if (r.running && r.waiting) out.push('采集 <b>等开播</b>')
  if (liveSince.value) out.push('本场开播 <b>' + fmtFull(liveSince.value) + '</b>')
  return out
})

const captureHintText = computed(() => {
  const r = props.room
  const problems: string[] = []
  if (r.last_error) problems.push('最近错误：' + r.last_error)
  if (r.warning) problems.push(r.warning)
  if (state.svc && state.svc.websockets === false) problems.push('缺少依赖 websockets')
  return { text: problems.join('　|　'), bad: problems.length > 0 }
})

const collectHintText = computed(() => {
  const parts: string[] = []
  parts.push(colLive.value ? (colOnly.value ? '直播间已开（只在开播时采）' : '直播间已开') : '直播间已关')
  parts.push(colDyn.value ? '动态已开' : '动态已关')
  if (props.room.running && props.room.waiting) parts.push('未开播 · 等开播')
  return parts.join(' · ')
})

// 只在切换采集器时回填，避免 4 秒轮询把用户刚勾的选项覆盖掉
watch(() => props.room.id, () => {
  colLive.value = props.room.collect_live !== false
  colDyn.value = props.room.collect_dynamics !== false
  colOnly.value = props.room.live_only === true
}, { immediate: true })

watch(colLive, (on) => {
  if (!on) colOnly.value = false
})

function switchTab(name: 'live' | 'dyn') {
  if (state.subTab === name) return
  if (state.subTab === 'dyn' && state.dirty.dyn &&
      !confirm('译文还没保存，切换标签会丢掉这次修改，继续吗？')) return
  state.subTab = name
  localStorage.setItem('lc-subtab', name)
  if (name === 'dyn') loadDynamics()
  else loadSessions()
}

async function toggleCapture() {
  if (!state.roomId) {
    hint('capture', '请先添加采集器', 'err')
    return
  }
  if (busy.value) return
  busy.value = true
  try {
    const action = props.room.running ? 'stop' : 'start'
    const j = await controlCapture(props.room.id, action)
    hint('capture', j.message, j.ok ? 'ok' : 'err')
  } catch (e: any) {
    hint('capture', '操作失败：' + e.message, 'err')
  } finally {
    busy.value = false
  }
}

async function doSaveCollect() {
  try {
    const only = colLive.value && colOnly.value
    const j = await saveCollect(props.room, {
      collect_live: colLive.value,
      collect_dynamics: colDyn.value,
      live_only: only,
    })
    hint('collect', '已保存：' + (colLive.value ? '采集直播间' : '不采直播间') +
      (only ? '（只在开播时）' : '') + ' · ' + (colDyn.value ? '采集动态' : '不采动态'), 'ok')
    if (j.room) {
      // 以服务端回填为准
      colLive.value = j.room.collect_live !== false
      colDyn.value = j.room.collect_dynamics !== false
      colOnly.value = j.room.live_only === true
    }
    if (colDyn.value) loadDynamics()
  } catch (e: any) {
    hint('collect', '保存失败：' + e.message, 'err')
  }
}
</script>

<template>
  <section class="card">
    <div class="anchor">
      <span>
        <CdnImg v-if="isHttpUrl(room.face)" :src="room.face" kind="anchor-face" alt="up 主头像" />
        <img v-else alt="up 主头像" src="data:," style="width:34px;height:34px">
      </span>
      <div class="who">
        <h2>{{ name }}</h2>
        <span class="sub">
          UID {{ room.uid || '—' }} · ROOM {{ room.id
          }}{{ room.real_room_id && room.real_room_id !== room.id ? ' · 真实 ' + room.real_room_id : ''
          }}{{ room.title ? ' · ' + room.title : '' }}
        </span>
      </div>
      <span class="live-pill" :class="{ on: room.live_status === 1 }">
        <i></i><span>{{ liveText }}</span>
      </span>
    </div>

    <div class="anchor-meta">
      <span v-for="(m, i) in metaList" :key="i" v-html="m"></span>
    </div>

    <div class="row">
      <a class="btn sm" :href="'https://live.bilibili.com/' + room.id"
         target="_blank" rel="noopener">直播间 ↗</a>
      <a v-if="room.uid" class="btn sm" :href="'https://space.bilibili.com/' + room.uid"
         target="_blank" rel="noopener">个人空间 ↗</a>
      <button class="btn" :class="room.running ? 'danger' : 'primary'"
              :disabled="busy || room.collect_live === false"
              :title="room.collect_live === false ? '这个采集器关了「采集直播间」' : ''"
              @click="toggleCapture">
        {{ room.running ? '停止采集' : '开始采集' }}
      </button>
    </div>
    <div class="hint" :class="{ err: captureHintText.bad }">{{ captureHintText.text }}</div>

    <div class="collect-row">
      <span class="lab">采集内容</span>
      <label><input v-model="colLive" type="checkbox"> 直播间（弹幕 / SC / 事件）</label>
      <label title="没开播就不连弹幕服务器，只定时探一次直播状态">
        <input v-model="colOnly" type="checkbox" :disabled="!colLive"> 只在开播时采集
      </label>
      <label><input v-model="colDyn" type="checkbox"> up 主动态</label>
      <button class="btn sm" @click="doSaveCollect">保存开关</button>
      <span class="muted">{{ collectHintText }}</span>
    </div>
    <div class="hint">
      这些开关和「开始采集 / 停止采集」只对当前这位 up 主生效，每个采集器各开各的。定时抓取在「动态」标签里单独设置，默认关闭。
    </div>

    <div class="sub-tabs">
      <button class="sub-tab" :class="{ on: state.subTab === 'live' }" @click="switchTab('live')">
        <i></i>直播间<span class="n">{{ state.segs.length ? state.segs.length + ' 段' : '0' }}</span>
      </button>
      <button class="sub-tab" :class="{ on: state.subTab === 'dyn' }" @click="switchTab('dyn')">
        <i></i>动态<span class="n">{{ state.dyn ? state.dyn.total + ' 条' : (room.dyns || 0) + ' 条' }}</span>
      </button>
    </div>

    <LiveFeedPanel v-if="state.subTab === 'live'" />
    <DynamicsPanel v-else />
  </section>
</template>
