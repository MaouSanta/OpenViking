import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useTheme } from 'next-themes'
import { Box, Grid3X3, Loader2, Scan, ZoomIn, ZoomOut } from 'lucide-react'
import {
  AmbientLight,
  Box3,
  BufferGeometry,
  Color,
  DirectionalLight,
  DoubleSide,
  Float32BufferAttribute,
  Group,
  LineBasicMaterial,
  LineSegments,
  Mesh,
  MeshPhongMaterial,
  PerspectiveCamera,
  Scene,
  Uint32BufferAttribute,
  Vector3,
  WebGLRenderer,
  WireframeGeometry,
} from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { ViewHelper } from 'three/addons/helpers/ViewHelper.js'
import occtWasmUrl from 'occt-import-js/dist/occt-import-js.wasm?url'
import type { Material, Object3D } from 'three'
import type { OcctMesh, OcctModule, OcctResult } from 'occt-import-js'

import { Button } from '#/components/ui/button'
import { getContentDownload } from '#/lib/ov-client'

import { formatSize } from '../-lib/normalize'
import type { VikingFsEntry } from '../-types/viking-fm'

export const MAX_STEP_PREVIEW_BYTES = 50 * 1024 * 1024

type DisplayMode = 'solid' | 'wireframe'
type LoadError = 'failed' | 'tooLarge'

export interface StepPreviewStats {
  dimensions: [number, number, number]
  meshCount: number
  triangleCount: number
}

interface StepModel extends StepPreviewStats {
  bounds: Box3
  solid: Group
  wireframe: Group
}

interface ViewerActions {
  fit: () => void
  render: () => void
  setMode: (mode: DisplayMode) => void
  viewHelper: ViewHelper
  zoom: (factor: number) => void
}

let occtPromise: Promise<OcctModule> | null = null

function loadOcct() {
  if (!occtPromise) {
    occtPromise = import('occt-import-js')
      .then(({ default: createOcct }) =>
        createOcct({
          locateFile: (path) => (path.endsWith('.wasm') ? occtWasmUrl : path),
        }),
      )
      .catch((error) => {
        occtPromise = null
        throw error
      })
  }
  return occtPromise
}

function solidMaterial(
  color?: [number, number, number] | null,
  fallback?: [number, number, number],
) {
  const resolved = [color, fallback].find(
    (candidate) => candidate && Math.max(...candidate) >= 0.02,
  )
  return new MeshPhongMaterial({
    color: resolved ? new Color(...resolved) : 0xb8bec7,
    shininess: 24,
    side: DoubleSide,
  })
}

function buildGeometry(mesh: OcctMesh): BufferGeometry {
  const geometry = new BufferGeometry()
  geometry.name = mesh.name
  geometry.setAttribute(
    'position',
    new Float32BufferAttribute(mesh.attributes.position.array, 3),
  )
  if (mesh.attributes.normal) {
    geometry.setAttribute(
      'normal',
      new Float32BufferAttribute(mesh.attributes.normal.array, 3),
    )
  } else {
    geometry.computeVertexNormals()
  }
  geometry.setIndex(new Uint32BufferAttribute(mesh.index.array, 1))
  return geometry
}

function addFaceMaterials(
  geometry: BufferGeometry,
  mesh: OcctMesh,
): MeshPhongMaterial | Array<MeshPhongMaterial> {
  const defaultMaterial = solidMaterial(mesh.color)
  const faces = mesh.brep_faces || []
  if (faces.length === 0) return defaultMaterial

  const materials = [
    defaultMaterial,
    ...faces.map((face) => solidMaterial(face.color, mesh.color)),
  ]
  const triangleCount = mesh.index.array.length / 3
  let triangle = 0
  faces.forEach((face, index) => {
    if (triangle < face.first) {
      geometry.addGroup(triangle * 3, (face.first - triangle) * 3, 0)
    }
    geometry.addGroup(
      face.first * 3,
      (face.last - face.first + 1) * 3,
      index + 1,
    )
    triangle = face.last + 1
  })
  if (triangle < triangleCount) {
    geometry.addGroup(triangle * 3, (triangleCount - triangle) * 3, 0)
  }
  return materials
}

