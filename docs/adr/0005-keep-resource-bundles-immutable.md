# Keep resource bundles immutable

**Status:** accepted

An atomic resource bundle is an immutable source snapshot after ingestion. `add-resource` creates a new bundle for new content, while move and removal act on the whole bundle boundary.
