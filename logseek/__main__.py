"""Lets you run `python -m logseek ...` (same as the `logseek` command)."""

import sys

from logseek.cli import main

sys.exit(main())
