<script setup lang="ts">
/** 数据：直播场次 + up 主动态。直播场次含日历、画面、弹幕 / SC / 事件。 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { download } from '@/api/client'
import { bytes as fmtBytes, fmtFull, fmtHM, num, qnLabel, segRange, segTitle } from '@/lib/format'
import { dayLabel } from '@/lib/calendar'
import { loadDynamics, loadRooms, state } from '@/stores/app'
import {
  anchorMs, canPlay, canSeek, deleteSegment, disposeLiveRoom, isLiveSeg, liveState,
  loadCalendar, loadWindow, recording, selectedDaySegs, selectedSeg,
  selectDate, selectSegment, setCurrent, setRoom, tickGap, tickLive,
} from '@/stores/liveRoom'
import type { Segment } from '@/types'
import ArchiveCalendar from '@/components/live/ArchiveCalendar.vue'
import DynamicsPanel from '@/components/DynamicsPanel.vue'
import SessionCards from '@/components/live/SessionCards.vue'
import StudioFeed from '@/components/live/StudioFeed.vue'
import VideoStage from '@/components/live/VideoStage.vue'

const videoRef = ref<{ seek: (ms: number) => boolean; pause: () => void } | null>(null)
const noteText = ref('')
const expType = ref<'danmaku' | 'sc' | 'events'>('danmaku')
const savedTab = localStorage.getItem('lc-data-tab')
const dataTab = ref<'live' | 'dyn'>(savedTab === 'dyn' ? 'dyn' : 'live')
/** 两个标签首次打开后都保持挂载，切换只切 v-show，避免重建 1.6 万条弹幕 DOM */
const liveMounted = ref(dataTab.value === 'live')
const dynMounted = ref(dataTab.value === 'dyn')
let timer: number | undefined
let noteTimer: number | undefined
let calTick = 0

const rooms = computed(() => state.rooms)
const seg = computed(() => selectedSeg.value)
const selectedRoom = computed(
  () => state.rooms.find((r) => r.id === liveState.roomId) || null,
)
const summary = computed(() => liveState.summary)
const segName = computed(() => {
  const s = seg.value
  if (!s) return ''
  return s.kind === 'live' ? '第 ' + s.index + ' 场' : segTitle(s)
})
const recStatus = computed(() => {
  if (seg.value && seg.value.kind === 'gap') return '场间数据'
  const r = recording.value
  if (!r) return '无录像'
  if (r.status === 'recording') return '录制中'
  if (r.status === 'ready') return 'MP4'
  if (r.status === 'flv') return 'FLV'
  if (r.status === 'pending' || r.status === 'remuxing') return '转封装中'
  return '录像异常'
})

onMounted(async () => {
  await loadRooms()
  const known = state.rooms.some((r) => r.id === liveState.roomId)
  const rid = known ? liveState.roomId : (state.roomId || state.rooms[0]?.id || 0)
  await setRoom(rid)
  timer = window.setInterval(onTick, 2000)
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
  window.clearTimeout(noteTimer)
  disposeLiveRoom()
})

function onTick() {
  if (document.hidden) return
  if (isLiveSeg.value) {
    tickLive()
    calTick += 1
    if (calTick % 10 === 0) void loadCalendar(true)
    return
  }
  // 下播后仍在收数据的尾部场间：继续追加，场次统计也跟着刷新
  const s = seg.value
  if (s && !s.end) {
    tickGap()
    calTick += 1
    if (calTick % 10 === 0) void loadCalendar(true)
    return
  }
  // 下播后轮询到 remux 完成，自动从 FLV 切到 MP4
  const r = recording.value
  if (r && (r.status === 'recording' || r.status === 'pending' || r.status === 'remuxing')) {
    calTick += 1
    if (calTick % 5 === 0) void loadCalendar(true)
  }
}

async function onRoomChange(event: Event) {
  const select = event.target as HTMLSelectElement
  const id = Number(select.value) || 0
  if (dataTab.value === 'dyn' && state.dirty.dyn &&
      !confirm('动态译文还没保存，切换 up 主会丢掉这次修改，继续吗？')) {
    select.value = String(liveState.roomId)
    return
  }
  await setRoom(id)
}

