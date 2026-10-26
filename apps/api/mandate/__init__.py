"""Mandate: an accountability layer for agent purchases on PayPal (prove, protect, resolve, attribute)."""

import warnings

__version__ = "0.1.0"

# The in-process PayPal simulator is driven through Starlette's TestClient; newer Starlette prints a harmless
# deprecation notice about its httpx backend on every start. Silence that one message only.
warnings.filterwarnings("ignore", message=".*httpx.*starlette.testclient.*", category=DeprecationWarning)
warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient`.*")
