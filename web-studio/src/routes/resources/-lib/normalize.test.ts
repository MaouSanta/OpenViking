import { describe, expect, it } from 'vitest'

import { detectFileType, shouldAutoRead } from './normalize'

describe('DWG preview routing', () => {
  it('routes DWG files to the drawing preview without reading them as text', () => {
    const entry = {
      isDir: false,
      uri: 'viking://resources/factory/floor-plan.DWG',
      sizeBytes: 1024,
    }

    expect(detectFileType(entry.uri)).toBe('drawing2d')
    expect(shouldAutoRead(entry)).toEqual({
      shouldRead: false,
      reason: 'binary',
    })
  })
})
