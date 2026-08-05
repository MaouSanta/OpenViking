import { describe, expect, it } from 'vitest'

import {
  MAX_CAD_UPLOAD_FILE_SIZE_BYTES,
  MAX_UPLOAD_FILE_SIZE_BYTES,
  getUploadPolicy,
} from './upload'

describe('CAD upload policy', () => {
  it('sends DWG and STEP files through Bundle ingestion with the CAD-sized limit', () => {
    expect(getUploadPolicy('factory-plan.DWG')).toEqual({
      bundle: true,
      maxSizeBytes: MAX_CAD_UPLOAD_FILE_SIZE_BYTES,
    })
    expect(getUploadPolicy('housing.stp')).toEqual({
      bundle: true,
      maxSizeBytes: MAX_CAD_UPLOAD_FILE_SIZE_BYTES,
    })
    expect(getUploadPolicy('housing.STEP')).toEqual({
      bundle: true,
      maxSizeBytes: MAX_CAD_UPLOAD_FILE_SIZE_BYTES,
    })
    expect(getUploadPolicy('notes.pdf')).toEqual({
      bundle: false,
      maxSizeBytes: MAX_UPLOAD_FILE_SIZE_BYTES,
    })
  })
})
