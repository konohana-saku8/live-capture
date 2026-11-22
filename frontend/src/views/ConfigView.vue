<script setup lang="ts">
/** 配置页：cookie 账号、采集参数、数据清空。 */
import { computed, onBeforeUnmount, reactive, ref, watch, watchEffect } from 'vue'

import { api, postJson, setToken } from '@/api/client'
import { hint, loadAccounts, loadStatus, state, uiLog, uploadAccount, accountAction } from '@/stores/app'
import { withBusy } from '@/stores/busy'
import type { ConfigPayload } from '@/types'
import CdnImg from '@/components/CdnImg.vue'

/** 采集参数与推送参数分成两张表单，各自有保存按钮，互不干扰。 */
const form = reactive({
  host: '0.0.0.0',
  port: 8765,
  auto_start: true,
  protover: 2,
  heartbeat_seconds: 30,
  stale_seconds: 120,
})
const pushForm = reactive({
  notify_live_seconds: 60,
  notify_dyn_seconds: 300,
})

let snapshot = ''
let pushSnapshot = ''
const tokenField = ref('')
const webhookField = ref('')
const secretField = ref('')
const fileInput = ref<HTMLInputElement | null>(null)
const acctName = ref('')
const uploading = ref(false)
/** 两个保存按钮各自的即时反馈：'ok' / 'err' / ''，几秒后自动收回。 */
const savedFlash = ref('')
const pushFlash = ref('')
let flashTimer: number | undefined
let pushFlashTimer: number | undefined

function flashSaved(which: 'cfg' | 'push', kind: string) {
  if (which === 'push') {
    pushFlash.value = kind
    window.clearTimeout(pushFlashTimer)
    pushFlashTimer = window.setTimeout(() => { pushFlash.value = '' }, 2600)
  } else {
    savedFlash.value = kind
    window.clearTimeout(flashTimer)
    flashTimer = window.setTimeout(() => { savedFlash.value = '' }, 2600)
  }
}
onBeforeUnmount(() => {
  window.clearTimeout(flashTimer)
  window.clearTimeout(pushFlashTimer)
})

const dirty = computed(() =>
  (snapshot !== '' && JSON.stringify(form) !== snapshot) ||
  (pushSnapshot !== '' && JSON.stringify(pushForm) !== pushSnapshot) ||
  !!webhookField.value || !!secretField.value)

watchEffect(() => {
  state.dirty.cfg = dirty.value
})

function applyConfig(cfg: ConfigPayload | null) {
  if (!cfg) return
  // 正在这个表单里操作，或已经有没保存的修改时，不要用轮询结果覆盖
  if (dirty.value) return
  form.host = cfg.host || '0.0.0.0'
  form.port = cfg.port || 8765
  form.auto_start = !!cfg.auto_start
  form.protover = cfg.protover || 2
  form.heartbeat_seconds = cfg.heartbeat_seconds || 30
  form.stale_seconds = cfg.stale_seconds || 120
  pushForm.notify_live_seconds = cfg.notify_live_seconds || 60
  pushForm.notify_dyn_seconds = cfg.notify_dyn_seconds || 300
  snapshot = JSON.stringify(form)
  pushSnapshot = JSON.stringify(pushForm)
}

watch(() => state.cfg, applyConfig, { immediate: true })

async function saveConfig() {
  const payload: Record<string, unknown> = {
    host: form.host.trim(),
    port: Number(form.port),
    auto_start: form.auto_start,
    protover: Number(form.protover),
    heartbeat_seconds: Number(form.heartbeat_seconds),
    stale_seconds: Number(form.stale_seconds),
  }
  const t = tokenField.value.trim()
  if (t) payload.token = t
  try {
    const j = await postJson('/api/config', payload)
    hint('cfg', '采集参数已保存' + (j.restart_required ? '；采集运行中，需停止后重新启动才生效' : ''), 'ok')
    if (t) {
      // 同步新口令，避免把自己锁在外面
      setToken(t)
      uiLog('web', '访问口令已更新')
    }
    tokenField.value = ''
    snapshot = JSON.stringify(form)
    flashSaved('cfg', 'ok')
    await loadStatus()
  } catch (e: any) {
    hint('cfg', '保存失败：' + e.message, 'err')
    flashSaved('cfg', 'err')
  }
}

async function savePush() {
  const payload: Record<string, unknown> = {
    notify_live_seconds: Number(pushForm.notify_live_seconds),
    notify_dyn_seconds: Number(pushForm.notify_dyn_seconds),
  }
  const hook = webhookField.value.trim()
  if (hook) payload.feishu_webhook = hook
  const sec = secretField.value.trim()
  if (sec) payload.feishu_secret = sec
  try {
    const j = await postJson('/api/config', payload)
    hint('push', '推送配置已保存' +
      (j.restart_required ? '；采集运行中，需停止后重新启动才生效' : ''), 'ok')
    webhookField.value = ''
    secretField.value = ''
    pushSnapshot = JSON.stringify(pushForm)
    flashSaved('push', 'ok')
    await loadStatus()
  } catch (e: any) {
    hint('push', '保存失败：' + e.message, 'err')
    flashSaved('push', 'err')
  }
}

