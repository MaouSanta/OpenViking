import { describe, expect, it } from 'vitest'

import { beautifyJson } from './normalize'

describe('beautifyJson', () => {
  it('formats valid JSON and leaves invalid JSON untouched', () => {
    expect(beautifyJson('{"beam":{"length":4200}}')).toBe(
      '{\n  "beam": {\n    "length": 4200\n  }\n}',
    )
    expect(beautifyJson('{broken')).toBe('{broken')
  })
})
