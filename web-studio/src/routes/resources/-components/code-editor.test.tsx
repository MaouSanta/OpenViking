// @vitest-environment jsdom

import { render, waitFor } from '@testing-library/react'
import { beforeAll, describe, expect, it } from 'vitest'

import { CodeEditor } from './code-editor'

beforeAll(() => {
  Range.prototype.getClientRects = () => [] as unknown as DOMRectList
})

describe('CodeEditor', () => {
  it('keeps highlighted previews read-only', async () => {
    const { container, unmount } = render(
      <CodeEditor
        initialContent={'{"beam":"KL6"}'}
        filename="drawing_text.json"
        readOnly
      />,
    )

    await waitFor(() => {
      expect(
        container.querySelector('.cm-content')?.getAttribute('contenteditable'),
      ).toBe('false')
    })
    unmount()
  })
})
