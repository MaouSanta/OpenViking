"""Shared in-memory filesystem for DWG parser tests."""


class _FakeVikingFS:
    def __init__(self):
        self.dirs = []
        self.files = {}
        self._counter = 0

    def create_temp_uri(self):
        self._counter += 1
        return f"viking://temp/dwg_{self._counter}"

    async def mkdir(self, uri, exist_ok=False):
        if uri not in self.dirs:
            self.dirs.append(uri)

    async def write_file(self, uri, content):
        self.files[uri] = content.encode("utf-8") if isinstance(content, str) else content

    async def write_file_bytes(self, uri, content):
        self.files[uri] = content

    async def delete_temp(self, uri):
        self.files = {key: value for key, value in self.files.items() if not key.startswith(uri)}
        self.dirs = [item for item in self.dirs if not item.startswith(uri)]
