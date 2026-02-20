"""Zimi -- Offline knowledge server for ZIM files.

Makes ``import zimi`` behave identically to the old flat-file layout.
Both attribute reads (``zimi.ZimHandler``) and writes (``zimi.ZIM_DIR = "/path"``)
operate on zimi.server's namespace, so existing code like zimi_desktop.py and
test files need zero changes.

How: a custom module subclass replaces this module in sys.modules.
Package-level attributes (__path__, __spec__, etc.) are stored on the proxy
itself so Python's import machinery works correctly. All other attribute
access is delegated to zimi.server.
"""

import sys
import types
import importlib

_server = importlib.import_module("zimi.server")

# Attributes that belong to the package, not to zimi.server
_PACKAGE_ATTRS = frozenset({
    "__name__", "__package__", "__path__", "__file__",
    "__spec__", "__loader__", "__doc__",
})


class _ZimiProxy(types.ModuleType):
    """Module proxy that delegates attribute access to zimi.server."""

    def __getattr__(self, name):
        return getattr(_server, name)

    def __setattr__(self, name, value):
        if name in _PACKAGE_ATTRS:
            # Store package metadata on the proxy itself
            super().__setattr__(name, value)
        else:
            setattr(_server, name, value)

    def __dir__(self):
        return sorted(set(dir(_server)) | _PACKAGE_ATTRS)


_proxy = _ZimiProxy(__name__)
_proxy.__package__ = __package__
_proxy.__path__ = __path__
_proxy.__file__ = __file__
_proxy.__spec__ = __spec__
sys.modules[__name__] = _proxy
