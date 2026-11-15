<script setup lang="ts">
/** 应用外壳：进站门禁、顶部导航、全局图片兜底、4 秒轮询。 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api, getToken, onUnauthorized, setToken } from '@/api/client'
import {
  loadAccounts, loadDashboard, loadLogs, loadRooms, loadSessions,
  loadStatus, loadSystem, state, uiLog,
} from '@/stores/app'
import CdnImg from '@/components/CdnImg.vue'

const route = useRoute()
const router = useRouter()

const gatePass = ref('')
const gatePass2 = ref('')
const gateErr = ref('')
let timer: number | undefined

const PAGES = [
  { name: 'home', label: '仪表盘', path: '/' },
  { name: 'rooms', label: '采集器', path: '/rooms' },
  { name: 'config', label: '配置', path: '/config' },
  { name: 'logs', label: '日志', path: '/logs' },
]

const activeAccount = computed(() => state.accounts.find((a) => a.active) || null)
const serviceAddr = computed(() => {
  const svc = state.svc
  return 'http://' + (svc?.lan_ip || '127.0.0.1') + ':' + (svc?.port || 8765)
})
const runLabel = computed(() => (state.running ? '采集中 ' + (state.active || 0) : '未启动'))

/* ---------------- 门禁 ---------------- */

async function bootGate() {
  // 验证期间先隐藏 header / main：本地口令有效就直进，
  // 不再把密码弹窗「先闪一下」再关掉。
  document.body.classList.add('gated')
  const saved = getToken()
  if (saved) {
    try {
      const vr = await fetch('/api/status', { headers: { 'X-Access-Token': saved } })
      if (vr.ok && (await vr.json()).ok) {
        unlockApp(saved)
        return
      }
      setToken('') // 口令失效，清掉重新来
    } catch { /* 网络失败继续探测门禁 */ }
  }
  let need = false
  try {
    const r = await fetch('/api/gate')
    need = !!(await r.json()).need
  } catch {
    need = true // 探测失败按「需要密码」处理，避免裸奔
  }
  state.gate.mode = need ? 'unlock' : 'setup'
  state.gate.open = true
  gatePass.value = ''
  gatePass2.value = ''
  gateErr.value = ''
  setTimeout(() => {
    ;(document.getElementById('gatePass') as HTMLInputElement | null)?.focus()
  }, 60)
}

async function submitGate() {
  const p = gatePass.value.trim()
  gateErr.value = ''
  if (!p) {
    gateErr.value = '请输入密码'
    return
  }
  if (state.gate.mode === 'setup') {
    if (p !== gatePass2.value.trim()) {
      gateErr.value = '两次输入不一致'
      return
    }
    try {
      const res = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: p }),
      })
      const j = await res.json().catch(() => ({}))
      if (!res.ok || !j.ok) throw new Error(j.error || '设置失败 ' + res.status)
    } catch (e: any) {
      gateErr.value = '设置失败：' + e.message
      return
    }
    unlockApp(p)
    return
  }
  try {
    const res = await fetch('/api/status', { headers: { 'X-Access-Token': p } })
    const j = await res.json().catch(() => ({}))
    if (!res.ok || !j.ok) throw new Error('密码错误')
  } catch (e: any) {
    gateErr.value = e.message === '密码错误' ? '密码错误，请重试' : '验证失败：' + e.message
    return
  }
  unlockApp(p)
}

function unlockApp(token: string) {
  setToken(token)
  state.gate.open = false
  state.ready = true
  document.body.classList.remove('gated')
  uiLog('info', '访问验证通过')
  loadAccounts()
  loadStatus()
  ensureData(String(route.name || 'home'))
  startPolling()
}

/** 口令失效（配置页里改过）→ 回门禁。 */
onUnauthorized(() => {
  state.ready = false
  bootGate()
})

/* ---------------- 取数调度 ---------------- */

function ensureData(name: string) {
  if (name === 'home') {
    loadDashboard()
    loadSystem()
  } else if (name === 'rooms') {
    loadRooms().then(() => loadSessions())
  } else if (name === 'config') {
    loadStatus()
    loadAccounts()
  } else if (name === 'logs') {
    loadLogs()
  }
}

function startPolling() {
  if (timer) return
  timer = window.setInterval(() => {
    if (document.hidden || !state.ready) return
    const name = String(route.name || 'home')
    if (name === 'home') {
      loadDashboard()
      loadSystem()
    } else if (name === 'rooms') {
      loadRooms()
      // 还在播的那一场只把新到的并进来，避免每 4 秒重画整段
      if (state.subTab === 'live' && state.autoRefresh) loadSessions({ tail: true })
    } else if (name === 'config') {
      loadStatus()
      loadAccounts()
    } else if (name === 'logs') {
      loadLogs()
    }
  }, 4000)
}

function goTo(path: string) {
  if (route.path === path) return
  if (state.dirty.dyn && route.name === 'rooms' &&
      !confirm('译文还没保存，离开采集器会丢掉这次修改，继续吗？')) return
  if (state.dirty.cfg && route.name === 'config' &&
      !confirm('采集参数还有未保存的修改，离开就会丢掉，继续吗？')) return
  router.push(path)
}

