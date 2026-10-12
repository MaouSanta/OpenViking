import { useEffect, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { AcApDocManager } from '@mlightcad/cad-simple-viewer'
import { AcGeBox2d } from '@mlightcad/data-model'
import { DwgPreview } from '#/routes/resources/-components/dwg-preview'
import '#/lib/ov-client'
import i18n from '#/i18n'
import type { VikingFsEntry } from '#/routes/resources/-types/viking-fm'
import type { Box, Drawing, Frame, Manifest } from './types'
import '#/styles.css'
import './style.css'

type Mode = 'original' | 'before' | 'after' | 'both'
const modes: Array<[Mode, string]> = [
  ['original', '原始图纸'],
  ['before', '修正前'],
  ['after', '修正后'],
  ['both', '前后叠加'],
]
function managerFor(drawing: Drawing) {
  try {
    const manager = AcApDocManager.instance
    return manager.curDocument.fileName === drawing.name ? manager : null
  } catch {
    return null
  }
}
function union(frames: Array<Frame>): Box | null {
  const boxes = frames.flatMap((f) => (f.bbox ? [f.bbox] : []))
  return boxes.length
    ? [
        Math.min(...boxes.map((b) => b[0])),
        Math.min(...boxes.map((b) => b[1])),
        Math.max(...boxes.map((b) => b[2])),
        Math.max(...boxes.map((b) => b[3])),
      ]
    : null
}
function zoom(drawing: Drawing, box: Box | null) {
  const manager = managerFor(drawing)
  if (manager && box)
    manager.curView.zoomTo(
      new AcGeBox2d({ x: box[0], y: box[1] }, { x: box[2], y: box[3] }),
      1.16,
    )
}

function Compare({ manifest }: { manifest: Manifest }) {
  const id = new URLSearchParams(location.search).get('drawing')
  const drawing =
    manifest.drawings.find((d) => d.id === id) || manifest.drawings[0]
  const [mode, setMode] = useState<Mode>('after')
  const [space, setSpace] = useState('model')
  const [ready, setReady] = useState(false)
  const [isPartial, setIsPartial] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const [labels, setLabels] = useState(true)
  const stage = useRef<HTMLDivElement>(null)
  const svg = useRef<SVGSVGElement>(null)
  const file = useMemo<VikingFsEntry>(
    () => ({
      uri: drawing.uri,
      name: drawing.name,
      isDir: false,
      size: `${(drawing.sizeBytes / 1024 / 1024).toFixed(2)} MiB`,
      sizeBytes: drawing.sizeBytes,
      modTime: '',
      modTimestamp: null,
      abstract: '',
    }),
    [drawing],
  )
  const spaces = useMemo(
    () => [
      { id: 'model', name: '模型空间' },
      ...drawing.before
        .filter((f) => f.space === 'paper')
        .map((f) => ({
          id: String(f.spaceHandle),
          name: f.layoutName || `布局 ${f.spaceHandle}`,
        }))
        .filter((v, i, all) => all.findIndex((x) => x.id === v.id) === i),
    ],
    [drawing],
  )
  const inSpace = (f: Frame) =>
    space === 'model' ? f.space === 'model' : String(f.spaceHandle) === space
  const oldFrames = drawing.before.filter(inSpace)
  const newFrames = drawing.after.filter(inSpace)
  const displayed =
    mode === 'before'
      ? oldFrames
      : mode === 'after'
        ? newFrames
        : mode === 'both'
          ? [...oldFrames, ...newFrames]
          : []
  const bounded = displayed.filter((f) => f.boundary)
  const list = mode === 'original' ? newFrames : displayed

  useEffect(() => {
    let timer = 0
    let positioned = false
    const poll = () => {
      const manager = managerFor(drawing)
      const readyText = i18n.t('resources:filePreview.dwg.ready')
      const partialText = i18n.t('resources:filePreview.dwg.partial')
      const text = stage.current?.textContent || ''
      const complete =
        text.includes(readyText) &&
        manager &&
        !manager.curView.isProcessingEntities
      const partial = text.includes(partialText)
      // The original preview deliberately allows pan/fit on recoverable partial
      // content. Overlays follow that capability while keeping its warning visible.
      if (manager && (complete || partial)) {
        if (!positioned) {
          manager.curView.activeLayoutBtrId = manager.curView.modelSpaceBtrId
          zoom(
            drawing,
            union(
              drawing.after.filter((f) => f.space === 'model' && f.counted),
            ),
          )
          positioned = true
        }
        setIsPartial(!complete)
        setReady(true)
        if (complete) return
      }
      timer = window.setTimeout(poll, 400)
    }
    poll()
    return () => window.clearTimeout(timer)
  }, [drawing])

  useEffect(() => {
    if (!ready) return
    let animation = 0
    let last = 0
    const update = (time: number) => {
      animation = requestAnimationFrame(update)
      if (time - last < 40) return
      last = time
      const view = managerFor(drawing)?.curView
      const target = svg.current
      if (!view || !target) return
      for (const group of target.querySelectorAll<SVGGElement>(
        'g[data-frame]',
      )) {
        const frame = bounded.find((f) => f.id === group.dataset.frame)
        if (!frame?.boundary) continue
        const points = frame.boundary.map(([x, y]) =>
          view.canvasToContainer(view.worldToScreen({ x, y })),
        )
        if (!points.every((p) => Number.isFinite(p.x) && Number.isFinite(p.y)))
          continue
        group
          .querySelector('polygon')
          ?.setAttribute('points', points.map((p) => `${p.x},${p.y}`).join(' '))
        const label = group.querySelector('text')
        const top = points.reduce((a, b) => (a.y < b.y ? a : b))
        if (label) {
          label.setAttribute('x', String(top.x + 5))
          label.setAttribute('y', String(top.y + 17))
        }
      }
    }
    animation = requestAnimationFrame(update)
    return () => cancelAnimationFrame(animation)
  }, [drawing, ready, bounded])

  function selectSpace(value: string) {
    const manager = managerFor(drawing)
    if (!manager) return
    manager.curView.activeLayoutBtrId =
      value === 'model'
        ? manager.curView.modelSpaceBtrId
        : Number(value).toString(16).toUpperCase()
    setSpace(value)
    setSelected(null)
    window.setTimeout(() => {
      if (value === 'model')
        zoom(
          drawing,
          union(drawing.after.filter((f) => f.space === 'model' && f.counted)),
        )
      else manager.curView.zoomToFitDrawing()
    }, 250)
  }
  return (
    <div className="compare-app">
      <header className="compare-header">
        <div className="brand-mark">OV</div>
        <div>
          <div className="eyebrow">WEB STUDIO / DWG REVIEW</div>
          <h1>图纸识别 · 前后对比</h1>
        </div>
        <div className="header-note">
          <span className="live-dot" />
          原始 DWG 交互预览
        </div>
      </header>
      <div className="compare-body">
        <aside className="compare-sidebar">
          <div className="eyebrow">
            选择图纸 <span>03</span>
          </div>
          <nav className="drawing-nav">
            {manifest.drawings.map((d, i) => (
              <a
                key={d.id}
                className={
                  d.id === drawing.id ? 'drawing-link active' : 'drawing-link'
                }
                href={`?drawing=${d.id}`}
              >
                <span className="drawing-number">0{i + 1}</span>
                <span>
                  <strong>{d.name.replace('.dwg', '')}</strong>
                  <small>{d.afterCount} 张 · 修正后</small>
                </span>
              </a>
            ))}
          </nav>
          <div className="count-grid">
            <div className="old-count">
              <span>旧版候选</span>
              <strong>{drawing.beforeCount}</strong>
              <small>模型筛选前</small>
            </div>
            <div className="new-count">
              <span>修正后图纸</span>
              <strong>{drawing.afterCount}</strong>
              <small>未调用模型</small>
            </div>
          </div>
          <p className="csv-reference">
            你的 CSV 旧数量 <b>{drawing.csvCount}</b>
          </p>
          <p className="scope-note">
            旧版边界为模型筛选前的候选，CSV 为筛选后数量；CSV
            未提供最终边界，不能逐框还原。
          </p>
          <div className="list-header">
            <h2>
              {mode === 'before'
                ? '旧版候选'
                : mode === 'both'
                  ? '前后边界'
                  : '修正后图纸'}
            </h2>
            <span>{list.length}</span>
          </div>
          <div className="frame-list">
            {list.map((f) => (
              <button
                key={f.id}
                disabled={!ready || !f.bbox}
                className={`frame-row ${selected === f.id ? 'selected' : ''}`}
                onClick={() => {
                  setSelected(f.id)
                  zoom(drawing, f.bbox)
                }}
              >
                <span
                  className={`frame-dot ${f.id.startsWith('before') ? 'old' : 'new'}`}
                />
                <span>
                  <strong>
                    {f.label}
                    {!f.counted ? ' · 待确定内容' : ''}
                  </strong>
                  <small>
                    {f.boundary ? `${f.textCount} 个文字实例` : '无独立图框'}
                    {f.textPreview[0]
                      ? ` · ${f.textPreview[0].slice(0, 24)}`
                      : ''}
                  </small>
                </span>
                <span className="locate">↗</span>
              </button>
            ))}
          </div>
          <p className="sidebar-footer">
            点选列表定位 · 滚轮缩放 · 拖拽平移
            <br />
            编号为对比标记，不是原图图纸编号。
          </p>
        </aside>
        <main className="compare-main">
          <div className="drawing-title">
            <h2 title={drawing.name}>{drawing.name}</h2>
            <span>{file.size}</span>
          </div>
          <div className="compare-toolbar">
            <div className="mode-switch" role="group" aria-label="对比模式">
              {modes.map(([value, label]) => (
                <button
                  key={value}
                  aria-pressed={mode === value}
                  onClick={() => {
                    setMode(value)
                    setSelected(null)
                  }}
                >
                  {label}
                </button>
              ))}
            </div>
            <div className="view-controls">
              <select
                aria-label="图纸空间"
                disabled={!ready}
                value={space}
                onChange={(e) => selectSpace(e.target.value)}
              >
                {spaces.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
              <button
                disabled={!ready}
                onClick={() =>
                  zoom(drawing, union(newFrames.filter((f) => f.counted)))
                }
              >
                定位新图框
              </button>
              <button
                disabled={!ready || !bounded.length}
                onClick={() => zoom(drawing, union(bounded))}
              >
                全部边界
              </button>
              <label>
                <input
                  type="checkbox"
                  checked={labels}
                  onChange={(e) => setLabels(e.target.checked)}
                />
                标签
              </label>
            </div>
          </div>
          <div
            className="dwg-stage"
            ref={stage}
            data-ready={ready}
            data-render-status={
              isPartial ? 'partial' : ready ? 'ready' : 'loading'
            }
          >
            <DwgPreview file={file} />
            <svg ref={svg} className="frame-overlay" aria-label="解析图框边界">
              {ready &&
                bounded.map((f) => (
                  <g
                    key={f.id}
                    data-frame={f.id}
                    className={`${f.id.startsWith('before') ? 'old-boundary' : 'new-boundary'} ${selected === f.id ? 'highlight-boundary' : ''}`}
                  >
                    <polygon points="" />
                    {labels && <text>{f.label}</text>}
                  </g>
                ))}
            </svg>
            {ready && mode !== 'original' && (
              <div className="overlay-legend">
                {(mode === 'before' || mode === 'both') && (
                  <span>
                    <i className="old" />
                    旧版候选边界
                  </span>
                )}
                {(mode === 'after' || mode === 'both') && (
                  <span>
                    <i className="new" />
                    修正后边界
                  </span>
                )}
              </div>
            )}
          </div>
          <footer className="canvas-footer">
            <span>
              {isPartial
                ? '部分预览，可交互'
                : ready
                  ? '可交互'
                  : '正在准备预览'}{' '}
              · 10.0 → 10.1 · 同一原始 DWG
            </span>
            <span>
              {space === 'model' ? '模型空间' : '布局空间'} · {bounded.length}{' '}
              个可叠加边界
            </span>
          </footer>
          <div className="review-note">
            {drawing.id.startsWith('2c4')
              ? '核对重点：旋转主图框是否完整；切到布局空间可查看另行保留的指标表。修正后为 1 张图纸，另有 1 个待确定的布局内容容器。'
              : drawing.id.startsWith('278')
                ? '核对重点：旧版把空占位格和局部边框也列为候选。切换“修正前”，再点“全部边界”查看它们的位置。'
                : '核对重点：两张真实图纸边界是否完整；旧版候选范围受镜像块坐标影响。切换前后叠加，可直接观察范围差异。'}
          </div>
        </main>
      </div>
    </div>
  )
}
async function start() {
  await i18n.changeLanguage('zh-CN')
  const response = await fetch('/__dwg_review/manifest')
  if (!response.ok) throw new Error('无法读取图纸对比数据')
  const manifest: Manifest = await response.json()
  for (const drawing of manifest.drawings) {
    let number = 0
    drawing.after = drawing.after.map((frame) => ({
      ...frame,
      label: frame.counted
        ? `新 ${String(++number).padStart(3, '0')}`
        : '布局内容',
    }))
  }
  createRoot(document.getElementById('root')!).render(
    <Compare manifest={manifest} />,
  )
}
void start().catch((error) => {
  document.getElementById('root')!.textContent = String(error)
})
