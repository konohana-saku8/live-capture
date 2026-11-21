<script setup lang="ts">
/**
 * 日志页：给写代码的人看。
 *
 * 一条日志除了时间与文本，还带「哪个文件第几行」和 error / warn 的完整
 * traceback（点开就是堆栈）；页面顶部是即时诊断，回答「服务现在健康吗」——
 * 线程还在不在、磁盘剩多少、DB 涨到多大、ffmpeg 有没有。
 */
import { computed, nextTick, ref, watch } from 'vue'

import { download } from '@/api/client'
import { bytes, escapeHtml, fmtFull, fmtStampFull, fmtDuration } from '@/lib/format'
import { loadLogs, state, uiLog } from '@/stores/app'
import type { LogRow } from '@/types'

interface Row extends LogRow {
  /** 用于着色与「只看问题」：网页日志从 `[error]` 前缀里取。 */
  sev: string
  /** 来源：后端是 `文件:行号`，网页操作是 `web`。 */
  srcLabel: string
}

const sev = ref('all')
const q = ref('')
const follow = ref(true)
/** 全部展开 / 全部收起；默认只展开 error（看错误是第一诉求）。 */
const openAll = ref(false)
const openErr = ref(true)
const showDiag = ref(true)
const showThreads = ref(false)
const copied = ref('')
const box = ref<HTMLDivElement | null>(null)
const opened = ref<Record<string, boolean>>({})

const SEVS: [string, string][] = [
  ['all', '全部'], ['issue', '只看问题'], ['error', '错误'],
  ['warn', '警告'], ['info', '信息'], ['web', '网页'],
]

const needle = computed(() => q.value.trim().toLowerCase())

function webRow(x: LogRow): Row {
  const m = /^\[(\w+)\]\s*/.exec(x.msg)
  const level = m ? m[1] : 'info'
  return {
    ts: x.ts, msg: x.msg, level: 'web', srcLabel: '', n: x.n,
    sev: level === 'error' ? 'error' : level === 'warn' ? 'warn' : 'info',
  }
}

const all = computed<Row[]>(() => {
  const rows: Row[] = [
    ...state.serverLogs.map((x) => ({ ...x, sev: x.level, srcLabel: x.src || '' })),
    ...state.webLogs.map(webRow),
  ]
  rows.sort((a, b) => a.ts - b.ts)
  return rows
})

const rows = computed(() => all.value.filter((x) => {
  if (sev.value === 'issue') {
    if (x.sev !== 'error' && x.sev !== 'warn') return false
  } else if (sev.value !== 'all' && x.level !== sev.value) {
    return false
  }
  const n = needle.value
  if (!n) return true
  return (x.msg + ' ' + x.srcLabel + ' ' + (x.tb || '')).toLowerCase().includes(n)
}))

const counts = computed(() => {
  const c: Record<string, number> = { error: 0, warn: 0, info: 0, web: 0 }
  for (const x of all.value) {
    if (x.level === 'web') c.web += 1
    else c[x.sev] = (c[x.sev] || 0) + 1
  }
  return c
})

const lastError = computed(() => {
  for (let i = all.value.length - 1; i >= 0; i -= 1) {
    if (all.value[i].sev === 'error') return all.value[i].ts
  }
  return 0
})

const stats = computed(() => state.logStats)
const diag = computed(() => state.logDiag)
const truncNote = computed(() => {
  const s = stats.value
  return s ? '缓冲 ' + s.buffered + '/' + s.capacity : ''
})

function rowId(x: Row) {
  return x.ts + '|' + x.level + '|' + x.msg.slice(0, 64)
}

const stacked = computed(() => openAll.value && openErr.value)

function toggleAll() {
  const next = !stacked.value
  openAll.value = next
  openErr.value = next
}

function isOpen(x: Row) {
  if (!x.tb) return false
  return openAll.value || (x.sev === 'error' && openErr.value) ||
    !!opened.value[rowId(x)]
}

function toggle(x: Row) {
  if (!x.tb) return
  const id = rowId(x)
  opened.value[id] = !opened.value[id]
}

