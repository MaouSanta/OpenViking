import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { AlertTriangle, Loader2, Scan } from 'lucide-react'
import {
  AcApDocManager,
  AcApSettingManager,
  AcEdOpenMode,
  AcEdViewMode,
} from '@mlightcad/cad-simple-viewer'

import { Button } from '#/components/ui/button'
import { getContentDownload } from '#/lib/ov-client'

import type { VikingFsEntry } from '../-types/viking-fm'

export const MAX_DWG_PREVIEW_BYTES = 25 * 1024 * 1024
const PREVIEW_TIMEOUT_MS = 120_000
const FONT_BASE_URL =
  'https://cdn.jsdelivr.net/gh/mlightcad/cad-data@b85f147f388447b2d0ab828b1299622c18c2c276/'
const WORKER_URLS = {
  dwgParser: new URL(
    '../../../assets/dwg-workers/libredwg-parser-worker.js',
    import.meta.url,
  ).href,
  mtextRender: new URL(
    '../../../assets/dwg-workers/mtext-renderer-worker.js',
    import.meta.url,
  ).href,
}

type LoadError = 'failed' | 'reload' | 'timeout' | 'tooLarge' | 'workers'

// ponytail: upstream exposes one non-cancellable viewer; fail closed until refresh if it hangs.
let destroyQueue = Promise.resolve()
let viewerUnavailable = false

function destroyViewer(
  manager: AcApDocManager,
  pendingOpen: Promise<boolean> | null,
) {
  destroyQueue = destroyQueue.then(async () => {
    await pendingOpen?.catch(() => undefined)
    await manager.destroy()
    viewerUnavailable = false
  })
  return destroyQueue
}

