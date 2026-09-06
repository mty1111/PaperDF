"""Build the versioned Windows executable and its SHA-256 checksum."""
import argparse
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def read_version():
    version = (ROOT / 'VERSION.txt').read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', version):
        raise ValueError('VERSION.txt must contain a numeric major.minor.patch version.')
    if any(int(part) > 65535 for part in version.split('.')):
        raise ValueError('Windows version components must fit in 16 bits.')
    return version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--print-version', action='store_true', help='Print the source version without building.')
    args = parser.parse_args()
    version = read_version()
    if args.print_version:
        print(version)
        return
    if sys.platform != 'win32':
        raise SystemExit('The Windows executable must be built on Windows.')

    from PyInstaller.utils.win32.versioninfo import load_version_info_from_text_file
    info = load_version_info_from_text_file(str(ROOT / 'version_info.txt'))
    major, minor, patch = (int(part) for part in version.split('.'))
    expected = ((major << 16) | minor, patch << 16)
    strings = {entry.name: entry.val for table in info.kids[0].kids for entry in table.kids}
    if ((info.ffi.fileVersionMS, info.ffi.fileVersionLS) != expected or
            (info.ffi.productVersionMS, info.ffi.productVersionLS) != expected or
            strings.get('FileVersion') != version or strings.get('ProductVersion') != version):
        raise SystemExit('Update version_info.txt to match VERSION.txt before building.')

    subprocess.run([
        sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed',
        '--name', 'PaperDF', '--icon', 'assets/icon.ico',
        '--add-data', 'assets/icon.png;assets', '--add-data', 'VERSION.txt;.',
        '--version-file', 'version_info.txt', 'pdf_metadata_renamer.py',
    ], cwd=ROOT, check=True)
    target = ROOT / 'dist' / f'PaperDF-v{version}-windows.exe'
    shutil.copy2(ROOT / 'dist' / 'PaperDF.exe', target)
    with target.open('rb') as stream:
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    target.with_suffix('.exe.sha256').write_text(f'{digest.hexdigest()}  {target.name}\n', encoding='utf-8')
    print(f'Built {target}\nSHA-256: {digest.hexdigest()}')


if __name__ == '__main__':
    main()
