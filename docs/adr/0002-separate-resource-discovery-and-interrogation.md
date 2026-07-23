# Separate resource discovery from resource interrogation

**Status:** accepted

`find-resource` reuses file-level `find` results, groups each hit by its nearest atomic resource bundle root, and returns each root once at its strongest child score with match evidence. An agent then chooses whether to read the asset card or use the existing resource-scoped `grep` directly on that root. This preserves ordinary Linux-style regular-expression grep semantics and returns literal evidence from text-interpretable formats such as JSON or STEP; binary artifacts need a deterministic text projection before they participate. Agents therefore do not need to download or parse the original asset merely to inspect its metadata.
