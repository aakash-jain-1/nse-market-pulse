"""
Unit tests for sectors.py — the curated NSE symbol → sector map.

Pure static data + tiny helpers, so these just assert the map's integrity and the
lookup/canonicalisation behaviour.

Run: python test_sectors.py   (also works under pytest)
"""

from nse_pulse.eod import sectors as S


def test_reverse_index_matches_forward_map():
    # every symbol in every sector resolves back to a sector (first-wins on dupes)
    for sec, syms in S.SECTORS.items():
        for sym in syms:
            assert S.sector_of(sym) is not None


def test_no_empty_sectors():
    for sec, syms in S.SECTORS.items():
        assert syms, f"{sec} has no constituents"


def test_symbols_are_canonical():
    # symbols should already be upper/stripped so bhavcopy lookups match
    for sec, syms in S.SECTORS.items():
        for sym in syms:
            assert sym == sym.upper().strip() and sym


def test_all_sectors_sorted_and_complete():
    names = S.all_sectors()
    assert names == sorted(names)
    assert set(names) == set(S.SECTORS.keys())


def test_sector_of_known_and_unknown():
    assert S.sector_of("TCS") == "IT"
    assert S.sector_of("HDFCBANK") == "Banks"
    assert S.sector_of("RELIANCE") == "Energy"
    assert S.sector_of("NOTATICKER") is None
    assert S.sector_of("") is None
    assert S.sector_of(None) is None


def test_sector_of_is_case_insensitive():
    assert S.sector_of("tcs") == "IT"
    assert S.sector_of("  Infy ") == "IT"


def test_symbols_helper():
    it = S.symbols("IT")
    assert "TCS" in it and "INFY" in it
    assert S.symbols("Nonexistent") == []
    # returns a copy — mutating it must not corrupt the map
    it.append("ZZZ")
    assert "ZZZ" not in S.symbols("IT")


def test_coverage_counts_match():
    cov = S.coverage()
    assert cov["sectors"] == len(S.SECTORS)
    assert cov["symbols"] == len(S.SYMBOL_SECTOR)
    # a healthy curated map: many sectors, a few hundred names
    assert cov["sectors"] >= 12 and cov["symbols"] >= 150


def test_no_symbol_ambiguity_within_first_wins():
    # if a symbol appears in two sectors, the reverse map keeps exactly one
    from collections import Counter
    seen = Counter()
    for syms in S.SECTORS.values():
        for sym in syms:
            seen[S._canon(sym)] += 1
    # reverse map has one entry per distinct symbol regardless of dupes
    assert len(S.SYMBOL_SECTOR) == len(seen)


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
