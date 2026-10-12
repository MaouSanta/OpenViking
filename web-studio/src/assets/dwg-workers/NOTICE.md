# CAD worker provenance and release gate

The files are copied unchanged from the worker dependencies used to build
`@mlightcad/cad-simple-viewer` 1.5.10:

- `libredwg-parser-worker.js`: `@mlightcad/libredwg-converter` 3.12.5,
  SHA-256 `71243d5bc391c98c18eb1dac9129e44c640a2b3f251de7762d71e5c083027cd7`.
  npm metadata declares `GPL-3.0` and
  https://github.com/mlightcad/realdwg-web, while the package's bundled
  `LICENSE` text says MIT. The worker contains GNU LibreDWG-derived code.
- `mtext-renderer-worker.js`: `@mlightcad/mtext-renderer` 0.12.4,
  SHA-256 `876d8bf6d9d821624190bc1e2232874ae3ef79ba8d3b512e02e9e6ccf30b30e6`,
  MIT, https://github.com/mlightcad/mtext-renderer.

Do not redistribute the DWG worker in a customer build until the conflicting
license metadata is resolved and the required corresponding source and license
materials are identified by the project's normal third-party review. Production
builds exclude this source directory unless `VITE_ENABLE_DWG_PREVIEW=true` is
set explicitly. Development mode enables it for the internal prototype. Keep
this notice with any internal prototype copy.
