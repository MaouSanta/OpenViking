# DWG Markdown / JSON parser validation

最新 v10 的区域并发、队列修复及固定返回回放见[并发消融验证](dwg-parser-concurrency-validation.md)。本页保留 v9 及更早版本的存储与恢复记录。

**Scope:** v9 checks are recorded first, followed by frozen v8 and v7
historical contracts and results. The [spatial-chunk design](dwg-parser-spatial-chunks.md)
is implemented. The paused v7 artifacts remain unchanged. Neither unit tests nor
the T0202 import establish equal retrieval quality; no 55-question A/B result is
claimed.

## Parser 9.0: whole-scope JSON and concurrent Sheet summaries — 2026-09-10

Implementation and live T0202 checks pass. V8 and its 560.419-second baseline
remain frozen; no full-corpus import or 55-question evaluation is resumed.

One fixed-input, no-model storage comparison builds Sheets/Markdown once, then
passes the same native input and selection to the frozen v8 writer and the v9
writer. All 37,749 objects, header fields, per-scope ordering, native types, key
order and Markdown bytes agree; only v8's retired document budget is omitted.
There are 4,225 old JSON files versus 21 complete new files. The legacy 256
fragment parts still reconstruct through the current reader; v9 emits none.

The single diagnostic invocation measured 5.769 seconds for v8 JSON generation
and 2.251 seconds for v9. This excludes filesystem publication and model calls;
it is not a repeated performance benchmark. Pretty JSON increases uncompressed
output from 23,731,431 to 52,520,036 characters, while making line-range reads
possible; fewer files does not imply fewer serialized bytes. Evidence is in
`.local-data/dwg-whole-t0202-v9-20260910/audit/storage-ablation.json`.

The obsolete JSON budget configuration and bounded writer are removed. New and
legacy JSON identities remain protected from summary/embedding inputs. Existing
line-range reads are reused; no parser cache, special query API, production
experiment switch or model-call heuristic is added. Before the separate OVPack
repair below, seven production/config files have a net reduction of 52 lines
relative to v8; including that repair, eight files have a net reduction of 23
lines. The targeted ingestion suite has
**314 passing tests**; Ruff and diff checks pass.

The isolated live v9 import runs at port 1950 and takes **291.452 server seconds**
(291.560 client seconds), a **48.0% reduction** from v8. It preserves 19 Sheets,
24 Markdown files, all 37,749 original objects and the header. 2,122 migrated
fields have Markdown references and complete native-use coverage; all 104
published files and 103 manifest entries are checked. There are exactly 21
whole-scope JSON files and zero fragments. Parse status remains partial.

Measured phases include 4.497 seconds for LibreDWG/normalization, 35.521 for
rendering, 105.726 for the 19 Sheet-image VLM calls, 2.403 for JSON generation,
0.522 for JSON validation/inventory, 135.900 for the concurrent Markdown summary
batch, and 4.048 for Markdown indexing/wait. The detailed table subtracts nested
spans to avoid double counting. Runtime logs confirm 19 summary roots, 24 Markdown
inputs, at most four simultaneous Sheet jobs, and zero JSON summary inputs.

Actual vector enumeration finds exactly 24 L2 context Markdown records under the
DWG and zero JSON vectors; consistency has zero missing records. All six line
pagination requests match the stored files. The native one-source OVPack is
3,844,880 bytes and contains the original DWG plus the same Markdown vectors.
Evidence is under `.local-data/dwg-whole-t0202-v9-20260910/audit/` and the browseable
copy is `review/README.md`.

The two live runs have identical Markdown paths/counts, but only 12 of 24 files
are byte-identical. VLM responses and latency vary even with unchanged rules.
This is a single end-to-end comparison, not proof of equal answer quality or a
fixed-output scheduling benchmark. The no-model fixed-input storage comparison
above remains the separate evidence for unchanged storage semantics.

### OVPack restore integration failure and repair

The first guarded 1951 restore returned HTTP 412 before any model call: its
ordinary file-write path rejected creation of a DWG Sheet `manifest.json`.
The export and 1950 DWG ingestion passed, but the attempted restore did not.
Evidence is retained in `audit/readback-import-response.json` and
`audit/readback-error.json`. Repair validation uses a separate empty workspace;
it does not overwrite the failed attempt or rerun DWG parsing.

