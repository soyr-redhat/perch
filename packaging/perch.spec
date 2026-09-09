# Build on the target operating system: python -m PyInstaller packaging/perch.spec
import sys
from pathlib import Path
root = Path(SPECPATH).parent
analysis = Analysis([str(root / 'perch.py')], pathex=[str(root)],
    datas=[(str(root / 'static'), 'static')], hiddenimports=['webview', 'tomlkit', 'watchdog.observers',
    'webview.platforms.cocoa' if sys.platform == 'darwin' else 'webview.platforms.edgechromium'],
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'tkinter'], noarchive=False)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name='Perch', console=False,
          debug=False, strip=False, upx=False)
collection = COLLECT(exe, analysis.binaries, analysis.datas, name='Perch', strip=False, upx=False)
if sys.platform == 'darwin':
    app = BUNDLE(collection, name='Perch.app', bundle_identifier='dev.soyr.perch',
        info_plist={'CFBundleShortVersionString':'0.2.0','NSHighResolutionCapable':True,
                    'NSHumanReadableCopyright':'Perch contributors'})
