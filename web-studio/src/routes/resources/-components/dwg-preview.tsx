import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { AlertTriangle, Loader2, Scan, X } from 'lucide-react'
import {
  AcApDocManager,
  AcApSettingManager,
  AcEdOpenMode,
  AcEdViewMode,
  eventBus,
} from '@mlightcad/cad-simple-viewer'
import type { AcDbProgressdEventArgs } from '@mlightcad/data-model'

import { Button } from '#/components/ui/button'
import { Progress } from '#/components/ui/progress'
import { getContentDownload } from '#/lib/ov-client'

import { fetchFsStat } from '../-lib/api'
import type { VikingFsEntry } from '../-types/viking-fm'

const AUTO_DWG_PREVIEW_BYTES = 5 * 1024 * 1024
export const MAX_DWG_PREVIEW_BYTES = 25 * 1024 * 1024
const PARSER_TIMEOUT_MS = 120_000
const PARSER_WATCHDOG_MS = 150_000
const RENDER_SOFT_TIMEOUT_MS = 120_000
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
type LoadStage = 'convert' | 'download' | 'font' | 'parse' | 'render'

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

function hasRecoverablePartialContent(manager: AcApDocManager) {
  const database = manager.curDocument.database
  const modelSpace = database.tables.blockTable.modelSpace as
    | typeof database.tables.blockTable.modelSpace
    | undefined
  return modelSpace
    ? modelSpace.newIterator().count > 0
    : !database.extents.isEmpty()
}

function scopeViewerHiddenRule() {
  for (const style of document.head.querySelectorAll('style')) {
    if (!style.textContent.includes('.ml-cli-container')) continue
    for (const rule of Array.from(style.sheet?.cssRules ?? [])) {
      if (rule instanceof CSSStyleRule && rule.selectorText === '.hidden') {
        rule.selectorText = '.ml-cli-container .hidden'
      }
    }
  }
}

