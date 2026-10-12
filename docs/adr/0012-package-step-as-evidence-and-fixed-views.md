# Package STEP as evidence and fixed views

**Status:** accepted

A `.stp` or `.step` source ingested with `bundle=true` is sealed with source-only `openviking.step-evidence/v1`, six fixed orthographic OCCT raster views (`768×768` pixels at `72×72` DPI), ordinary `.abstract.md` and `.overview.md`, and the Bundle Manifest. Neighboring timeline data and benchmark-only SVG, prompt, compact-evidence, video, and ray-tracing artifacts are excluded so the Bundle depends only on its source; any required artifact or semantic failure aborts publication without retry.
