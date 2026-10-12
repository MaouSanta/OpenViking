import { createReadStream, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Local review entry: reuses the production DwgPreview without starting ingestion.
// Only the three explicitly selected source files are downloadable.
const manifestPath = resolve(
  process.env.DWG_COMPARISON_MANIFEST || '.dwg-compare.local/manifest.json',
)
type Manifest = {
  drawings: Array<{ source: string; uri: string; sizeBytes: number }>
}
function loadManifest(): Manifest {
  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8')) as Manifest
  return {
    ...manifest,
    drawings: manifest.drawings.map((drawing) => ({
      ...drawing,
      source: resolve(dirname(manifestPath), drawing.source),
    })),
  }
}
export default defineConfig({
  resolve: { alias: { '#': fileURLToPath(new URL('./src', import.meta.url)) } },
  plugins: [
    react(),
    tailwindcss(),
    {
      name: 'local-dwg-comparison',
      configureServer(server) {
        server.middlewares.use((req, res, next) => {
          const url = new URL(req.url || '/', 'http://localhost')
          if (url.pathname === '/') {
            res.writeHead(302, { Location: '/dwg-compare.html' })
            res.end()
            return
          }
          if (
            url.pathname !== '/__dwg_review/manifest' &&
            url.pathname !== '/api/v1/content/download'
          ) {
            next()
            return
          }
          let manifest: Manifest
          try {
            manifest = loadManifest()
          } catch {
            res.writeHead(503, { 'Content-Type': 'text/plain; charset=utf-8' })
            res.end(
              'DWG comparison data is unavailable. Run script/prepare-dwg-comparison.mjs or set DWG_COMPARISON_MANIFEST.',
            )
            return
          }
          if (url.pathname === '/__dwg_review/manifest') {
            res.setHeader('Content-Type', 'application/json; charset=utf-8')
            res.end(
              JSON.stringify({
                ...manifest,
                drawings: manifest.drawings.map(
                  ({ source: _source, ...d }) => d,
                ),
              }),
            )
            return
          }
          const drawing = manifest.drawings.find(
            (d) => d.uri === url.searchParams.get('uri'),
          )
          if (!drawing || (req.method !== 'GET' && req.method !== 'HEAD')) {
            res.writeHead(404)
            res.end()
            return
          }
          res.setHeader('Content-Type', 'application/octet-stream')
          res.setHeader('Content-Length', drawing.sizeBytes)
          res.setHeader('Cache-Control', 'private, max-age=3600')
          if (req.method === 'HEAD') {
            res.end()
            return
          }
          createReadStream(drawing.source).pipe(res)
        })
      },
    },
  ],
  server: { host: '0.0.0.0', port: 3173, strictPort: true },
})