function switchTab(tab: 'live' | 'dyn') {
  if (dataTab.value === tab) return
  if (tab === 'live') liveMounted.value = true
  else dynMounted.value = true
  dataTab.value = tab
  localStorage.setItem('lc-data-tab', tab)
  // 动态面板挂载后不会卸载；隐藏直播时暂停视频，避免后台继续解码 / 出声
  if (tab === 'dyn') void nextTick(() => videoRef.value?.pause())
}

function onTime(ms: number) {
  setCurrent(ms)
}

function onPick(at: number) {
  const target = at - anchorMs.value
  if (target < 0) {
    note('这条消息早于录像开始')
    return
  }
  if (isLiveSeg.value) {
    note('直播中不能回跳，等回放再点')
    return
  }
  if (canSeek.value && videoRef.value) {
    videoRef.value.seek(target)
    setCurrent(target)
    return
  }
  // 没有可播放画面时，仍用播放头驱动右侧高亮
  setCurrent(target)
  note(canPlay.value ? '这段录像还在转封装，先按时间浏览消息' : '这段没有录像，只能按时间浏览消息')
}

function note(msg: string) {
  noteText.value = msg
  window.clearTimeout(noteTimer)
  noteTimer = window.setTimeout(() => { noteText.value = '' }, 3200)
}

function refresh() {
  if (dataTab.value === 'dyn') void loadDynamics(true)
  else void loadCalendar(true)
}

/** 导出当前选中段（场 / 场间）的某一类数据。 */
async function exportData(fmt: 'json' | 'csv') {
  const s = seg.value
  if (!s || !liveState.roomId) {
    note('先在左侧选一段')
    return
  }
  let url = '/api/export?type=' + expType.value + '&format=' + fmt +
    '&room_id=' + liveState.roomId + '&q=' + encodeURIComponent(liveState.q)
  if (s.start) url += '&since=' + s.start
  if (s.end) url += '&until=' + s.end
  const name = 'room-' + liveState.roomId + '-' + expType.value + '-' +
    s.kind + s.index + '.' + fmt
  try {
    await download(url, name)
  } catch (e: any) {
    note('导出失败：' + e.message)
  }
}

async function onDeleteSegment(s: Segment) {
  const label = segTitle(s) + ' · ' + segRange(s)
  const marks = s.kind === 'live' ? '，含开播 / 下播标记' : '（保留开播 / 下播标记）'
  const detail = '弹幕 ' + num(s.danmaku) + '、SC ' + num(s.sc) + marks
  if (!window.confirm('确认删除「' + label + '」？\n' + detail +
      '\n删除后不可恢复。')) return
  try {
    const j = await deleteSegment(s)
    const d = (j && j.deleted) || {}
    note('已删除：弹幕 ' + (d.danmaku || 0) + '、SC ' + (d.sc || 0) +
      '、事件 ' + (d.events || 0))
  } catch (e: any) {
    note('删除失败：' + e.message)
  }
}

// 下播时整段数据要重新取一次，把末尾的「下播」事件也带进来
watch(isLiveSeg, (live, prev) => {
  if (!prev || live) return
  setCurrent(0)
  void loadWindow()
})
</script>

