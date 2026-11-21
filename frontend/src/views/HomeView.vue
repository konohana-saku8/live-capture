<script setup lang="ts">
/** 首页只描述数据状态：存了多少场、每场多少、占了多大空间。 */
import { computed, ref } from 'vue'

import { bytes, dur, fmtFull, num } from '@/lib/format'
import { state } from '@/stores/app'

const ROOMS_PER_PAGE = 8
const roomPage = ref(0)

const sessions = computed(() => state.dash?.data_status?.sessions || null)
const storage = computed(() => state.dash?.data_status?.storage || null)

const roomPages = computed(() =>
  Math.max(1, Math.ceil((sessions.value?.rooms.length || 0) / ROOMS_PER_PAGE)))
const pageRooms = computed(() => {
  const all = sessions.value?.rooms || []
  return all.slice(roomPage.value * ROOMS_PER_PAGE,
    (roomPage.value + 1) * ROOMS_PER_PAGE)
})

const serviceAddr = computed(() => {
  const svc = state.svc
  return 'http://' + (svc?.lan_ip || '127.0.0.1') + ':' + (svc?.port || 8765)
})

function pct(part: number, total: number) {
  return total ? (part / total * 100).toFixed(1) + '%' : '—'
}

function sourceText(src: string) {
  return ({ live: '实时采集', import: '历史导入', 'live+import': '实时 + 导入' } as Record<string, string>)[src] || '无数据'
}

const PIE_COLORS: Record<string, string> = {
  mp4: '#e0a45e', flv: '#c9772f', sqlite: '#6b9bd1', other: '#7d7770',
}

/** 纯 SVG 圆环，不引入图表库；极小切片至少给 1px 以免看不见。 */
const donut = computed(() => {
  const list = storage.value?.pie || []
  const total = storage.value?.total || 0
  const circumference = 2 * Math.PI * 46
  let used = 0
  return list.map((s) => {
    const len = total ? Math.max(1, s.bytes / total * circumference) : 0
    const item = { ...s, color: PIE_COLORS[s.key] || '#7d7770',
      dash: len + ' ' + (circumference - len), offset: -used }
    used += len
    return item
  })
})

const sqliteBytes = computed(() =>
  (storage.value?.pie || []).find((x) => x.key === 'sqlite')?.bytes || 0)
const sqliteMax = computed(() =>
  Math.max(1, ...(storage.value?.sqlite_detail || []).map((x) => x.bytes)))
</script>

