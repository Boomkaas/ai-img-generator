"""Print the dependencies of pyproject.toml plus the given extras, one per line, for pip install -r.

Installing them before the sources are copied keeps the dependency layer cached across code changes.
"""

import sys
import tomllib

with open("pyproject.toml", "rb") as f:
    project = tomllib.load(f)["project"]

requirements = list(project["dependencies"])
for extra in sys.argv[1:]:
    requirements += project["optional-dependencies"][extra]
print("\n".join(requirements))
