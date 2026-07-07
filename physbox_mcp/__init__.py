import os
import re
from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("physbox-mcp")
except PackageNotFoundError:
    try:
        toml_path = os.path.join(os.path.dirname(__file__), "..", "pyproject.toml")
        with open(toml_path, "r", encoding="utf-8") as f:
            match = re.search(r'version\s*=\s*["\']([^"\']+)["\']', f.read())
            __version__ = match.group(1) if match else "0.1.0"
    except Exception:
        __version__ = "0.1.0"
