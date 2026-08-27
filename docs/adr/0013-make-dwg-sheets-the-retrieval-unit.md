# Make DWG sheets the retrieval unit

**Status:** accepted

A DWG is ingested as a root v2 `kind=dwg` DWG Document Container that owns the unchanged source exactly once. The parser discovers Sheets from Paper Space Layouts and Model Space drawing frames, then generates a nested v3 `kind=dwg-sheet` Bundle for each Sheet; each Sheet Bundle uses `source_ref` to identify the shared source and is the minimum DWG `find_resource` result. Sheet identity comes from its source locator, while drawing number and title remain display metadata. This controlled nesting is the intentional DWG exception to the normal non-overlapping Bundle Boundary, and it supersedes the DWG package shape and spatial `Drawing Text Index` semantics in ADR-0011.

Each DWG Sheet Bundle uses `content.md` as its default model-indexed artifact, following the PDF Markdown length and splitting limits; metadata, tables, notes, views, labels, and directives remain sections of that content rather than separate indexed files. A non-indexed `structure.json` retains deterministic Regions, table structure, marks, source-object references, and Drawing Annotation Graph links. Sheet Bundles reference the shared DWG Source, and DWG parsing generates or retains no raster or rendered preview artifact.
