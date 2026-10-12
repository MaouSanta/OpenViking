import { describe, expect, it } from 'vitest'
import type { Mesh, MeshPhongMaterial } from 'three'
import type { OcctResult } from 'occt-import-js'

import { detectFileType } from '../-lib/normalize'
import { buildStepModel } from './step-preview'

describe('STEP preview', () => {
  it('routes STEP files and derives preview geometry statistics', () => {
    const result: OcctResult = {
      success: true,
      meshes: [
        {
          name: 'part',
          color: [1, 0, 0],
          brep_faces: [{ first: 0, last: 0, color: [0, 1, 0] }],
          attributes: {
            position: { array: [0, 0, 0, 2, 0, 0, 0, 3, 4] },
          },
          index: { array: [0, 1, 2] },
        },
      ],
    }

    const model = buildStepModel(result)

    expect(detectFileType('viking://resources/part.stp')).toBe('model3d')
    expect(model.dimensions).toEqual([2, 3, 4])
    expect(model.meshCount).toBe(1)
    expect(model.triangleCount).toBe(1)
    expect(model.solid.children).toHaveLength(1)
    expect(model.wireframe.children).toHaveLength(1)
  })

  it('uses neutral gray for OCCT no-color black', () => {
    const result: OcctResult = {
      success: true,
      meshes: [
        {
          name: 'uncolored',
          color: [0, 0, 0],
          brep_faces: [],
          attributes: {
            position: { array: [0, 0, 0, 1, 0, 0, 0, 1, 0] },
          },
          index: { array: [0, 1, 2] },
        },
      ],
    }

    const model = buildStepModel(result)
    const mesh = model.solid.children[0] as Mesh

    expect((mesh.material as MeshPhongMaterial).color.getHex()).toBe(0xb8bec7)
  })

  it('ignores near-black face ID colors', () => {
    const result: OcctResult = {
      success: true,
      meshes: [
        {
          name: 'face-ids',
          color: [0.35, 0.35, 0.35],
          brep_faces: [{ first: 0, last: 0, color: [0.0015, 0, 0] }],
          attributes: {
            position: { array: [0, 0, 0, 1, 0, 0, 0, 1, 0] },
          },
          index: { array: [0, 1, 2] },
        },
      ],
    }

    const model = buildStepModel(result)
    const mesh = model.solid.children[0] as Mesh
    const materials = mesh.material as Array<MeshPhongMaterial>

    expect(materials[1].color.getHex()).toBe(materials[0].color.getHex())
  })
})
