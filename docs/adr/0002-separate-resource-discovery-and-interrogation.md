# Separate resource discovery from resource interrogation

**Status:** accepted

`find-resource` retrieves exactly one system-tagged discovery card per atomic resource bundle and projects each hit directly to its bundle root. This makes `limit=N` mean up to N distinct bundles without over-fetching or child-hit aggregation. An agent then chooses whether to read the asset card or use the existing resource-scoped `grep` directly on that root. This preserves ordinary Linux-style regular-expression grep semantics and returns literal evidence from text-interpretable formats such as JSON or STEP; binary artifacts need a deterministic text projection before they participate. Agents therefore do not need to download or parse the original asset merely to inspect its metadata.
