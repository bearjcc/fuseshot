"""PyInstaller entry: review UI with no args, or fuseshot subcommands with argv."""

import sys

from fuseshot import runtime

if __name__ == "__main__":
    runtime.seed_environment()
    if len(sys.argv) > 1:
        from fuseshot.__main__ import main

        main()
    else:
        from fuseshot.desktop import run_app

        run_app()
