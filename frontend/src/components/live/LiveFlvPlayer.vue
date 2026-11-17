<script setup lang="ts">
/** mpegts.js 播放 FLV：直播代理与没有 MP4 时的静态回放共用。 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import mpegts from 'mpegts.js'

const props = defineProps<{ src: string; live: boolean }>()
const emit = defineEmits<{
  (e: 'time', ms: number): void
  (e: 'duration', ms: number): void
  (e: 'ended'): void
  (e: 'error', msg: string): void
}>()

const video = ref<HTMLVideoElement | null>(null)
const failed = ref('')
let player: ReturnType<typeof mpegts.createPlayer> | null = null

function destroy() {
  const p = player
  player = null
  if (!p) return
  try { p.pause() } catch { /* 已销毁 */ }
  try { p.unload() } catch { /* 已销毁 */ }
  try { p.detachMediaElement() } catch { /* 已销毁 */ }
  try { p.destroy() } catch { /* 已销毁 */ }
}

function load() {
  destroy()
  failed.value = ''
  const el = video.value
  if (!el || !props.src) return
  if (!mpegts.isSupported()) {
    failed.value = '当前浏览器不支持 MSE，无法播放 FLV。可换 Chrome / Edge，或等录像转封装成 MP4。'
    return
  }
  const p = mpegts.createPlayer(
    { type: 'flv', url: props.src, isLive: props.live, cors: false },
    props.live
      ? {
          enableWorker: false,
          enableStashBuffer: false,
          liveBufferLatencyChasing: true,
          liveBufferLatencyMaxLatency: 3,
          liveBufferLatencyMinRemain: 1,
          lazyLoad: false,
        }
      : {
          enableWorker: false,
          lazyLoad: true,
          autoCleanupSourceBuffer: true,
          seekType: 'range',
        },
  )
  player = p
  p.on(mpegts.Events.ERROR, (_type: string, _detail: string, info: { msg?: string }) => {
    emit('error', (info && info.msg) || 'FLV 播放失败')
  })
  p.attachMediaElement(el)
  p.load()
  const started = p.play()
  if (started && typeof (started as Promise<void>).catch === 'function') {
    ;(started as Promise<void>).catch(() => { /* 等用户点播放 */ })
  }
}

function onTime() {
  const el = video.value
  if (el) emit('time', (el.currentTime || 0) * 1000)
}

function onMeta() {
  const el = video.value
  if (el && Number.isFinite(el.duration)) emit('duration', el.duration * 1000)
}

function seek(sec: number) {
  const el = video.value
  if (!el || !Number.isFinite(sec)) return false
  try {
    el.currentTime = Math.max(0, sec)
    return true
  } catch {
    return false
  }
}

function pause() {
  try { player?.pause() } catch { /* 已销毁 */ }
  video.value?.pause()
}

defineExpose({ seek, pause })

watch(() => props.src, () => load())
onMounted(() => load())
onBeforeUnmount(destroy)
</script>

<template>
  <div class="flv-wrap">
    <video ref="video" class="studio-video-el" controls playsinline preload="auto"
           :muted="live" @timeupdate="onTime" @loadedmetadata="onMeta"
           @ended="emit('ended')"></video>
    <div v-if="failed" class="flv-fail">{{ failed }}</div>
  </div>
</template>
