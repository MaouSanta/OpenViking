import { readFileSync } from 'node:fs'
import { defineConfig, loadEnv } from 'vite'
import { devtools } from '@tanstack/devtools-vite'
import tsconfigPaths from 'vite-tsconfig-paths'

import { tanstackRouter } from '@tanstack/router-plugin/vite'

import viteReact from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const config = defineConfig(({ mode }) => {
  const dwgPreviewEnabled =
    loadEnv(mode, process.cwd(), '').VITE_ENABLE_DWG_PREVIEW === 'true'

  return {
    plugins: [
      {
        name: 'dwg-preview-release-gate',
        generateBundle(_options, bundle) {
          if (dwgPreviewEnabled) {
            this.emitFile({
              type: 'asset',
              fileName: 'DWG_WORKERS_NOTICE.md',
              source: readFileSync(
                new URL('./src/assets/dwg-workers/NOTICE.md', import.meta.url),
              ),
            })
            return
          }
          for (const fileName of Object.keys(bundle)) {
            if (
              fileName.includes('libredwg-parser-worker') ||
              fileName.includes('mtext-renderer-worker')
            ) {
              delete bundle[fileName]
            }
          }
        },
      },
      devtools(),
      tsconfigPaths({ projects: ['./tsconfig.json'] }),
      tailwindcss(),
      tanstackRouter({ target: 'react', autoCodeSplitting: true }),
      viteReact(),
    ],
  }
})

export default config
