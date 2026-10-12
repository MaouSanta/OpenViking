export type Box = [number, number, number, number]
export type Frame = {
  id: string
  label: string
  kind: string
  space: 'model' | 'paper'
  spaceHandle: number | null
  layoutName: string | null
  bbox: Box | null
  boundary: Array<[number, number]> | null
  textCount: number
  textPreview: Array<string>
  sourceKind: string
  sourceRef: { handle?: number; instance_path?: Array<number> } | null
  counted: boolean
}
export type Drawing = {
  id: string
  name: string
  uri: string
  sizeBytes: number
  sha256: string
  beforeCount: number
  csvCount: number
  afterCount: number
  bundleCount: number
  before: Array<Frame>
  after: Array<Frame>
}
export type Manifest = { drawings: Array<Drawing> }
