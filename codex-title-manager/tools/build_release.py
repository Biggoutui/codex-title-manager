"""Build only reviewed source files; scan source and archive before publishing locally."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.1.0'
FILES = (
    'catalog_repair.py', 'history_titles.py', 'README.md', 'README.en.md',
    'SECURITY.md', 'LICENSE', 'requirements.txt', '.gitignore',
    'examples/rename-plan.example.json', '.github/workflows/tests.yml',
    'tests/test_tool.py', 'tests/test_release.py', 'tools/build_release.py',
)


def scan(data, deny):
    findings = []
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return ['non-text']
    if '\x00' in text: findings.append('non-text')
    if any(value and value.casefold() in text.casefold() for value in deny):
        findings.append('known-private-value')
    path_pattern = r'(?i)(?:[A-Z]:[\\/]+Users[\\/]+[^\s\\/]+|/' + r'home/[^\s/]+|/' + r'Users/[^\s/]+)'
    if re.search(path_pattern, text):
        findings.append('personal-path')
    if re.search(r'(?i)\bsk-[a-z0-9_-]{20,}|\bBearer\s+[a-z0-9._-]{20,}|\bgh[pousr]_[a-z0-9]{20,}', text):
        findings.append('secret-token')
    if re.search(r'(?i)[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}', text):
        findings.append('email-address')
    return findings


def build(output_dir, deny=()):
    if len(FILES) != len(set(FILES)): raise ValueError('Duplicate allowlist entry')
    files = {}
    for relative in FILES:
        path = ROOT / relative
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError('Invalid allowlist path')
        payload = path.read_bytes()
        findings = scan(payload, deny)
        if findings: raise ValueError('Privacy scan rejected ' + relative + ': ' + ','.join(findings))
        files[relative] = payload
    buffer = io.BytesIO()
    prefix = 'codex-title-manager-' + VERSION + '/'
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for relative, payload in sorted(files.items()):
            entry = zipfile.ZipInfo(prefix + relative, date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, payload)
    archive_bytes = buffer.getvalue()
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        if set(archive.namelist()) != {prefix + name for name in FILES}: raise ValueError('Archive allowlist mismatch')
        for name in archive.namelist():
            if scan(archive.read(name), deny): raise ValueError('Archive privacy scan failed')
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    zip_path = output_dir / ('codex-title-manager-' + VERSION + '.zip')
    fd, temp = tempfile.mkstemp(dir=output_dir, suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(archive_bytes); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, zip_path)
    finally:
        if os.path.exists(temp): os.unlink(temp)
    digest = hashlib.sha256(archive_bytes).hexdigest()
    (output_dir / 'SHA256SUMS.txt').write_text(digest + '  ' + zip_path.name + '\n', encoding='utf-8')
    report = {'version': VERSION, 'source_files': len(files), 'archive_files': len(files),
              'privacy_findings': 0, 'sha256': digest, 'known_deny_values_count': len(deny),
              'files': [{'path': name, 'sha256': hashlib.sha256(payload).hexdigest()} for name, payload in sorted(files.items())]}
    (output_dir / 'release-manifest.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--deny-file', type=Path, help='Private JSON array of additional forbidden strings; never distributed')
    args = parser.parse_args()
    deny = json.loads(args.deny_file.read_text(encoding='utf-8')) if args.deny_file else []
    if not isinstance(deny, list) or any(not isinstance(x, str) or not x for x in deny):
        raise ValueError('Deny file must contain nonempty strings')
    result = build(args.output_dir, deny)
    print(json.dumps({k:v for k,v in result.items() if k!='files'}, indent=2))


if __name__ == '__main__': main()
