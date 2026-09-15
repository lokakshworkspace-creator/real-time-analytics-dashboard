import path from 'node:path'
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const rootDir = path.dirname(fileURLToPath(import.meta.url))

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // Vite loads .env files from the project root by default (frontend/).
  // This project keeps a single .env at the repo root, shared with the
  // backend and docker-compose (see .env.example there) — pointing
  // envDir one level up means VITE_API_BASE_URL only has to be defined
  // in one place instead of duplicated into a second frontend/.env.
  envDir: path.resolve(rootDir, '..'),
})
