# Keep the Source Manifest Minimal

**Status:** accepted

The immutable root `manifest.json` requires only `schema` (`openviking.resource-bundle/v1`), `kind`, and `source.path`, `source.sha256`, and `source.ingested_at`. Bundle URI supplies identity; tags and other metadata remain optional, and no mutable timestamp is recorded. V1 indexes this manifest as its sole retrieval artifact rather than synthesizing L0/L1 overview records.
