#!/usr/bin/env python3
"""Verify a downloaded Rocky GenericCloud QCOW2 and convert it once to VHDX."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


def checksum_from_file(path, filename):
    # Rocky publishes BSD-style SHA256 (filename) = digest checksums.
    text = Path(path).read_text()
    pattern = r'^SHA256\s*\(' + re.escape(filename) + r'\)\s*=\s*([0-9a-fA-F]{64})\s*$'
    matches = re.findall(pattern, text, re.MULTILINE)
    if len(matches) != 1:
        raise ValueError('Expected one SHA256 entry for the exact downloaded filename')
    return matches[0].lower()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--checksum-file', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output already exists; template replacement must be deliberate')
    expected = checksum_from_file(args.checksum_file, args.source.name)
    sha = hashlib.sha256()
    with args.source.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            sha.update(block)
    if sha.hexdigest() != expected:
        raise ValueError('QCOW2 SHA256 mismatch; refusing conversion')
    info = json.loads(subprocess.check_output(['qemu-img', 'info', '--output=json', str(args.source)], text=True))
    if info['format'] != 'qcow2' or info.get('backing-filename'):
        raise ValueError('Expected a standalone QCOW2 image without backing files')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix('.converting.vhdx')
    if temporary.exists():
        raise ValueError('A previous conversion temp file exists; inspect it before retrying')
    try:
        subprocess.run(['qemu-img', 'convert', '-p', '-f', 'qcow2', '-O', 'vhdx',
                        '-o', 'subformat=dynamic', str(args.source), str(temporary)], check=True)
        temporary.rename(args.output)
    finally:
        temporary.unlink(missing_ok=True)
    print('Verified QCOW2 and created:', args.output)
    print('Use this as an unbooted Generation 2 template; never attach/boot the shared template directly.')


if __name__ == '__main__':
    main()
