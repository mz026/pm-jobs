"""Load searches.yaml into typed search definitions.

The point of this module is that a search's location is stated *once*. jobspy
takes location three different ways (`location`, `google_search_term`,
`country_indeed`) and silently searches the wrong place if they disagree, so
all three are derived here rather than hand-written in config.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path("searches.yaml")

KNOWN_BOARDS = {"linkedin", "indeed", "zip_recruiter", "google", "glassdoor", "bayt", "naukri", "bdjobs"}


class ConfigError(Exception):
    """searches.yaml is malformed. Message is aimed at whoever edits the file."""


@dataclass(frozen=True)
class Location:
    query: str
    region: str
    country: str
    indeed_country: str

    def google_term(self, search_term: str) -> str:
        """jobspy's Google scraper wants a prose query, not a location field.

        A country-wide search sets region == country; saying it twice ("jobs in
        Netherlands, Netherlands") is the kind of phrasing that skews results.
        """
        where = self.region if self.region == self.country else f"{self.region}, {self.country}"
        return f"{search_term} jobs in {where}"


@dataclass(frozen=True)
class Search:
    name: str
    terms: tuple[str, ...]
    location: Location
    boards: tuple[str, ...]
    results_wanted: int
    hours_old: int
    distance: int          # radius around location.query, in miles (jobspy's unit)
    fetch_descriptions: bool
    enabled: bool = True

    def jobs(self) -> list[tuple[str, str]]:
        """Every (term, board) pair this search expands into.

        Each pair is scraped and recorded independently so one board failing on
        one term does not lose the rest of the run.
        """
        return list(itertools.product(self.terms, self.boards))

    def jobspy_kwargs(self, term: str, board: str, hours_old: int | None = None) -> dict:
        """Build the jobspy call for one leg.

        `hours_old` overrides the configured window — a scrape that runs daily
        only needs to look back to the previous run, not the full window.
        """
        kwargs = {
            "site_name": [board],
            "search_term": term,
            "location": self.location.query,
            "country_indeed": self.location.indeed_country,
            "results_wanted": self.results_wanted,
            "hours_old": self.hours_old if hours_old is None else hours_old,
            "distance": self.distance,
            "description_format": "markdown",
            "verbose": 0,
        }
        if board == "google":
            kwargs["google_search_term"] = self.location.google_term(term)
        if board == "linkedin":
            kwargs["linkedin_fetch_description"] = self.fetch_descriptions
        return kwargs


@dataclass
class Config:
    searches: list[Search] = field(default_factory=list)
    path: Path = DEFAULT_CONFIG_PATH

    def enabled(self) -> list[Search]:
        return [s for s in self.searches if s.enabled]

    def get(self, name: str) -> Search:
        for search in self.searches:
            if search.name == name:
                return search
        known = ", ".join(s.name for s in self.searches) or "(none defined)"
        raise ConfigError(f"no search named {name!r} in {self.path}. Known searches: {known}")


def _require(mapping: dict, key: str, where: str):
    if key not in mapping or mapping[key] in (None, "", []):
        raise ConfigError(f"{where}: missing required key {key!r}")
    return mapping[key]


def _parse_location(raw: dict, where: str) -> Location:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: 'location' must be a block with query/region/country/indeed_country")
    return Location(
        query=_require(raw, "query", where),
        region=_require(raw, "region", where),
        country=_require(raw, "country", where),
        indeed_country=_require(raw, "indeed_country", where),
    )


def _parse_search(raw: dict, defaults: dict, index: int) -> Search:
    if not isinstance(raw, dict):
        raise ConfigError(f"searches[{index}]: expected a mapping, got {type(raw).__name__}")

    name = _require(raw, "name", f"searches[{index}]")
    where = f"search {name!r}"

    terms = _require(raw, "terms", where)
    if isinstance(terms, str):
        terms = [terms]

    boards = raw.get("boards", defaults.get("boards", ["linkedin", "indeed"]))
    if isinstance(boards, str):
        boards = [boards]
    unknown = sorted(set(boards) - KNOWN_BOARDS)
    if unknown:
        raise ConfigError(f"{where}: unknown board(s) {unknown}. Known boards: {sorted(KNOWN_BOARDS)}")

    return Search(
        name=name,
        terms=tuple(terms),
        location=_parse_location(_require(raw, "location", where), where),
        boards=tuple(boards),
        results_wanted=int(raw.get("results_wanted", defaults.get("results_wanted", 50))),
        hours_old=int(raw.get("hours_old", defaults.get("hours_old", 72))),
        distance=int(raw.get("distance", defaults.get("distance", 25))),
        fetch_descriptions=bool(raw.get("fetch_descriptions", defaults.get("fetch_descriptions", False))),
        enabled=bool(raw.get("enabled", True)),
    )


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")

    raw = yaml.safe_load(path.read_text()) or {}
    defaults = raw.get("defaults") or {}
    raw_searches = raw.get("searches")
    if not raw_searches:
        raise ConfigError(f"{path}: no searches defined")

    searches = [_parse_search(s, defaults, i) for i, s in enumerate(raw_searches)]

    duplicates = {s.name for s in searches if [x.name for x in searches].count(s.name) > 1}
    if duplicates:
        raise ConfigError(f"{path}: duplicate search name(s): {sorted(duplicates)}")

    return Config(searches=searches, path=path)
