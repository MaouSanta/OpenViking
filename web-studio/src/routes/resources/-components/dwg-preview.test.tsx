// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { VikingFsEntry } from '../-types/viking-fm'
import { DwgPreview, MAX_DWG_PREVIEW_BYTES } from './dwg-preview'

type OpenProgressEvent = {
  database?: unknown
  percentage: number
  stage: 'CONVERSION' | 'FETCH_FILE'
  subStage?: string
  subStageStatus: string
}

const mocks = vi.hoisted(() => ({
  checkWorkers: vi.fn(),
  commandLineSettings: { isShowCommandLine: true },
  createInstance: vi.fn(),
  destroy: vi.fn(),
  download: vi.fn(),
  fit: vi.fn(),
  openDocument: vi.fn(),
  progressHandlers: new Set<(event: OpenProgressEvent) => void>(),
  stat: vi.fn(),
}))

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}))

vi.mock('#/lib/ov-client', () => ({
  getContentDownload: mocks.download,
}))

vi.mock('../-lib/api', () => ({
  fetchFsStat: mocks.stat,
}))

vi.mock('@mlightcad/cad-simple-viewer', () => ({
  AcApSettingManager: { instance: mocks.commandLineSettings },
  AcEdOpenMode: { Read: 0 },
  AcEdViewMode: { PAN: 1 },
  eventBus: {
    off: vi.fn((_event: string, handler: (event: OpenProgressEvent) => void) =>
      mocks.progressHandlers.delete(handler),
    ),
    on: vi.fn((_event: string, handler: (event: OpenProgressEvent) => void) =>
      mocks.progressHandlers.add(handler),
    ),
  },
  LIBREDWG_PARSER_WORKER_FILE: 'libredwg-parser-worker.js',
  MTEXT_RENDERER_WORKER_FILE: 'mtext-renderer-worker.js',
  AcApDocManager: class {
    static checkWebworkerReadiness = mocks.checkWorkers
    static createInstance = mocks.createInstance
    static get instance() {
      return {
        areWorkersReady: mocks.checkWorkers,
        curView: {
          missedData: { fonts: {}, images: new Map(), xrefs: [] },
          zoomToFitDrawing: mocks.fit,
        },
        destroy: mocks.destroy,
        openDocument: mocks.openDocument,
      }
    }
  },
}))

const AUTO_DWG_PREVIEW_BYTES = 5 * 1024 * 1024
const file: VikingFsEntry = {
  uri: 'viking://resources/factory/floor-plan.dwg',
  name: 'floor-plan.dwg',
  isDir: false,
  size: '5 MiB',
  sizeBytes: AUTO_DWG_PREVIEW_BYTES,
  modTime: '',
  modTimestamp: null,
  abstract: '',
}
const emptyTables = {
  blockTable: { modelSpace: { newIterator: () => ({ count: 0 }) } },
}
let processing = false