/** 清除本地记住的访问码：回门禁重新输入。 */
function clearToken() {
  setToken('')
  state.ready = false
  uiLog('web', '已清除本地访问码')
  bootGate()
}

/* ---------------- 图片兜底 ---------------- */

/* CDN 拒绝带本地 Referer 的请求：先直连（不带 Referer），失败转同源代理，
   再失败就把表情降级成 [名字]、头像隐藏。error 不冒泡，必须捕获阶段监听。 */
function onImgError(e: Event) {
  const t = e.target as HTMLImageElement | null
  if (!t || t.tagName !== 'IMG') return
  const raw = t.getAttribute('data-src')
  if (!raw) return
  if (!t.dataset.fallback) {
    t.dataset.fallback = '1'
    t.src = '/api/emote?u=' + encodeURIComponent(raw)
    return
  }
  if (t.classList.contains('emote')) {
    const span = document.createElement('span')
    span.className = 'emote-fallback'
    span.textContent = '[' + String(t.getAttribute('alt') || '表情') + ']'
    span.title = '表情图片加载失败'
    t.parentNode?.replaceChild(span, t)
  } else {
    t.style.visibility = 'hidden'
  }
}

function onBeforeUnload(e: BeforeUnloadEvent) {
  if (state.dirty.dyn || state.dirty.cfg) {
    e.preventDefault()
    e.returnValue = ''
  }
}

function onVisible() {
  if (document.hidden || !state.ready) return
  loadStatus()
  loadAccounts()
  ensureData(String(route.name || 'home'))
}

watch(() => route.name, (name) => {
  if (state.ready && name) ensureData(String(name))
})

onMounted(() => {
  document.addEventListener('error', onImgError, true)
  document.addEventListener('visibilitychange', onVisible)
  window.addEventListener('beforeunload', onBeforeUnload)
  bootGate()
})

onBeforeUnmount(() => {
  document.removeEventListener('error', onImgError, true)
  document.removeEventListener('visibilitychange', onVisible)
  window.removeEventListener('beforeunload', onBeforeUnload)
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <!-- 进站门禁 -->
  <div v-if="state.gate.open" class="gate">
    <div class="gate-card">
      <div class="gmark">採</div>
      <h1>{{ state.gate.mode === 'unlock' ? '访问验证' : '首次设置' }}</h1>
      <p class="gsub">
        {{ state.gate.mode === 'unlock'
          ? '请输入访问密码后继续'
          : '本服务尚未设置访问密码，请先设置一个，之后进站都需输入。' }}
      </p>
      <form @submit.prevent="submitGate">
        <input id="gatePass" v-model="gatePass" type="password" placeholder="访问密码"
               autocomplete="current-password">
        <input v-if="state.gate.mode === 'setup'" v-model="gatePass2" type="password"
               placeholder="再次输入密码" autocomplete="new-password">
        <div class="gerr">{{ gateErr }}</div>
        <button class="btn primary" type="submit">
          {{ state.gate.mode === 'unlock' ? '进入' : '设置并进入' }}
        </button>
      </form>
      <p class="ghint">
        {{ state.gate.mode === 'unlock'
          ? '忘记密码可在手机端 config.json 清除 token 字段后重新设置。'
          : '密码保存在本机服务配置中；清除后下次进站会重新要求设置。' }}
      </p>
    </div>
  </div>

  <header class="top">
    <div class="brand">
      <div class="brand-mark">採</div>
      <div class="brand-text"><strong>直播采集</strong><em>LIVE CAPTURE</em></div>
    </div>
    <nav class="nav">
      <button v-for="p in PAGES" :key="p.name" :class="{ on: route.name === p.name }"
              @click="goTo(p.path)">{{ p.label }}</button>
    </nav>
    <div class="top-right">
      <button type="button" class="acct-chip" :class="{ anon: !activeAccount }"
              :title="activeAccount ? activeAccount.label + ' · 点击进入配置' : '匿名采集'"
              @click="goTo('/config')">
        <template v-if="activeAccount">
          <CdnImg :src="activeAccount.profile?.face" alt="头像" size="26px" />
          <b>{{ activeAccount.profile?.uname || activeAccount.label }}</b>
        </template>
        <template v-else>匿名采集</template>
      </button>
      <span class="badge" :class="state.running ? 'on' : 'off'">
        <i></i><span>{{ runLabel }}</span>
      </span>
      <button v-if="getToken()" type="button" class="btn sm" title="清除本地记住的访问码，回门禁重新输入"
              @click="clearToken">清除访问码</button>
    </div>
    <span class="navinfo">
      {{ serviceAddr }} · {{ state.svc?.websockets === false ? '缺依赖' : '服务正常' }}
    </span>
  </header>

  <main v-if="state.ready">
    <router-view v-slot="{ Component }">
      <component :is="Component" />
    </router-view>
    <footer>LIVE CAPTURE · 数据仅保存在本机 · 弹幕 / SC / 事件</footer>
  </main>
</template>
