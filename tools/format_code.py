"""Format repository sources without traversing dependencies or runtime data."""

import argparse
import os
from pathlib import Path
import subprocess
import tomllib

import black


ROOT = Path(__file__).resolve().parents[1]
WEB_SUFFIXES = {'.js', '.mjs', '.html', '.css', '.json'}


def format_python(paths, check):
    settings = tomllib.loads((ROOT / 'pyproject.toml').read_text())['tool']['black']
    mode = black.Mode(
        line_length=settings['line-length'],
        target_versions={
            black.TargetVersion[value.upper()] for value in settings['target-version']
        },
        string_normalization=not settings.get('skip-string-normalization', False),
    )
    changes = 0
    for path in paths:
        # Keep the AST safety check; sequential processing also works in restricted containers.
        if black.format_file_in_place(
            ROOT / path,
            fast=False,
            mode=mode,
            write_back=black.WriteBack.CHECK if check else black.WriteBack.YES,
        ):
            changes += 1
            print(f'{"Would reformat" if check else "Reformatted"}: {path}', flush=True)
    print(f'Python: {len(paths)} files, {changes} formatting changes.', flush=True)
    return check and changes > 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Check without changing files')
    args = parser.parse_args()
    paths = sorted(
        set(
            subprocess.check_output(
                ['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
                cwd=ROOT,
            )
            .decode()
            .split('\0')
        )
    )
    sources = [Path(path) for path in paths if path and (ROOT / path).is_file()]
    python_files = [str(path) for path in sources if path.suffix == '.py']
    web_files = [str(path) for path in sources if path.suffix in WEB_SUFFIXES]
    prettier = Path(
        os.environ.get('OMS_PRETTIER', ROOT / 'frontend/node_modules/prettier/bin/prettier.cjs')
    ).resolve()
    if not prettier.is_file():
        parser.error('Install the pinned formatter first: npm --prefix frontend install')
    failed = format_python(python_files, args.check)
    command = ['node', str(prettier), '--check' if args.check else '--write', *web_files]
    failed |= subprocess.run(command, cwd=ROOT, check=False).returncode != 0
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
