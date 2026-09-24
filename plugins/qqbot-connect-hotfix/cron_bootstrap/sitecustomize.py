"""QQ-child-only bootstrap for Hermes CLI paths that skip plugin discovery.

Do not import Hermes for unrelated Python programs. Attach the existing policy
only after cron.jobs has finished importing, including absolute CLI paths.
"""
import importlib
from importlib.machinery import PathFinder
import os
from pathlib import Path
import sys
from types import ModuleType

_qq_plugin_root = Path(__file__).resolve().parents[1]


class _QQCronImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname != 'cron.jobs':
            return None
        spec = PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return None
        original = spec.loader.exec_module

        def execute(module):
            original(module)
            sys.meta_path.remove(self)
            package = ModuleType('_qq_cron_bootstrap')
            package.__path__ = [str(_qq_plugin_root)]
            sys.modules[package.__name__] = package
            binding = importlib.import_module(package.__name__ + '.cron_binding')
            binding.patch_job_binding()

        spec.loader.exec_module = execute
        return spec


if os.environ.get('HERMES_QQ_CRON_CONTEXT'):
    sys.meta_path.insert(0, _QQCronImports())

# Preserve a pre-existing sitecustomize rather than shadowing its behavior.
_qq_original = PathFinder.find_spec('sitecustomize', [
    p for p in sys.path if Path(p or '.').resolve() != _qq_plugin_root / 'cron_bootstrap'
])
if _qq_original is not None and _qq_original.loader is not None:
    sys.modules[__name__].__file__ = _qq_original.origin
    _qq_original.loader.exec_module(sys.modules[__name__])