The repair uses the existing internal bundle-write permission only for fully
validated OVPack members, and rejects imports beneath an existing bundle before
writing. Ordinary file writes still cannot create or modify bundle manifests.
Whole-root overwrite keeps the existing deletion policy. **77 focused tests
pass**, including nine real-VikingFS regressions for manifest ordering, nested
targets, ordinary writes, damaged hashes and resource-scope backup restoration.

The fresh retry under `readback-fixed/` restores the same package with
`vector_mode=require` in **0.750 seconds**. Upload is 0.017 seconds and re-export
is 0.402 seconds; the guarded HTTP window is 1.263 seconds. Content hashes,
portable index records and dense vector bytes are identical, with 24 Markdown
vectors and zero JSON vectors. Configured-provider sentinels record **zero
attempts** during restore/re-export; native model usage is unchanged. The 26
provider calls made during new-OV initialization are explicitly outside this
measurement. See `readback-fixed/audit/readback-validation.json`.

The failed snapshot remains immutable. The retry/final snapshot differs from
the timed v9 snapshot only in `openviking/storage/ovpack/operations.py`; the DWG
parser and summary implementation are identical. The main 1950 service loads
that final snapshot using the existing workspace, without re-parsing the DWG.
Audit-only 1951 is stopped after validation. Generic user/agent backup-alias
policy is outside this resource-bundle repair.

## Parser 8.0: minimal storage and model exclusion — 2026-09-10

The implementation now writes document `metadata_*.json`, document `chunks/`,
and Sheet `chunks/`; it no longer writes View/catalog/shared/unassigned trees,
locators or an expanded instance graph. Native object ordinals, definitions,
references and necessary Markdown occurrence paths remain. The existing
longest-axis spatial ordering and reversible fragment reader are reused. Relative
to the frozen v7 source, the three parser files have a net reduction of 24 lines;
five processing-boundary files add a net 123 lines. Those additions cover the
explicit no-VLM/no-embedding requirement, rather than a new storage framework.

The Sheet-image VLM selects only prose and independent tables, once per
candidate. An empty selection is a valid graphical Sheet. Unknown selection
remains partial within its native Sheet; only explicit `is_sheet:false` rejects
a candidate. Graphical text is retained as JSON. No detail-review call or
production experiment switch was added.

- Parser, rendering, frame, atomic-bundle and OVPack suites: **174 passed**.
- Read-only artifact routing and file-vectorization suites: **52 passed**.
  These capture actual test model inputs and vector queues, including recursive
  and incremental summaries, reindex, mixed directories, stale hidden summaries,
  git-restore markers and final embedding enqueue guards. They do not claim a
  recording of every live provider payload.
- T0202 without VLM/ODA/rendering: all **37,749 original objects** and the complete
  non-OBJECTS header round-trip; **4,225 JSON files**, including **256 fragment
  parts**, satisfy the budget (maximum **1,733 estimated tokens / 6,000 chars**).
  There are 19 native Sheet proposals. Parse status is partial because VLM is
  disabled; data preservation passes. This is a preservation baseline, not a
  live region-quality result.

Evidence and the frozen source snapshot are under
`.local-data/dwg-spatial-t0202-v8-20260910/`; the offline audit is
`audit/native-before-ingest.json`. The isolated live T0202 import uses port 1946
and `viking://resources/t0202-spatial-v8`. It completed with 19 Sheets, 24 Markdown
files and 4,305 parser JSON files. The live maximum is 1,765 estimated tokens /
6,000 characters. `audit/live-artifacts.json` verifies all 37,749 original objects
once, the complete header, 2,169 migrated fields with Markdown references and
native-use coverage, and all 256 fragment parts. All other fields retain their
original values and types; the 4,388 published files remain byte-identical after
auditing. Markdown reading order, SHX decoding and semantic region accuracy are
not certified, and parse status remains `partial`.

Actual vector-store enumeration finds exactly 24 L2 Markdown
records under this DWG and **zero JSON vectors**; native consistency reports zero
missing records. The other 26 tenant records are pre-existing namespace L0/L1
records created before ingestion, not drawing data.

