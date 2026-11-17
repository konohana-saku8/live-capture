<script setup lang="ts">
/** up 主动态：按天分组、分页、定时抓取（按 up 主）、可编辑译文。 */
import { computed, onBeforeUnmount, ref, watch, watchEffect } from 'vue'

import { fmt, fmtFull, dynDayKey, dynDayLabel, num } from '@/lib/format'
import {
  currentRoom, fetchDynamics, hint, loadDynamics, loadRooms,
  saveDynConfig, saveDynTranslation, state,
} from '@/stores/app'
import type { Dynamics } from '@/types'
import CdnImg from './CdnImg.vue'

const hour = ref(8)
const minute = ref(0)
const auto = ref(false)
const busy = ref(false)
/** 正在编辑的卡片：dyn_id -> 草稿文本 */
const drafts = ref<Record<string, string>>({})
let qTimer: number | undefined

const testSource = computed(() => currentRoom.value?.uid === 0)
const cfg = computed(() => state.dyn?.config)
const items = computed(() => state.dyn?.items || [])
const total = computed(() => state.dyn?.total || 0)
const pages = computed(() => Math.max(1, Math.ceil(total.value / (state.dyn?.limit || state.dynPageSize))))

const groups = computed(() => {
  const map = new Map<string, Dynamics[]>()
  for (const it of items.value) {
    const k = dynDayKey(it.pub_ts)
    if (!map.has(k)) map.set(k, [])
    map.get(k)!.push(it)
  }
  return [...map.entries()].map(([key, list]) => ({ key, label: dynDayLabel(key), list }))
})

const infoText = computed(() => {
  if (testSource.value) return '共 0 条'
  const c = cfg.value
  const hh = ('0' + (c?.dyn_hour ?? 8)).slice(-2)
  const mm = ('0' + (c?.dyn_minute ?? 0)).slice(-2)
  return '共 ' + total.value + ' 条 · ' +
    (c?.dyn_last_ts ? '上次拉取 ' + fmt(c.dyn_last_ts) : '还没拉过') +
    (c?.dyn_auto ? ' · 每天 ' + hh + ':' + mm + ' 定时抓取' : ' · 定时抓取已关')
})

const dirty = computed(() => {
  for (const it of items.value) {
    const draft = drafts.value[it.dyn_id]
    if (draft !== undefined && draft.trim() !== String(it.text_trans || '').trim()) return true
  }
  return false
})

watchEffect(() => {
  state.dirty.dyn = dirty.value
})

// 离开数据页 / 收起面板时清掉脏标记，避免之后每次导航都被拦
onBeforeUnmount(() => { state.dirty.dyn = false })

// 面板挂载 / 换 up 主时自己取一次动态；测试源没有动态可查
watch(() => currentRoom.value?.id, (id) => {
  if (id && !testSource.value) loadDynamics()
  else state.dyn = null
}, { immediate: true })

// 服务端回填这一位 up 主的定时
watch(() => cfg.value, (c) => {
  if (!c) return
  auto.value = !!c.dyn_auto
  hour.value = c.dyn_hour ?? 8
  minute.value = c.dyn_minute ?? 0
}, { immediate: true })

const hourOptions = Array.from({ length: 24 }, (_, i) => i)
const minuteOptions = Array.from({ length: 60 }, (_, i) => i)
const pad = (n: number) => ('0' + n).slice(-2)

function confirmDirty(msg = '译文还没保存，确定放弃这次的修改？') {
  if (!dirty.value) return true
  return confirm(msg)
}

async function doFetch() {
  busy.value = true
  try {
    await fetchDynamics()
    await loadDynamics()
  } finally {
    busy.value = false
  }
}

async function saveSchedule() {
  const room = currentRoom.value
  if (!room) return
  try {
    const j = await saveDynConfig(room, auto.value, hour.value, minute.value)
    await loadDynamics(true)
    hint('dyn', (room.uname || room.label) + '：' +
      (j.config.dyn_auto ? '每天 ' + pad(hour.value) + ':' + pad(minute.value) + ' 自动拉取'
        : '已关闭定时拉取'), 'ok')
    loadRooms()
  } catch (e: any) {
    hint('dyn', '保存失败：' + e.message, 'err')
  }
}

function onQueryInput() {
  window.clearTimeout(qTimer)
  qTimer = window.setTimeout(() => {
    state.dynPage = 0
    loadDynamics()
  }, 300)
}

async function turnPage(page: number) {
  if (!confirmDirty('译文还没保存，翻页会丢掉这次修改，继续吗？')) return
  drafts.value = {}
  state.dynPage = Math.max(0, page)
  await loadDynamics(true)
}

function startEdit(it: Dynamics) {
  drafts.value = { ...drafts.value, [it.dyn_id]: it.text_trans || '' }
}

function cancelEdit(it: Dynamics) {
  const draft = drafts.value[it.dyn_id]
  if (draft !== undefined && draft.trim() !== String(it.text_trans || '').trim() &&
      !confirm('译文还没保存，确定放弃这次的修改吗？')) return
  const next = { ...drafts.value }
  delete next[it.dyn_id]
  drafts.value = next
}

