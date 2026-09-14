import { describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import type * as DataModel from '@mlightcad/data-model'
import {
  AcDbPolyline,
  AcGeBox3d,
  AcGeMatrix3d,
  AcGePoint2d,
  AcGePoint3d,
} from '@mlightcad/data-model'
import type {
  AcGeArea2d,
  AcGePoint3dLike,
  AcGiEntity,
  AcGiRenderer,
} from '@mlightcad/data-model'
import { installDwgPolylineOcsFix } from './dwg-polyline-ocs'
import { fitDwgView } from './dwg-fit-view'

// Exercise the actual published implementation. Its ESM build has directory
// imports requiring a browser bundler; the CJS build is directly runnable in Node.
vi.mock('@mlightcad/data-model', async () => {
  const { createRequire } = await import('node:module')
  return createRequire(import.meta.url)(
    '@mlightcad/data-model',
  ) as typeof DataModel
})

installDwgPolylineOcsFix()

describe('explicit DWG fit', () => {
  it('immediately fits visible geometry after a manual zoom, even during a partial draw', () => {
    const box = new AcGeBox3d(
      { x: 7336, y: -3507, z: 0 },
      { x: 10616, y: -2437, z: 0 },
    )
    const zoomTo = vi.fn()
    const zoomToFitDrawing = vi.fn(() => {
      throw new Error('Must not use automatic-fit state')
    })
    const view = {
      cadScene: { box },
      isProcessingEntities: true,
      zoomTo,
      zoomToFitDrawing,
    }
    expect(fitDwgView(view)).toBe(true)
    expect(zoomTo).toHaveBeenCalledOnce()
    const [bounds, margin] = zoomTo.mock.calls[0]
    expect([bounds.min.x, bounds.min.y, bounds.max.x, bounds.max.y]).toEqual([
      7336, -3507, 10616, -2437,
    ])
    expect(margin).toBe(1.1)
    expect(zoomToFitDrawing).not.toHaveBeenCalled()
  })

  it.each([
    undefined,
    new AcGeBox3d(),
    new AcGeBox3d({ x: NaN, y: 0, z: 0 }, { x: 10, y: 10, z: 0 }),
  ])('does not move the camera for absent or invalid geometry %#', (box) => {
    const zoomTo = vi.fn()
    expect(fitDwgView({ cadScene: { box }, zoomTo })).toBe(false)
    expect(zoomTo).not.toHaveBeenCalled()
  })
})

// The converter is private inside the published viewer bundle. Execute its
// actual installed method with the real data-model constructors, not a mock
// reimplementation, to catch metadata lost between the worker and the renderer.
const bundle = readFileSync(
  new URL(
    '../../../node_modules/@mlightcad/cad-simple-viewer/dist/cad-simple-viewer.js',
    import.meta.url,
  ),
  'utf8',
)
const prefix = 'r.prototype.convertLWPolyline = '
const start = bundle.indexOf(prefix)
const end = bundle.indexOf('}, r.prototype.convertPolyline2d', start)
if (start < 0 || end < 0) throw new Error('Unsupported bundled DWG converter')
const convert = runInNewContext(
  `(${bundle.slice(start + prefix.length, end + 1)})`,
  {
    et: AcDbPolyline,
    q: AcGePoint2d, // cad-simple-viewer 1.5.9
    X: AcGePoint2d, // cad-simple-viewer 1.5.10
  },
) as (value: Record<string, unknown>) => AcDbPolyline

function polyline(
  normal: number[],
  elevation: number,
  vertices: number[][],
  width = 0,
  bulge = 0,
) {
  const entity = new AcDbPolyline()
  entity.normal = { x: normal[0], y: normal[1], z: normal[2] }
  entity.elevation = elevation
  vertices.forEach(([x, y], index) =>
    entity.addVertexAt(
      index,
      new AcGePoint2d(x, y),
      index === 0 ? bulge : 0,
      width,
      width,
    ),
  )
  return entity
}
function draw(entity: AcDbPolyline) {
  let points: AcGePoint3d[] = []
  let areaUsed = false
  let transforms = 0
  const drawable = {
    applyMatrix(matrix: AcGeMatrix3d) {
      transforms++
      points = points.map((point) => point.clone().applyMatrix4(matrix))
    },
  } as AcGiEntity
  const renderer = {
    subEntityTraits: {},
    lines(value: AcGePoint3dLike[]) {
      points = value.map((p) => new AcGePoint3d(p))
      return drawable
    },
    area(value: AcGeArea2d) {
      areaUsed = true
      points = value
        .getPoints(100)
        .flat()
        .map((p) => new AcGePoint3d(p.x, p.y, 0))
      return drawable
    },
  } as AcGiRenderer
  const result = entity.subWorldDraw(renderer)
  return {
    result,
    get points() {
      return points
    },
    areaUsed,
    transforms,
  }
}
function expectPoint(
  point: { x: number; y: number; z: number },
  expected: number[],
) {
  for (const [index, value] of [point.x, point.y, point.z].entries())
    expect(value).toBeCloseTo(expected[index], 6)
}

describe('DWG preview polyline OCS compatibility', () => {
  it('retains the default normal when LibreDWG omits extrusion (closed bit 512)', () => {
    const entity = convert({
      flag: 512,
      extrusionDirection: { x: 0, y: 0, z: 0 },
      elevation: 99,
      thickness: 88,
      vertices: [
        { x: 0, y: 0 },
        { x: 10, y: 0 },
        { x: 10, y: 10 },
      ],
    })
    expectPoint(entity.normal, [0, 0, 1])
    expect(entity.elevation).toBe(0)
    expect(entity.thickness).toBe(0)
    expect(entity.closed).toBe(true)
    expect(draw(entity).points.length).toBeGreaterThan(2)
  })

  it('preserves explicit plane metadata through the actual bundled converter', () => {
    const entity = convert({
      flag: 1 | 8 | 2,
      extrusionDirection: { x: 0, y: 1, z: 0 },
      elevation: 339197.30731997447,
      thickness: 2,
      vertices: [
        { x: -305132.4493500941, y: 0 },
        { x: -305180.189350094, y: 0 },
      ],
    })
    expect(entity.closed).toBe(false)
    expect(entity.thickness).toBe(2)
    expectPoint(
      draw(entity).points[0],
      [305132.4493500941, 339197.30731997447, 0],
    )
  })
  it('places total-plan annotation at its true world coordinates', () => {
    const entity = polyline([0, 1, 0], 339197.30731997447, [
      [-305132.4493500941, 0],
      [-305180.189350094, 0],
    ])
    const result = draw(entity)
    expectPoint(result.points[0], [305132.4493500941, 339197.30731997447, 0])
    expectPoint(
      result.points.at(-1)!,
      [305180.189350094, 339197.30731997447, 0],
    )
    expectPoint(
      entity.geometricExtents.min,
      [305132.4493500941, 339197.30731997447, 0],
    )
    expectPoint(
      entity.geometricExtents.max,
      [305180.189350094, 339197.30731997447, 0],
    )
  })

  it('places the cable annotation using its oblique plane and negative elevation', () => {
    const result = draw(
      polyline([-0.49076970482818, 0.87128933014405, 0], -7377.056398220011, [
        [-7789.645503010252, 0],
        [-7837.385503010242, 0],
      ]),
    )
    expectPoint(result.points[0], [10407.470803432652, -2604.6285034114826, 0])
    expectPoint(
      result.points.at(-1)!,
      [10449.066156053721, -2581.19915770299, 0],
    )
  })

  // Golden values come from ezdxf OCS.to_wcs, independently of the JS library.
  it.each([
    [
      [0, 0, -1],
      [-3, 4, -5],
    ],
    [
      [1, 2, 3],
      [-2.7812496946388983, 1.1457045432215727, 6.399375847355152],
    ],
    [
      [0.001, 0.002, 1],
      [3.0049904875251716, 4.009991975032094, 4.9889875255467855],
    ],
  ])('uses a right-handed basis for normal %j', (normal, expected) => {
    const result = draw(
      polyline(normal, 5, [
        [3, 4],
        [4, 4],
      ]),
    )
    expectPoint(result.points[0], expected)
  })

  it('keeps the source vertices and plane unchanged across repeated rendering', () => {
    const entity = polyline([0, 1, 0], 20, [
      [3, 4],
      [5, 6],
    ])
    const before = JSON.stringify({
      point: entity.getPoint2dAt(0),
      normal: entity.normal,
      elevation: entity.elevation,
    })
    const first = draw(entity)
    const second = draw(entity)
    expect(second.points).toEqual(first.points)
    expect(
      JSON.stringify({
        point: entity.getPoint2dAt(0),
        normal: entity.normal,
        elevation: entity.elevation,
      }),
    ).toBe(before)
  })

  it('preserves the default XY fast path', () => {
    const result = draw(
      polyline([0, 0, 1], 0, [
        [3, 4],
        [5, 6],
      ]),
    )
    expect(result.transforms).toBe(0)
    expectPoint(result.points[0], [3, 4, 0])
  })

  it.each([
    [0, 0, 1],
    [0, 1, 0],
  ])(
    'places filled wide polylines at their elevation for normal %j',
    (...normal) => {
      const result = draw(
        polyline(
          normal,
          20,
          [
            [0, 0],
            [10, 0],
          ],
          2,
        ),
      )
      expect(result.areaUsed).toBe(true)
      const box = new AcGeBox3d().setFromPoints(result.points)
      if (normal[2] === 1) {
        expectPoint(box.min, [0, -1, 20])
        expectPoint(box.max, [10, 1, 20])
      } else {
        expectPoint(box.min, [-10, 20, -1])
        expectPoint(box.max, [0, 20, 1])
      }
    },
  )

  it('retains curved bulge geometry instead of replacing it with a chord', () => {
    const result = draw(
      polyline(
        [0, 1, 0],
        7,
        [
          [0, 0],
          [10, 0],
        ],
        0,
        1,
      ),
    )
    expect(result.points.length).toBeGreaterThan(5)
    expect(result.points.every((p) => Math.abs(p.y - 7) < 1e-9)).toBe(true)
    expect(
      Math.max(...result.points.map((p) => Math.abs(p.z))),
    ).toBeGreaterThan(4.9)
  })

  it('composes once with a later INSERT scale and translation', () => {
    const result = draw(
      polyline([0, 1, 0], 7, [
        [3, 4],
        [5, 6],
      ]),
    )
    result.result.applyMatrix(
      new AcGeMatrix3d()
        .makeTranslation(100, 200, 300)
        .multiply(new AcGeMatrix3d().makeScale(2, 3, 4)),
    )
    expectPoint(result.points[0], [94, 221, 316])
  })

  it('does not apply the fix twice when the preview is mounted again', () => {
    const method = AcDbPolyline.prototype.subWorldDraw
    const getter = Object.getOwnPropertyDescriptor(
      AcDbPolyline.prototype,
      'geometricExtents',
    )!.get
    installDwgPolylineOcsFix()
    expect(AcDbPolyline.prototype.subWorldDraw).toBe(method)
    expect(
      Object.getOwnPropertyDescriptor(
        AcDbPolyline.prototype,
        'geometricExtents',
      )!.get,
    ).toBe(getter)
  })
})
