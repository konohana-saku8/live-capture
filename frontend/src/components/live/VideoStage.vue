<script setup lang="ts">
/** 画面区：录制中走 FLV 直播代理；合并产物优先播放，FLV 用 mpegts.js。 */
import { computed, ref, watch } from 'vue'

import { getToken } from '@/api/client'
import { bytes as fmtBytes, fmtDuration, fmtOffset, qnLabel } from '@/lib/format'
import type { RecordingInfo, RecordingPart } from '@/types'
import LiveFlvPlayer from './LiveFlvPlayer.vue'

const props = defineProps<{
  recording: RecordingInfo | null
  live: boolean
  anchor: number
  currentMs: number
}>()
const emit = defineEmits<{ (e: 'time', ms: number): void; (e: 'error', msg: string): void }>()

const part = ref(0)
const partsOpen = ref(true)
const pendingSeek = ref<number | null>(null)
const autoplayOnLoad = ref(false)
const video = ref<HTMLVideoElement | null>(null)
const flv = ref<{ seek: (sec: number) => boolean; pause?: () => void } | null>(null)

const tokenQuery = (() => {
  const token = getToken()
  return token ? '&token=' + encodeURIComponent(token) : ''
})()

const rn = computed(() => props.recording)
const mode = computed<'live' | 'mp4' | 'flv' | 'none'>(() => {
  const r = rn.value
  if (!r) return 'none'
  if (r.status === 'recording' && props.live) return 'live'
  // 回放统一走 MP4 分片：原生 video 不依赖 MSE，PC 与手机都能播
  if (r.mp4_count > 0) return 'mp4'
  if (r.flv_count > 0) return 'flv'
  return 'none'
})

const parts = computed<RecordingPart[]>(() => {
  const r = rn.value
  if (!r) return []
  return mode.value === 'mp4' ? (r.mp4_parts || []) : (r.flv_parts || [])
})

const duration = computed(() => {
  const list = parts.value
  return list.reduce((sum, p) => sum + (p.duration_ms || 0), 0)
})

// MP4 分片用原生 video，直播与纯 FLV 场次才走 mpegts.js
const usesNative = computed(() => mode.value === 'mp4')
const usesFlv = computed(() => mode.value === 'live' || mode.value === 'flv')

const modeLabel = computed(() => ({
  live: '实时画面', mp4: 'MP4 回放', flv: 'FLV 回放', none: '无画面',
} as Record<string, string>)[mode.value])

const mediaUrl = computed(() => {
  const r = rn.value
  if (!r) return ''
  if (mode.value === 'live') {
    const qn = r.actual_qn || r.requested_qn || 250
    return '/api/live/flv?room_id=' + r.room_id + '&qn=' + qn + tokenQuery
  }
  if (mode.value === 'mp4' || mode.value === 'flv') {
    return '/api/recordings/' + r.id + '/media?part=' + part.value +
      '&kind=' + mode.value + tokenQuery
  }
  return ''
})

const noVideoText = computed(() => {
  const r = rn.value
  if (!r) return props.live ? '这场直播没有开启录制' : '这场没有录像'
  if (r.status === 'recording') return '正在录制，下播后这里会出现回放'
  if (r.status === 'pending' || r.status === 'remuxing') return '录像正在转封装成 MP4，稍后自动出现'
  if (r.status === 'failed' || r.status === 'interrupted') {
    return '录像文件异常：' + (r.error || '转封装失败')
  }
  return '这场没有可播放的录像'
})

function offsetOf(index: number) {
  const p = parts.value[index]
  return ((p && p.started_at) || props.anchor) - props.anchor
}

/** 点选集：切到指定分 P，从头播，并把弹幕时间轴同步过去。 */
function pickPart(index: number) {
  if (index === part.value) return
  part.value = index
  pendingSeek.value = 0
  autoplayOnLoad.value = true
  emit('time', offsetOf(index))
}

function onNativeTime() {
  const el = video.value
  if (!el) return
  emit('time', offsetOf(part.value) + (el.currentTime || 0) * 1000)
}

function onFlvTime(ms: number) {
  if (mode.value === 'live') return
  emit('time', offsetOf(part.value) + ms)
}

function applySeek(sec: number) {
  const target = Math.max(0, sec)
  if (usesFlv.value) {
    return flv.value ? flv.value.seek(target) : false
  }
  const el = video.value
  if (!el) {
    pendingSeek.value = target
    return true
  }
  try {
    el.currentTime = target
    el.play().catch(() => { /* 等用户点播放 */ })
    return true
  } catch {
    return false
  }
}

