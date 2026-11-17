<script setup lang="ts">
/** 仪表盘：设备与服务、采集器卡片、近 12 小时柱状图、高额 SC。 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import { bytes, dur, fmt, fmtFull, num } from '@/lib/format'
import { selectRoom, state } from '@/stores/app'

const router = useRouter()

const timeline = computed(() => state.dash?.timeline || { labels: [], danmaku: [], sc: [], events: [] })

/** 三组柱子共用一条纵轴，峰值至少为 1，避免全 0 时除零。 */
const peak = computed(() => {
  const tl = timeline.value
  let max = 1
  for (const k of ['danmaku', 'sc', 'events'] as const) {
    for (const v of tl[k] || []) if (v > max) max = v
  }
  return max
})

const serviceAddr = computed(() => {
  const svc = state.svc
  return 'http://' + (svc?.lan_ip || '127.0.0.1') + ':' + (svc?.port || 8765)
})

const liveText: Record<number, string> = { 0: '未开播', 1: '直播中', 2: '轮播' }

function barHeight(kind: 'danmaku' | 'sc' | 'events', i: number) {
  const v = (timeline.value[kind] || [])[i] || 0
  return Math.max(2, Math.round((v / peak.value) * 100)) + '%'
}

function roomLabel(id: number) {
  const r = state.rooms.find((x) => x.id === id)
  return r ? (r.label || '房间 ' + id) : '房间 ' + id
}

async function openRoom(id: number, testSource: boolean) {
  await selectRoom(id)
  router.push(testSource ? '/live' : '/rooms')
}
</script>

<template>
  <div class="page">
    <div class="hero">
      <div class="eyebrow">Overview · 仪表盘</div>
      <h1>采集总览<span class="thin">。</span></h1>
      <p class="lead">
        弹幕、Super Chat 与 up 主动态都留在手机本机。切到「采集器」按 up 主 uid 添加，可以同时采集多位。
      </p>
    </div>

    <section class="card">
      <div class="sec-head">
        <h2><i>◆</i>设备与服务</h2>
        <span class="more">
          <span>{{ serviceAddr }}</span>
          <span>{{ state.sys?.ts ? ' · 更新于 ' + fmt(state.sys.ts) : '' }}</span>
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

    <section class="card">
      <div class="sec-head">
        <h2><i>◆</i>采集器</h2>
        <span class="more">{{ state.rooms.length }} 位 up 主 · {{ state.active }} 位正在采集</span>
      </div>
      <div class="room-grid">
        <div v-for="r in state.rooms" :key="r.id" class="room-card" :class="{ on: r.running }">
          <h3>{{ r.uname || r.label }}</h3>
          <div class="sub">
            {{ r.uid === 0 ? '测试源' : 'UID ' + (r.uid || '—') }} · ROOM {{ r.id }}
            · {{ r.uid === 0 ? '本地视频' : (liveText[r.live_status ?? 0] || '未知') }}
          </div>
          <div class="metrics">
            <div><b>{{ num(r.totals?.sessions) }}</b><span>开播场次</span></div>
            <div class="hl"><b>¥{{ num(r.totals?.sc_amount) }}</b><span>累计流水</span></div>
            <div><b>{{ num(r.totals?.interactions) }}</b><span>互动总数</span></div>
            <div><b>{{ num(r.totals?.viewers) }}</b><span>观众数</span></div>
          </div>
          <div class="nums">
            <span>弹幕 <b>{{ num(r.totals?.danmaku) }}</b></span>
            <span>SC <b>{{ num(r.totals?.sc) }}</b></span>
            <span>动态 <b>{{ num(r.dyns) }}</b></span>
          </div>
          <div class="foot">
            <span class="dot" :class="{ on: r.running }">
              <i></i>{{ r.uid === 0 ? '测试源' : (r.running ? '采集中' : '未采集') }}
            </span>
            <button class="btn sm" @click="openRoom(r.id, r.uid === 0)">查看内容 →</button>
          </div>
        </div>
        <div v-if="!state.rooms.length" class="empty">
          还没有采集器。切到「采集器」页填 up 主 uid 即可开始。
        </div>
      </div>
    </section>

    <section class="card">
      <div class="sec-head">
        <h2><i>◆</i>近 12 小时</h2>
        <span class="more">全部采集器合计 · 弹幕 / SC / 事件</span>
      </div>
      <div class="bars">
        <div v-for="(label, i) in timeline.labels" :key="label + i" class="col">
          <div class="bar">
            <i :style="{ height: barHeight('danmaku', i) }"
               :title="'danmaku ' + (timeline.danmaku[i] || 0)"></i>
            <i class="sc" :style="{ height: barHeight('sc', i) }"
               :title="'sc ' + (timeline.sc[i] || 0)"></i>
            <i class="events" :style="{ height: barHeight('events', i) }"
               :title="'events ' + (timeline.events[i] || 0)"></i>
          </div>
          <span>{{ label }}</span>
        </div>
      </div>
    </section>

    <section class="card">
      <div class="sec-head">
        <h2><i>◆</i>高额 SC</h2>
        <span class="more">全部采集器 · 按金额前 5</span>
      </div>
      <div class="recent">
        <div v-for="(row, i) in state.dash?.top_sc || []" :key="i" class="line">
          <span class="t">{{ fmt(row.received_at) }}</span>
          <span class="rk">¥{{ row.price }}</span>
          <span class="rx">
            <b>{{ roomLabel(row.room_id) }}</b> · {{ row.uname || '—' }}：{{ row.message || '' }}
          </span>
        </div>
        <div v-if="!(state.dash?.top_sc || []).length" class="empty">还没有 Super Chat</div>
      </div>
    </section>
  </div>
</template>