/** 关键词高亮：先转义再插 <mark>，日志里带 HTML 也不会漏出去。 */
function hl(text: string) {
  const safe = escapeHtml(text)
  const n = needle.value
  if (!n) return safe
  const re = new RegExp(n.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi')
  return safe.replace(re, (m) => '<mark>' + m + '</mark>')
}

/** http://192.168.x.x 不是安全上下文，clipboard API 不可用，退回 execCommand。 */
function copyText(text: string) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text)
  }
  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.position = 'fixed'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  ta.select()
  try {
    document.execCommand('copy')
  } finally {
    document.body.removeChild(ta)
  }
  return Promise.resolve()
}

async function copyRow(x: Row) {
  const text = fmtFull(x.ts) + ' ' + x.sev.toUpperCase() +
    (x.srcLabel ? ' [' + x.srcLabel + ']' : '') + ' ' + x.msg +
    (x.tb ? '\n' + x.tb : '')
  try {
    await copyText(text)
    copied.value = rowId(x)
    uiLog('info', '已复制一条日志')
    window.setTimeout(() => { copied.value = '' }, 1500)
  } catch (e: any) {
    uiLog('error', '复制失败：' + e.message)
  }
}

function reload() {
  loadLogs()
  uiLog('info', '手动刷新日志')
}

async function downloadLogs() {
  try {
    await download('/api/logs/file', 'service.log')
    uiLog('info', '已下载完整日志')
  } catch (e: any) {
    uiLog('error', '下载日志失败：' + e.message)
  }
}

watch(rows, async () => {
  const el = box.value
  if (!el || !follow.value) return
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40
  await nextTick()
  if (atBottom) el.scrollTop = el.scrollHeight
}, { flush: 'post' })
</script>

