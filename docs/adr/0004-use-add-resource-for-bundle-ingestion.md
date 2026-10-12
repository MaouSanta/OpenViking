# Use add-resource for bundle ingestion

**Status:** superseded by ADR-0010

Atomic resource bundles are created through opt-in `add-resource(bundle=true)`, reusing the existing upload and authorization paths. The target URI is the bundle root; ingestion derives a canonical Resource Kind from the original filename suffix, normalizes known aliases, and rejects an extensionless source. It creates only its immutable `manifest.json` and original artifact, without running a Parser or persisting semantic sidecars. Replacing content creates a new bundle rather than a public in-place update.
