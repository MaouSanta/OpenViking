# Local DWG comparison

This review entry imports the production Web Studio `DwgPreview` component, including its OCS and manual-fit fixes. Parser borders are separate SVG overlays projected through the viewer's world-to-screen transform. Source DWG bytes are not edited.

Use Node 22 and run from `web-studio`:

```sh
npm ci
node script/prepare-dwg-comparison.mjs /path/to/review-data/manifest-template.json /path/to/AutoCAD
npx vite --config vite.dwg-compare.config.ts
```

The preparation script verifies each source DWG's SHA-256 and writes `.dwg-compare.local/manifest.json`, which is Git-ignored. The template and original drawing dataset are separate review inputs, not code dependencies or committed CAD files. The handoff package includes the template; supply your own matching AutoCAD dataset directory.

Open `http://localhost:3173/dwg-compare.html`. Alternatively set `DWG_COMPARISON_MANIFEST` to any prepared manifest. Source paths inside that manifest may be absolute or relative to the manifest's directory. If no manifest is present, the normal application and tests can still start; comparison-data requests return a clear 503 response.

The local middleware downloads only the exact source URIs in the manifest. No ingestion or model call is made. This is a separate local inspection entry, not a new production route.

The before overlay contains **pre-VLM candidates from parser 10.0**, regenerated from LibreDWG 0.13.4 JSON. The after overlay contains parser 10.1 results without model calls. The old CSV contains **post-selection counts only**, so its final accepted borders cannot be reconstructed from that CSV. The UI keeps these figures distinct. Model and paper coordinates are never overlaid in the same space.

“Fit view” includes all visible original geometry, while “Focus new frames” restricts the camera to recognized drawing sheets. In particular, the cable DWG contains four real TEXT objects around (-90260, -447870). When those are rendered, full-scene fit can legitimately make the two sheets appear extremely small; the smaller bounds recorded during partial rendering are not the final full-scene bounds.

Missing fonts/images/references and incomplete rendering remain visible through the original warnings. The interface font subset does not change CAD fonts or geometry. `UI_FONT_LICENSE.txt` retains the source font notices.
