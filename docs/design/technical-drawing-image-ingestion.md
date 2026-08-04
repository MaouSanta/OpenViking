# Technical drawing image ingestion

**Status:** implemented; pilot-ready
**Date:** 2026-08-03

## Problem

OpenViking currently handles a large raster image by creating a low-resolution preview and an eager fixed grid of tiles. That is useful for photographs whose content is spatially continuous, but it cuts mechanical drawing views and their dimensions at arbitrary boundaries. A technical drawing instead needs semantic regions that preserve the visual unit a human drafter intended.

This design concerns raster technical drawings uploaded by a user. It does not replace deterministic DWG or STEP parsing.

## Evidence

### Local experiments

- On the representative TechMB drawing, a whole-sheet VLM pass found the principal views, detail views, title block, and note area.
- A live smoke test against the current configured `doubao-seed-2-0-pro-260215` returned six valid-looking boxes for a `2056 x 1448` sheet, but ignored an explicit source-pixel instruction and emitted `0..1000`-style coordinates. Treating them as source pixels silently selected the wrong areas; deterministic normalized-to-pixel conversion aligned all six regions.
- On the dense reference drawing, the same open-region approach found 17 semantic regions. Eleven bbox pairs overlapped, showing that rectangular view crops cannot establish pixel ownership by themselves.
- For the tested local fact questions, a raw padded crop answered `3/3`, while a whole image with a box and a spotlighted image each answered `2/3`.
- On a real overlapping section region, both a raw crop and a targeted mask answered `3/3`; masking provided no measured benefit.
- The earlier DWG validation found that a coordinate-selected local crop could answer facts that the whole drawing could not, while eager image tiling did not reduce per-image prompt tokens on the tested model.

These are pilot results, not population estimates. They justify a minimal semantic-crop implementation and a larger validation pass; they do not justify a learned detector or destructive masking.

### Papers

