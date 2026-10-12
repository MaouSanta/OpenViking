# OpenViking Resource Model

This context defines the terms used when a resource directory represents one complete external asset.

## Language

**Atomic Resource Bundle**:
A marked resource directory that represents one independently retrievable resource unit together with its associated artifacts as the minimum `find_resource` result. It normally owns its source bytes; a DWG Sheet Bundle instead references the shared DWG Source owned by its DWG Document Container.
_Avoid_: atomic folder, leaf node, leaf folder

**Bundle Manifest**:
The required `manifest.json` at a resource bundle's own root that marks and inventories the sealed package. It carries a Manifest Schema, exactly one Resource Kind, source identity or source reference, and each derived artifact's role, path, schema, and hash.
_Avoid_: optional metadata file, source-only card, directory-name convention

**Bundle Boundary**:
The complete subtree owned by a resource bundle. Bundle Boundaries normally do not overlap; the deliberate exception is generated DWG Sheet Bundles nested inside their owning DWG Document Container.
_Avoid_: arbitrary nested bundle, manually nested bundle

**Bundle Ingestion**:
The explicit, opt-in creation of immutable resource units through `add-resource(bundle=true)`. It normally publishes one Atomic Resource Bundle; DWG ingestion publishes one root DWG Document Container plus generated nested DWG Sheet Bundles.
_Avoid_: separate bundle upload API, implicit bundle creation, raw-only upload, parser-free bundle

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

**DWG Document Container**:
A root v2 Resource Bundle of kind `dwg` that owns one unchanged DWG Source and all DWG Sheet Bundles discovered from it. It is the document-wide publication and source-ownership boundary, while its Sheets are the minimum DWG retrieval units.
_Avoid_: sheet bundle, manifest-less folder

**DWG Sheet Bundle**:
A generated v3 Resource Bundle of kind `dwg-sheet` for exactly one Sheet and the minimum DWG discovery and retrieval result. Its content and evidence are Sheet-scoped, while `source_ref` identifies the DWG Source owned by its parent DWG Document Container.
_Avoid_: whole-DWG bundle, drawing text folder, page preview

**DWG Source**:
The unchanged `.dwg` file stored exactly once by a DWG Document Container and referenced by its DWG Sheet Bundles. It remains the authoritative source for every Sheet derived from that document.
_Avoid_: generated drawing, bundle URI, preview artifact

**Drawing Preview**:
A transient, read-only rendering of a DWG Source for visual verification, when a client chooses to provide one. It never replaces the DWG Source, and no raster or rendered preview is retained as a DWG parsing artifact.
_Avoid_: CAD editor, source of truth, persisted preview artifact

**Sheet**:
A bounded drawing unit intended to stand alone for identification and retrieval. It may originate from a Paper Space Layout or a drawing frame in Model Space; its identity does not depend on a potentially missing or repeated drawing title.
_Avoid_: page image, viewport, component, small drawing, region

**Document Sheet**:
A text-dominant Sheet, such as a multi-column design specification, whose numbered prose and inline tables form one reading-ordered document. It is restored as a document flow rather than regrouped into semantic buckets.
_Avoid_: notes region, drawing labels, semantic summary

**Title Block**:
The bounded Sheet area that identifies the drawing, project, issuer, revision, and responsible roles. Its fields are Sheet metadata and are excluded from the Document Sheet body.
_Avoid_: generic metadata text, body notes, drawing title guess

**Region**:
A typed semantic area within one Sheet, such as metadata, a table, notes, a plan, or a detail. It provides containment and reading scope but is neither a fixed pixel tile nor an independent bundle.
_Avoid_: crop tile, sheet, bundle, storage chunk

**Drawing View**:
A spatially separate drawing within a Sheet, including its title, dimensions, leaders, annotations, and internal tables. Small reference diagrams and large plans are both Drawing Views.
_Avoid_: sheet, pixel tile, independent bundle, storage chunk

**Drawing Asset**:
An identifiable group of graphical content within one Sheet, usually associated with a figure number or title. It may contain several related views, their annotations and an associated table.
_Avoid_: source file, storage chunk, independent bundle

**Sheet Asset Catalog**:
A searchable, human-readable navigation aid that connects Drawing Asset identifiers and visible content descriptions to their stored evidence. Its links indicate related data, not exclusive ownership of that data.
_Avoid_: object catalog, geometry partition, asset database

**Drawing Instance**:
One placed occurrence of a native drawing object, including its enclosing block insertions. Multiple instances may share the same native object definition while appearing in different regions.
_Avoid_: unique handle, copied object definition

**DWG Data Chunk**:
A bounded, reconstructable portion of preserved native drawing data, grouped by spatial proximity when placement is known. Its storage boundary need not coincide with a Drawing View or Region.
_Avoid_: semantic summary, simplified geometry, drawing view

**Document-level DWG Data**:
Native data belonging to the DWG as a whole or lacking a unique Sheet owner, including document properties, reusable definitions and objects spanning Sheets. This scope does not itself imply a parsing failure.
_Avoid_: error bucket, failed geometry, duplicated Sheet data

**Beam Topology Index**:
A deterministic derived DWG artifact that binds beam labels to available geometric and dimensional evidence while preserving confidence states and source-object references. It covers beam topology only, not the complete drawing.
_Avoid_: DWG topology, complete drawing model, source of truth

**Drawing Annotation Graph**:
A deterministic, Sheet-scoped relationship model connecting annotations to their supported targets, spaces, grids, or other drawing objects while preserving source evidence and confidence. It is not a complete drawing model and does not assign unrelated annotations to the Beam Topology Index.
_Avoid_: Drawing Text Index, beam topology, complete DWG graph

**Searchable Artifact**:
An artifact explicitly exposed to model retrieval because its content can be reliably text-interpreted. Machine-readable evidence is not searchable by default; a bundle may retain deterministic structure solely for validation and provenance.
_Avoid_: text sidecar

**STEP Evidence**:
A deterministic JSON artifact derived only from the complete geometry and header of one STEP source. It excludes neighboring modeling-history files and experimental benchmark outputs.
_Avoid_: timeline evidence, STEP summary, benchmark evidence pack

**Manifest Schema**:
The versioned identifier for a manifest's machine-readable layout. Ordinary bundles and DWG Document Containers use `openviking.resource-bundle/v2`, generated DWG Sheet Bundles use `openviking.resource-bundle/v3`, and readers retain compatibility with `v1`.
_Avoid_: implicit JSON shape, parser version

**Resource Kind**:
A single canonical family that classifies a bundle's resource unit. Ordinary bundles and a DWG Document Container use the original source format, while a generated DWG Sheet Bundle uses `dwg-sheet`; the value is recorded in the Bundle Manifest and projected as the required `kind=<value>` Index Tag.
_Avoid_: broad 3D category, raw file extension
