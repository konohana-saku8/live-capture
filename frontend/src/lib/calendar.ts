/** 日历索引：按本地日期给直播场次分组，生成月历格子。 */

const pad = (n: number) => ('0' + n).slice(-2)

export const WEEKDAYS = ['日', '一', '二', '三', '四', '五', '六']

export function dayKey(ms?: number) {
  if (!ms) return ''
  const d = new Date(ms)
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
}

export function todayKey() {
  return dayKey(Date.now())
}

export function dayLabel(key: string) {
  if (!key) return ''
  const p = key.split('-').map(Number)
  if (p.length !== 3) return key
  const d = new Date(p[0], p[1] - 1, p[2])
  const label = p[0] + '.' + pad(p[1]) + '.' + pad(p[2]) + ' 周' + WEEKDAYS[d.getDay()]
  return label + (key === todayKey() ? ' · 今天' : '')
}

export function keyParts(key: string) {
  const p = key.split('-').map(Number)
  const now = new Date()
  if (p.length !== 3 || !p[0]) {
    return { year: now.getFullYear(), month: now.getMonth() + 1 }
  }
  return { year: p[0], month: p[1] }
}

export function shiftMonth(year: number, month: number, delta: number) {
  const d = new Date(year, month - 1 + delta, 1)
  return { year: d.getFullYear(), month: d.getMonth() + 1 }
}

export interface CalCell {
  key: string
  day: number
  inMonth: boolean
}

/** 固定 6 行 × 7 列，周日开头，前后补上下月的日期。 */
export function monthCells(year: number, month: number): CalCell[] {
  const first = new Date(year, month - 1, 1)
  const lead = first.getDay()
  const out: CalCell[] = []
  const start = new Date(year, month - 1, 1 - lead)
  for (let i = 0; i < 42; i++) {
    const d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i)
    out.push({
      key: dayKey(d.getTime()),
      day: d.getDate(),
      inMonth: d.getMonth() === month - 1,
    })
  }
  return out
}

export function monthLabel(year: number, month: number) {
  return year + ' 年 ' + pad(month) + ' 月'
}