<template>
  <div class="page">
    <div class="hero">
      <div class="eyebrow">Overview · 数据状态</div>
      <h1>数据状态<span class="thin">。</span></h1>
      <p class="lead">
        弹幕、Super Chat、动态与录像都留在手机本机。这里只记录存了多少、每场多少、占了多大空间。
      </p>
    </div>

    <section class="card">
      <div class="sec-head">
        <h2><i>◆</i>数据家底</h2>
        <span class="more">全部房间 · 只统计场次区间内的数据</span>
      </div>
      <template v-if="sessions">
        <div class="stat-strip">
          <div><b>{{ num(sessions.total_shows) }}</b><span>已记录场次</span></div>
          <div><b>{{ num(sessions.total_records) }}</b><span>场内记录</span></div>
          <div><b>{{ num(sessions.avg) }}</b><span>平均每场</span></div>
        </div>
        <p v-if="sessions.unsegmented || sessions.gap" class="stat-note">
          另有 {{ num(sessions.unsegmented) }} 条未分段、{{ num(sessions.gap) }} 条落在场间，未计入场次平均。
        </p>
        <div class="data-table-wrap">
          <table class="data-table">
            <thead>
              <tr><th>房间</th><th>场数</th><th>场内记录</th><th>平均每场</th><th>数据来源</th></tr>
            </thead>
            <tbody>
              <tr v-for="r in pageRooms" :key="r.room_id">
                <td>
                  <b>{{ r.label }}</b>
                  <small>#{{ r.room_id }} · <span class="src-mobile">{{ sourceText(r.source) }}</span></small>
                </td>
                <td>
                  {{ num(r.shows) }}
                  <small v-if="r.unsegmented">+ 未分段 {{ num(r.unsegmented) }}</small>
                </td>
                <td>{{ num(r.records) }}</td>
                <td>{{ r.shows ? num(r.avg) : '—' }}</td>
                <td><span class="src-tag" :class="r.source">{{ sourceText(r.source) }}</span></td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="roomPages > 1" class="pager">
          <button class="btn sm" :disabled="roomPage <= 0" @click="roomPage--">上一页</button>
          <span>{{ roomPage + 1 }} / {{ roomPages }}</span>
          <button class="btn sm" :disabled="roomPage >= roomPages - 1" @click="roomPage++">下一页</button>
        </div>
      </template>
      <div v-else class="empty">还没有采集到数据。</div>
    </section>

    <section class="card">
      <div class="sec-head">
        <h2><i>◆</i>存储构成</h2>
        <span class="more">
          合计 {{ bytes(storage?.total) }} · 视频 {{ pct(storage?.video || 0, storage?.total || 0) }}
        </span>
      </div>
      <template v-if="storage">
        <div class="storage-grid">
          <div class="donut-box">
            <svg viewBox="0 0 120 120" class="donut" role="img" aria-label="数据目录存储占用饼图">
              <circle v-for="s in donut" :key="s.key" cx="60" cy="60" r="46"
                fill="none" :stroke="s.color" stroke-width="13"
                :stroke-dasharray="s.dash" :stroke-dashoffset="s.offset"
                transform="rotate(-90 60 60)" />
            </svg>
            <div class="donut-center"><b>{{ bytes(storage.total) }}</b><span>data/</span></div>
          </div>
          <div class="storage-legend">
            <div v-for="s in storage.pie" :key="s.key" class="legend-row">
              <i :style="{ background: PIE_COLORS[s.key] || '#7d7770' }"></i>
              <span>{{ s.label }}</span>
              <b>{{ bytes(s.bytes) }}</b>
              <em>{{ pct(s.bytes, storage.total) }}</em>
            </div>
          </div>
        </div>
        <div class="storage-detail">
          <div class="detail-col">
            <h3>SQLite 内部 · {{ bytes(sqliteBytes) }}</h3>
            <div v-for="d in storage.sqlite_detail" :key="d.label" class="detail-row">
              <div class="detail-top"><span>{{ d.label }}</span><b>{{ bytes(d.bytes) }}</b></div>
              <div class="detail-bar"><i :style="{ width: Math.max(2, d.bytes / sqliteMax * 100) + '%' }"></i></div>
            </div>
          </div>
          <div class="detail-col">
            <h3>占用排行</h3>
            <div v-for="(r, i) in storage.ranking.slice(0, 6)" :key="r.key" class="rank-row">
              <em>{{ i + 1 }}</em><span>{{ r.label }}</span><b>{{ bytes(r.bytes) }}</b>
            </div>
          </div>
        </div>
      </template>
      <div v-else class="empty">还没有存储信息。</div>
    </section>

    <section class="card">
      <div class="sec-head">
        <h2><i>◆</i>设备与服务</h2>
        <span class="more">
          <span>{{ serviceAddr }}</span>
          <span>{{ state.sys?.ts ? ' · 更新于 ' + fmtFull(state.sys.ts) : '' }}</span>
        </span>
      </div>
      <div class="dev-grid">
        <div class="dev">
          <div class="dev-h">
            <span>CPU 占用</span>
            <b>{{ (state.sys?.cpu?.percent ?? 0) >= 10
              ? (state.sys?.cpu?.percent ?? 0).toFixed(0)
              : (state.sys?.cpu?.percent ?? 0).toFixed(1) }}%</b>
          </div>
          <div class="bar">
            <i :class="(state.sys?.cpu?.percent ?? 0) >= 90 ? 'bad'
              : (state.sys?.cpu?.percent ?? 0) >= 70 ? 'warn' : ''"
               :style="{ width: Math.min(100, state.sys?.cpu?.percent ?? 0) + '%' }"></i>
          </div>
          <div class="dev-sub">
            {{ state.sys?.cpu?.scope === 'process'
              ? '本服务占用 · ' + (state.sys?.cpu?.cores || 0) + ' 核机器'
              : (state.sys?.cpu?.cores || 0) + ' 核 · 负载 ' +
                (state.sys?.load?.['1'] || 0) + ' / ' + (state.sys?.load?.['5'] || 0) +
                ' / ' + (state.sys?.load?.['15'] || 0) }}
          </div>
        </div>

        <div class="dev">
          <div class="dev-h">
            <span>内存占用</span>
            <b>{{ (state.sys?.memory?.percent ?? 0).toFixed(0) }}%</b>
          </div>
          <div class="bar">
            <i :class="(state.sys?.memory?.percent ?? 0) >= 90 ? 'bad'
              : (state.sys?.memory?.percent ?? 0) >= 70 ? 'warn' : ''"
               :style="{ width: Math.min(100, state.sys?.memory?.percent ?? 0) + '%' }"></i>
          </div>
          <div class="dev-sub">
            {{ state.sys?.memory?.total
              ? bytes(state.sys.memory.used) + ' / ' + bytes(state.sys.memory.total)
              : '此系统不提供内存信息' }}
          </div>
        </div>

        <div class="dev">
          <div class="dev-h">
            <span>存储占用</span>
            <b>{{ (state.sys?.disk?.percent ?? 0).toFixed(0) }}%</b>
          </div>
          <div class="bar">
            <i :class="(state.sys?.disk?.percent ?? 0) >= 90 ? 'bad'
              : (state.sys?.disk?.percent ?? 0) >= 70 ? 'warn' : ''"
               :style="{ width: Math.min(100, state.sys?.disk?.percent ?? 0) + '%' }"></i>
          </div>
          <div class="dev-sub">
            {{ bytes(state.sys?.disk?.used) }} / {{ bytes(state.sys?.disk?.total) }}
            （剩 {{ bytes(state.sys?.disk?.free) }}）
          </div>
        </div>

        <div class="dev">
          <div class="dev-h">
            <span>网络</span>
            <b style="font-size:12px">{{ state.sys?.network?.lan_ip || '—' }}</b>
          </div>
          <div class="dot-live" :class="{ on: state.sys?.network?.online }">
            <i></i>{{ state.sys?.network?.online ? '在线' : '离线' }}
          </div>
          <div class="dev-sub">
            数据库 {{ bytes(state.sys?.db_size || 0) }}
            {{ state.sys?.uptime ? ' · 开机 ' + dur(Date.now() - state.sys.uptime * 1000) : '' }}
          </div>
        </div>
      </div>

      <div class="kv-grid wide">
        <div class="kv"><span>局域网</span><b>{{ serviceAddr }}</b></div>
        <div class="kv">
          <span>监听</span><b>{{ (state.svc?.host || '0.0.0.0') + ':' + (state.svc?.port || 8765) }}</b>
        </div>
        <div class="kv"><span>服务已运行</span><b>{{ dur(state.svc?.started_at) }}</b></div>
        <div class="kv">
          <span>websockets</span><b>{{ state.svc?.websockets ? '已安装' : '缺失' }}</b>
        </div>
        <div class="kv">
          <span>最后收包</span>
          <b>{{ state.dash?.last_received_at ? fmtFull(state.dash.last_received_at) : '—' }}</b>
        </div>
        <div class="kv"><span>访问口令</span><b>{{ state.cfg?.token_set ? '已设置' : '无' }}</b></div>
        <div class="kv full"><span>数据目录</span><b>{{ state.svc?.data_dir || '—' }}</b></div>
      </div>
    </section>
  </div>
</template>
