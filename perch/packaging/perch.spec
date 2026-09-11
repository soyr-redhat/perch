# Build on the target operating system: python -m PyInstaller packaging/perch.spec
import sys
import runpy
from pathlib import Path
root = Path(SPECPATH).parent
icons = runpy.run_path(str(root / 'packaging' / 'make_icons.py'))['make_icons'](root / 'build' / 'icons')
app_name = 'Perch'
entry = root / 'packaging' / 'entry.py'
analysis = Analysis([str(entry)], pathex=[str(root / 'src')],
    datas=[(str(root / 'src' / 'perch' / 'static'), 'perch/static')], hiddenimports=['webview', 'tomlkit', 'watchdog.observers',
    'webview.platforms.cocoa' if sys.platform == 'darwin' else 'webview.platforms.edgechromium'],
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'tkinter'], noarchive=False)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name=app_name, console=False,
          debug=False, strip=False, upx=False, icon=str(icons / 'perch.ico') if sys.platform == 'win32' else None)
cli = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name='perch-cli', console=True,
          debug=False, strip=False, upx=False)
collection = COLLECT(exe, cli, analysis.binaries, analysis.datas, name=app_name, strip=False, upx=False)
if sys.platform == 'darwin':
    app = BUNDLE(collection, name=app_name + '.app', bundle_identifier='dev.soyr.perch',
        icon=str(icons / 'perch.icns'), info_plist={'CFBundleShortVersionString':'0.3.0','LSBackgroundOnly':False,'NSHighResolutionCapable':True,
                    'NSHumanReadableCopyright':'Perch contributors'})
