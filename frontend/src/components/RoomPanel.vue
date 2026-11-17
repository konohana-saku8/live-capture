<script setup lang="ts">
/** 单个采集器的详情面板：只看当前抓取状态 + 开关；数据预览去「数据」页。 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import { bytes as fmtBytes, dur, fmtFull, isHttpUrl, num, qnLabel } from '@/lib/format'
import {
  controlCapture, hint, saveCollect, saveDynConfig, state,
} from '@/stores/app'
import type { Room } from '@/types'
import CdnImg from './CdnImg.vue'

const props = defineProps<{ room: Room }>()

const colLive = ref(true)
const colDyn = ref(true)
const colOnly = ref(false)
const autoRec = ref(false)
const recQn = ref(250)
const recDanmaku = ref(true)
const recSc = ref(true)
const recEvents = ref(true)
const recGuard = ref(true)
const recGift = ref(true)
const recRedPocket = ref(true)
const recEnter = ref(false)
const recInteract = ref(true)
const busy = ref(false)

/** 动态定时：按当前 up 主保存，和采集开关一起放在采集器页。 */
const dynAuto = ref(false)
const dynHour = ref(8)
const dynMinute = ref(0)
const schedBusy = ref(false)
const hourOptions = Array.from({ length: 24 }, (_, i) => i)
const minuteOptions = Array.from({ length: 60 }, (_, i) => i)
const pad = (n: number) => ('0' + n).slice(-2)

/** 两个保存按钮共用的即时反馈（'ok' / 'err' / ''）。 */
const savedWhich = ref('')
let flashTimer: number | undefined
function flashSaved(which: string) {
  savedWhich.value = which
  window.clearTimeout(flashTimer)
  flashTimer = window.setTimeout(() => { savedWhich.value = '' }, 2600)
}
onBeforeUnmount(() => window.clearTimeout(flashTimer))

const name = computed(() => props.room.uname || props.room.label || '')
/** uid=0 是本地测试源：不连 B 站，只挂一段本地视频和复用的直播数据。 */
const testSource = computed(() => props.room.uid === 0)
const liveText = computed(() =>
  ({ 0: '未开播', 1: '直播中', 2: '轮播' } as Record<number, string>)[props.room.live_status ?? 0] || '未知')
const captureText = computed(() => {
  if (testSource.value) return '测试源'
  const r = props.room
  if (!r.running) return '未采集'
  return r.waiting ? '等待开播' : '采集中'
})

const liveSince = computed(() => {
  if (props.room.live_status !== 1) return 0
  return props.room.live_started_at || (props.room.live_time || 0) * 1000
})

function ago(ms?: number) {
  if (!ms) return '—'
  const s = Math.max(0, Math.floor((Date.now() - ms) / 1000))
  if (s < 5) return '刚刚'
  if (s < 60) return s + ' 秒前'
  if (s < 3600) return Math.floor(s / 60) + ' 分钟前'
  return fmtFull(ms)
}

const lastPacketText = computed(() => ago(props.room.last_packet_at))
const runStatsText = computed(() => {
  if (testSource.value) return '复用已有场次'
  const c = props.room.counts || {}
  return '弹幕 ' + num(c.danmaku) + ' · SC ' + num(c.sc) +
    ' · 事件 ' + num(c.events)
})
const liveStatsText = computed(() => {
  const st = props.room.live_stats
  if (props.room.live_status !== 1 || !st) return '—'
  return '弹幕 ' + num(st.danmaku) + ' · SC ' + num(st.sc) +
    ' · 事件 ' + num(st.events) +
    (st.sc_amount ? ' · ¥' + num(st.sc_amount) : '')
})
const recordText = computed(() => {
  if (testSource.value) return '本地视频'
  const r = props.room
  if (r.recording) {
    return '录制中 · ' + fmtBytes(r.record_bytes) + ' · ' + dur(r.record_started_at)
  }
  if (r.record_error) return '异常：' + r.record_error
  if (r.auto_record) return '开播自动录制 · ' + qnLabel(r.record_qn)
  return '未开启'
})

