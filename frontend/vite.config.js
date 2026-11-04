import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  server: { proxy: { '/api': 'http://127.0.0.1:8000', '/health': 'http://127.0.0.1:8000', '/ws': { target: 'ws://127.0.0.1:8000', ws: true } } },
})
