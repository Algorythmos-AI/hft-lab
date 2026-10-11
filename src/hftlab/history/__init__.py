"""Imported history: tick files the owner exported himself, read from disk into a store of their own (ADR 0028).

The platform's own recordings come first. Before enough of them exist, a model can be fitted to history from
elsewhere, and only fitted: results on somebody else's quotes are not this venue's results, and no imported day
counts towards a promotion.

`describe` is what the owner states about a folder of files, `parsers` splits a file's lines, `times` reads a
time in a stated format and zone, `rows` applies the stated rule to every row, and `importer` writes the days.
Nothing here downloads anything, names a server or imports a network library: the files are on a disk because
the owner put them there, under terms he accepted and this platform has not verified. Every imported day says
so in its own journal: `source: imported`, `terms_verified: false`.
"""
