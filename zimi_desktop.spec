# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Zimi Desktop.

Build:
    pyinstaller zimi_desktop.spec

Output:
    dist/Zimi/          — one-dir bundle (all platforms)
    dist/Zimi.app/      — macOS app bundle (macOS only)
"""

import platform

block_cipher = None

a = Analysis(
    ['zimi_desktop.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('templates', 'templates'),
        ('assets', 'assets'),
    ],
    hiddenimports=[
        'zimi',
        'libzim',
        'libzim._libzim',
        'libzim.reader',
        'libzim.search',
        'libzim.suggestion',
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

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Zimi',
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
