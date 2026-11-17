<script setup lang="ts">
/** 配置页：cookie 账号、采集参数、数据清空。 */
import { computed, reactive, ref, watch, watchEffect } from 'vue'

import { api, postJson, setToken } from '@/api/client'
import { hint, loadAccounts, loadStatus, state, uiLog, uploadAccount, accountAction } from '@/stores/app'
import type { ConfigPayload } from '@/types'
import CdnImg from '@/components/CdnImg.vue'

const form = reactive({
  host: '0.0.0.0',
  port: 8765,
  auto_start: true,
  protover: 2,
  heartbeat_seconds: 30,
  stale_seconds: 120,
  record_danmaku: true,
  record_sc: true,
  record_events: true,
  record_guard: true,
  record_gift: true,
  record_red_pocket: true,
  record_enter: false,
  record_interact: true,
})

let snapshot = ''
const tokenField = ref('')
const fileInput = ref<HTMLInputElement | null>(null)
const acctName = ref('')
const uploading = ref(false)

const dirty = computed(() => snapshot !== '' && JSON.stringify(form) !== snapshot)

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
  form.record_danmaku = cfg.record_danmaku !== false
  form.record_sc = cfg.record_sc !== false
  form.record_events = cfg.record_events !== false
  form.record_guard = cfg.record_guard !== false
  form.record_gift = cfg.record_gift !== false
  form.record_red_pocket = cfg.record_red_pocket !== false
  form.record_enter = cfg.record_enter === true
  form.record_interact = cfg.record_interact !== false
  snapshot = JSON.stringify(form)
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
    record_danmaku: form.record_danmaku,
    record_sc: form.record_sc,
    record_events: form.record_events,
    record_guard: form.record_guard,
    record_gift: form.record_gift,
    record_red_pocket: form.record_red_pocket,
    record_enter: form.record_enter,
    record_interact: form.record_interact,
  }
  const t = tokenField.value.trim()
  if (t) payload.token = t
  try {
    const j = await postJson('/api/config', payload)
    hint('cfg', '配置已保存' + (j.restart_required ? '；采集运行中，需停止后重新启动才生效' : ''), 'ok')
    if (t) {
      // 同步新口令，避免把自己锁在外面
      setToken(t)
      uiLog('web', '访问口令已更新')
    }
    tokenField.value = ''
    snapshot = JSON.stringify(form)
    await loadStatus()
  } catch (e: any) {
    hint('cfg', '保存失败：' + e.message, 'err')
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

async function clearData() {
  if (!confirm('确定清空全部弹幕 / SC / 事件记录？此操作不可撤销。')) return
  try {
    await api('/api/clear', { method: 'POST', body: JSON.stringify({ confirm: 'yes' }) })
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
    const j = await accountAction(action, id)
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
        <span class="more">全局默认；采集器页可按 up 主单独覆盖</span>
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
        <div class="checkline" style="margin-top:12px">
          <label><input v-model="form.record_danmaku" type="checkbox">记录弹幕</label>
          <label><input v-model="form.record_sc" type="checkbox">记录 SC</label>
          <label><input v-model="form.record_events" type="checkbox">记录事件</label>
          <label><input v-model="form.record_guard" type="checkbox" :disabled="!form.record_events">上舰</label>
          <label><input v-model="form.record_gift" type="checkbox" :disabled="!form.record_events">礼物 / 投喂</label>
          <label><input v-model="form.record_red_pocket" type="checkbox" :disabled="!form.record_events">红包</label>
          <label title="入场事件量很大，默认关闭">
            <input v-model="form.record_enter" type="checkbox" :disabled="!form.record_events">记录入场（默认关）
          </label>
          <label title="关注 / 分享 / 特别关注 / 互相关注">
            <input v-model="form.record_interact" type="checkbox" :disabled="!form.record_events">其他互动
          </label>
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
          <button class="btn primary" type="submit">保存配置</button>
          <button class="btn" type="button" @click="clearToken">清除访问口令</button>
          <span class="muted">口令留空表示不修改；采集参数改动需停止后重新启动才生效</span>
        </div>
      </form>
      <div class="hint" :class="state.hints.cfg.kind">{{ state.hints.cfg.msg }}</div>
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
