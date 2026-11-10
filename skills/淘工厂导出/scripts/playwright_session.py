"""Backward-compatible alias of the login skill's canonical session adapter.

Do not copy adapter logic here or change sys.path: both script directories also
contain unrelated same-named modules. Module identity preserves legacy mocks of
MCPClient/_acquire_direct as well as execution-scoped caches and error classes.
"""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

_KEY = "_tgc_canonical_playwright_session"
_PATH = Path(__file__).resolve().parents[2] / "淘工厂登录" / "scripts" / "playwright_session.py"
_impl = sys.modules.get(_KEY)
if _impl is None:
    # Reuse an already imported canonical module under any legacy alias.
    _impl = next((module for module in tuple(sys.modules.values())
                  if getattr(module, "__file__", None)
                  and Path(module.__file__).resolve() == _PATH), None)
if _impl is None:
    _spec = spec_from_file_location(_KEY, _PATH)
    if _spec is None or _spec.loader is None:
        raise ImportError("Canonical login session adapter unavailable")
    _impl = module_from_spec(_spec)
    sys.modules[_KEY] = _impl
    try:
        _spec.loader.exec_module(_impl)
    except BaseException:
        sys.modules.pop(_KEY, None)
        raise
sys.modules[_KEY] = _impl
# Also populate the executing object for importlib callers retaining it; normal
# imports return the canonical object itself, not a star-import proxy.
globals().update({key: value for key, value in vars(_impl).items()
                  if key not in {"__name__", "__file__", "__spec__", "__loader__", "__package__"}})
# spec_from_file_location callers may retain the initial shim object even after
# sys.modules replacement. Forward their patches too (not just attribute reads).
import types as _types
_shim = sys.modules.get(__name__)
if _shim is not None and _shim is not _impl:
    class _AdapterAlias(_types.ModuleType):
        def __getattribute__(self, name):
            if name.startswith('__') or name in {'_impl', '_AdapterAlias'}:
                return super().__getattribute__(name)
            return getattr(_impl, name)

        def __setattr__(self, name, value):
            if name.startswith('__'):
                return super().__setattr__(name, value)
            setattr(_impl, name, value)

        def __delattr__(self, name):
            delattr(_impl, name)
    _shim.__class__ = _AdapterAlias
sys.modules[__name__] = _impl