export function DwgPreview({ file }: { file: VikingFsEntry }) {
  const { t } = useTranslation('resources')
  const containerRef = useRef<HTMLDivElement>(null)
  const [error, setError] = useState<LoadError | null>(null)
  const [loading, setLoading] = useState(true)
  const [ready, setReady] = useState(false)
  const [warning, setWarning] = useState(false)
  const managerRef = useRef<AcApDocManager | null>(null)

  useEffect(() => {
    const lifecycleController = new AbortController()
    const downloadController = new AbortController()
    let manager: AcApDocManager | null = null
    let openPromise: Promise<boolean> | null = null
    let openPending = false
    let loadTimeout: number | null = null
    let downloadTooLarge = false
    let loadExpired = false
    const cancelled = () => lifecycleController.signal.aborted || loadExpired

    const clearLoadTimeout = () => {
      if (loadTimeout !== null) window.clearTimeout(loadTimeout)
      loadTimeout = null
    }

    const releaseManager = () => {
      const current = manager
      const pendingOpen = openPromise
      manager = null
      openPromise = null
      managerRef.current = null
      return current ? destroyViewer(current, pendingOpen) : Promise.resolve()
    }

    const fail = async (loadError: LoadError) => {
      clearLoadTimeout()
      let reason = loadError
      try {
        await releaseManager()
      } catch (cleanupError) {
        console.error('DWG viewer cleanup failed:', cleanupError)
        reason = 'failed'
      }
      if (!cancelled()) {
        setError(reason)
        setLoading(false)
        setReady(false)
      }
    }

    const load = async () => {
      setError(null)
      setLoading(true)
      setReady(false)
      setWarning(false)
      loadTimeout = window.setTimeout(() => {
        if (cancelled()) return
        loadExpired = true
        if (openPending) viewerUnavailable = true
        setError('timeout')
        setLoading(false)
        setReady(false)
        downloadController.abort()
        void releaseManager().catch((cleanupError) =>
          console.error('DWG viewer cleanup failed:', cleanupError),
        )
      }, PREVIEW_TIMEOUT_MS)

      if (file.sizeBytes !== null && file.sizeBytes > MAX_DWG_PREVIEW_BYTES) {
        await fail('tooLarge')
        return
      }

      try {
        if (viewerUnavailable) {
          await fail('reload')
          return
        }
        await destroyQueue
        if (cancelled()) return

        const response = await getContentDownload({
          query: { uri: file.uri },
          onDownloadProgress: ({ loaded }) => {
            if (loaded <= MAX_DWG_PREVIEW_BYTES) return
            downloadTooLarge = true
            downloadController.abort()
          },
          responseType: 'arraybuffer',
          signal: downloadController.signal,
          throwOnError: true,
        })
        if (cancelled()) return

        const bytes = response.data
        if (!(bytes instanceof ArrayBuffer) || bytes.byteLength === 0) {
          await fail('failed')
          return
        }
        if (bytes.byteLength > MAX_DWG_PREVIEW_BYTES) {
          await fail('tooLarge')
          return
        }

        const container = containerRef.current
        if (cancelled() || !container) return
        if (!(await AcApDocManager.checkWebworkerReadiness(WORKER_URLS))) {
          await fail('workers')
          return
        }
        if (cancelled()) return

        AcApSettingManager.instance.isShowCommandLine = false
        const createdManager = AcApDocManager.createInstance({
          autoResize: true,
          baseUrl: FONT_BASE_URL,
          builtinOpenFileDialog: false,
          busyIndicatorHost: container,
          container,
          useMainThreadDraw: true,
          webworkerFileUrls: WORKER_URLS,
        })
        if (!createdManager) {
          await fail('failed')
          return
        }
        manager = createdManager
        managerRef.current = manager

        openPromise = manager.openDocument(file.name, bytes, {
          mode: AcEdOpenMode.Read,
          progressiveRendering: true,
          timeout: PREVIEW_TIMEOUT_MS,
        })
        openPending = true
        let opened: boolean
        try {
          opened = await openPromise
          if (opened) {
            openPromise = (async () => {
              while (createdManager.curView.isProcessingEntities) {
                await new Promise((resolve) => window.setTimeout(resolve, 100))
              }
              return true
            })()
            await openPromise
          }
        } finally {
          openPending = false
        }

        if (cancelled()) return
        if (!opened) {
          await fail(
            manager.curDocument.database.lastOpenError?.code ===
              'worker_timeout'
              ? 'timeout'
              : 'failed',
          )
          return
        }

        manager.curView.mode = AcEdViewMode.PAN
        const missed = manager.curView.missedData
        setWarning(
          Object.keys(missed.fonts).length > 0 ||
            missed.images.size > 0 ||
            missed.xrefs.length > 0,
        )
        clearLoadTimeout()
        setLoading(false)
        setReady(true)
      } catch (caught) {
        if (downloadTooLarge) {
          await fail('tooLarge')
          return
        }
        if (cancelled()) return
        const message =
          caught instanceof Error ? caught.message : String(caught)
        await fail(
          /worker_timeout|timed out/i.test(message) ? 'timeout' : 'failed',
        )
      }
    }

    void load()
    return () => {
      clearLoadTimeout()
      lifecycleController.abort()
      downloadController.abort()
      if (openPending) viewerUnavailable = true
      void releaseManager().catch((cleanupError) =>
        console.error('DWG viewer cleanup failed:', cleanupError),
      )
    }
  }, [file.name, file.sizeBytes, file.uri])

  return (
    <div className="relative h-full min-h-[360px] overflow-hidden rounded-md border bg-black">
      <div
        ref={containerRef}
        className="absolute inset-0"
        aria-label={t('filePreview.dwg.canvas')}
      />

      <div className="absolute right-3 top-3 z-10 flex items-center gap-2 rounded-md border bg-background/90 p-1.5 shadow-sm backdrop-blur">
        {ready ? (
          <span className="px-1 text-xs text-muted-foreground">
            {t('filePreview.dwg.ready')}
          </span>
        ) : null}
        <Button
          size="sm"
          variant="ghost"
          disabled={!ready}
          aria-label={t('filePreview.dwg.fit')}
          title={t('filePreview.dwg.fit')}
          onClick={() => managerRef.current?.curView.zoomToFitDrawing()}
        >
          <Scan className="mr-1 size-4" />
          {t('filePreview.dwg.fit')}
        </Button>
      </div>

      {loading ? (
        <div
          className="absolute inset-0 z-20 flex items-center justify-center bg-background/85 text-sm"
          role="status"
        >
          <Loader2 className="mr-2 size-4 animate-spin" />
          {t('filePreview.dwg.loading')}
        </div>
      ) : null}

      {error ? (
        <div
          className="absolute inset-0 z-20 flex items-center justify-center bg-background/95 p-6 text-center text-sm text-destructive"
          role="alert"
        >
          <AlertTriangle className="mr-2 size-4" />
          {t(`filePreview.dwg.${error}`)}
        </div>
      ) : null}

      {warning ? (
        <div
          className="absolute bottom-3 left-3 right-3 z-10 rounded-md border border-amber-500/40 bg-background/90 px-3 py-2 text-xs text-amber-700 shadow-sm backdrop-blur dark:text-amber-300"
          role="status"
        >
          {t('filePreview.dwg.incomplete')}
        </div>
      ) : null}
    </div>
  )
}
