import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'

import App from './App.vue'
import './styles/legacy.css'
import './styles/app.css'

import HomeView from './views/HomeView.vue'
import RoomsView from './views/RoomsView.vue'
import ConfigView from './views/ConfigView.vue'
import LogsView from './views/LogsView.vue'
import LiveRoomView from './views/LiveRoomView.vue'
import { uiLog } from './stores/app'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'home', component: HomeView },
    { path: '/rooms', name: 'rooms', component: RoomsView },
    { path: '/live', name: 'live', component: LiveRoomView },
    { path: '/config', name: 'config', component: ConfigView },
    { path: '/logs', name: 'logs', component: LogsView },
    // 旧地址与手输路径都回仪表盘
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

// 前端异常也进日志页：否则页面白屏时只剩一条「已登录」，看不出哪儿炸了
window.addEventListener('error', (e) => {
  const file = String(e.filename || '').split('/').pop() || '?'
  uiLog('error', '前端错误：' + (e.message || '未知') + ' @ ' + file + ':' + e.lineno)
})
window.addEventListener('unhandledrejection', (e) => {
  const reason: any = e.reason
  uiLog('error', '未处理的 Promise：' + (reason?.message || String(reason)))
})

const app = createApp(App)
app.config.errorHandler = (err, _instance, info) => {
  const e = err as Error
  uiLog('error', 'Vue 运行错误（' + info + '）：' + (e?.message || e) +
    (e?.stack ? '\n' + e.stack : ''))
}
app.use(router).mount('#app')
