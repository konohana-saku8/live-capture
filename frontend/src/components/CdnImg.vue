<script setup lang="ts">
/** 带防盗链兜底的图片：直连不带 Referer，失败由 App 的全局监听转 /api/emote。 */
import { isHttpUrl } from '@/lib/format'

defineProps<{
  src?: string
  alt?: string
  kind?: string
  size?: string
}>()
</script>

<template>
  <img v-if="isHttpUrl(src)" :class="kind" :src="src" :data-src="src"
       :alt="alt || ''" :style="size ? 'width:' + size + ';height:' + size : undefined"
       referrerpolicy="no-referrer" loading="lazy" decoding="async">
</template>
