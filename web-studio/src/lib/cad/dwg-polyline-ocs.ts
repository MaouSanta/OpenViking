import { AcDbPolyline, AcGeMatrix3d, AcGeVector3d } from '@mlightcad/data-model'
import type { AcGeBox3d, AcGiRenderer } from '@mlightcad/data-model'

const installed = Symbol.for('openviking.dwg-polyline-ocs.v1')

function ocsMatrix(polyline: AcDbPolyline, elevation = 0): AcGeMatrix3d {
  const { x, y, z } = polyline.normal
  const length = Math.hypot(x, y, z)
  if (!Number.isFinite(length) || length === 0 || !Number.isFinite(elevation)) {
    throw new Error('Invalid DWG polyline plane')
  }
  const normal = new AcGeVector3d(x / length, y / length, z / length)
  const reference =
    Math.abs(normal.x) < 1 / 64 && Math.abs(normal.y) < 1 / 64
      ? new AcGeVector3d(0, 1, 0)
      : new AcGeVector3d(0, 0, 1)
  const axisX = reference.cross(normal).normalize()
  const axisY = normal.clone().cross(axisX)
  // Basis vectors must be columns (OCS -> owner coordinates). Do not use
  // setFromExtrusionDirection in geometry-engine 3.12.2: it writes rows.
  return new AcGeMatrix3d()
    .makeBasis(axisX, axisY, normal)
    .setPosition(
      normal.x * elevation,
      normal.y * elevation,
      normal.z * elevation,
    )
}

function isDefaultPlane(polyline: AcDbPolyline) {
  const { x, y, z } = polyline.normal
  return x === 0 && y === 0 && Number.isFinite(z) && z > 0
}

/**
 * Compatibility fix for the pinned data-model 1.12.2, installed before a DWG
 * is opened. Source vertices/plane fields remain native; only the transient
 * drawable and its extent are transformed. INSERT then applies its own matrix.
 * Remove together with the converter postinstall patch when upgrading upstream.
 */
export function installDwgPolylineOcsFix() {
  const prototype = AcDbPolyline.prototype
  if (Reflect.get(prototype, installed)) return
  const draw = prototype.subWorldDraw
  const extent = Object.getOwnPropertyDescriptor(prototype, 'geometricExtents')
  const getExtent = extent?.get as
    | ((this: AcDbPolyline) => AcGeBox3d)
    | undefined
  if (!getExtent)
    throw new Error('Unsupported DWG polyline extent implementation')

  prototype.subWorldDraw = function (renderer: AcGiRenderer) {
    if (isDefaultPlane(this) && this.elevation === 0) {
      return draw.call(this, renderer)
    }
    const placement = ocsMatrix(this, this.elevation)
    // Upstream area() (wide polylines) is planar at z=0, while lines() applies
    // elevation. Draw both on a temporary zero-elevation view, then place once.
    // In 1.12.2 these methods only read the shared geometry; no vertex is edited.
    const planar = Object.create(this) as AcDbPolyline
    Object.defineProperty(planar, 'elevation', { value: 0 })
    const drawable = draw.call(planar, renderer)
    drawable.applyMatrix(placement)
    return drawable
  }
  Object.defineProperty(prototype, 'geometricExtents', {
    ...extent,
    get(this: AcDbPolyline) {
      const box = getExtent.call(this)
      if (box.isEmpty() || isDefaultPlane(this)) return box
      // The upstream local extent already includes elevation.
      return box.clone().applyMatrix4(ocsMatrix(this))
    },
  })
  Object.defineProperty(prototype, installed, { value: true })
}
