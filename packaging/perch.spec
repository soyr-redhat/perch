# Build on the target operating system: python -m PyInstaller packaging/perch.spec
import sys
import os
import runpy
from pathlib import Path
root = Path(SPECPATH).parent
icons = runpy.run_path(str(root / 'packaging' / 'make_icons.py'))['make_icons'](root / 'build' / 'icons')
demo_build = os.environ.get('PERCH_DEMO_BUILD') == '1'
app_name = 'Perch Demo' if demo_build else 'Perch'
entry = root / 'packaging' / 'smoke_app.py' if demo_build else root / 'perch.py'
analysis = Analysis([str(entry)], pathex=[str(root)],
    datas=[(str(root / 'static'), 'static')], hiddenimports=['webview', 'tomlkit', 'watchdog.observers',
    'webview.platforms.cocoa' if sys.platform == 'darwin' else 'webview.platforms.edgechromium'],
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'tkinter'], noarchive=False)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name=app_name, console=False,
          debug=False, strip=False, upx=False, icon=str(icons / 'perch.ico') if sys.platform == 'win32' else None)
collection = COLLECT(exe, analysis.binaries, analysis.datas, name=app_name, strip=False, upx=False)
if sys.platform == 'darwin':
    app = BUNDLE(collection, name=app_name + '.app', bundle_identifier='dev.soyr.perch.demo' if demo_build else 'dev.soyr.perch',
        icon=str(icons / 'perch.icns'), info_plist={'CFBundleShortVersionString':'0.2.0','NSHighResolutionCapable':True,
                    'NSHumanReadableCopyright':'Perch contributors'})
