import { createHash } from 'node:crypto'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, isAbsolute, resolve } from 'node:path'

const [
  templatePath,
  drawingRoot,
  destination = '.dwg-compare.local/manifest.json',
] = process.argv.slice(2)
if (!templatePath || !drawingRoot) {
  throw new Error(
    'Usage: node script/prepare-dwg-comparison.mjs <manifest-template.json> <AutoCAD-root> [output-manifest.json]',
  )
}
const manifest = JSON.parse(await readFile(resolve(templatePath), 'utf8'))
for (const drawing of manifest.drawings) {
  if (isAbsolute(drawing.source))
    throw new Error(`Template source must be relative: ${drawing.name}`)
  const source = resolve(drawingRoot, drawing.source)
  const bytes = await readFile(source)
  if (createHash('sha256').update(bytes).digest('hex') !== drawing.sha256) {
    throw new Error(`DWG does not match the comparison data: ${drawing.name}`)
  }
  drawing.source = source
  drawing.sizeBytes = bytes.length
}
const output = resolve(destination)
await mkdir(dirname(output), { recursive: true })
await writeFile(output, JSON.stringify(manifest, null, 2) + '\n')
console.log(`Prepared ${manifest.drawings.length} drawings: ${output}`)
