import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { readFileSync } from 'node:fs'

// Show the backend package version in the UI.
const pyproject = readFileSync(new URL('../pyproject.toml', import.meta.url), 'utf8')
const appVersion = /^version\s*=\s*"([^"]+)"/m.exec(pyproject)?.[1] ?? 'dev'

// https://vite.dev/config/
export default defineConfig({
  define: {
    __APP_VERSION__: JSON.stringify(appVersion),
  },
  plugins: [
    react(),
    tailwindcss()
  ],
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/jobs': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/health': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/webhook': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      }
    }
  }
})

