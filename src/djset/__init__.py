"""Personal Spotify playlist & DJ set generator.

Three layers, strictly separated:

1. :mod:`djset.spotify`   — what music exists, and writing finished sets back.
2. :mod:`djset.enrichment` — the local BPM/key database Spotify cannot provide.
3. filter + matching       — pure functions over SQLite, zero network.

Layer 3 must never import layer 1.
"""

__version__ = "0.1.0"
