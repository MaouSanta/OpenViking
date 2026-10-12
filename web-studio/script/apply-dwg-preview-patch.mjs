import { readFile, rename, rm, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'

// cad-simple-viewer 1.5.9 bundles its DWG converter. Preserve plane metadata
// here; src/lib/cad/dwg-polyline-ocs.ts applies it at draw/extent time.
// Version and exact-source checks prevent silently patching a different release.
const packageRoot = new URL(
  '../node_modules/@mlightcad/cad-simple-viewer/',
  import.meta.url,
)
const info = JSON.parse(
  await readFile(new URL('package.json', packageRoot), 'utf8'),
)
if (info.version !== '1.5.9') {
  throw new Error(
    `Review the DWG polyline compatibility fix for cad-simple-viewer ${info.version}`,
  )
}
const file = new URL('dist/cad-simple-viewer.js', packageRoot)
let resolveFrom = createRequire(import.meta.url)
for (const [name, version] of [
  ['data-model', '1.12.2'],
  ['geometry-engine', '3.12.2'],
]) {
  // Resolve from the dependency itself so pnpm's non-hoisted layout works too.
  const entry = resolveFrom.resolve(`@mlightcad/${name}`)
  const dependency = JSON.parse(
    await readFile(new URL('../package.json', pathToFileURL(entry)), 'utf8'),
  )
  if (dependency.version !== version)
    throw new Error(
      `Review the DWG OCS adapter for ${name} ${dependency.version}`,
    )
  resolveFrom = createRequire(entry)
}
const before = `}, r.prototype.convertLWPolyline = function(e) {
      var t, n = new et();
      n.closed = !!(e.flag & 512);`
const after = `}, r.prototype.convertLWPolyline = function(e) {
      var t, n = new et();
      // OpenViking: retain native LWPOLYLINE OCS fields for the preview adapter.
      n.elevation = (e.flag & 8) ? e.elevation : 0;
      n.thickness = (e.flag & 2) ? e.thickness : 0;
      if ((e.flag & 1) && e.extrusionDirection) n.normal = e.extrusionDirection;
      n.closed = !!(e.flag & 512);`
const source = await readFile(file, 'utf8')
if (source.includes(after)) {
  console.log('DWG preview converter patch already applied')
} else {
  if (source.split(before).length !== 2) {
    throw new Error(`Unexpected DWG converter source: ${fileURLToPath(file)}`)
  }
  // Atomic replacement also breaks pnpm's hard link to its content store.
  const temporary = new URL(`dist/.dwg-ocs-${process.pid}.tmp`, packageRoot)
  try {
    await writeFile(temporary, source.replace(before, after))
    await rename(temporary, file)
  } finally {
    await rm(temporary, { force: true })
  }
  console.log('Applied DWG preview converter OCS metadata patch')
}
