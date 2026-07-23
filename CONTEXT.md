# OpenViking Resource Model

This context defines the terms used when a resource directory represents one complete external asset.

## Language

**Atomic Resource Bundle**:
An explicitly marked, opt-in resource directory that represents one source asset together with its associated artifacts. It is a generic resource primitive, with 3D assets as its first consumer, and its identity does not change when it contains files or subdirectories.
_Avoid_: atomic folder, leaf node, leaf folder

**Bundle Manifest**:
The required immutable root-level `manifest.json` of an atomic resource bundle. It marks the bundle, acts as the source asset card, and carries a Manifest Schema, exactly one Resource Kind, and the original asset's path, hash, and ingestion time.
_Avoid_: optional metadata file, directory-name convention

**Bundle Boundary**:
The complete subtree rooted at an atomic resource bundle. It may contain ordinary directories but cannot contain another Bundle Manifest.
_Avoid_: nested bundle, package hierarchy

**Bundle Ingestion**:
The opt-in creation of an immutable atomic resource bundle through `add-resource(bundle=true)`. The target URI names the bundle root; ingestion derives the Resource Kind from the original artifact's suffix, rejecting an extensionless source, then creates its Bundle Manifest and original artifact without running a parser.
_Avoid_: separate bundle upload API, implicit bundle creation

**Bundle Snapshot**:
The immutable source manifest and artifact inventory created by Bundle Ingestion. Replacing content creates a new bundle; move and removal operate on the complete Bundle Boundary.
_Avoid_: mutable resource package, partial bundle update

**Index Tag**:
A normalized `key=value` exact-match value projected from an atomic resource bundle's Bundle Manifest into the retrieval index. The required `kind=<value>` is one such tag; tags are not the bundle's authoritative metadata.
_Avoid_: metadata source, asset truth

**Asset Card**:
The structured, machine-verifiable source inventory in a Bundle Manifest.
_Avoid_: L1 overview, generated summary

**Resource Discovery**:
Semantic selection across atomic resource bundles that returns bundle roots, scored by the strongest relevant artifact within each bundle.
_Avoid_: file search, bundle interrogation

**Resource Interrogation**:
A query scoped to one known atomic resource bundle that returns specific metadata or content evidence without requiring the original asset to be downloaded. In the first version, an agent uses the existing `grep` directly on the bundle root.
_Avoid_: resource discovery, model download

**Searchable Artifact**:
An artifact that can be reliably text-interpreted, including JSON or STEP; a binary artifact needs a deterministic text projection before it is searchable. V1 uses ordinary `grep` over the bundle root.
_Avoid_: text sidecar

**Manifest Schema**:
The versioned identifier for a manifest's machine-readable layout. The initial Bundle Manifest uses `openviking.resource-bundle/v1`.
_Avoid_: implicit JSON shape, parser version

**Resource Kind**:
A single canonical source-format family, such as `step`, `glb`, or `gltf`, that classifies a bundle's original asset. Bundle Ingestion derives it from the original artifact's suffix, normalizing known aliases such as `.stp` to `step`; it rejects only sources whose type cannot be inferred because they have no suffix. It is recorded in the Bundle Manifest and projected as the required `kind=<value>` Index Tag, while auxiliary files do not change it.
_Avoid_: broad 3D category, raw file extension