<template>
  <div class="page logv">
    <section class="card">
      <div class="sec-head">
        <h2><i>◆</i>运行日志</h2>
        <span class="more">{{ truncNote }}</span>
      </div>

      <!-- 级别计数：点一下就筛，错误永远是第一眼要看的 -->
      <div class="logv-stats">
        <button class="st" :class="{ on: sev === 'error' }" @click="sev = 'error'">
          <b class="bad">{{ counts.error }}</b> 错误
        </button>
        <button class="st" :class="{ on: sev === 'warn' }" @click="sev = 'warn'">
          <b class="gold">{{ counts.warn }}</b> 警告
        </button>
        <span class="st plain"><b>{{ counts.info }}</b> 信息</span>
        <span class="st plain"><b>{{ counts.web }}</b> 网页</span>
        <span class="st plain sep">最近错误 {{ lastError ? fmtFull(lastError) : '无' }}</span>
        <span v-if="stats" class="st plain">持久化 {{ bytes(stats.journal_bytes) }}</span>
      </div>

      <div class="row logv-tools">
        <select v-model="sev" style="width:120px">
          <option v-for="[k, label] in SEVS" :key="k" :value="k">{{ label }}</option>
        </select>
        <input v-model="q" class="logv-q" type="search"
               placeholder="搜索消息 / 文件:行号 / 堆栈…">
        <button class="btn sm" @click="toggleAll">
          {{ stacked ? '全部收起' : '展开所有堆栈' }}
        </button>
        <button class="btn sm" @click="reload">刷新</button>
        <button class="btn sm" @click="downloadLogs">下载完整日志</button>
        <label class="logv-check">
          <input v-model="follow" type="checkbox">自动滚动
        </label>
      </div>

      <div ref="box" class="logv-body">
        <template v-for="x in rows" :key="rowId(x)">
          <div class="logv-row" :class="[x.sev, { clickable: !!x.tb, open: isOpen(x) }]"
               @click="toggle(x)">
            <span class="ts" :title="fmtStampFull(x.ts)">{{ fmtFull(x.ts) }}</span>
            <span class="lv">{{ x.level }}</span>
            <span v-if="x.srcLabel" class="src">{{ x.srcLabel }}</span>
            <span class="txt" v-html="hl(x.msg)"></span>
            <span v-if="(x.n || 1) > 1" class="n">×{{ x.n }}</span>
            <span v-if="x.replay" class="tag">重启前</span>
            <span v-if="x.tb" class="chev">{{ isOpen(x) ? '▾' : '▸' }}</span>
          </div>
          <pre v-if="isOpen(x)" class="logv-tb">{{ x.tb }}</pre>
          <div v-if="isOpen(x)" class="logv-acts">
            <button class="btn sm" @click.stop="copyRow(x)">
              {{ copied === rowId(x) ? '已复制' : '复制这条（含堆栈）' }}
            </button>
          </div>
        </template>
        <div v-if="!rows.length" class="empty">没有符合条件的日志</div>
      </div>
    </section>

    <!-- 诊断：日志是「发生过什么」，这里是「现在健康吗」 -->
    <section v-if="diag" class="card">
      <div class="sec-head">
        <h2><i>◆</i>运行状态</h2>
        <span class="more">
          pid {{ diag.pid }} · Python {{ diag.python }} · {{ diag.platform }} ·
          已运行 {{ fmtDuration(diag.uptime_ms) }}
        </span>
      </div>

      <div class="logv-diag">
        <div class="cell" :class="{ bad: diag.threads.missing.length }">
          <i>线程</i>
          <b>{{ diag.threads.items.length }} 业务 · {{ diag.threads.pool }} 池</b>
          <span v-if="diag.threads.missing.length" class="bad">
            已停止：{{ diag.threads.missing.join('、') }}
          </span>
          <span v-else>
            共 {{ diag.threads.count }} 个<template
              v-if="diag.threads.expected_capture.length"> · 采集线程都在</template>
          </span>
        </div>
        <div class="cell">
          <i>磁盘</i>
          <b>{{ bytes(diag.disk.free) }} 可用</b>
          <span>{{ diag.disk.percent }}% 已用 · 共 {{ bytes(diag.disk.total) }}</span>
        </div>
        <div class="cell">
          <i>数据库</i>
          <b>{{ bytes(diag.db.bytes) }}</b>
          <span>WAL {{ bytes(diag.db.wal_bytes) }}</span>
        </div>
        <div class="cell">
          <i>内存</i>
          <b>{{ diag.process.rss ? 'RSS ' + bytes(diag.process.rss) : 'RSS 不可读' }}</b>
          <span>{{ diag.memory.percent ? '整机 ' + diag.memory.percent + '%' : '整机占用不可读' }}</span>
        </div>
        <div class="cell" :class="{ bad: !diag.ffmpeg.ok }">
          <i>ffmpeg</i>
          <b>{{ diag.ffmpeg.ok ? '可用' : '未找到' }}</b>
          <span class="ell">{{ diag.ffmpeg.version || diag.ffmpeg.path || 'remux 会全部停在 pending' }}</span>
        </div>
        <div class="cell" :class="{ bad: !diag.websockets }">
          <i>websockets</i>
          <b>{{ diag.websockets ? '正常' : '缺失' }}</b>
          <span>{{ diag.websockets ? '可连弹幕服务器' : '采集会直接失败' }}</span>
        </div>
      </div>

      <button class="btn sm" style="margin-top:10px" @click="showThreads = !showThreads">
        {{ showThreads ? '收起线程清单' : '展开线程清单' }}
      </button>
      <div v-if="showThreads" class="logv-threads">
        <span v-for="t in diag.threads.items" :key="t.name" :class="{ stop: !t.alive }">
          {{ t.name }}{{ t.alive ? '' : '（已退出）' }}
        </span>
      </div>
    </section>

    <section class="card">
      <div class="sec-head">
        <h2><i>◆</i>说明</h2>
        <span class="more" style="cursor:pointer" @click="showDiag = !showDiag">
          {{ showDiag ? '收起' : '展开' }}
        </span>
      </div>
      <div v-show="showDiag" class="hint">
        错误与警告会带完整 traceback，点那一条展开；行首是产生日志的 `文件:行号`。
        同一行连续重复只留一条并标 ×N。warn/error 额外落在
        <code>data/logs/journal.jsonl</code>，服务重启后仍会显示（标「重启前」）。
        完整日志按 5MB 轮转，可下载。
      </div>
    </section>
  </div>
</template>
