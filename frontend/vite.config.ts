import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { existsSync, readFileSync } from 'node:fs'

// Show the backend package version in the UI. APP_VERSION wins; otherwise read
// pyproject.toml when it is present (it is not in every build context, e.g. a
// Docker stage that copies only frontend/), falling back to 'dev'.
function readAppVersion(): string {
  if (process.env.APP_VERSION) return process.env.APP_VERSION
  const pyproject = new URL('../pyproject.toml', import.meta.url)
  if (!existsSync(pyproject)) return 'dev'
  return /^version\s*=\s*"([^"]+)"/m.exec(readFileSync(pyproject, 'utf8'))?.[1] ?? 'dev'
}
const appVersion = readAppVersion()

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

