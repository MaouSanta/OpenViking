# Parse DWG through the normal resource pipeline

**Status:** accepted

`.dwg` files added through the ordinary `add-resource` path are routed to a DWG Parser. The parser preserves the original DWG and a complete UTF-8-normalized LibreDWG JSON export, then creates non-authoritative L0/L1 drawing navigation; numerical and geometric answers remain grounded in the preserved source data. V1 makes one configured-VLM call per drawing from deduplicated non-numeric semantic labels and an optional embedded thumbnail, not text chunks or a whole-sheet renderer. LibreDWG export, JSON validation, or the required VLM call failing fails `add-resource`; missing previews, XREFs, and proxy objects are recorded as warnings instead. `bundle=true` remains a separate raw-only immutable-ingestion path and does not run the DWG Parser.
