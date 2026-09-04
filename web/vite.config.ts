import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// A worktree checkout often runs alongside the main checkout, so the dev port is
// taken from the environment when one is supplied and falls back to 5173.
declare const process: { cwd(): string; env: Record<string, string | undefined> }
const devPort = Number(process.env.PORT) || 5173

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const backend = env.MIZAN_API_URL || 'http://127.0.0.1:8000'
  const token = env.MIZAN_API_TOKEN

  return {
    plugins: [react()],
    resolve: {
      // Root-relative alias keeps the config free of Node type dependencies.
      alias: { '@': '/src' },
    },
    server: {
      port: devPort,
      proxy: {
        // The tenant bearer stays server-side. It is never compiled into the
        // browser bundle or exposed through a VITE_* environment variable.
        '/api': {
          target: backend,
          changeOrigin: true,
          rewrite: (p) => p.replace(/^\/api/, ''),
          headers: token ? { authorization: `Bearer ${token}` } : undefined,
        },
      },
    },
  }
})
