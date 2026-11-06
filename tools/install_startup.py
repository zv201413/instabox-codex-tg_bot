#!/usr/bin/env python3
"""Insert an idempotent startup hook before the user's existing exec/exit commands."""
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


def install(script, repo, data, python=sys.executable):
    script, repo, data = Path(script), Path(repo).resolve(), Path(data).resolve()
    original = script.read_text() if script.exists() else '#!/bin/sh\n'
    if original.count(BEGIN) != original.count(END) or original.count(BEGIN) > 1:
        raise ValueError('Malformed existing startup block')
    command = ' '.join(shlex.quote(str(item)) for item in [python, repo / 'tools/controller_service.py', 'start', '--data-dir', data])
    block = f'{BEGIN}\n{command} || echo "instabox-controller startup failed; inspect its log" >&2\n{END}\n'
    if BEGIN in original:
        start, end = original.index(BEGIN), original.index(END) + len(END)
        updated = original[:start] + block.rstrip('\n') + original[end:]
    else:
        first, separator, rest = original.partition('\n')
        updated = first + '\n' + block + rest if first.startswith('#!') else '#!/bin/sh\n' + block + original
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
