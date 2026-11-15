<script setup lang="ts">
/** 采集器页：清单（每页 3 个）+ 详情面板 + 添加对话框。 */
import { ref } from 'vue'

import {
  currentRoom, hint, refreshRooms, removeRoom, roomPages, selectRoom,
  state, stopAll, visibleRooms,
} from '@/stores/app'
import type { Room } from '@/types'
import AddRoomModal from '@/components/AddRoomModal.vue'
import RoomCollectorItem from '@/components/RoomCollectorItem.vue'
import RoomPanel from '@/components/RoomPanel.vue'

const showAdd = ref(false)
const busy = ref(false)

const anyRunning = () => state.rooms.some((r) => r.running)

async function onSelect(room: Room) {
  if (room.id === state.roomId) return
  if (state.dirty.dyn && !confirm('译文还没保存，切换采集器会丢掉这次修改，继续吗？')) return
  await selectRoom(room.id)
}

async function onRemove(room: Room) {
  if (!confirm('删除采集器「' + (room.uname || room.label) + '」？\n已采集的历史数据会保留，可随时重新添加。')) return
  try {
    await removeRoom(room)
    hint('room', '已删除「' + (room.uname || room.label) + '」（数据保留）', 'ok')
  } catch (e: any) {
    hint('room', '删除失败：' + e.message, 'err')
  }
}

async function onRefresh() {
  if (!state.rooms.length) return
  hint('room', '正在刷新直播间信息…')
  try {
    const j = await refreshRooms()
    hint('room', '已刷新 ' + j.updated + ' 个采集器', 'ok')
  } catch (e: any) {
    hint('room', '刷新失败：' + e.message, 'err')
  }
}

async function onStopAll() {
  if (!anyRunning()) return
  busy.value = true
  try {
    const j = await stopAll()
    hint('room', j.message, 'ok')
  } catch (e: any) {
    hint('room', '停止失败：' + e.message, 'err')
  } finally {
    busy.value = false
  }
}

function turnPage(n: number) {
  state.roomPage = Math.max(0, Math.min(roomPages.value - 1, n))
}
</script>

<template>
  <div class="page">
    <section class="card">
      <div class="sec-head">
        <h2><i>◆</i>采集目标</h2>
        <div class="tab-tools">
          <span class="more">绿点＝采集中 · 红点＝未采集</span>
          <button class="btn sm" :disabled="!state.rooms.length" @click="onRefresh">刷新信息</button>
          <button class="btn sm" :disabled="!anyRunning() || busy" @click="onStopAll">全部停止</button>
        </div>
      </div>

      <div class="collector-list">
        <RoomCollectorItem v-for="r in visibleRooms" :key="r.id" :room="r"
                           :selected="r.id === state.roomId"
                           @select="onSelect(r)" @remove="onRemove(r)" />
        <div v-if="!state.rooms.length" class="empty">
          还没有采集器。点下方「＋ 添加采集器」填 up 主 uid，会自动带出直播间信息。
        </div>
      </div>

      <div v-if="roomPages > 1" class="room-pager">
        <button class="btn sm" :disabled="state.roomPage <= 0"
                @click="turnPage(state.roomPage - 1)">‹ 上一页</button>
        <span class="pg">
          第 <b>{{ state.roomPage + 1 }}</b> / {{ roomPages }} 页 ·
          第 {{ state.roomPage * 3 + 1 }}–{{ Math.min(state.rooms.length, state.roomPage * 3 + 3) }} 个
          / 共 {{ state.rooms.length }} 个
        </span>
        <button class="btn sm" :disabled="state.roomPage >= roomPages - 1"
                @click="turnPage(state.roomPage + 1)">下一页 ›</button>
      </div>

      <div class="collector-add">
        <button class="btn sm" @click="showAdd = true">＋ 添加采集器</button>
        <span class="muted">填 up 主 uid，自动解析直播间号、昵称与头像</span>
      </div>
      <div class="hint" :class="state.hints.room.kind">{{ state.hints.room.msg }}</div>
    </section>

    <RoomPanel v-if="currentRoom" :room="currentRoom" />

    <AddRoomModal :open="showAdd" @close="showAdd = false" />
  </div>
</template>
