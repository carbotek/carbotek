# pyright: strict, reportPrivateUsage=false
"""``python -m carbotek`` runs the command line interface."""

from .cli import main

raise SystemExit(main())