export function buildStepModel(result: OcctResult): StepModel {
  if (!result.success || result.meshes.length === 0) {
    throw new Error('STEP import returned no meshes')
  }

  const solid = new Group()
  const wireframe = new Group()
  let triangleCount = 0

  result.meshes.forEach((source) => {
    const geometry = buildGeometry(source)
    const mesh = new Mesh(geometry, addFaceMaterials(geometry, source))
    mesh.name = source.name
    solid.add(mesh)

    wireframe.add(
      new LineSegments(
        new WireframeGeometry(geometry),
        new LineBasicMaterial({ color: 0x87909e }),
      ),
    )
    triangleCount += source.index.array.length / 3
  })

  const bounds = new Box3().setFromObject(solid)
  const size = bounds.getSize(new Vector3())
  return {
    bounds,
    dimensions: [size.x, size.y, size.z],
    meshCount: result.meshes.length,
    solid,
    triangleCount,
    wireframe,
  }
}

function disposeObject(object: Object3D) {
  object.traverse((child) => {
    if (!(child instanceof Mesh) && !(child instanceof LineSegments)) return
    child.geometry.dispose()
    const materials = Array.isArray(child.material)
      ? child.material
      : [child.material]
    materials.forEach((material: Material) => material.dispose())
  })
}

function formatDimension(value: number): string {
  return new Intl.NumberFormat(undefined, {
    maximumFractionDigits: 2,
  }).format(value)
}

function ToolButton({
  active = false,
  label,
  onClick,
  children,
}: {
  active?: boolean
  label: string
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <Button
      type="button"
      size="icon-sm"
      variant={active ? 'default' : 'ghost'}
      aria-label={label}
      aria-pressed={active || undefined}
      title={label}
      onClick={onClick}
    >
      {children}
    </Button>
  )
}