export function DwgPreview({ file }: { file: VikingFsEntry }) {
  const { t } = useTranslation('resources')
  const containerRef = useRef<HTMLDivElement>(null)
  const [error, setError] = useState<LoadError | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadProgress, setLoadProgress] = useState<number | null>(null)
  const [loadStage, setLoadStage] = useState<LoadStage>('download')
  const [partial, setPartial] = useState(false)
  const [ready, setReady] = useState(false)
  const [warning, setWarning] = useState(false)
  const [warningDismissed, setWarningDismissed] = useState(false)
  const [approvedUri, setApprovedUri] = useState<string | null>(null)
  const [sizeProbe, setSizeProbe] = useState<{
    sizeBytes: number | null
    uri: string
  } | null>(null)
  const managerRef = useRef<AcApDocManager | null>(null)
  const sizeResolved = file.sizeBytes !== null || sizeProbe?.uri === file.uri
  const previewSizeBytes =
    file.sizeBytes ?? (sizeProbe?.uri === file.uri ? sizeProbe.sizeBytes : null)
  const requiresManualLoad =
    previewSizeBytes === null ||
    (previewSizeBytes > AUTO_DWG_PREVIEW_BYTES &&
      previewSizeBytes <= MAX_DWG_PREVIEW_BYTES)
  const loadApproved =
    sizeResolved && (!requiresManualLoad || approvedUri === file.uri)

  useEffect(() => {
    if (file.sizeBytes !== null) return
    let alive = true
    void fetchFsStat(file.uri)
      .then((entry) => {
        if (alive) setSizeProbe({ sizeBytes: entry.sizeBytes, uri: file.uri })
      })
      .catch(() => {
        if (alive) setSizeProbe({ sizeBytes: null, uri: file.uri })
      })
    return () => {
      alive = false
    }
  }, [file.sizeBytes, file.uri])

  useEffect(() => {
    if (!loadApproved) return

    const lifecycleController = new AbortController()
    const downloadController = new AbortController()
    let manager: AcApDocManager | null = null
    let openPromise: Promise<boolean> | null = null
    let openPending = false
    let openWatchdog: number | null = null
    let renderSoftTimeout: number | null = null
    let downloadTooLarge = false
    let loadExpired = false
    const cancelled = () => lifecycleController.signal.aborted || loadExpired

    const clearTimeouts = () => {
      if (openWatchdog !== null) window.clearTimeout(openWatchdog)
      if (renderSoftTimeout !== null) window.clearTimeout(renderSoftTimeout)
      openWatchdog = null
      renderSoftTimeout = null
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
      clearTimeouts()
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
        setLoadProgress(null)
        setPartial(false)
        setReady(false)
      }
    }

    const handleOpenProgress = (progress: AcDbProgressdEventArgs) => {
      if (
        cancelled() ||
        !manager ||
        progress.database !== manager.curDocument.database
      ) {
        return
      }
      const percentage = Math.max(
        0,
        Math.min(100, Math.round(progress.percentage)),
      )
      if (progress.stage === 'FETCH_FILE') {
        setLoadStage('download')
        setLoadProgress(percentage)
      } else if (progress.subStage === 'PARSE') {
        setLoadStage('parse')
        setLoadProgress(null)
      } else if (progress.subStage === 'FONT') {
        setLoadStage('font')
        setLoadProgress(percentage)
      } else {
        setLoadStage('convert')
        setLoadProgress(percentage)
      }
    }

    eventBus.on('open-file-progress', handleOpenProgress)

    const load = async () => {
      setError(null)
      setLoading(true)
      setLoadProgress(null)
      setLoadStage('download')
      setPartial(false)
      setReady(false)
      setWarning(false)
      setWarningDismissed(false)

      if (
        previewSizeBytes !== null &&
        previewSizeBytes > MAX_DWG_PREVIEW_BYTES
      ) {
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
          onDownloadProgress: ({ loaded, total }) => {
            const expectedBytes = total ?? previewSizeBytes
            if (expectedBytes) {
              setLoadProgress(
                Math.min(100, Math.round((loaded / expectedBytes) * 100)),
              )
            }
            if (
              loaded <= MAX_DWG_PREVIEW_BYTES &&
              (total === undefined || total <= MAX_DWG_PREVIEW_BYTES)
            ) {
              return
            }
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
        scopeViewerHiddenRule()
        if (!createdManager) {
          await fail('failed')
          return
        }
        manager = createdManager
        managerRef.current = manager

        openWatchdog = window.setTimeout(() => {
          if (cancelled()) return
          loadExpired = true
          viewerUnavailable = true
          setError('timeout')
          setLoading(false)
          setLoadProgress(null)
          setPartial(false)
          setReady(false)
          void releaseManager().catch((cleanupError) =>
            console.error('DWG viewer cleanup failed:', cleanupError),
          )
        }, PARSER_WATCHDOG_MS)

        openPromise = manager.openDocument(file.name, bytes, {
          mode: AcEdOpenMode.Read,
          progressiveRendering: true,
          timeout: PARSER_TIMEOUT_MS,
        })
        openPending = true
        let opened: boolean
        try {
          opened = await openPromise
          if (cancelled()) return
          if (!opened) {
            if (!hasRecoverablePartialContent(createdManager)) {
              await fail(
                manager.curDocument.database.lastOpenError?.code ===
                  'worker_timeout'
                  ? 'timeout'
                  : 'failed',
              )
              return
            }
            setLoading(false)
            setLoadProgress(null)
            setPartial(true)
          }

          clearTimeouts()
          createdManager.curView.mode = AcEdViewMode.PAN
          if (createdManager.curView.isProcessingEntities) {
            if (opened) {
              setLoadStage('render')
              setLoadProgress(null)
              renderSoftTimeout = window.setTimeout(() => {
                if (cancelled()) return
                setLoading(false)
                setPartial(true)
              }, RENDER_SOFT_TIMEOUT_MS)
            }
            openPromise = (async () => {
              while (createdManager.curView.isProcessingEntities) {
                await new Promise((resolve) => window.setTimeout(resolve, 100))
              }
              return true
            })()
            await openPromise
          }
          if (!opened) return
        } finally {
          openPending = false
        }

        if (cancelled()) return
        clearTimeouts()
        const missed = manager.curView.missedData
        setWarning(
          Object.keys(missed.fonts).length > 0 ||
            missed.images.size > 0 ||
            missed.xrefs.length > 0,
        )
        setLoading(false)
        setLoadProgress(null)
        setPartial(false)
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
      clearTimeouts()
      eventBus.off('open-file-progress', handleOpenProgress)
      lifecycleController.abort()
      downloadController.abort()
      if (openPending) viewerUnavailable = true
      void releaseManager().catch((cleanupError) =>
        console.error('DWG viewer cleanup failed:', cleanupError),
      )
    }
  }, [file.name, file.uri, loadApproved, previewSizeBytes])

  if (!sizeResolved) {
    return (
      <div
        className="flex h-full min-h-[360px] items-center justify-center rounded-md border bg-muted/20 text-sm text-muted-foreground"
        role="status"
      >
        <Loader2 className="mr-2 size-4 animate-spin" />
        {t('filePreview.dwg.checkingSize')}
      </div>
    )
  }

  if (!loadApproved) {
    return (
      <div className="flex h-full min-h-[360px] items-center justify-center rounded-md border bg-muted/20 p-6 text-center">
        <div className="max-w-md">
          <AlertTriangle className="mx-auto size-5 text-amber-500" />
          <div className="mt-3 text-sm font-medium">
            {t('filePreview.dwg.manualLoadTitle')}
          </div>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            {t('filePreview.dwg.manualLoadDescription')}
          </p>
          <Button className="mt-4" onClick={() => setApprovedUri(file.uri)}>
            {t('filePreview.dwg.manualLoad')}
          </Button>
        </div>
      </div>
    )
  }

  return (
    <div className="relative isolate h-full min-h-[360px] overflow-hidden rounded-md border bg-black">
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
          disabled={!ready && !partial && loadStage !== 'render'}
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
          className="pointer-events-none absolute bottom-3 left-3 right-3 z-20 rounded-md border bg-background/90 px-3 py-2 text-sm shadow-sm backdrop-blur"
          role="status"
        >
          <div className="flex items-center gap-2">
            <Loader2 className="size-4 animate-spin" />
            <span>{t(`filePreview.dwg.progress.${loadStage}`)}</span>
            {loadProgress !== null ? (
              <span className="ml-auto tabular-nums text-muted-foreground">
                {loadProgress}%
              </span>
            ) : null}
          </div>
          {loadProgress !== null ? (
            <Progress
              aria-label={t(`filePreview.dwg.progress.${loadStage}`)}
              className="mt-2"
              value={loadProgress}
            />
          ) : null}
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

      {(partial || warning) && !warningDismissed ? (
        <div
          className="absolute bottom-3 left-3 right-3 z-10 flex items-center gap-2 rounded-md border border-amber-500/40 bg-background/90 px-3 py-2 text-xs text-amber-700 shadow-sm backdrop-blur dark:text-amber-300"
          role="status"
        >
          <span className="min-w-0 flex-1">
            {t(
              partial
                ? 'filePreview.dwg.partial'
                : 'filePreview.dwg.incomplete',
            )}
          </span>
          <Button
            size="icon-sm"
            variant="ghost"
            aria-label={t('filePreview.dwg.dismissWarning')}
            onClick={() => setWarningDismissed(true)}
          >
            <X className="size-4" />
          </Button>
        </div>
      ) : null}
    </div>
  )
}