Real semantic queue logs contain 19 Sheet jobs with only the 24 `context*.md`
inputs. The 19 image-request log entries are the permitted upstream Sheet-image
calls. The native one-source OVPack contains the same source and 24 Markdown
vectors; its hash and validation are in `audit/index-export-validation.json`.
This verifies the running input allowlists and stored vectors, without claiming
a recording of all provider request bodies.

Server wall time is 560.419 seconds: 172.818 seconds before the first summary
enqueue (conversion, rendering, image VLM, artifact generation and preparation),
382.024 seconds across Sheet summaries/scheduling, and 5.577 seconds for remaining
publication/indexing/return. Exclusive conversion, VLM, JSON-writing and embedding
latencies were not persisted and cannot be inferred from these combined stages.
See `audit/import-timing.json`. The byte-verified browse copy is `review/README.md`.
No batch import or QA evaluation is resumed.

## Historical v7 contract and checks

The parser treats normalized LibreDWG JSON as its complete intermediate data. It
moves confirmed prose to the existing Markdown reading-order path and groups the
remaining original fields into bounded JSON files. It does not export `sheet.dwg`.

## Data contract

`openviking.dwg-json-chunk/v1` files contain a `records` array. `object` records hold
an original `OBJECTS` array ordinal and its native `value`; `instance` records
reference that ordinal and carry location/region information without rewriting
the native coordinates. `header` retains every non-`OBJECTS` top-level field.
`catalog/` contains bounded source-object locators, including oversized fragments.
Shared raw objects are stored once. Unassigned and unsupported data remain present.

Only an actually emitted prose field is replaced by Markdown references. All of
its placed occurrences must be accounted for in Markdown, and none may still be
used in a view. A differing unused `text`/`text_value` alias is preserved. Native
table records remain available even when their readable projection is Markdown.

Whole records are packed spatially. An oversized record is serialized into
`fragment`, `part`, `total`, and `text` envelopes; concatenation and SHA-256
verification reconstruct it exactly. `read_json_records()` reads both forms and
rejects missing, duplicate, or corrupt fragments. Every emitted JSON file includes
its envelope/escaping overhead in both length checks. The defaults are 2048
estimated tokens and 6000 characters, capped further at 80% of
`embedding.max_input_tokens`. Only ResourceService's v2/v3 system manifests are
exempt. These are estimator bounds, not a claim about a provider's true tokenizer.