/** 媒体就绪后补做一次跳转，或播放下一段。 */
function onMeta() {
  if (pendingSeek.value != null) {
    const sec = pendingSeek.value
    pendingSeek.value = null
    if (!applySeek(sec)) pendingSeek.value = sec
    return
  }
  if (autoplayOnLoad.value) {
    autoplayOnLoad.value = false
    video.value?.play().catch(() => { /* 等用户点播放 */ })
  }
}

function onEnded() {
  if (part.value < parts.value.length - 1) {
    part.value += 1
    autoplayOnLoad.value = true
  }
}

function onError(msg: string) {
  emit('error', msg)
}

function seek(ms: number) {
  if (mode.value === 'live' || mode.value === 'none') return false
  const target = Math.max(0, ms)
  let index = 0
  for (let i = 0; i < parts.value.length; i++) {
    if (offsetOf(i) <= target + 500) index = i
  }
  const local = Math.max(0, target - offsetOf(index)) / 1000
  if (index !== part.value) {
    part.value = index
    pendingSeek.value = local
    return true
  }
  return applySeek(local)
}

function pause() {
  video.value?.pause()
  flv.value?.pause?.()
}

defineExpose({ seek, pause })

watch(() => rn.value?.id, () => {
  part.value = 0
  pendingSeek.value = null
  autoplayOnLoad.value = false
})

// 直播结束、remux 完成后自动换成 MP4 分片，并尽量停在原来的位置
watch(mode, (next, prev) => {
  if (next !== 'mp4' && next !== 'flv') return
  if (!prev || prev === next) return
  const target = Math.max(0, props.currentMs)
  let index = 0
  for (let i = 0; i < parts.value.length; i++) {
    if (offsetOf(i) <= target + 500) index = i
  }
  part.value = index
  pendingSeek.value = Math.max(0, target - offsetOf(index)) / 1000
})
</script>

<template>
  <div class="studio-video">
    <div class="studio-video-frame">
      <video v-if="usesNative" :key="'native-' + part" ref="video"
             class="studio-video-el" controls playsinline preload="metadata"
             :src="mediaUrl" @timeupdate="onNativeTime" @loadedmetadata="onMeta"
             @ended="onEnded" @error="emit('error', '视频播放失败')"></video>
      <LiveFlvPlayer v-else-if="usesFlv" ref="flv"
                     :src="mediaUrl" :live="mode === 'live'" @time="onFlvTime"
                     @duration="onMeta" @ended="onEnded" @error="onError" />
      <div v-else class="studio-no-video">
        <span class="nv-title">{{ noVideoText }}</span>
        <span class="nv-sub">没有画面时，右侧仍可按时间浏览这场的数据。</span>
      </div>
    </div>
    <div class="studio-video-bar">
      <span class="vtag" :class="mode">{{ modeLabel }}</span>
      <span v-if="rn">{{ qnLabel(rn.actual_qn || rn.requested_qn) }}</span>
      <span v-if="rn && rn.bytes">录像 {{ fmtBytes(rn.bytes) }}</span>
      <span v-if="parts.length > 1">第 {{ part + 1 }} / {{ parts.length }} 段</span>
      <span v-if="duration" class="muted">时长 {{ fmtDuration(duration) }}</span>
      <span class="muted">播放头 {{ fmtOffset(currentMs) }}</span>
    </div>

    <div v-if="parts.length > 1" class="studio-parts">
      <button type="button" class="parts-head" :aria-expanded="partsOpen"
              @click="partsOpen = !partsOpen">
        <span class="ph-title">选集</span>
        <span class="ph-count">共 {{ parts.length }} 段</span>
        <span class="ph-cur">正在播 P{{ part + 1 }}</span>
        <span class="ph-toggle">{{ partsOpen ? '收起' : '展开' }}</span>
      </button>
      <div v-show="partsOpen" class="parts-list">
        <button v-for="(p, i) in parts" :key="i" type="button"
                class="part-chip" :class="{ on: i === part }" @click="pickPart(i)">
          <b>P{{ i + 1 }}</b>
          <em>{{ p.duration_ms ? fmtDuration(p.duration_ms) : '—' }}</em>
        </button>
      </div>
    </div>
  </div>
</template>
