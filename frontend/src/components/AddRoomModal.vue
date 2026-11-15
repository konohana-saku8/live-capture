<script setup lang="ts">
/** 添加采集器：填 up 主 uid，服务端自动解析直播间号 / 昵称 / 头像。 */
import { nextTick, ref, watch } from 'vue'

import { addRoom, hint, state } from '@/stores/app'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const uid = ref('')
const label = ref('')
const busy = ref(false)
const uidInput = ref<HTMLInputElement | null>(null)

watch(() => props.open, async (open) => {
  if (!open) return
  uid.value = ''
  label.value = ''
  hint('add', '')
  await nextTick()
  uidInput.value?.focus()
})

async function submit() {
  const id = parseInt(uid.value, 10)
  if (!id || id < 1) {
    hint('add', '请填写有效的 up 主 uid', 'err')
    return
  }
  if (busy.value) return
  busy.value = true
  hint('add', '正在按 uid 读取 up 主与直播间信息…')
  try {
    const j = await addRoom(id, label.value.trim())
    hint('room', '已添加：' + (j.room.uname || j.room.label) + '（房间 ' + j.room.id + '）' +
      (j.warning ? '（' + j.warning + '）' : ''), 'ok')
    emit('close')
  } catch (e: any) {
    hint('add', '添加失败：' + e.message, 'err')
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div v-if="open" class="modal" @click.self="emit('close')">
    <div class="card box">
      <div class="sec-head">
        <h2><i>◆</i>添加采集器</h2>
        <button class="btn sm" @click="emit('close')">✕ 关闭</button>
      </div>
      <label>up 主 uid</label>
      <input ref="uidInput" v-model="uid" type="number" min="1"
             placeholder="如 2094031249" autocomplete="off"
             @keydown.enter="submit">
      <div class="hint" style="margin-top:6px">
        填 uid 会自动解析直播间号、昵称、头像与直播状态。
      </div>
      <div style="height:10px"></div>
      <label>备注名（可选）</label>
      <input v-model="label" type="text" placeholder="留空用 up 主昵称"
             autocomplete="off" @keydown.enter="submit">
      <div class="row" style="margin-top:14px">
        <button class="btn primary" :disabled="busy" @click="submit">添加</button>
        <button class="btn" @click="emit('close')">取消</button>
      </div>
      <div class="hint" :class="state.hints.add.kind">{{ state.hints.add.msg }}</div>
    </div>
  </div>
</template>
