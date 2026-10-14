import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { viteSingleFile } from 'vite-plugin-singlefile'

// `npm run build:single` produces one self-contained HTML file (data inlined).
export default defineConfig(({ mode }) => ({
  base: './',
  plugins: mode === 'single' ? [react(), viteSingleFile()] : [react()],
  build: { chunkSizeWarningLimit: 1500, ...(mode === 'single' ? { outDir: 'dist-single' } : {}) },
}))
