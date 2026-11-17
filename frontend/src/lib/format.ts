/** 展示层格式化：与旧页面的输出保持一字不差，避免迁移后视觉跳动。 */
import type { Segment } from '@/types'

export const GUARD: Record<number, string> = { 1: '总督', 2: '提督', 3: '舰长' }

export const KIND: Record<string, string> = {
  danmaku: '弹幕', sc: 'SC', events: '事件', guard: '上舰', gift: '礼物',
  red_pocket: '红包', enter: '入场', follow: '关注', share: '分享',
  special_follow: '特别关注', mutual_follow: '互相关注',
  sc_delete: '删 SC', live: '直播状态',
}

const pad = (n: number) => ('0' + n).slice(-2)

export function fmt(ms?: number) {
  if (!ms) return '—'
  const d = new Date(ms)
  return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds())
}

export function fmtHM(ms?: number) {
  if (!ms) return '—'
  const d = new Date(ms)
  return pad(d.getHours()) + ':' + pad(d.getMinutes())
}

export function fmtFull(ms?: number) {
  if (!ms) return '—'
  const d = new Date(ms)
  return pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + fmt(ms)
}

export function mdhm(ms: number) {
  const d = new Date(ms)
  return pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + fmtHM(ms)
}

/** 「场 / 场间」的时间段：同一天只写时分，跨天补日期，未结束写「至今」。 */
export function segRange(s?: Segment) {
  if (!s || !s.start) return '—'
  const same = s.end ? new Date(s.start).toDateString() === new Date(s.end).toDateString() : true
  const a = same ? fmtHM(s.start) : mdhm(s.start)
  const b = s.end ? (same ? fmtHM(s.end) : mdhm(s.end)) : '至今'
  return a + '-' + b
}

export function num(n?: number) {
  return String(n == null ? 0 : n).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

export function dur(ms?: number) {
  if (!ms) return '—'
  const s = Math.floor((Date.now() - ms) / 1000)
  if (s < 0) return '—'
  if (s < 60) return s + ' 秒'
  if (s < 3600) return Math.floor(s / 60) + ' 分'
  return Math.floor(s / 3600) + ' 时 ' + Math.floor((s % 3600) / 60) + ' 分'
}

/** 一段固定时长：1 时 08 分 / 12 分 30 秒。 */
export function fmtDuration(ms?: number) {
  const total = Math.max(0, Math.floor((Number(ms) || 0) / 1000))
  if (!total) return '—'
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h) return h + ' 时 ' + pad(m) + ' 分'
  if (m) return m + ' 分 ' + pad(s) + ' 秒'
  return s + ' 秒'
}

/** 相对播放头的偏移：+12:34，超过一小时 +1:02:03。 */
export function fmtOffset(ms?: number) {
  const total = Math.max(0, Math.floor((Number(ms) || 0) / 1000))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  return '+' + (h ? h + ':' + pad(m) + ':' + pad(s) : pad(m) + ':' + pad(s))
}

/** B 站清晰度编号。 */
export function qnLabel(qn?: number) {
  return ({ 10000: '原画', 400: '1080P', 250: '720P', 150: '480P', 80: '360P' } as Record<number, string>)[Number(qn) || 0] || '未知'
}

export function bytes(n?: number) {
  let v = Number(n) || 0
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return (i ? v.toFixed(1) : v.toFixed(0)) + ' ' + units[i]
}

export function escapeHtml(s: unknown) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c] as string))
}

/** B 站 CDN 会拒绝本地 Referer，统一不允许带来源。 */
export function isHttpUrl(url?: string) {
  return /^(https?:\/\/|\/\/)/i.test(String(url || ''))
}

export function dynDayKey(ts?: number) {
  if (!ts) return '未知日期'
  const d = new Date(ts * 1000)
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
}

export function dynDayLabel(key: string) {
  if (key === '未知日期') return key
  const p = key.split('-').map(Number)
  const d = new Date(p[0], p[1] - 1, p[2])
  const wd = ['日', '一', '二', '三', '四', '五', '六'][d.getDay()]
  const today = dynDayKey(Math.floor(Date.now() / 1000))
  return key.replace(/-/g, '.') + ' 周' + wd + (key === today ? ' · 今天' : '')
}

export function segmentOf(list: Segment[], key: string) {
  return list.find((s) => s.kind + '-' + s.index === String(key)) || null
}

export function segKeyOf(s: Segment) {
  return s.kind + '-' + s.index
}

/** 场次标题：直播是「第 N 场」，未开播前是「首次开播前」，其余是「场间 N」。 */
export function segTitle(s: Segment) {
  if (s.kind === 'live') return '第 ' + s.index + ' 场'
  if (s.single) return '未记录开播 · 全部内容'
  return s.lead ? '首次开播前' : '场间 ' + s.index
}

export function segBadge(s: Segment) {
  if (s.kind === 'live') return '场'
  if (s.single) return '未分段'
  return s.lead ? '更早' : '场间'
}
