"""The Interactive Brokers adapter: the only package that may import the broker's client library.

`translate` is pure and holds every assumption about what the gateway sends; `client` is the seam to the
library; `adapter` is the venue; `probe` measures what the paper account can actually do.
"""
