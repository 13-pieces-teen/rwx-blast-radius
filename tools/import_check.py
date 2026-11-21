"""Import modified modules in a disposable process, without writing the source tree."""
import importlib
import importlib.abc
import importlib.util
import json
from pathlib import Path
import sys


def main():
    request = json.load(sys.stdin)
    root = Path(request["source"])
    sys.path.insert(0, str(root))
    overrides = request["overrides"]

    class Loader(importlib.abc.SourceLoader):
        def __init__(self, path, text):
            self.path, self.text = path, text

        def get_filename(self, fullname):
            return str(root / self.path)

        def get_data(self, path):
            return self.text.encode()

        def get_code(self, fullname):
            return compile(self.text, str(root / self.path), "exec")

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            prefix = fullname.replace(".", "/")
            for candidate in (prefix + ".py", prefix + "/__init__.py"):
                if candidate in overrides:
                    loader = Loader(candidate, overrides[candidate])
                    return importlib.util.spec_from_file_location(
                        fullname, root / candidate, loader=loader,
                        submodule_search_locations=[str(root / prefix)] if candidate.endswith("/__init__.py") else None)
            return None

    sys.meta_path.insert(0, Finder())
    for path in dict.fromkeys([*overrides, *request.get("imports", [])]):
        module = path.removesuffix(".py").replace("/", ".")
        module = module.removesuffix(".__init__")
        importlib.import_module(module)


if __name__ == "__main__":
    main()
