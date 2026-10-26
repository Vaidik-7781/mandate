# Vendored: Verifiable Intent reference implementation

- Upstream: https://github.com/agent-intent/verifiable-intent (Mastercard, Apache-2.0, draft v0.1)
- Commit: `356c29635f1c44df7de02edb58699ca9f29bece6`
- Copied unmodified from the package built at that commit (`verifiable_intent/`), plus its `LICENSE`.

Why vendored: the spec is a draft and the dependency used to be a `git+https` URL. A clone that cannot reach
GitHub, or an upstream force-push, would break `pip install`. The copy keeps the project installable offline and
makes the pinned behaviour part of this repository. Mandate's own code lives in `apps/api/mandate/`; nothing
here was edited. To update, replace this directory from a newer commit and re-run `pytest tests/test_vectors.py`.