async function clearToken() {
  try {
    await postJson('/api/config', { token: '' })
    setToken('')
    tokenField.value = ''
    hint('cfg', '已清除访问口令', 'ok')
    await loadStatus()
  } catch (e: any) {
    hint('cfg', e.message, 'err')
  }
}

async function testPush() {
  const hook = webhookField.value.trim() || undefined
  const secret = secretField.value.trim() || undefined
  try {
    await postJson('/api/notify/test', { webhook: hook, secret })
    hint('push', '测试消息已发出，去飞书看看', 'ok')
  } catch (e: any) {
    hint('push', '推送测试失败：' + e.message, 'err')
  }
}

async function clearData() {
  if (!confirm('确定清空全部弹幕 / SC / 事件记录？此操作不可撤销。')) return
  try {
    await withBusy('正在清空弹幕 / SC / 事件…', () =>
      api('/api/clear', { method: 'POST', body: JSON.stringify({ confirm: 'yes' }) }))
    hint('cfg', '数据已清空', 'ok')
  } catch (e: any) {
    hint('cfg', e.message, 'err')
  }
}

async function onUpload() {
  const f = fileInput.value?.files?.[0]
  if (!f) {
    hint('acct', '请先选择一个 cookie CSV 文件', 'err')
    return
  }
  uploading.value = true
  try {
    const text = await f.text()
    const j = await uploadAccount(acctName.value.trim() || f.name, text)
    hint('acct', '已保存 ' + j.item.label + (j.item.error ? '（' + j.item.error + '）' : '，已启用') +
      (j.restart_required ? '；采集运行中，需停止后重新启动才生效' : ''),
      j.item.error ? 'err' : 'ok')
    if (fileInput.value) fileInput.value.value = ''
    acctName.value = ''
  } catch (e: any) {
    hint('acct', '上传失败：' + e.message, 'err')
  } finally {
    uploading.value = false
  }
}

async function onAccountAction(action: 'remove' | 'activate', id: string) {
  if (action === 'remove') {
    const item = state.accounts.find((a) => a.id === id)
    if (!confirm('删除账号「' + (item ? item.label : id) + '」？')) return
  }
  try {
    const j = await withBusy(action === 'remove' ? '正在删除账号…' : '正在启用账号…',
      () => accountAction(action, id))
    hint('acct', action === 'remove' ? '账号已删除' : '账号已启用', 'ok')
    if (j.restart_required && action === 'remove') {
      hint('acct', '账号已删除；采集运行中，需停止后重新启动才生效', 'ok')
    }
  } catch (e: any) {
    hint('acct', (action === 'remove' ? '删除' : '启用') + '失败：' + e.message, 'err')
  }
}

async function disableAccount() {
  try {
    await accountAction('activate', '')
    hint('acct', '已停用账号，之后匿名采集', 'ok')
  } catch (e: any) {
    hint('acct', e.message, 'err')
  }
}

function refreshAccounts() {
  loadAccounts()
}
</script>

