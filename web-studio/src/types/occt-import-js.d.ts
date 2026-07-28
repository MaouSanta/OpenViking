declare module 'occt-import-js' {
  export interface OcctFace {
    first: number
    last: number
    color: [number, number, number] | null
  }

  export interface OcctMesh {
    name: string
    color?: [number, number, number]
    brep_faces?: Array<OcctFace>
    attributes: {
      position: { array: Array<number> }
      normal?: { array: Array<number> }
    }
    index: { array: Array<number> }
  }

  export interface OcctResult {
    success: boolean
    meshes: Array<OcctMesh>
  }

  export interface OcctModule {
    ReadStepFile: (
      content: Uint8Array,
      params: { linearUnit: 'millimeter' },
    ) => OcctResult
  }

  export default function createOcct(options?: {
    locateFile?: (path: string) => string
  }): Promise<OcctModule>
}
