# PyInstaller 6.22.3, target-native onedir console worker, never onefile/windowed.
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
# Collectors run before Analysis applies pathex (including isolated discovery
# children). Pin the checkout for both resource and hidden-module discovery;
# a stale installed worker wheel must never supply a second source of truth.
sys.path.insert(0, str(root/'worker/src'))
for name in list(sys.modules):
    if name == 'threadrelay' or name.startswith('threadrelay.'):
        del sys.modules[name]
packages = ['threadrelay', 'jsonschema', 'jsonschema_specifications', 'referencing', 'rpds', 'playwright']
datas = []
for package in packages:
    datas += collect_data_files(package, include_py_files=(package == 'playwright'))
for distribution in ['threadrelay-worker', 'jsonschema', 'jsonschema-specifications', 'referencing', 'rpds-py', 'playwright', 'greenlet', 'pyee']:
    datas += copy_metadata(distribution)
a = Analysis([str(root/'packaging/worker-entry.py')],
    pathex=[str(root/'worker/src')], binaries=[], datas=datas,
    hiddenimports=collect_submodules('threadrelay') + ['playwright.sync_api'],
    hookspath=[], runtime_hooks=[], excludes=['pytest', 'ruff'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True,
    name='threadrelay-worker', debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=True, disable_windowed_traceback=False,
    target_arch=None, codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='threadrelay-worker')