<template>
  <div class="page">
    <section class="card">
      <div class="sec-head">
        <h2><i>◆</i>账号</h2>
        <span class="more">只接收 cookie CSV</span>
      </div>
      <div class="row">
        <input ref="fileInput" type="file" accept=".csv,.txt,text/csv" style="flex:1 1 200px">
        <input v-model="acctName" type="text" placeholder="名称（留空用文件名）"
               style="flex:1 1 170px">
        <button class="btn primary" :disabled="uploading" @click="onUpload">上传并启用</button>
      </div>
      <div class="hint">
        和 sc collector 一样：一个 CSV 一个账号，支持 Netscape / Chrome 扩展导出（Tab 分隔）或 name,value 两列。含 SESSDATA 才可用；上传即自动启用。不上传则匿名采集。采集运行中换账号需停止后重新启动。
      </div>

      <div>
        <div v-if="!state.accounts.length" class="hint">
          还没有账号。上传一个 cookie CSV 即可启用登录态；不上传则匿名采集（昵称可能显示成 ***）。
          <button class="btn sm" style="margin-left:8px" @click="refreshAccounts">刷新</button>
        </div>
        <div v-for="a in state.accounts" :key="a.id" class="acct" :class="{ on: a.active }">
          <CdnImg v-if="a.profile?.face" :src="a.profile.face" kind="acct-face" alt="账号头像" />
          <div class="who">
            <b>{{ a.profile?.uname || a.label }}</b>
            <span>
              uid {{ a.uid || '—' }} · {{ a.sessdata_mask || '无 SESSDATA' }}
              · {{ a.cookie_count }} 条 cookie{{ a.file_name ? ' · ' + a.file_name : '' }}
            </span>
          </div>
          <span class="pill" :class="a.error ? 'bad' : 'ok'">
            {{ a.error ? a.error : (a.active ? '已启用' : '可用') }}
          </span>
          <button v-if="!a.active" class="btn sm" @click="onAccountAction('activate', a.id)">启用</button>
          <button class="btn sm danger" @click="onAccountAction('remove', a.id)">删除</button>
        </div>
      </div>

      <div class="row" style="margin-top:10px">
        <button class="btn sm" @click="disableAccount">停用账号（匿名采集）</button>
      </div>
      <div class="hint" :class="state.hints.acct.kind">{{ state.hints.acct.msg }}</div>
    </section>

    <section class="card">
      <div class="sec-head">
        <h2><i>◆</i>采集参数</h2>
        <span class="more">记录弹幕 / SC / 事件等开关已移到采集器页，按 up 主单独配置</span>
      </div>
      <form @submit.prevent="saveConfig">
        <div class="fields">
          <div>
            <label>压缩协议</label>
            <select v-model.number="form.protover">
              <option :value="2">2 · zlib（推荐，无额外依赖）</option>
              <option :value="3">3 · brotli（需装 brotli）</option>
            </select>
          </div>
          <div>
            <label>心跳间隔（秒）</label>
            <input v-model.number="form.heartbeat_seconds" type="number" min="10" max="120">
          </div>
          <div>
            <label>静默重连（秒）</label>
            <input v-model.number="form.stale_seconds" type="number" min="30" max="600">
          </div>
        </div>
        <div class="fields" style="margin-top:16px">
          <div><label>监听地址</label><input v-model="form.host" type="text"></div>
          <div><label>端口</label><input v-model.number="form.port" type="number" min="1" max="65535"></div>
          <div>
            <label>API 访问口令</label>
            <input v-model="tokenField" type="password" autocomplete="off"
                   :placeholder="state.cfg?.token_set ? '已设置 · 留空不修改' : '可选'">
          </div>
        </div>
        <div class="checkline" style="margin-top:14px">
          <label><input v-model="form.auto_start" type="checkbox">启动服务时自动采集所有采集器</label>
        </div>
        <div class="row" style="margin-top:14px">
          <button class="btn primary" :class="savedFlash" type="submit">
            {{ savedFlash === 'ok' ? '✓ 已保存' : savedFlash === 'err' ? '保存失败' : '保存采集参数' }}
          </button>
          <button class="btn" type="button" @click="clearToken">清除访问口令</button>
          <span class="muted">口令留空表示不修改；采集参数改动需停止后重新启动才生效</span>
        </div>
      </form>
      <div class="hint" :class="state.hints.cfg.kind">{{ state.hints.cfg.msg }}</div>
    </section>

    <section class="card">
      <div class="sec-head">
        <h2><i>◆</i>开播 / 动态推送</h2>
        <span class="more">飞书自定义机器人；每位 up 主的开关在采集器页</span>
      </div>
      <form @submit.prevent="savePush">
        <div class="fields">
          <div>
            <label>飞书 webhook</label>
            <input v-model="webhookField" type="password" autocomplete="off"
                   :placeholder="state.cfg?.feishu_webhook_set ? '已设置 · 留空不修改' : 'https://open.feishu.cn/open-apis/bot/v2/hook/…'">
          </div>
          <div>
            <label>签名校验 secret</label>
            <input v-model="secretField" type="password" autocomplete="off"
                   :placeholder="state.cfg?.feishu_secret_set ? '已设置 · 留空不修改' : '机器人没开签名校验就留空'">
          </div>
        </div>
        <div class="fields" style="margin-top:16px">
          <div>
            <label>开播探测间隔（秒）</label>
            <input v-model.number="pushForm.notify_live_seconds" type="number" min="20" max="3600">
          </div>
          <div>
            <label>动态轮询间隔（秒）</label>
            <input v-model.number="pushForm.notify_dyn_seconds" type="number" min="60" max="3600">
          </div>
        </div>
        <div class="row" style="margin-top:14px">
          <button class="btn primary" :class="pushFlash" type="submit">
            {{ pushFlash === 'ok' ? '✓ 已保存' : pushFlash === 'err' ? '保存失败' : '保存推送配置' }}
          </button>
          <button class="btn" type="button" @click="testPush">发一条测试消息</button>
          <span class="muted">webhook 留空表示不修改或不推送；改完点本栏的「保存推送配置」才生效</span>
        </div>
      </form>
      <div class="hint" :class="state.hints.push.kind">{{ state.hints.push.msg }}</div>
    </section>

    <section class="card">
      <div class="sec-head"><h2><i>◆</i>数据管理</h2></div>
      <div class="row">
        <button class="btn danger" @click="clearData">清空全部采集数据</button>
        <span class="muted">需要二次确认；不会删除采集器列表、配置与账号</span>
      </div>
    </section>
  </div>
</template>
