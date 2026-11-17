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

createApp(App).use(router).mount('#app')