export function StepPreview({ file }: { file: VikingFsEntry }) {
  const { t } = useTranslation('resources')
  const { resolvedTheme } = useTheme()
  const mountRef = useRef<HTMLDivElement>(null)
  const viewerRef = useRef<ViewerActions | null>(null)
  const [error, setError] = useState<LoadError | null>(null)
  const [loading, setLoading] = useState(true)
  const [mode, setMode] = useState<DisplayMode>('solid')
  const [stats, setStats] = useState<
    (StepPreviewStats & { bytes: number }) | null
  >(null)

  useEffect(() => {
    const mount = mountRef.current
    if (!mount) return

    const abortController = new AbortController()
    const isAborted = () => abortController.signal.aborted
    let model: StepModel | null = null

    const renderer = new WebGLRenderer({ alpha: true, antialias: true })
    renderer.autoClear = false
    renderer.setClearColor(0x000000, 0)
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    mount.appendChild(renderer.domElement)

    const scene = new Scene()
    scene.add(new AmbientLight(0xffffff, 1.6))
    const light = new DirectionalLight(0xffffff, 2.2)
    light.position.set(1, -1, 2)
    scene.add(light)

    const camera = new PerspectiveCamera(45, 1, 0.01, 1000)
    camera.up.set(0, 1, 0)
    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = false
    controls.screenSpacePanning = true

    const viewHelper = new ViewHelper(camera, renderer.domElement)
    viewHelper.location.right = 8
    viewHelper.location.bottom = 4
    viewHelper.setLabels('X', 'Y', 'Z')
    viewHelper.setLabelStyle(
      '14px sans-serif',
      document.documentElement.classList.contains('dark')
        ? '#f8fafc'
        : '#111827',
      14,
    )

    const render = () => {
      renderer.clear()
      renderer.render(scene, camera)
      viewHelper.render(renderer)
    }
    controls.addEventListener('change', render)

    let viewAnimationFrame = 0
    let previousViewAnimationTime = 0
    const animateView = (time: number) => {
      const delta = previousViewAnimationTime
        ? (time - previousViewAnimationTime) / 1000
        : 0
      previousViewAnimationTime = time
      viewHelper.update(delta)
      render()
      if (viewHelper.animating) {
        viewAnimationFrame = requestAnimationFrame(animateView)
      } else {
        previousViewAnimationTime = 0
        viewAnimationFrame = 0
        controls.update()
      }
    }
    const handleViewClick = (event: PointerEvent) => {
      if (!viewHelper.handleClick(event)) return
      viewAnimationFrame = requestAnimationFrame(animateView)
    }
    renderer.domElement.addEventListener('pointerup', handleViewClick)

    const fit = () => {
      if (!model) return
      const center = model.bounds.getCenter(new Vector3())
      const size = model.bounds.getSize(new Vector3())
      const maxDimension = Math.max(size.x, size.y, size.z, 0.001)
      const distance =
        (maxDimension / (2 * Math.tan((camera.fov * Math.PI) / 360))) *
        (1.5 / Math.min(camera.aspect, 1))
      camera.near = maxDimension / 1000
      camera.far = Math.max(maxDimension * 100, distance * 10)
      camera.up.set(0, 1, 0)
      camera.position
        .copy(center)
        .add(new Vector3(1, -1, 0.75).normalize().multiplyScalar(distance))
      camera.updateProjectionMatrix()
      controls.target.copy(center)
      controls.minDistance = maxDimension * 0.01
      controls.maxDistance = maxDimension * 100
      controls.update()
      viewHelper.center.copy(center)
      render()
    }

    viewerRef.current = {
      fit,
      render,
      setMode: (nextMode) => {
        if (!model) return
        model.solid.visible = nextMode === 'solid'
        model.wireframe.visible = nextMode === 'wireframe'
        render()
      },
      viewHelper,
      zoom: (factor) => {
        camera.position
          .sub(controls.target)
          .multiplyScalar(factor)
          .add(controls.target)
        controls.update()
        render()
      },
    }

    const resizeObserver = new ResizeObserver(([entry]) => {
      const width = entry.contentRect.width
      const height = entry.contentRect.height
      if (width === 0 || height === 0) return
      renderer.setSize(width, height)
      camera.aspect = width / height
      camera.updateProjectionMatrix()
      render()
    })
    resizeObserver.observe(mount)

    const load = async () => {
      setError(null)
      setLoading(true)
      setMode('solid')
      setStats(null)
      try {
        if (
          file.sizeBytes !== null &&
          file.sizeBytes > MAX_STEP_PREVIEW_BYTES
        ) {
          setError('tooLarge')
          return
        }

        const response = await getContentDownload({
          query: { uri: file.uri },
          responseType: 'arraybuffer',
          signal: abortController.signal,
          throwOnError: true,
        })
        if (isAborted()) return
        const bytes = response.data as ArrayBuffer
        if (bytes.byteLength > MAX_STEP_PREVIEW_BYTES) {
          setError('tooLarge')
          return
        }

        const occt = await loadOcct()
        if (isAborted()) return
        await new Promise<void>((resolve) =>
          requestAnimationFrame(() => resolve()),
        )
        // ponytail: main-thread parsing is capped at 50 MiB; move it to a Worker only if measured stalls require it.
        const result = occt.ReadStepFile(new Uint8Array(bytes), {
          linearUnit: 'millimeter',
        })
        model = buildStepModel(result)
        if (isAborted()) {
          disposeObject(model.solid)
          disposeObject(model.wireframe)
          return
        }
        model.wireframe.visible = false
        scene.add(model.solid, model.wireframe)
        setStats({
          bytes: bytes.byteLength,
          dimensions: model.dimensions,
          meshCount: model.meshCount,
          triangleCount: model.triangleCount,
        })
        fit()
      } catch {
        if (!isAborted()) setError('failed')
      } finally {
        if (!isAborted()) setLoading(false)
      }
    }

    void load()
    return () => {
      abortController.abort()
      resizeObserver.disconnect()
      controls.removeEventListener('change', render)
      renderer.domElement.removeEventListener('pointerup', handleViewClick)
      cancelAnimationFrame(viewAnimationFrame)
      controls.dispose()
      viewHelper.dispose()
      viewerRef.current = null
      if (model) {
        scene.remove(model.solid, model.wireframe)
        disposeObject(model.solid)
        disposeObject(model.wireframe)
      }
      renderer.dispose()
      renderer.domElement.remove()
    }
  }, [file.sizeBytes, file.uri])

  useEffect(() => {
    const viewer = viewerRef.current
    if (!viewer) return
    viewer.viewHelper.setLabelStyle(
      '14px sans-serif',
      resolvedTheme === 'dark' ? '#f8fafc' : '#111827',
      14,
    )
    viewer.render()
  }, [resolvedTheme])

  const changeMode = (nextMode: DisplayMode) => {
    setMode(nextMode)
    viewerRef.current?.setMode(nextMode)
  }

  return (
    <div className="relative h-full min-h-[28rem] overflow-hidden rounded-xl border bg-slate-50 dark:bg-[#0b0b0b]">
      <div ref={mountRef} className="absolute inset-0" />

      {loading ? (
        <div className="absolute inset-0 flex items-center justify-center bg-background/60 text-sm text-muted-foreground backdrop-blur-sm">
          <Loader2 className="mr-2 size-4 animate-spin" />
          {t('filePreview.step.loading')}
        </div>
      ) : null}

      {error ? (
        <div
          role="alert"
          className="absolute inset-0 flex items-center justify-center px-6 text-center text-sm text-muted-foreground"
        >
          {t(`filePreview.step.${error}`)}
        </div>
      ) : null}

      {stats ? (
        <dl
          aria-label={t('filePreview.step.info')}
          className="absolute bottom-4 left-4 min-w-56 rounded-xl border bg-background/90 p-4 text-xs shadow-sm backdrop-blur"
        >
          <div className="mb-2 text-sm font-semibold">
            {t('filePreview.step.info')}
          </div>
          {[
            [
              t('filePreview.step.triangles'),
              stats.triangleCount.toLocaleString(),
            ],
            [t('filePreview.step.meshes'), stats.meshCount.toLocaleString()],
            [
              t('filePreview.step.dimensions'),
              `${stats.dimensions.map(formatDimension).join(' × ')} mm`,
            ],
            [t('filePreview.step.fileSize'), formatSize(stats.bytes)],
          ].map(([label, value]) => (
            <div key={label} className="flex justify-between gap-6 leading-6">
              <dt className="text-muted-foreground">{label}</dt>
              <dd className="font-medium tabular-nums">{value}</dd>
            </div>
          ))}
        </dl>
      ) : null}

      {stats ? (
        <div className="absolute right-4 top-1/2 flex -translate-y-1/2 flex-col gap-1 rounded-xl border bg-background/90 p-1.5 shadow-sm backdrop-blur">
          <ToolButton
            label={t('filePreview.step.zoomIn')}
            onClick={() => viewerRef.current?.zoom(0.8)}
          >
            <ZoomIn />
          </ToolButton>
          <ToolButton
            label={t('filePreview.step.zoomOut')}
            onClick={() => viewerRef.current?.zoom(1.25)}
          >
            <ZoomOut />
          </ToolButton>
          <ToolButton
            label={t('filePreview.step.fit')}
            onClick={() => viewerRef.current?.fit()}
          >
            <Scan />
          </ToolButton>
          <div className="my-1 border-t" />
          <ToolButton
            active={mode === 'solid'}
            label={t('filePreview.step.solid')}
            onClick={() => changeMode('solid')}
          >
            <Box />
          </ToolButton>
          <ToolButton
            active={mode === 'wireframe'}
            label={t('filePreview.step.wireframe')}
            onClick={() => changeMode('wireframe')}
          >
            <Grid3X3 />
          </ToolButton>
        </div>
      ) : null}
    </div>
  )
}
