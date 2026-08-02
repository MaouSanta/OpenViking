# CAD worker provenance and release gate

The files are copied unchanged from the worker dependencies used to build
`@mlightcad/cad-simple-viewer` 1.5.9:

- `libredwg-parser-worker.js`: `@mlightcad/libredwg-converter` 3.12.2,
  SHA-256 `71243d5bc391c98c18eb1dac9129e44c640a2b3f251de7762d71e5c083027cd7`.
  npm metadata declares `GPL-3.0` and
  https://github.com/mlightcad/realdwg-web, while the package's bundled
  `LICENSE` text says MIT. The worker contains GNU LibreDWG-derived code.
- `mtext-renderer-worker.js`: `@mlightcad/mtext-renderer` 0.12.1,
  SHA-256 `ec6022bd6de1c6104b12c33dbd8da93fd1da831406c2f73259f02277ed70008d`,
  MIT, https://github.com/mlightcad/mtext-renderer.

Do not redistribute the DWG worker in a customer build until the conflicting
license metadata is resolved and the required corresponding source and license
materials are identified by the project's normal third-party review. Production
builds exclude this source directory unless `VITE_ENABLE_DWG_PREVIEW=true` is
set explicitly. Development mode enables it for the internal prototype. Keep
this notice with any internal prototype copy.