const metaList = computed(() => {
  const r = props.room
  const out: string[] = []
  out.push('人气 <b>' + (r.online || '—') + '</b>')
  out.push('房间号 <b>' + r.id + '</b>')
  if (!testSource.value) out.push('动态 <b>' + num(r.dyns || 0) + '</b>')
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

const contentText = computed(() => {
  const kinds: string[] = []
  if (recDanmaku.value) kinds.push('弹幕')
  if (recSc.value) kinds.push('SC')
  if (recEvents.value) {
    const ev: string[] = []
    if (recGuard.value) ev.push('上舰')
    if (recGift.value) ev.push('礼物')
    if (recRedPocket.value) ev.push('红包')
    if (recEnter.value) ev.push('入场')
    if (recInteract.value) ev.push('互动')
    kinds.push('事件' + (ev.length ? '(' + ev.join('/') + ')' : '关'))
  }
  return kinds.length ? kinds.join(' · ') : '不记内容'
})

const collectHintText = computed(() => {
  const parts: string[] = []
  parts.push(colLive.value ? (colOnly.value ? '直播间已开（只在开播时采）' : '直播间已开') : '直播间已关')
  parts.push(colDyn.value ? '动态已开' : '动态已关')
  if (colLive.value) parts.push(contentText.value)
  if (autoRec.value) parts.push('开播自动录制 · ' + qnLabel(recQn.value))
  if (props.room.record_qn_actual) parts.push('实际 ' + qnLabel(props.room.record_qn_actual))
  return parts.join(' · ')
})

// 只在切换采集器时回填，避免 4 秒轮询把用户刚勾的选项覆盖掉
watch(() => props.room.id, () => {
  colLive.value = props.room.collect_live !== false
  colDyn.value = props.room.collect_dynamics !== false
  colOnly.value = props.room.live_only === true
  autoRec.value = props.room.auto_record === true
  recQn.value = props.room.record_qn || 250
  // 服务端已把「全局默认 + 本采集器覆盖」合并成有效值
  recDanmaku.value = props.room.record_danmaku !== false
  recSc.value = props.room.record_sc !== false
  recEvents.value = props.room.record_events !== false
  recGuard.value = props.room.record_guard !== false
  recGift.value = props.room.record_gift !== false
  recRedPocket.value = props.room.record_red_pocket !== false
  recEnter.value = props.room.record_enter === true
  recInteract.value = props.room.record_interact !== false
  // 定时抓取按 up 主存，服务端已经合并成这一位的值
  dynAuto.value = props.room.dyn_auto === true
  const sched = String(props.room.dyn_schedule || '08:00').split(':')
  dynHour.value = Number(sched[0]) || 0
  dynMinute.value = Number(sched[1]) || 0
}, { immediate: true })

watch(colLive, (on) => {
  if (!on) {
    colOnly.value = false
    autoRec.value = false
  }
})

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
      auto_record: colLive.value && autoRec.value,
      record_qn: recQn.value,
      record_danmaku: recDanmaku.value,
      record_sc: recSc.value,
      record_events: recEvents.value,
      record_guard: recGuard.value,
      record_gift: recGift.value,
      record_red_pocket: recRedPocket.value,
      record_enter: recEnter.value,
      record_interact: recInteract.value,
    })
    hint('collect', '已保存：' + (colLive.value ? '采集直播间' : '不采直播间') +
      (only ? '（只在开播时）' : '') + ' · ' + contentText.value +
      ' · ' + (colDyn.value ? '采集动态' : '不采动态') +
      (autoRec.value ? ' · 开播自动录制 ' + qnLabel(recQn.value) : '') +
      (props.room.running ? '（采集中，内容开关需停止后重新开始才生效）' : ''), 'ok')
    flashSaved('collect')
    if (j.room) {
      // 以服务端回填为准
      colLive.value = j.room.collect_live !== false
      colDyn.value = j.room.collect_dynamics !== false
      colOnly.value = j.room.live_only === true
      autoRec.value = !!(j.room.params && j.room.params.auto_record)
      recQn.value = Number(j.room.params?.record_qn) || recQn.value
      const p = j.room.params || {}
      recDanmaku.value = p.record_danmaku !== false
      recSc.value = p.record_sc !== false
      recEvents.value = p.record_events !== false
      recGuard.value = p.record_guard !== false
      recGift.value = p.record_gift !== false
      recRedPocket.value = p.record_red_pocket !== false
      recEnter.value = p.record_enter === true
      recInteract.value = p.record_interact !== false
    }
  } catch (e: any) {
    hint('collect', '保存失败：' + e.message, 'err')
    flashSaved('collect-err')
  }
}

