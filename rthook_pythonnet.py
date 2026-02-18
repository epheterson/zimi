"""PyInstaller runtime hook: configure pythonnet for bundled .NET runtime on Windows."""
import os
import sys
import platform

if platform.system() == "Windows":
    os.environ.setdefault("PYTHONNET_RUNTIME", "coreclr")

    # Point clr_loader at the bundled .NET runtime (avoids needing dotnet installed)
    if getattr(sys, '_MEIPASS', None):
        bundled = os.path.join(sys._MEIPASS, "dotnet_runtime")
        if os.path.isdir(bundled):
            os.environ.setdefault("DOTNET_ROOT", bundled)
