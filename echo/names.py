"""Artist name normalization, the fallback key for entity resolution.

IDs are always preferred (Spotify ID, then MusicBrainz ID). Names are only used
to link records that arrive without a shared ID, e.g. a demo or Last.fm artist
that has no Spotify ID yet. The normalization is deliberately conservative:
it removes differences in accents, case, punctuation and "&" vs "and", but it
does not drop words like "The", since that merges different artists more often
than it fixes real mismatches.
"""

from __future__ import annotations

import re
import unicodedata

_CONNECTORS = re.compile(r"\s*[&+]\s*")
_NON_ALNUM = re.compile(r"[^\w\s]", flags=re.UNICODE)
_SPACES = re.compile(r"\s+")


def normalize_artist_name(name: str) -> str:
    """'Beyoncé' -> 'beyonce'; 'Florence + The Machine' -> 'florence and the machine'."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = _CONNECTORS.sub(" and ", text)
    text = _NON_ALNUM.sub("", text).replace("_", "")
    text = _SPACES.sub(" ", text).strip()
    # Names made only of symbols (rare, but real) would otherwise collapse to "".
    return text or name.strip().casefold()