async function saveSchedule() {
  if (schedBusy.value) return
  schedBusy.value = true
  try {
    const j = await saveDynConfig(props.room, dynAuto.value, dynHour.value, dynMinute.value)
    const c = j.config || {}
    hint('sched', (props.room.uname || props.room.label) + '：' +
      (c.dyn_auto ? '每天 ' + pad(Number(c.dyn_hour) || 0) + ':' + pad(Number(c.dyn_minute) || 0) + ' 定时拉取'
        : '已关闭定时拉取'), 'ok')
    if (c) {
      dynAuto.value = !!c.dyn_auto
      dynHour.value = Number(c.dyn_hour) || 0
      dynMinute.value = Number(c.dyn_minute) || 0
    }
    flashSaved('sched')
  } catch (e: any) {
    hint('sched', '保存定时失败：' + e.message, 'err')
    flashSaved('sched-err')
  } finally {
    schedBusy.value = false
  }
}

</script>

<template>
  <section class="card">
    <div class="anchor">
      <span>
        <CdnImg v-if="isHttpUrl(room.face)" :src="room.face" kind="anchor-face" alt="up 主头像" />
        <span v-else class="anchor-face-ph">{{ (name || '?').slice(0, 1) }}</span>
      </span>
      <div class="who">
        <h2>{{ name }}</h2>
        <span class="sub">
          <template v-if="testSource">ROOM {{ room.id }} · {{ room.title || '本地测试源' }}</template>
          <template v-else>UID {{ room.uid || '—' }} · ROOM {{ room.id
            }}{{ room.real_room_id && room.real_room_id !== room.id ? ' · 真实 ' + room.real_room_id : ''
            }}{{ room.title ? ' · ' + room.title : '' }}</template>
        </span>
      </div>
      <span class="live-pill" :class="{ on: room.live_status === 1 }">
        <i></i><span>{{ liveText }}</span>
      </span>
    </div>

    <div class="anchor-meta">
      <span v-for="(m, i) in metaList" :key="i" v-html="m"></span>
    </div>

    <div class="cap-grid">
      <div class="cap-stat">
        <span>采集状态</span>
        <b :class="{ on: room.running && !room.waiting, wait: room.waiting }">{{ captureText }}</b>
      </div>
      <div class="cap-stat">
        <span>最近收包</span>
        <b>{{ lastPacketText }}</b>
      </div>
      <div class="cap-stat">
        <span>本次运行</span>
        <b>{{ runStatsText }}</b>
      </div>
      <div class="cap-stat">
        <span>本场数据</span>
        <b>{{ liveStatsText }}</b>
      </div>
      <div class="cap-stat">
        <span>本次开播</span>
        <b>{{ liveSince ? fmtFull(liveSince) : '—' }}</b>
      </div>
      <div class="cap-stat" :class="{ bad: !!room.record_error }">
        <span>录制</span>
        <b>{{ recordText }}</b>
      </div>
    </div>

    <template v-if="testSource">
      <div class="hint">
        测试源：本地视频 + 复用已有场次的弹幕 / SC / 事件，不连接 B 站。
        播放、场次管理和导出都在「数据」页。
      </div>
    </template>
    <template v-else>
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
        <span class="lab">采集源</span>
        <label><input v-model="colLive" type="checkbox"> 采集直播间</label>
        <label title="没开播就不连弹幕服务器，只定时探一次直播状态">
          <input v-model="colOnly" type="checkbox" :disabled="!colLive"> 只在开播时采集
        </label>
        <label title="主播一开播就自动把直播流录成 FLV，下播停止">
          <input v-model="autoRec" type="checkbox" :disabled="!colLive"> 开播自动录制
        </label>
        <label title="未登录时 B 站可能只给 480P，以实际清晰度为准">
          清晰度
          <select v-model.number="recQn" :disabled="!colLive" style="width:86px">
            <option :value="10000">原画</option>
            <option :value="400">1080P</option>
            <option :value="250">720P</option>
            <option :value="150">480P</option>
            <option :value="80">360P</option>
          </select>
        </label>
        <label><input v-model="colDyn" type="checkbox"> up 主动态</label>
      </div>

      <div class="collect-row sched-row">
        <span class="lab">动态定时</span>
        <label><input v-model="dynAuto" type="checkbox" :disabled="!colDyn"> 每天定时拉取</label>
        <label>
          <select v-model.number="dynHour" :disabled="!colDyn || !dynAuto" style="width:66px">
            <option v-for="h in hourOptions" :key="h" :value="h">{{ pad(h) }}</option>
          </select> 时
          <select v-model.number="dynMinute" :disabled="!colDyn || !dynAuto" style="width:66px">
            <option v-for="m in minuteOptions" :key="m" :value="m">{{ pad(m) }}</option>
          </select> 分
        </label>
        <button class="btn sm" :disabled="!colDyn || schedBusy"
                :class="savedWhich === 'sched' ? 'ok' : savedWhich === 'sched-err' ? 'err' : ''"
                @click="saveSchedule">
          {{ schedBusy ? '保存中…' : savedWhich === 'sched' ? '✓ 已保存' : savedWhich === 'sched-err' ? '保存失败' : '保存定时' }}
        </button>
        <span class="muted">{{ dynAuto ? '每天 ' + pad(dynHour) + ':' + pad(dynMinute) : '未开启' }}</span>
      </div>
      <div class="hint" :class="state.hints.sched.kind">{{ state.hints.sched.msg }}</div>

      <div class="collect-row content-row">
        <span class="lab">直播内容</span>
        <label><input v-model="recDanmaku" type="checkbox"> 弹幕</label>
        <label><input v-model="recSc" type="checkbox"> SC</label>
        <label><input v-model="recEvents" type="checkbox"> 事件</label>
        <span class="muted">按当前 up 主保存</span>
      </div>
      <div class="collect-row events-row" :class="{ off: !recEvents }">
        <span class="lab">事件类型</span>
        <label><input v-model="recGuard" type="checkbox" :disabled="!recEvents"> 上舰</label>
        <label title="包括普通礼物和投喂">
          <input v-model="recGift" type="checkbox" :disabled="!recEvents"> 礼物 / 投喂
        </label>
        <label><input v-model="recRedPocket" type="checkbox" :disabled="!recEvents"> 红包</label>
        <label title="入场事件量大，默认关闭">
          <input v-model="recEnter" type="checkbox" :disabled="!recEvents"> 入场
        </label>
        <label title="关注 / 分享 / 特别关注 / 互相关注">
          <input v-model="recInteract" type="checkbox" :disabled="!recEvents"> 其他互动
        </label>
      </div>

      <div class="collect-row">
        <button class="btn sm" @click="doSaveCollect"
                :class="savedWhich === 'collect' ? 'ok' : savedWhich === 'collect-err' ? 'err' : ''">
          {{ savedWhich === 'collect' ? '✓ 已保存' : savedWhich === 'collect-err' ? '保存失败' : '保存开关' }}
        </button>
        <span class="muted">{{ collectHintText }}</span>
      </div>
      <div class="hint">
        以上开关按当前 up 主分别保存，和「开始采集 / 停止采集」一样只对这位主播生效；采集运行中修改内容开关，需要停止后重新开始才生效。动态定时也在本页设置。弹幕 / SC / 事件与动态的预览、场次管理、导出都在「数据」页。
      </div>

    </template>
  </section>
</template>
