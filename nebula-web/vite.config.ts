import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// During dev, proxy /nebula/* to the FastAPI backend on port 8787
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/nebula': {
        target: 'http://127.0.0.1:8787',
        changeOrigin: true,
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
