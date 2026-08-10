"""HTTP transport over the same functions the CLI calls.

The API process is the only thing that talks to Apple: it owns a single
long-lived `Fetcher`, so its token bucket is the one authority on the 15
requests/minute the iTunes endpoints allow per IP. A second fetching process
on the same host means a second bucket and a guaranteed 403.
"""