describe('DwgPreview', () => {
  beforeEach(() => {
    processing = false
    mocks.progressHandlers.clear()
    mocks.commandLineSettings.isShowCommandLine = true
    mocks.checkWorkers.mockReset().mockResolvedValue(true)
    mocks.createInstance.mockReset().mockReturnValue({
      curDocument: { database: { lastOpenError: null, tables: emptyTables } },
      curView: {
        get isProcessingEntities() {
          return processing
        },
        missedData: { fonts: {}, images: new Map(), xrefs: [] },
        zoomToFitDrawing: mocks.fit,
      },
      destroy: mocks.destroy,
      openDocument: mocks.openDocument,
    })
    mocks.destroy.mockReset().mockResolvedValue(undefined)
    mocks.download.mockReset().mockResolvedValue({
      data: new Uint8Array([65, 67, 49, 48, 51, 50]).buffer,
    })
    mocks.fit.mockReset()
    mocks.openDocument.mockReset().mockResolvedValue(true)
    mocks.stat.mockReset().mockResolvedValue({
      ...file,
      sizeBytes: AUTO_DWG_PREVIEW_BYTES,
    })
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('automatically loads a drawing at the 5 MiB boundary', async () => {
    const view = render(<DwgPreview file={file} />)

    expect(await screen.findByText('filePreview.dwg.ready')).toBeTruthy()
    expect(mocks.download).toHaveBeenCalledWith({
      query: { uri: file.uri },
      onDownloadProgress: expect.any(Function),
      responseType: 'arraybuffer',
      signal: expect.anything(),
      throwOnError: true,
    })
    expect(mocks.openDocument).toHaveBeenCalledWith(
      file.name,
      expect.any(ArrayBuffer),
      { mode: 0, progressiveRendering: true, timeout: 120_000 },
    )
    expect(mocks.createInstance).toHaveBeenCalledWith(
      expect.objectContaining({ useMainThreadDraw: true }),
    )
    expect(mocks.commandLineSettings.isShowCommandLine).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'filePreview.dwg.fit' }))
    expect(mocks.fit).toHaveBeenCalledTimes(1)

    view.unmount()
    await waitFor(() => expect(mocks.destroy).toHaveBeenCalledTimes(1))
  })

  it('opens the next drawing after a ready viewer unmounts', async () => {
    const first = render(<DwgPreview file={file} />)
    expect(await screen.findByText('filePreview.dwg.ready')).toBeTruthy()
    first.unmount()

    const second = render(
      <DwgPreview
        file={{ ...file, name: 'next.dwg', uri: 'viking://next.dwg' }}
      />,
    )
    expect(await screen.findByText('filePreview.dwg.ready')).toBeTruthy()
    expect(mocks.download).toHaveBeenCalledTimes(2)
    expect(mocks.openDocument).toHaveBeenCalledTimes(2)

    second.unmount()
    await waitFor(() => expect(mocks.destroy).toHaveBeenCalledTimes(2))
  })

  it('keeps loading until progressive entity rendering finishes', async () => {
    processing = true

    render(<DwgPreview file={file} />)
    await waitFor(() => expect(mocks.openDocument).toHaveBeenCalledTimes(1))
    expect(screen.queryByText('filePreview.dwg.ready')).toBeNull()

    processing = false
    expect(await screen.findByText('filePreview.dwg.ready')).toBeTruthy()
  })

  it('shows the viewer conversion percentage and stage', async () => {
    processing = true

    render(<DwgPreview file={file} />)
    try {
      await waitFor(() => expect(mocks.progressHandlers.size).toBe(1))

      act(() => {
        for (const handler of mocks.progressHandlers) {
          handler({
            database:
              mocks.createInstance.mock.results[0].value.curDocument.database,
            percentage: 42,
            stage: 'CONVERSION',
            subStage: 'PARSE',
            subStageStatus: 'IN-PROGRESS',
          })
        }
      })

      expect(screen.getByText('filePreview.dwg.progress.parse')).toBeTruthy()
      expect(screen.queryByRole('progressbar')).toBeNull()

      act(() => {
        for (const handler of mocks.progressHandlers) {
          handler({
            database:
              mocks.createInstance.mock.results[0].value.curDocument.database,
            percentage: 42,
            stage: 'CONVERSION',
            subStage: 'ENTITY',
            subStageStatus: 'IN-PROGRESS',
          })
        }
      })

      expect(
        screen.getByRole('progressbar').getAttribute('aria-valuenow'),
      ).toBe('42')
      expect(screen.getByText('filePreview.dwg.progress.convert')).toBeTruthy()
    } finally {
      processing = false
      await screen.findByText('filePreview.dwg.ready')
    }
  })

  it('keeps an interactive partial canvas after progressive rendering exceeds the soft deadline', async () => {
    const timeoutSpy = vi.spyOn(window, 'setTimeout')
    processing = true

    render(<DwgPreview file={file} />)
    try {
      await vi.waitFor(() =>
        expect(mocks.openDocument).toHaveBeenCalledTimes(1),
      )
      expect(screen.getByText('filePreview.dwg.progress.render')).toBeTruthy()
      expect(screen.queryByRole('progressbar')).toBeNull()
      expect(
        screen.getByRole('button', {
          name: 'filePreview.dwg.fit',
        }).disabled,
      ).toBe(false)
      const softTimeout = timeoutSpy.mock.calls.find(
        ([, delay]) => delay === 120_000,
      )?.[0]
      expect(typeof softTimeout).toBe('function')
      act(() => {
        if (typeof softTimeout === 'function') softTimeout()
      })

      expect(screen.queryByRole('alert')).toBeNull()
      expect(screen.getByText('filePreview.dwg.partial')).toBeTruthy()
      expect(
        screen.getByRole('button', {
          name: 'filePreview.dwg.fit',
        }).disabled,
      ).toBe(false)
      expect(mocks.destroy).not.toHaveBeenCalled()
    } finally {
      processing = false
      timeoutSpy.mockRestore()
      await screen.findByText('filePreview.dwg.ready')
    }
    expect(screen.getByText('filePreview.dwg.ready')).toBeTruthy()
    expect(screen.queryByText('filePreview.dwg.partial')).toBeNull()
  })

  it('waits for progressive entity rendering before destroying', async () => {
    processing = true

    const view = render(<DwgPreview file={file} />)
    await waitFor(() => expect(mocks.openDocument).toHaveBeenCalledTimes(1))
    view.unmount()
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(mocks.destroy).not.toHaveBeenCalled()

    processing = false
    await waitFor(() => expect(mocks.destroy).toHaveBeenCalledTimes(1))
  })

  it('rejects an oversized drawing before downloading it', async () => {
    render(
      <DwgPreview file={{ ...file, sizeBytes: MAX_DWG_PREVIEW_BYTES + 1 }} />,
    )

    expect((await screen.findByRole('alert')).textContent).toContain(
      'filePreview.dwg.tooLarge',
    )
    expect(mocks.download).not.toHaveBeenCalled()
  })

  it('requires manual confirmation through the 25 MiB hard-limit boundary', async () => {
    render(<DwgPreview file={{ ...file, sizeBytes: MAX_DWG_PREVIEW_BYTES }} />)

    expect(
      await screen.findByText('filePreview.dwg.manualLoadTitle'),
    ).toBeTruthy()
    expect(mocks.download).not.toHaveBeenCalled()

    fireEvent.click(
      screen.getByRole('button', { name: 'filePreview.dwg.manualLoad' }),
    )

    expect(await screen.findByText('filePreview.dwg.ready')).toBeTruthy()
    expect(mocks.download).toHaveBeenCalledTimes(1)
  })

  it('automatically loads an unknown-size drawing when stat reports 5 MiB', async () => {
    render(<DwgPreview file={{ ...file, sizeBytes: null }} />)

    expect(await screen.findByText('filePreview.dwg.ready')).toBeTruthy()
    expect(mocks.stat).toHaveBeenCalledWith(file.uri)
    expect(mocks.download).toHaveBeenCalledTimes(1)
  })

  it('requires confirmation when stat cannot determine the size', async () => {
    mocks.stat.mockResolvedValueOnce({ ...file, sizeBytes: null })

    render(<DwgPreview file={{ ...file, sizeBytes: null }} />)

    expect(
      await screen.findByText('filePreview.dwg.manualLoadTitle'),
    ).toBeTruthy()
    expect(mocks.download).not.toHaveBeenCalled()
  })

  it('confirms an unknown large size, then enforces the response hard limit', async () => {
    mocks.stat.mockResolvedValueOnce({
      ...file,
      sizeBytes: AUTO_DWG_PREVIEW_BYTES + 1,
    })
    mocks.download.mockImplementationOnce(({ onDownloadProgress, signal }) => {
      onDownloadProgress({ loaded: 1, total: MAX_DWG_PREVIEW_BYTES + 1 })
      expect(signal.aborted).toBe(true)
      return Promise.reject(new DOMException('aborted', 'AbortError'))
    })

    render(<DwgPreview file={{ ...file, sizeBytes: null }} />)

    expect(
      await screen.findByText('filePreview.dwg.manualLoadTitle'),
    ).toBeTruthy()
    expect(mocks.download).not.toHaveBeenCalled()
    fireEvent.click(
      screen.getByRole('button', { name: 'filePreview.dwg.manualLoad' }),
    )

    expect((await screen.findByRole('alert')).textContent).toContain(
      'filePreview.dwg.tooLarge',
    )
  })

  it('aborts a download without a reported total after it crosses 25 MiB', async () => {
    mocks.stat.mockResolvedValueOnce({ ...file, sizeBytes: null })
    mocks.download.mockImplementationOnce(({ onDownloadProgress, signal }) => {
      onDownloadProgress({ loaded: MAX_DWG_PREVIEW_BYTES + 1 })
      expect(signal.aborted).toBe(true)
      return Promise.reject(new DOMException('aborted', 'AbortError'))
    })

    render(<DwgPreview file={{ ...file, sizeBytes: null }} />)
    fireEvent.click(
      await screen.findByRole('button', {
        name: 'filePreview.dwg.manualLoad',
      }),
    )

    expect((await screen.findByRole('alert')).textContent).toContain(
      'filePreview.dwg.tooLarge',
    )
  })

  it('shows a clear error and releases the viewer when parsing fails', async () => {
    mocks.openDocument.mockResolvedValue(false)

    render(<DwgPreview file={file} />)

    expect((await screen.findByRole('alert')).textContent).toContain(
      'filePreview.dwg.failed',
    )
    expect(mocks.destroy).toHaveBeenCalledTimes(1)
  })

  it('reports the viewer worker timeout explicitly', async () => {
    mocks.openDocument.mockRejectedValue(
      new Error('Worker operation timed out after 120000ms'),
    )

    render(<DwgPreview file={file} />)

    expect((await screen.findByRole('alert')).textContent).toContain(
      'filePreview.dwg.timeout',
    )
    expect(mocks.destroy).toHaveBeenCalledTimes(1)
  })

  it('reports a worker timeout returned through the viewer error state', async () => {
    mocks.createInstance.mockReturnValue({
      curDocument: {
        database: {
          lastOpenError: { code: 'worker_timeout' },
          tables: emptyTables,
        },
      },
      curView: {
        missedData: { fonts: {}, images: new Map(), xrefs: [] },
        zoomToFitDrawing: mocks.fit,
      },
      destroy: mocks.destroy,
      openDocument: mocks.openDocument,
    })
    mocks.openDocument.mockResolvedValue(false)

    render(<DwgPreview file={file} />)

    expect((await screen.findByRole('alert')).textContent).toContain(
      'filePreview.dwg.timeout',
    )
    expect(mocks.destroy).toHaveBeenCalledTimes(1)
  })

  it('keeps an extents-only partial preview and drains rendering before destroy', async () => {
    processing = true
    mocks.createInstance.mockReturnValue({
      curDocument: {
        database: {
          extents: { isEmpty: () => false },
          lastOpenError: { code: 'worker_timeout' },
          tables: { blockTable: { modelSpace: null } },
        },
      },
      curView: {
        get isProcessingEntities() {
          return processing
        },
        missedData: { fonts: {}, images: new Map(), xrefs: [] },
        zoomToFitDrawing: mocks.fit,
      },
      destroy: mocks.destroy,
      openDocument: mocks.openDocument,
    })
    mocks.openDocument.mockResolvedValue(false)

    const view = render(<DwgPreview file={file} />)

    expect(await screen.findByText('filePreview.dwg.partial')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(
      screen.getByRole('button', {
        name: 'filePreview.dwg.fit',
      }).disabled,
    ).toBe(false)
    expect(mocks.destroy).not.toHaveBeenCalled()

    view.unmount()
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(mocks.destroy).not.toHaveBeenCalled()

    processing = false
    await waitFor(() => expect(mocks.destroy).toHaveBeenCalledTimes(1))
  })

  it('waits for a pending open before destroying the singleton', async () => {
    let finishOpen: (opened: boolean) => void = () => undefined
    mocks.openDocument.mockReturnValueOnce(
      new Promise<boolean>((resolve) => {
        finishOpen = resolve
      }),
    )

    const view = render(<DwgPreview file={file} />)
    await waitFor(() => expect(mocks.openDocument).toHaveBeenCalledTimes(1))
    view.unmount()
    await Promise.resolve()

    expect(mocks.destroy).not.toHaveBeenCalled()
    finishOpen(true)
    await waitFor(() => expect(mocks.destroy).toHaveBeenCalledTimes(1))
  })

  it('fails closed after a parser outlives its watchdog', async () => {
    vi.useFakeTimers()
    let finishOpen: (opened: boolean) => void = () => undefined
    mocks.openDocument.mockReturnValueOnce(
      new Promise<boolean>((resolve) => {
        finishOpen = resolve
      }),
    )

    const first = render(<DwgPreview file={file} />)
    await vi.waitFor(() => expect(mocks.openDocument).toHaveBeenCalledTimes(1))
    await act(() => vi.advanceTimersByTimeAsync(150_000))

    expect(screen.getByRole('alert').textContent).toContain(
      'filePreview.dwg.timeout',
    )
    expect(mocks.destroy).not.toHaveBeenCalled()
    const second = render(<DwgPreview file={file} />)
    await act(async () => undefined)
    expect(
      screen
        .getAllByRole('alert')
        .some((alert) => alert.textContent.includes('filePreview.dwg.reload')),
    ).toBe(true)
    expect(mocks.download).toHaveBeenCalledTimes(1)

    await act(async () => finishOpen(true))
    await vi.waitFor(() => expect(mocks.destroy).toHaveBeenCalledTimes(1))
    first.unmount()
    second.unmount()
  })

  it('does not destroy the singleton twice when unmounted during failure cleanup', async () => {
    let finishDestroy: () => void = () => undefined
    mocks.destroy.mockReturnValueOnce(
      new Promise<void>((resolve) => {
        finishDestroy = resolve
      }),
    )
    mocks.openDocument.mockResolvedValue(false)

    const view = render(<DwgPreview file={file} />)
    await waitFor(() => expect(mocks.destroy).toHaveBeenCalledTimes(1))
    view.unmount()
    finishDestroy()
    await new Promise((resolve) => setTimeout(resolve, 0))

    expect(mocks.destroy).toHaveBeenCalledTimes(1)
  })
})
