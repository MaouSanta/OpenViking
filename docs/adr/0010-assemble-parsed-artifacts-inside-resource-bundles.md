# Assemble parsed artifacts inside resource bundles

**Status:** accepted

An Atomic Resource Bundle is the minimum `find_resource` result and a sealed consistency boundary selected explicitly with `add-resource(bundle=true)`, not a raw-only ingestion mode. Bundle ingestion may run the source-format parser while assembling the bundle; after every required source, derived, and semantic artifact succeeds, its `manifest.json` identifies the source and inventories every artifact by role, path, schema, and hash, then the complete bundle is published atomically. A failed assembly reports the error without retrying or exposing a partial bundle. This supersedes the raw-only, parser-free, source-only-manifest constraints in ADR-0001, ADR-0004, ADR-0008, and ADR-0009; ADR-0005's whole-bundle immutability remains in force.
