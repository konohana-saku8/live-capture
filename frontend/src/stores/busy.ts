/** 全站阻塞操作的全局状态：删除 / 清空这类耗时且不可中断的动作期间盖一层遮罩。 */
import { ref } from 'vue'

/** 非空即有阻塞操作在跑，内容是要显示给用户的动作说明。 */
export const busyText = ref('')

/** 遮罩最短停留时间：接口很快时也让它露一下，避免一闪而过看不出来。 */
const MIN_BUSY_MS = 350

/**
 * 包住一次阻塞操作。
 * 调用方负责在操作前自行确认（confirm），这里只管遮罩的开与关。
 */
export async function withBusy<T>(text: string, fn: () => Promise<T>): Promise<T> {
  busyText.value = text
  const t0 = Date.now()
  try {
    return await fn()
  } finally {
    const rest = MIN_BUSY_MS - (Date.now() - t0)
    if (rest > 0) await new Promise((r) => window.setTimeout(r, rest))
    busyText.value = ''
  }
}
