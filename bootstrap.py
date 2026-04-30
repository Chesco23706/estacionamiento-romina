from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


def load_create_app():
    package_root = Path(__file__).resolve().parent / "app"
    init_file = package_root / "__init__.py"
    spec = spec_from_file_location(
        "romina_internal_app",
        init_file,
        submodule_search_locations=[str(package_root)],
    )
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.create_app
