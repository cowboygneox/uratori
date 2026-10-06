"""Deterministic extraction over document pages (documents-plan-v3, D4).

This package is the engine-adjacent half of `extract`: pure functions over
one page's word layer and the records already produced for it, with no I/O
and no model at run time. The server owns the other half (storage, the
pass, the routes) under `uratori/server/`.
"""
