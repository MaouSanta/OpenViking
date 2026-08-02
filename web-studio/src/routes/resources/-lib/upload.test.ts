import { describe, expect, it } from 'vitest'

import {
  MAX_DWG_UPLOAD_FILE_SIZE_BYTES,
  MAX_UPLOAD_FILE_SIZE_BYTES,
  getUploadPolicy,
} from './upload'

describe('DWG upload policy', () => {
  it('sends DWG files through Bundle ingestion with the corpus-sized limit', () => {
    expect(getUploadPolicy('factory-plan.DWG')).toEqual({
      bundle: true,
      maxSizeBytes: MAX_DWG_UPLOAD_FILE_SIZE_BYTES,
    })
    expect(getUploadPolicy('notes.pdf')).toEqual({
      bundle: false,
      maxSizeBytes: MAX_UPLOAD_FILE_SIZE_BYTES,
    })
  })
})