<template>
  <div class="page live-room-view">
    <div class="live-head">
      <div>
        <div class="eyebrow">DATA</div>
        <h1>数据</h1>
      </div>
      <div class="live-head-right">
        <select v-if="rooms.length" class="live-room-select" :value="liveState.roomId"
                @change="onRoomChange">
          <option v-for="r in rooms" :key="r.id" :value="r.id">
            {{ r.uname || r.label || r.id }}
          </option>
        </select>
        <button class="btn sm" @click="refresh">刷新</button>
      </div>
    </div>

    <div v-if="!rooms.length" class="empty pad">还没有采集器，请先到「采集器」页添加。</div>
    <template v-else>
      <div class="sub-tabs data-tabs">
        <button class="sub-tab" :class="{ on: dataTab === 'live' }" @click="switchTab('live')">
          <i></i>直播场次<span class="n">{{ liveState.segments.length }} 段</span>
        </button>
        <button class="sub-tab" :class="{ on: dataTab === 'dyn' }" @click="switchTab('dyn')">
          <i></i>up 主动态<span class="n">{{ selectedRoom?.dyns || 0 }} 条</span>
        </button>
      </div>

      <div v-if="liveMounted" v-show="dataTab === 'live'">
      <div class="live-summary">
        <span v-if="selectedRoom">主播 <b>{{ selectedRoom.uname || selectedRoom.label }}</b></span>
        <span>录像 <b>{{ summary ? summary.count : 0 }}</b> 场 · <b>{{ fmtBytes(summary?.total_bytes) }}</b></span>
        <span>磁盘剩余 <b>{{ fmtBytes(summary?.free_bytes) }}</b></span>
        <span v-if="summary?.warn" class="warn">剩余不足 2GB，建议清理</span>
      </div>

      <div class="live-tools">
        <span class="muted">导出当前段</span>
        <select v-model="expType" class="live-tool-select">
          <option value="danmaku">弹幕</option>
          <option value="sc">SC</option>
          <option value="events">事件</option>
        </select>
        <button class="btn sm" :disabled="!seg" @click="exportData('json')">JSON</button>
        <button class="btn sm" :disabled="!seg" @click="exportData('csv')">CSV</button>
        <span class="muted">场 / 场间数据都可在右侧预览；✕ 删除整段</span>
      </div>

      <div class="live-index">
        <ArchiveCalendar :segments="liveState.segments" :selected-date="liveState.selectedDate"
                         :loading="liveState.loadingCal" @select="selectDate" />
        <div class="live-day">
          <div class="live-day-head">
            <h3>{{ dayLabel(liveState.selectedDate) || '选择日期' }}</h3>
            <span class="muted">{{ selectedDaySegs.length }} 段</span>
          </div>
          <SessionCards :segments="selectedDaySegs" :selected-key="liveState.selectedKey"
                        @select="selectSegment" @delete="onDeleteSegment" />
        </div>
      </div>

      <div v-if="seg" class="studio-stage">
        <div class="studio-left">
          <div class="studio-seg-head">
            <b>{{ fmtHM(seg.start) }}<template v-if="seg.end"> - {{ fmtHM(seg.end) }}</template><template v-else> - 至今</template></b>
            <span class="muted">{{ segName }} · {{ fmtFull(seg.start) }}</span>
            <span class="vtag" :class="isLiveSeg ? 'live' : (seg.kind === 'gap' ? 'none' : 'mp4')">{{ recStatus }}</span>
            <span v-if="recording && recording.actual_qn" class="muted">
              实际 {{ qnLabel(recording.actual_qn) }}
            </span>
            <span v-if="isLiveSeg" class="live-dot">直播中</span>
          </div>
          <VideoStage ref="videoRef" :recording="recording" :live="isLiveSeg"
                      :anchor="anchorMs" :current-ms="liveState.currentMs"
                      @time="onTime" @error="note" />
        </div>
        <StudioFeed :anchor-ms="anchorMs" :relative="!!seg"
                    :seekable="!isLiveSeg" @pick="onPick" />
      </div>
      <div v-else class="empty pad">这位主播还没有直播场次记录。</div>

      <div class="hint">{{ noteText || liveState.error }}</div>
      <div class="hint">
        日历列出直播场次与场间；「下播」事件归在对应场次末尾。直播中看实时画面，下播后自动转封装成 MP4
        再回放。弹幕 / SC / 事件会按画面时间直接叠在视频上，画面右上角「弹」可开关；右侧三列用于浏览
        和搜索，默认显示最新一页，往上滑到列顶继续加载更早的消息，直播时新消息追加到列尾。回放时
        点击任意消息可跳到对应画面。
      </div>
      </div>
      <div v-if="dynMounted" v-show="dataTab === 'dyn'">
        <DynamicsPanel />
      </div>
    </template>
  </div>
</template>