async function saveEdit(it: Dynamics) {
  const text = String(drafts.value[it.dyn_id] ?? '').trim()
  try {
    await saveDynTranslation(it.dyn_id, text)
    hint('dyn', '译文已保存', 'ok')
    const next = { ...drafts.value }
    delete next[it.dyn_id]
    drafts.value = next
    await loadDynamics(true)
  } catch (e: any) {
    hint('dyn', '保存译文失败：' + e.message, 'err')
  }
}

/** 跳转链接可能是 // 开头的协议相对地址。 */
function jumpUrl(url?: string) {
  if (!url) return ''
  return url.indexOf('//') === 0 ? 'https:' + url : url
}
</script>

<template>
  <div class="room-block" data-panel="dyn">
    <div class="block-meta">{{ infoText }}</div>

    <template v-if="testSource">
      <div class="hint">
        测试源没有 up 主动态；只有本地视频和复用的直播数据，切到「直播场次」查看。
      </div>
    </template>
    <template v-else>
      <div class="row">
        <button class="btn sm primary" :disabled="busy" @click="doFetch">立即拉取</button>
        <label style="display:flex;align-items:center;gap:6px;margin:0;color:var(--ink-2);font-size:12px">
          <input v-model="auto" type="checkbox"> 每天定时拉取
        </label>
        <label style="display:flex;align-items:center;gap:6px;margin:0;color:var(--ink-2);font-size:12px">
          <select v-model.number="hour" style="width:70px">
            <option v-for="h in hourOptions" :key="h" :value="h">{{ pad(h) }}</option>
          </select> 时
          <select v-model.number="minute" style="width:70px">
            <option v-for="m in minuteOptions" :key="m" :value="m">{{ pad(m) }}</option>
          </select> 分
        </label>
        <button class="btn sm" @click="saveSchedule">保存定时</button>
        <input v-model="state.dynQuery" type="text" placeholder="搜索动态 / 译文"
               style="flex:1 1 160px" @input="onQueryInput">
      </div>

      <div class="hint" :class="state.hints.dyn.kind">{{ state.hints.dyn.msg }}</div>
      <div class="hint">
        按天展示这位 up 主的 B 站动态，可随时添加或修改译文。定时抓取是按 up 主分别设置的，默认关闭；打开后只需服务保持运行。
      </div>
    </template>

    <div>
      <div v-if="!items.length && !testSource" class="empty" style="padding:14px 4px">
        {{ state.dynQuery
          ? '没有匹配「' + state.dynQuery + '」的动态。'
          : '还没有动态。点「立即拉取」从 B 站获取这位 up 主的最新动态。' }}
      </div>

      <div v-for="g in groups" :key="g.key" class="dyn-day">
        <h3>{{ g.label }}</h3>
        <div v-for="it in g.list" :key="it.dyn_id" class="dyn-card">
          <div class="dyn-head">
            <span class="dyn-time">{{ fmtFull((it.pub_ts || 0) * 1000) }}</span>
            <a v-if="it.jump_url" class="dyn-open" :href="jumpUrl(it.jump_url)"
               target="_blank" rel="noopener">打开原动态 ↗</a>
          </div>
          <div class="dyn-text">{{ it.text || '（无正文）' }}</div>
          <div v-if="it.text_trans" class="dyn-trans">{{ it.text_trans }}</div>
          <div v-if="(it.pics || []).length" class="dyn-pics">
            <CdnImg v-for="(u, i) in it.pics" :key="i" :src="u" alt="动态图片" />
          </div>

          <div v-if="drafts[it.dyn_id] !== undefined" class="dyn-edit"
               :class="{ dirty: drafts[it.dyn_id].trim() !== String(it.text_trans || '').trim() }">
            <textarea v-model="drafts[it.dyn_id]"
                      placeholder="添加译文（人工翻译，留空保存即清除）"></textarea>
            <span class="unsaved">未保存 · 记得点「保存译文」</span>
            <div class="dyn-actions">
              <button class="btn sm primary" @click="saveEdit(it)">保存译文</button>
              <button class="btn sm" @click="cancelEdit(it)">取消</button>
            </div>
          </div>
          <div v-else class="dyn-actions dyn-tools">
            <button class="btn sm" @click="startEdit(it)">
              {{ it.text_trans ? '修改译文' : '添加译文' }}
            </button>
          </div>
        </div>
      </div>

      <div v-if="pages > 1" class="dyn-pager">
        <button class="btn sm" :disabled="state.dynPage <= 0"
                @click="turnPage(state.dynPage - 1)">‹ 上一页</button>
        <span class="pg">
          第 <b>{{ state.dynPage + 1 }}</b> / {{ pages }} 页 ·
          第 {{ num((state.dyn?.offset || 0) + 1) }}–{{ num((state.dyn?.offset || 0) + items.length) }} 条
          / 共 {{ num(total) }} 条
        </span>
        <button class="btn sm" :disabled="state.dynPage >= pages - 1"
                @click="turnPage(state.dynPage + 1)">下一页 ›</button>
      </div>
    </div>
  </div>
</template>
