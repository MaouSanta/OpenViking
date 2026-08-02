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

const mocks = vi.hoisted(() => ({
  checkWorkers: vi.fn(),
  commandLineSettings: { isShowCommandLine: true },
  createInstance: vi.fn(),
  destroy: vi.fn(),
  download: vi.fn(),
  fit: vi.fn(),
  openDocument: vi.fn(),
}))

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}))

vi.mock('#/lib/ov-client', () => ({
  getContentDownload: mocks.download,
}))

vi.mock('@mlightcad/cad-simple-viewer', () => ({
  AcApSettingManager: { instance: mocks.commandLineSettings },
  AcEdOpenMode: { Read: 0 },
  AcEdViewMode: { PAN: 1 },
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

const file: VikingFsEntry = {
  uri: 'viking://resources/factory/floor-plan.dwg',
  name: 'floor-plan.dwg',
  isDir: false,
  size: '1 KiB',
  sizeBytes: 1024,
  modTime: '',
  modTimestamp: null,
  abstract: '',
}

describe('DwgPreview', () => {
  beforeEach(() => {
    mocks.commandLineSettings.isShowCommandLine = true
    mocks.checkWorkers.mockReset().mockResolvedValue(true)
    mocks.createInstance.mockReset().mockReturnValue({
      curDocument: { database: { lastOpenError: null } },
      curView: {
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
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('downloads with auth, opens read-only, and destroys the singleton', async () => {
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
    let processing = true
    mocks.createInstance.mockReturnValue({
      curDocument: { database: { lastOpenError: null } },
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

    render(<DwgPreview file={file} />)
    await waitFor(() => expect(mocks.openDocument).toHaveBeenCalledTimes(1))
    expect(screen.queryByText('filePreview.dwg.ready')).toBeNull()

    processing = false
    expect(await screen.findByText('filePreview.dwg.ready')).toBeTruthy()
  })

  it('waits for progressive entity rendering before destroying', async () => {
    let processing = true
    mocks.createInstance.mockReturnValue({
      curDocument: { database: { lastOpenError: null } },
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

  it('aborts an unknown-size download when it crosses the preview limit', async () => {
    mocks.download.mockImplementationOnce(({ onDownloadProgress, signal }) => {
      onDownloadProgress({ loaded: MAX_DWG_PREVIEW_BYTES + 1 })
      expect(signal.aborted).toBe(true)
      return Promise.reject(new DOMException('aborted', 'AbortError'))
    })

    render(<DwgPreview file={{ ...file, sizeBytes: null }} />)

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
        database: { lastOpenError: { code: 'worker_timeout' } },
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

  it('fails fast after a parser stays pending past the total timeout', async () => {
    vi.useFakeTimers()
    let finishOpen: (opened: boolean) => void = () => undefined
    mocks.openDocument.mockReturnValueOnce(
      new Promise<boolean>((resolve) => {
        finishOpen = resolve
      }),
    )

    const first = render(<DwgPreview file={file} />)
    await vi.waitFor(() => expect(mocks.openDocument).toHaveBeenCalledTimes(1))
    await act(() => vi.advanceTimersByTimeAsync(120_000))

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
