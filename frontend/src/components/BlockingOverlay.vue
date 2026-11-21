<script setup lang="ts">
/** 全屏阻塞遮罩：删除这类耗时且不可中断的操作期间盖住整页，挡住其它点击与滚动。 */
defineProps<{ show: boolean; text?: string }>()
</script>

<template>
  <Transition name="blk">
    <div v-if="show" class="blk" aria-busy="true" aria-live="assertive"
         @click.stop.prevent @contextmenu.stop.prevent>
      <div class="blk-box">
        <span class="blk-spin"></span>
        <span class="blk-text">{{ text || '处理中…' }}</span>
        <span class="blk-sub">请稍等，不要关闭页面</span>
      </div>
    </div>
  </Transition>
</template>