Configuration accepts `dwg` or `parsers.dwg` with `json_max_tokens` and
`json_max_chars`. `DWGParser` returns `json_artifacts` and `parse_status`; each
Sheet adds its JSON inventory and `status`. New JSON is non-searchable evidence.
The formal Bundle path summarizes and indexes only Sheet `context*.md`.
The proposed `chunks/` layout makes this an explicit processing boundary: DWG
JSON files must not trigger VLM processing or embedding, directly or through
parent-directory summaries/rebuilds. This requirement does not disable the
upstream Sheet-image VLM or query-time JSON reading. See the
[model-routing rules](dwg-parser-spatial-chunks.md#模型处理边界); a non-searchable
flag alone is not a summary allowlist. Generic reindexing and unfiltered parent
refresh still need protection; the initial-ingestion checks below do not certify
those paths. OVPack protection depends on valid DWG manifests.
Standalone DWG imports carry the same content allowlist through ordinary/deferred
processing and vectors-only mode. Mixed directory imports are not covered by this
change: the existing DirectoryParser does not aggregate child DWG metadata.

## Incremental checks

The automated fixtures keep the source constant and vary only the added step:

- Without VLM: every native object and header field round-trips, all JSON is
  bounded, and the parser reports `partial` rather than inventing view boundaries.
- With fixed region responses: views acquire separate directories, prose moves
  to Markdown, crossing objects share references, and all other native fields
  remain unchanged.
- Oversized objects, arrays, escaped strings/keys, Unicode, nulls and scalar
  OBJECTS entries survive splitting. Repeated INSERTs do not share placement
  identity. Missing geometry remains explicitly unassigned/partial.
- Render checks cover curves, fills, text, nonsquare crops and CAD/pixel mapping;
  images have no margins and CAD Y increases upward. Cropping leaves the source
  recording unchanged and writes no output files.
- Ingestion checks cover nested JSON inventories, hashes, unsafe/missing/invalid
  artifacts, rollback, Markdown-only summaries and unchanged index selection.

Two existing frame defects were fixed only after reproducing them: zero-area
block candidates could displace a valid frame, and a distant outside line could
make the frame appear too small relative to the entire scene.

Initial targeted suite: **238 passed**, with four existing Pydantic deprecation
warnings. Ruff and `git diff --check` passed. The dependency lock check passed
offline against the existing public registry; only the already-locked matplotlib
root dependency edges were added, with no unrelated version or registry changes.
The complete test output is retained in
`.local-data/dwg-parser-json-validation-20260909/test-results-final.txt`.

## Follow-up code reduction — 2026-09-09

Relative to that implementation snapshot, five Python source files lost a net
**1040 lines**, including 16 retired top-level functions. The parser now has one
region-selection path. Removed code includes the candidate-only classifier and
its compatibility switch, guessed figure/title ranking, the approximate Pillow
layout fallback, and an unused semantic structure report. Marker/table/form
recovery, instance coordinates, bounded JSON and reversible fragments remain.
The instance walker also reuses the existing child-object index.

`build_sheet_artifacts()` now returns `region_diagnostic` directly rather than
building the obsolete nested `structure` report. Its old
`document_region_classifier` keyword is retired; the Markdown batch caller uses
the current classifier and real renderer. No VLM or missing renderer yields
`partial`. The public DWGParser artifact layout, bytes and status are unchanged
in the comparisons below.

- No-VLM and fixed-partition fixtures: all Markdown/JSON bytes and public Sheet
  descriptors match the pre-cleanup snapshots exactly.
- Three real sources: all **38,928 JSON files** have identical tree hashes to
  the prior baseline; all **147,731 native objects** and headers still reconstruct
  exactly and every JSON file satisfies the same budgets. These are no-VLM
  preservation comparisons, not segmentation-accuracy measurements.
- Current targeted suite: **236 passed**. Three tests specific to retired
  behavior were removed; a missing-renderer check was added. Retained reading
  order, tables, frames, title-block metadata and view-directory behavior still
  have concrete assertions. Ruff and diff checks pass.

Source snapshots, per-source hashes, comparisons and test output are retained in
`.local-data/dwg-parser-minimality-20260909/`. This cleanup made no model calls.

## Real-source preservation baseline — 2026-09-09

Only the three source DWGs were read; no previous parser output or Gold was used.
VLM and ODA were disabled. Every in-memory output file was decoded, budget-checked,
and reconstructed; the original OBJECTS and header values/types matched exactly.

| Source | Native objects | Detected Sheets | JSON files | Max estimated tokens | Max characters | Total seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Staircase, SHA `54483f8d2010…` | 11,486 | 4 | 2,002 | 1,575 | 6,000 | 6.669 |
| Structural, SHA `c5cb217fd294…` | 67,464 | 22 | 11,074 | 1,914 | 6,000 | 36.113 |
| Security, SHA `eec3c596e672…` | 68,781 | 13 | 25,852 | 1,993 | 6,000 | 118.959 |

Source and code fingerprints were unchanged within each run. The highest measured
Python peak RSS was 2224.42 MiB, including full reconstruction checks. Parser
status was `partial` for all three because VLM was disabled. The Sheet counts are
detector output, not independently verified ground truth. These checks establish
preservation and bounded storage, not VLM segmentation quality.

Local evidence, source/code hashes, timings and converter warnings are under
`.local-data/dwg-parser-json-validation-20260909/baseline/`. Chunks were checked
in memory rather than retaining tens of thousands of diagnostic files.

## Real render and online validation boundary

The initial real-Sheet render failed inside the macOS sandbox with an ODA
PasteBoard initialization error; no model request occurred. Its error and replay
checks are retained in `vlm-smoke/`. A separate local-only native render succeeded
and produced a 2048-pixel-wide PNG in `local-render-native/`, with zero VLM calls.

The initial request to send an actual drawing to the configured VLM was rejected
by automatic approval review because sensitive-payload authorization was not yet
established. The user subsequently authorized the complete import and evaluation
workflow. Authorization and real corpus ingestion do not themselves establish
segmentation accuracy.

## Synthetic live VLM ablation

Two calls used exactly the same newly generated CAD image (no real drawing data)
and fixed geometric checks. Each condition made one application call and one
observed HTTP request, with no observed retry. The configured provider was
Volcengine / `doubao-seed-2-0-pro-260215`.

| Coordinate contract | Note body anchors covered | Complete view extents covered | Call seconds |
| --- | ---: | ---: | ---: |
| Model asked to return CAD coordinates | 1/4 | 0/2 | 8.700 |
| Normalized image coordinates, converted in code | 4/4 | 2/2 | 9.066 |

The retained adapter requests image coordinates on 0–1000, with the origin at
the top left, and converts them to the crop's CAD bounds with the Y axis reversed.
Fixed adapter tests cover nonsquare bounds and invalid output. This small change
addresses the observed coordinate failure without adding new segmentation
heuristics. The original response is retained in `synthetic-vlm/`; the comparison
is in `synthetic-vlm-normalized/`. Passing this one synthetic case is not a claim
about real-DWG segmentation accuracy.

Model-frame partitioning is the first-version boundary. Layout/viewport projection,
unsupported geometry and unavailable rendering yield preserved partial results.
Text and dimension ownership uses native anchors; supported curves may use
conservative bounds. This is region grouping, not an exact CAD clipping or
reconstruction guarantee.

## Reproduced input repairs and final parser 7.0 — 2026-09-10

The initial imports exposed concrete frame errors before any formal QA question
ran. Each retained change has a fixed failure case:

- Repeated generic block bounds no longer remove a real outer frame. The solar
  electrical source changes from 56 component candidates to its one real frame.
- The old repeated-block size/ratio bypass was removed. 11-PL retains its exact
  11 full A034 frames and removes 43 palette objects; signature and cross-Sheet
  bounds in the fixed structural/security samples are removed too.
- An open four-corner U polyline becomes a frame only when its exact closing LINE
  already exists in the same placed instance. Its original flags/points stay
  untouched and `source_ref.closing_line` records the native evidence. The garbage
  structural source changes from 25 detail rectangles to five real frames. Gaps,
  bulges and edges from other instances do not qualify.
- The temporary renderer uses ezdxf's existing MIF decoder. All 304 decoded
  display fields in the electrical sample match the original LibreDWG text at the
  same handle. Original files, native JSON fields and fonts are unchanged.
- The existing region call may return `is_sheet:false` for clear non-Sheet
  material. Successful responses containing only `unknown` regions leave those
  objects in document-level unassigned JSON. Mixed known/unknown regions remain
  within their Sheet; failed/unavailable classification remains partial. If every
  proposal is omitted, the existing synthetic partial representation preserves
  the document and still publishes normally. No extra model call or runtime
  experiment switch was added.

Seven fixed sources passed native/header reconstruction and every JSON budget:
**256,576 objects and 56,823 JSON files**, without VLM. Their counts describe
native proposals, not independent physical-sheet ground truth.

One production VLM pilot used 22 calls and retained eight candidates: three real
Sheets and five all-unknown areas. Its strict three-Sheet check **failed**; that
result remains unchanged. A subsequent deterministic replay of those same 22
returns, using the final unknown-data placement rule, retained the three real
Sheets and preserved 8,824 objects, 341 migrated text fields, 90 fragments and all
budgets. The replay made zero VLM/ODA calls. It is post-hoc code validation, not a
new live VLM success or a segmentation-accuracy certificate. **108 targeted tests
passed**, including all-unknown publication and preservation; Ruff passed.

Evidence is retained in `.local-data/dwg-parser-repairs-20260910/` and the v5
run's `audit/` directory. The incomplete v5/v6 corpora remain diagnostic only.
The final clean v7 run uses the unchanged frozen 55-question set and answering
protocol. Its source snapshot is immutable; remaining VLM uncertainty is recorded
as partial and evaluated without question-specific parser changes.
