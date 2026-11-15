import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 产物直接落到 web/dist，Flask 的 web_dir 指向这里，不需要再拷一次
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    outDir: '../web/dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 900,
  },
  server: {
    port: 5173,
    proxy: {
      // 开发期直接打手机或本地 Flask，不用改前端代码
      '/api': {
        target: process.env.LCAP_API || 'http://127.0.0.1:8765',
        changeOrigin: true,
      },
    },
  },
})
