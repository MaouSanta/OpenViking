import { AcGeBox2d } from '@mlightcad/data-model'

type FittableView = {
  readonly cadScene: {
    readonly box:
      | {
          min: { x: number; y: number }
          max: { x: number; y: number }
          isEmpty: () => boolean
        }
      | undefined
  }
  zoomTo: (box: AcGeBox2d, margin: number) => void
}

/** Fit a user-requested view to current rendered geometry, including partial draws. */
export function fitDwgView(view: FittableView): boolean {
  const box = view.cadScene.box
  if (!box || box.isEmpty()) return false
  const { min, max } = box
  if (![min.x, min.y, max.x, max.y].every(Number.isFinite)) return false
  // Upstream zoomToFitDrawing uses the automatic-open controller. It can wait
  // indefinitely or ignore the request after the user has panned/zoomed.
  // These public APIs use the same visible geometry without that auto-fit state.
  view.zoomTo(
    new AcGeBox2d({ x: min.x, y: min.y }, { x: max.x, y: max.y }),
    1.1,
  )
  return true
}
