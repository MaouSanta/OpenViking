# OpenViking Resource Model

This context defines the terms used when a resource directory represents one complete external asset.

## Language

**Atomic Resource Bundle**:
A marked resource directory that represents one source asset together with its associated artifacts as the minimum `find_resource` result. Ingestion assembles it before publication; after publication, it is replaced or removed only as a complete unit.
_Avoid_: atomic folder, leaf node, leaf folder

**Bundle Manifest**:
The required root-level `manifest.json` that marks an atomic resource bundle and inventories the sealed package. It carries a Manifest Schema, exactly one Resource Kind, source identity, and each derived artifact's role, path, schema, and hash.
_Avoid_: optional metadata file, source-only card, directory-name convention

**Bundle Boundary**:
The complete subtree rooted at an atomic resource bundle. It may contain ordinary directories but cannot contain another Bundle Manifest.
_Avoid_: nested bundle, package hierarchy

**Bundle Ingestion**:
The explicit creation of an atomic resource bundle through `add-resource(bundle=true)`. The target URI names the bundle root; ingestion preserves the original artifact and may run its format parser to add derived artifacts before publishing the bundle.
_Avoid_: raw-only upload, parser-free bundle

**Bundle Snapshot**:
The source manifest and artifact inventory sealed by Bundle Ingestion. Replacing content creates a new bundle; move and removal operate on the complete Bundle Boundary.
_Avoid_: mutable resource package, partial bundle update

**Index Tag**:
A normalized `key=value` exact-match value projected from an atomic resource bundle's Bundle Manifest into the retrieval index. The required `kind=<value>` is one such tag; tags are not the bundle's authoritative metadata.
_Avoid_: metadata source, asset truth

**Resource Discovery**:
Semantic selection across atomic resource bundles that returns bundle roots, scored by the strongest relevant artifact within each bundle.
_Avoid_: file search, bundle interrogation

**Resource Interrogation**:
A query scoped to one known atomic resource bundle that returns specific metadata or content evidence without requiring the original asset to be downloaded. In the first version, an agent uses the existing `grep` directly on the bundle root.
_Avoid_: resource discovery, model download

**Parsed Resource**:
A resource processed by its extension parser, which may persist derived artifacts and semantic navigation. It may also be an Atomic Resource Bundle when those artifacts must be discovered and managed as one resource unit.
_Avoid_: raw-only resource

**Beam Topology Index**:
A deterministic derived DWG artifact that binds beam labels to available geometric and dimensional evidence while preserving confidence states and source-object references. It covers beam topology only, not the complete drawing.
_Avoid_: DWG topology, complete drawing model, source of truth

**Drawing Text Index**:
A deterministic derived DWG artifact containing source-grounded drawing text, source-object references, and spatial chunks suitable for retrieval. It excludes OCR and semantic completion.
_Avoid_: drawing summary, VLM navigation, OCR transcript

**Searchable Artifact**:
An artifact that can be reliably text-interpreted, including JSON or STEP; a binary artifact needs a deterministic text projection before it is searchable. V1 uses ordinary `grep` over the bundle root.
_Avoid_: text sidecar

**Manifest Schema**:
The versioned identifier for a manifest's machine-readable layout. New Bundle Manifests use `openviking.resource-bundle/v2`; readers retain compatibility with `v1`.
_Avoid_: implicit JSON shape, parser version

**Resource Kind**:
A single canonical source-format family, such as `step`, `glb`, or `gltf`, that classifies a bundle's original asset. Bundle Ingestion derives it from the original artifact's suffix, normalizing known aliases such as `.stp` to `step`; it rejects only sources whose type cannot be inferred because they have no suffix. It is recorded in the Bundle Manifest and projected as the required `kind=<value>` Index Tag, while auxiliary files do not change it.
_Avoid_: broad 3D category, raw file extension

## Raster Technical Drawing Language

**Technical Drawing Sheet**:
A user-declared raster image containing one engineering drawing sheet. It is the authoritative visual source from which drawing regions are derived; OpenViking does not infer this identity from an arbitrary image in the first version.
_Avoid_: Drawing Preview, ordinary image, tile set

**Drawing Recognition**:
The derivation of semantic drawing regions from a Technical Drawing Sheet. It does not mean classifying arbitrary images or guaranteeing exact transcription of dimensions and engineering annotations.
_Avoid_: image classification, annotation extraction, OCR

**Semantic Drawing Region**:
A model-proposed rectangular area on a Technical Drawing Sheet with one of six V1 types: `view`, `section`, `detail`, `isometric`, `title_block`, or `notes`. An optional label preserves source names such as `DETAIL A` or `SECTION B-B`; V1 does not infer orthographic direction or parent/child relations. A region is independent navigation evidence, may overlap another region, and is not authoritative geometry.
_Avoid_: tile, component mask, source geometry

**Drawing View Crop**:
A padded rectangular copy of a Semantic Drawing Region that preserves the source pixels and may include neighboring content. Its source linkage uses integer `xyxy` coordinates in the original sheet's pixel space, with a top-left origin. It is derived input for semantic indexing, not an isolated or masked source of truth.
_Avoid_: clean view, segmentation mask, tile

**Technical Drawing Bundle**:
An Atomic Resource Bundle whose source is a Technical Drawing Sheet and whose derived image artifacts are source-linked Drawing View Crops. Its Bundle Manifest records the drawing-recognition outcome and the provenance of each crop.
_Avoid_: image directory, tile set, source-only image bundle
