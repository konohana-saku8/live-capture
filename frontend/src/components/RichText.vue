<script setup lang="ts">
/** 弹幕正文渲染：把 [表情名] 换成本地代理的图片，其余文本转义。 */
import { computed } from 'vue'

import { escapeHtml, isHttpUrl } from '@/lib/format'
import type { FeedRow } from '@/types'

const props = defineProps<{ text?: string; row: FeedRow }>()

/** 同源代理 + 不限来源，失败由 App 的全局 error 监听降级成 [名字]。 */
function emoteImg(url: unknown, px: unknown, alt: string) {
  if (!isHttpUrl(String(url || ''))) return ''
  const h = Math.max(16, Math.min(80, parseInt(String(px), 10) || 20))
  return '<img class="emote" src="' + escapeHtml(url) + '" data-src="' + escapeHtml(url) +
    '" referrerpolicy="no-referrer" style="height:' + h + 'px" alt="' + escapeHtml(alt || '表情') +
    '" loading="lazy" decoding="async">'
}

const html = computed(() => {
  const text = String(props.text == null ? '' : props.text)
  const row = props.row || ({} as FeedRow)

  let map: Record<string, [string, number]> = {}
  try {
    map = JSON.parse(row.emote_map || '{}') || {}
  } catch {
    map = {}
  }
  const keys = Object.keys(map)
  // 整条就是一张大表情
  if (row.is_emoticon && row.emote_url && !keys.length) {
    return emoteImg(row.emote_url, row.emote_px, text || '表情')
  }

  let out = ''
  let last = 0
  const re = /\[([^\[\]\s][^\[\]]{0,31})\]/g
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    out += escapeHtml(text.slice(last, m.index))
    const hit = map[m[1]]
    out += (hit ? emoteImg(hit[0], hit[1], m[1]) : '') || escapeHtml(m[0])
    last = re.lastIndex
  }
  out += escapeHtml(text.slice(last))
  return out
})
</script>

<template>
  <!-- eslint-disable-next-line vue/no-v-html -->
  <span class="rich" v-html="html"></span>
</template>
