# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Zimi Desktop.

Build:
    pyinstaller zimi_desktop.spec

Output:
    dist/Zimi/          — one-dir bundle (all platforms)
    dist/Zimi.app/      — macOS app bundle (macOS only)
"""

import glob
import os
import platform
import sysconfig

block_cipher = None

# ---------------------------------------------------------------------------
# Collect libzim native libraries
# ---------------------------------------------------------------------------
# libzim is a single Cython extension (libzim.cpython-3XX-{platform}.so/.pyd)
# plus a native C++ shared library (libzim.9.dylib / libzim-9.dll / libzim.so.9).
# The submodules (reader, search, suggestion) are .pyi stubs, NOT real modules.
# PyInstaller auto-detects the extension via `import libzim`, but the native
# shared library lives in a separate libzim/ directory and must be collected
# explicitly.

def collect_libzim_binaries():
    """Find libzim native shared libraries for the current platform."""
    binaries = []
    site_packages = sysconfig.get_path('purelib')

    # The libzim/ directory contains the native C++ library
    libzim_dir = os.path.join(site_packages, 'libzim')
    if not os.path.isdir(libzim_dir):
        # Try platlib (where compiled packages go)
        site_packages = sysconfig.get_path('platlib')
        libzim_dir = os.path.join(site_packages, 'libzim')

    if os.path.isdir(libzim_dir):
        if platform.system() == 'Darwin':
            for lib in glob.glob(os.path.join(libzim_dir, '*.dylib')):
                binaries.append((lib, '.'))
        elif platform.system() == 'Windows':
            for lib in glob.glob(os.path.join(libzim_dir, '*.dll')):
                binaries.append((lib, '.'))
        elif platform.system() == 'Linux':
            for lib in glob.glob(os.path.join(libzim_dir, '*.so*')):
                binaries.append((lib, '.'))

    return binaries

libzim_bins = collect_libzim_binaries()

a = Analysis(
    ['zimi_desktop.py'],
    pathex=[],
    binaries=libzim_bins,
    datas=[
        ('templates', 'templates'),
        ('assets', 'assets'),
    ],
    hiddenimports=[
        'zimi',
        'libzim',
        'fitz',
        'PIL',
        'webview',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'mcp',
        'zimi_mcp',
        'matplotlib',
        'numpy',
        'scipy',
        'pandas',
        'IPython',
        'jupyter',
        'tkinter',
        'pystray',
    ],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Zimi',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon='assets/icon.icns' if platform.system() == 'Darwin' else 'assets/icon.ico',
)

# contents_directory='.' flattens the _internal/ subfolder on Windows/Linux
# so users see Zimi.exe + libs in one clean folder (macOS uses .app bundle)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Zimi',
    contents_directory='.' if platform.system() != 'Darwin' else '_internal',
)

# macOS: wrap into .app bundle
if platform.system() == 'Darwin':
    app = BUNDLE(
        coll,
        name='Zimi.app',
        icon='assets/icon.icns',
        bundle_identifier='io.zosia.zimi',
        info_plist={
            'CFBundleShortVersionString': '1.3.0',
            'LSUIElement': False,  # show in Dock (native window app)
            'NSLocalNetworkUsageDescription': 'Zimi runs a local server on this computer to display your offline library. It does not access other devices.',
            'NSAppTransportSecurity': {
                'NSAllowsArbitraryLoads': True,  # needed for localhost HTTP
            },
        },
    )
