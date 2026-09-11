# Make DWG sheets the retrieval unit

**Status:** accepted. Whole-scope storage is implemented in v9; v10 improves concurrency without changing that contract. Live preservation, index and timing checks pass; retrieval-quality evaluation remains pending. Frozen earlier artifacts remain unchanged.

A DWG is ingested as a root v2 `kind=dwg` DWG Document Container that owns the unchanged source exactly once. The parser discovers Sheets from Paper Space Layouts and Model Space drawing frames, then generates a nested v3 `kind=dwg-sheet` Bundle for each Sheet. Each Sheet references the shared source and is the minimum DWG `find_resource` result. Sheet identity comes from its source locator; drawing number and title remain display metadata. This controlled nesting is the intentional DWG exception to the normal non-overlapping Bundle Boundary, superseding the DWG package shape and spatial Drawing Text Index semantics in ADR-0011.

The preservation contract uses the complete normalized LibreDWG JSON as intermediate data. Confirmed prose and independent tables use the existing Markdown path; other native fields remain in JSON. Coordinates are not rewritten. A prose field is removed only after its text is emitted to Markdown and all placed uses are accounted for; any graphical or uncertain use retains the native text. Original object order, block definitions and references remain recoverable. New parses do not export `sheet.dwg` or persist an additional monolithic `structure.json`.

## Storage and reading are separate

V8 stored 4,305 small JSON files for T0202, including 2,145 metadata files. This made browsing difficult, while JSON was already excluded from automatic model input. V9 replaces persistent token/character-sized fragments with one complete `metadata.json`, one document `drawing.json`, and one `drawing.json` per Sheet. Metadata contains reusable block definitions, their internal graphics, non-graphical records and top-level information. Drawing files contain placed data in the existing spatial order. Cross-Sheet, outside and uncertain placed records remain at document scope. No View/catalog/shared/unassigned tree, expanded instance graph or extra object-location catalog is introduced.

New files use `openviking.dwg-json/v1`. The original record ordinals and necessary Markdown occurrence paths remain; new objects are not split into fragment envelopes. Legacy `openviking.dwg-json-chunk/v1` artifacts remain readable and reconstructable. The former JSON 2048-token/6000-character storage budgets and their DWG configuration are retired. Markdown still uses its existing budgets.

Multi-line JSON is readable through the existing line-offset/limit API. Reading a range does not promise a self-contained JSON value or server-side streaming. A specialized handle/block/bbox reader can be added only when a measured reading failure justifies it. Persistent storage no longer has to match a model input window.

## Model processing and import time

DWG JSON is read-accessible evidence and is excluded from embedding and automatic VLM interpretation, rewriting or summarization, including parent refresh, mixed imports and rebuilds. Valid DWG manifests or either JSON schema identify this data; filenames alone do not. Stale hidden summaries within data-only directories are also excluded. Sheet `context*.md` remains the explicit summary/index allowlist. Query-time JSON reading and VLM use on temporary Sheet images remain available.

T0202 v8 spent 382.024 of 560.419 server seconds across serial Sheet summaries and scheduling. V9 queues Sheet summaries together and uses the existing concurrency limits, then waits once before sealing and publication. Sheet validation precedes enqueueing. The request tracker is registered and cleaned once for the batch; parallel calls must not clean each other's state. Failure retains the document-wide rollback boundary. Prompts, model configuration, Markdown generation and Sheet-image call policy are unchanged in this revision.

Repeated evaluation uses the existing native OVPack containing source, artifacts, summaries and vectors. A fresh parser/model/prompt version needs fresh artifacts; a restored old package is not a new parse. No additional parser-cache framework, evaluation-only ingestion path or model-call heuristic is introduced.

Validated package restoration uses the existing internal bundle-write permission; it must still reject a destination beneath an existing bundle and preserve ordinary-write restrictions. T0202 package restoration passes content/index/vector equality checks with zero provider attempts in the restore window. New-OV initialization is outside that measurement.

The [storage design](../design/dwg-parser-spatial-chunks.md) and [validation record](../design/dwg-parser-json-validation.md) separate fixed-input storage checks, live T0202 timing, and future retrieval evaluation. Performance claims require measurements; no 55-question quality equivalence is inferred from this demonstration.

V10 reuses the configured VLM concurrency for independent Model-frame region calls, the existing async bridge, and the original rendering lock. Results retain candidate order and request context. A failed renderer is checked under that lock, avoiding repeated failed initialization. The shared semantic scheduler accounts for busy workers when admitting queued work. Fixed recorded T0202 responses produce byte-identical v9/v10 Markdown and JSON; live total time falls from 291.452 to 170.268 seconds. The [concurrency validation](../design/dwg-parser-concurrency-validation.md) separates that end-to-end result from the scheduler's independent regression evidence and notes that region limits are per DWG build, not a global cross-import model quota.
