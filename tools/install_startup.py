#!/usr/bin/env python3
"""Install at an explicit marker after the user's environment initialization."""
import argparse
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import time

BEGIN = '# BEGIN instabox-controller startup'
END = '# END instabox-controller startup'
POSITION = '# INSTABOX_CONTROLLER_START'


def place_hook(original, block):
    lines = original.splitlines(keepends=True)
    begins = [i for i, line in enumerate(lines) if line.strip() == BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == END]
    if len(begins) != len(ends) or len(begins) > 1 or (begins and begins[0] >= ends[0]):
        raise ValueError('Malformed existing startup block')
    if begins:
        del lines[begins[0]:ends[0] + 1]
    positions = [i for i, line in enumerate(lines) if line.strip() == POSITION]
    if len(positions) > 1:
        raise ValueError('Use exactly one startup position marker')
    if not positions:
        # Creating an empty entry point is safe. Never guess the initialization
        # boundary of an existing shell script, function, conditional or heredoc.
        if any(line.strip() and not line.lstrip().startswith('#') for line in lines):
            raise ValueError('Add # INSTABOX_CONTROLLER_START after environment initialization and before the final exec/exit')
        if not lines or not lines[0].startswith('#!'):
            lines.insert(0, '#!/bin/sh\n')
        if not lines[-1].endswith('\n'):
            lines[-1] += '\n'
        lines.append(POSITION + '\n')
        positions = [len(lines) - 1]
    index = positions[0]
    if not lines[index].endswith('\n'):
        lines[index] += '\n'
    lines.insert(index + 1, block)
    return ''.join(lines)


def install(script, repo, data, python=sys.executable):
    script, repo, data = Path(script), Path(repo).resolve(), Path(data).resolve()
    original = script.read_text() if script.exists() else '#!/bin/sh\n'
    command = ' '.join(shlex.quote(str(item)) for item in [python, repo / 'tools/controller_service.py', 'start', '--data-dir', data])
    block = f'{BEGIN}\n{command} || echo "instabox-controller startup failed; inspect its log" >&2\n{END}\n'
    updated = place_hook(original, block)
    if updated == original:
        return None
    script.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if script.exists():
        backup = script.with_name(script.name + f'.backup-{time.time_ns()}')
        shutil.copy2(script, backup)
    fd, temporary = tempfile.mkstemp(prefix='.startup-', dir=script.parent)
    try:
        with os.fdopen(fd, 'w') as file:
            file.write(updated)
            file.flush()
            os.fsync(file.fileno())
        mode = script.stat().st_mode & 0o777 if script.exists() else 0o700
        os.chmod(temporary, mode)
        os.replace(temporary, script)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return backup


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--script', type=Path, required=True)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()
    backup = install(args.script, args.repo, args.data_dir)
    print('Startup hook installed; existing commands retained. No service started.')
    if backup:
        print('Backup:', backup)


if __name__ == '__main__':
    main()