| Paper | Useful result | Boundary for OpenViking |
| --- | --- | --- |
| [Khan et al., 2510.21862](https://arxiv.org/abs/2510.21862) | Detect high-level `View`, `Title Block`, and `Notes` regions before parsing annotations inside each view. | Directly supports semantic view-first processing, but reports no bbox mAP/IoU, no end-to-end comparison, no mask, and no overlap ownership. |
| [Khan et al., 2506.17374](https://arxiv.org/abs/2506.17374) | Detect nine annotation categories with oriented boxes and parse each patch into schema-constrained JSON. | Useful only after view partitioning; the paper explicitly leaves view–annotation linking to future work. |
| [Zhang et al., 2212.00290](https://arxiv.org/abs/2212.00290) | Vectorize raster strokes into a graph and classify contour, text, and dimension components. | Does not group view instances and is weakest at intersections, so it cannot safely erase neighboring drawing content. |

The papers support hierarchical localization, not OCR-only segmentation, fixed tiles, or pixel deletion.

## Confirmed V1 boundary

`Drawing Recognition` means:

1. the caller explicitly declares that the input image is a Technical Drawing Sheet;
2. OpenViking identifies Semantic Drawing Regions;
3. OpenViking retains the complete sheet and creates source-preserving Drawing View Crops for semantic indexing;
4. the source and crops are published as one Technical Drawing Bundle.

V1 deliberately excludes:

- automatic classification of ordinary images as technical drawings;
- exact structured extraction of dimensions, GD&T, threads, tolerances, or material;
- learned YOLO, Donut, Florence, or graph models;
- default white-out, spotlight, or pixel masks;
- fixed-grid tiles as the primary drawing representation.
- a duplicate whole-sheet preview;
- a separate drawing-region sidecar.

The intended existing API shape is:

```python
add_resource(
    path="drawing.png",
    bundle=True,
    args={"image_mode": "technical_drawing"},
)
```

No separate drawing-upload endpoint or new top-level `add_resource` parameter is needed.

Both switches are explicit. `bundle=true` without the image mode retains the source-only image Bundle behavior. `image_mode=technical_drawing` without `bundle=true` is rejected instead of silently changing packaging or degrading to ordinary image ingestion.

V1 accepts one raster image per ingestion request. Base64 is decoded at the upload boundary before the resulting named image enters this contract. Directory and archive batch ingestion are not included.

The unchanged source image is also the whole-sheet visual artifact. Region crops are separate image artifacts. Recognition status and each crop's structured source linkage belong in the Bundle Manifest rather than in a duplicate `drawing_regions.json` file. Optional Manifest fields remain backward-compatible within `openviking.resource-bundle/v2`; a new manifest version is not required.

### Region vocabulary

Each accepted region has exactly one of six V1 types: `view`, `section`, `detail`, `isometric`, `title_block`, or `notes`. `view` is deliberately orientation-neutral; the recognizer does not guess front, top, left, or right views. An optional short label may preserve visible identifiers such as `DETAIL A` or `SECTION B-B`.

Regions are independent and may overlap. V1 does not create parent/child links between a detail, its callout, and an enclosing view; those relations are not required to generate useful crops and are unreliable to infer from the pilot evidence.

All valid overlapping regions are retained. V1 performs no NMS or approximate-box merging and stores no pairwise `overlaps` metadata because the source-pixel boxes already preserve that relationship. It removes only exact duplicates with identical `type`, normalized `label`, and raw `bbox`.

### Crop coordinates

The private VLM response contract uses integer `[x1, y1, x2, y2]` values normalized to `0..1000`. The current configured Doubao Seed model was observed to behave according to this convention even when prompted for source pixels; this does not claim that the provider exposes or guarantees an internal coordinate system. OpenViking therefore makes `normalized_1000` its own explicit prompt contract. The program immediately maps `x` by the source width and `y` by the source height; it never guesses the coordinate system from returned values.

The Bundle Manifest exposes only the converted original-image pixel box. Its origin is the top-left corner, `x` increases rightward, `y` increases downward, and the lower-right edge is exclusive. An Agent therefore never sees or translates normalized coordinates.

Before cropping, the program expands every side by `2%` of the sheet's shorter edge and clamps the result to the image bounds. The padded box stored in the Manifest is the exact source rectangle used to create the crop. It is not shrunk, masked, or trimmed to avoid another region.

After exact duplicate removal, one recognition result contains at most 32 regions. The prompt states this limit. More than 32 regions makes the recognition output invalid: the source-only Bundle is still published with failed recognition, but OpenViking neither truncates an order-dependent response nor retries it.

### Bundle-aware parser scope

Technical drawings become the third explicit bundle-aware assembly branch, alongside DWG and STEP. Other Resource Kinds retain the existing source-only Bundle fallback. This iteration does not run every registered Parser during Bundle ingestion.

General Parser enrichment is deferred until multiple formats require it. At that point a Parser must opt in with an explicit staging root and artifact role/path/schema contract; Bundle assembly must not infer a sealed inventory from arbitrary Parser output.

### Lifecycle

V1 treats each uploaded Technical Drawing Bundle as an immutable snapshot. Agents may discover the Bundle and inspect it through `find`, `grep`, `read`, `ls`, or `download`, but `watch_interval` scheduled re-ingestion is not supported. Uploaded images have no stable re-readable source, and immutable Bundle refresh would require a separate revision and alias model.

### Recognition failure

A valid Technical Drawing Sheet remains a successfully ingested resource when semantic region recognition fails or returns invalid output. The resource records an explicit failed recognition status and reason, creates no fabricated regions or crops, and does not silently fall back to fixed-grid tiles. Source preservation and fallible semantic enrichment therefore have separate outcomes.

### Recognition engine

V1 uses the VLM configured by `get_openviking_config().vlm`, which in the intended deployment is a Doubao Seed-series model. The feature does not hard-code a model ID, provider, endpoint, or credential, so it follows the same model configuration as the rest of OpenViking.

Recognition runs once on the complete sheet during Bundle staging, after the source image is written and before the Manifest is sealed. A dedicated semantic helper requests strict region JSON, validates it, and creates deterministic crops. It does not place an LLM call inside `ImageParser`, add a second queue, or introduce an OCR/CV detector. The existing Bundle summarization then sees the source and all accepted crops in one staged snapshot.

Bundle summarization uses a compact L1 contract: a whole-drawing brief, exactly one concise navigation entry for every source or crop, and a small number of topic-based detailed sections. It does not create one long H3 section per artifact. The existing 4000-character limit remains a final guard; if generation omits an artifact or would be truncated, semantic assembly fails and the recognized Bundle is not published.

The feature makes one recognition-method invocation and never retries a failed or invalid semantic result. Lower-level transport retries, if any, remain governed by the already configured VLM backend; this feature does not mutate that shared policy.

## Confirmed V1 contract

### Stored artifacts and retrieval identity

A successfully recognized Bundle is flat because the current indexer skips subdirectories:

```text
bundle/
├── <unchanged-source>.jpg
├── region_001.png
├── region_002.png
├── .abstract.md
├── .overview.md
└── manifest.json
```

Every crop is encoded losslessly as PNG. Accepted regions are sorted by `(top, left, bottom, right, type, label)` before deterministic numbering; model response order never becomes artifact identity. There is no preview, region sidecar, nested `regions/` directory, or persisted raw VLM response.

`kind` remains the normalized source format such as `jpg` or `png`. Indexing adds the system-owned tags `kind=<format>` and `image_mode=technical_drawing` to the Bundle and its indexed children, so technical drawings can be filtered across raster formats without redefining Resource Kind.

### Manifest projection

The existing `openviking.resource-bundle/v2` Manifest gains one optional top-level object and optional artifact metadata:

```json
{
  "recognition": {
    "schema": "openviking.technical-drawing-regions/v1",
    "status": "complete",
    "source_width": 3308,
    "source_height": 2339
  },
  "artifacts": {
    "region_001": {
      "path": "region_001.png",
      "schema": "png",
      "sha256": "...",
      "metadata": {
        "type": "section",
        "label": "SECTION B-B",
        "bbox": [120, 240, 980, 1640]
      }
    }
  }
}
```

`type` and the final padded source-pixel `bbox` are required for a region artifact. `label` is omitted unless the sheet visibly provides one. Region count, IDs, normalized boxes, overlap, parent links, confidence, provider, and model response are not duplicated into the Manifest. Actual model/provider usage remains observable through existing telemetry; recording configured provenance could be wrong when VLM failover occurs.

A recognition failure stores only `schema`, `status: "failed"`, and one bounded stable `reason`: `vlm_unavailable`, `model_error`, `invalid_response`, or `no_regions`. It contains no region artifacts and no raw exception text.

### Final failure boundary

- An unreadable or unsupported raster is an invalid input and fails the request.
- VLM call, response, schema, bbox, zero-region, or over-32-region failure publishes the unchanged source and failed-recognition Manifest without crops, tiles, retry, or semantic sidecars.
- After recognition succeeds, crop encoding, staging writes, hashing, Manifest sealing, semantic L0/L1 generation, or atomic publication failure aborts and rolls back the entire Bundle. This reuses the existing DWG/STEP atomic assembly rule.

### Acceptance for this iteration

The merge gate is deterministic: strict response validation, normalized-to-pixel conversion, padding and clamping, exact deduplication, the 32-region limit, synthetic pixel crops, Manifest hashes, source preservation, atomic rollback, image-mode validation, and REST/MCP upload-argument propagation must all pass without a real VLM.

The live pilot uses seven one-shot recognition calls with the currently configured VLM and no retry: the dense user screenshot plus six deduplicated TechMB sheets sampled as two low-, two medium-, and two high-complexity drawings. The unannotated source for the representative simple sheet is included in those six; the hand-marked screenshot is not used as model input. The pilot reports recognition completion, major-region coverage, usable-crop rate, false regions, and integrity errors. A provisional pass means at least `5/6` TechMB sheets complete, at least `80%` major-region coverage and usable crops, at least one usable sheet in every complexity tier, and zero source/hash/bbox/crop integrity errors.

This establishes `pilot-ready / best-effort`, not production accuracy. TechMB has no semantic-region ground truth, so a 180-sheet run would add cost without creating a trustworthy accuracy measure. Production thresholds require a later annotated sample from the real business distribution and measured downstream retrieval benefit.

### Pilot result — 2026-08-03

The live pilot used the OpenViking configuration observed at execution time: VolcEngine `doubao-seed-2-0-pro-260215`. Each sheet received exactly one application-level recognition invocation; ordinary Bundle summarization and indexing were intentionally excluded from this quality pilot so region crops did not trigger additional VLM calls.

| Input | Complexity | Regions | Recognition |
| --- | ---: | ---: | --- |
| `24446_b13b250f_3` | 22.766 | 5 | complete |
| `24412_a8e106be_6` | 30.766 | 4 | complete |
| `24498_29513c1a_2` | 43.9149 | 2 | complete |
| `24481_6e1400f0_2` | 59.9149 | 3 | complete |
| `24448_75004b6c_3` | 95.1915 | 4 | complete |
| `24416_b5f2fd88_0` | 633.936 | 6 | complete |
| dense video screenshot | not a TechMB sample | 16 | complete |

All `6/6` TechMB sheets completed, and all three complexity tiers contained usable sheets. Across all seven inputs, 40 crops decoded successfully; automated checks found zero out-of-bounds, degenerate, or crop-size-versus-Manifest mismatches. Post-hoc visual review found every crop contained relevant drawing content and found no obvious omitted principal view in the six TechMB sheets. The dense screenshot produced 16 view/section/detail/isometric/title/notes regions; neither the playback control nor scrollbar became a standalone region, although the playback control remains inside the unmodified central-view crop.

This passes the provisional V1 gate. The coverage and usability review was not performed against frozen region ground truth, so it remains exploratory evidence and must not be reported as a population accuracy estimate.

## Current code facts

- Parser-specific `args` already flow through the SDK, HTTP API, CLI, resource service, media processor, and into `ImageParser.parse(**kwargs)`.
- `ImageParser` currently ignores `image_mode`; for large images it only creates a preview, grid overlay, and fixed tiles.
- Image summaries are generated later with a generic fixed prompt. Passing only `instruction` or metadata therefore does not produce semantic drawing regions.
- The internal image parser is the smallest shared place to validate the mode and preserve the sheet, but `TreeBuilder` explicitly keeps LLM work out of parsers. VLM-based bbox recognition therefore belongs in semantic enrichment; placing it inside `ImageParser` would violate the current processing boundary.
- A deterministic OCR/CV detector could run during parsing, but the current experiments do not justify inventing one. V1 instead reuses the configured OpenViking VLM during Bundle staging.
- Raw base64 text is not an image input contract. An upload boundary must decode it into a named image file before normal `add_resource` ingestion.
- The MCP signed-upload branch currently drops parser `args`; it must be repaired before claiming equivalent support across upload entry points.
- Directory media ingestion bypasses or fails to propagate image parser options, and image bundles bypass the normal image parser. V1 therefore supports only one raster source per request.
- Image bundles bypass parsing because `_add_atomic_resource_bundle()` currently has parsed assembly branches only for `dwg` and `step`; every other Resource Kind enters a source-only fallback. This is an implementation whitelist, not an inherent Bundle limitation.
- Extending bundle assembly to raster technical drawings is compatible with the existing model only if the required versus conditional artifacts and recognition-failure contract are explicit before the manifest is sealed.

## Decision

The stored-artifact contract, failure boundary, and pilot gate above were confirmed on 2026-08-03. Implementation may proceed against this boundary.
