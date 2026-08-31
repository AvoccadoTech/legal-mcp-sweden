"""Monitor over Domstolsverket's published case law.

The API has no search and no filters — `page` is its only parameter — so this
package mirrors the corpus locally and serves everything from there.

That constraint is also the point: the corpus lands on the firm's own disk, and
the watchlist of what the firm cares about never leaves it.
"""

__all__ = ["client", "format", "models", "store", "watch", "server"]
