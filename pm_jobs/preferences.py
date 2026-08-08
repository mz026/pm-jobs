"""Load preferences.yaml — what to drop, what to tag, which model to ask.

The hash matters as much as the values. Every review run records the hash of
the preferences that produced it, so when a verdict looks wrong you can tell
whether it came from the rules you have now or the rules you had last week.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import ConfigError

DEFAULT_PREFERENCES_PATH = Path("preferences.yaml")

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_EFFORT = "low"
DEFAULT_SUMMARY_SENTENCES = 3


@dataclass(frozen=True)
class Preferences:
    speak: tuple[str, ...]
    role_includes: tuple[str, ...]
    tags: dict[str, str]
    model: str
    effort: str
    summary_sentences: int
    path: Path = DEFAULT_PREFERENCES_PATH
    _hash: str = field(default="", compare=False)

    @property
    def hash(self) -> str:
        return self._hash

    @property
    def tag_names(self) -> tuple[str, ...]:
        return tuple(self.tags)

    def role_pattern(self) -> re.Pattern:
        """Title matcher built from the configured role phrases.

        Phrases are matched as whole words so 'product lead' doesn't also match
        'Product Leadership Coordinator', and are escaped so a phrase with
        punctuation can't quietly become a regex.
        """
        alts = "|".join(re.escape(p.strip()) for p in self.role_includes if p.strip())
        return re.compile(rf"\b({alts})\b", re.IGNORECASE)


def _require(mapping: dict, key: str, where: str):
    if key not in mapping or mapping[key] in (None, "", [], {}):
        raise ConfigError(f"{where}: missing required key {key!r}")
    return mapping[key]


def load_preferences(path: Path | str = DEFAULT_PREFERENCES_PATH) -> Preferences:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"preferences file not found: {path}")

    raw = yaml.safe_load(path.read_text()) or {}

    languages = _require(raw, "languages", str(path))
    speak = _require(languages, "speak", f"{path}: languages")
    if isinstance(speak, str):
        speak = [speak]

    roles = _require(raw, "roles", str(path))
    includes = _require(roles, "include", f"{path}: roles")
    if isinstance(includes, str):
        includes = [includes]

    tags = _require(raw, "tags", str(path))
    if not isinstance(tags, dict):
        raise ConfigError(f"{path}: 'tags' must be a mapping of tag name -> description")
    tags = {str(k): " ".join(str(v).split()) for k, v in tags.items()}

    review = raw.get("review") or {}

    # Hash only what changes a verdict. The model and effort do; a reworded
    # comment in the file does not.
    material = json.dumps(
        {"speak": sorted(speak), "roles": sorted(includes), "tags": tags,
         "model": review.get("model", DEFAULT_MODEL)},
        sort_keys=True, ensure_ascii=False,
    )

    return Preferences(
        speak=tuple(speak),
        role_includes=tuple(includes),
        tags=tags,
        model=str(review.get("model", DEFAULT_MODEL)),
        effort=str(review.get("effort", DEFAULT_EFFORT)),
        summary_sentences=int(review.get("summary_sentences", DEFAULT_SUMMARY_SENTENCES)),
        path=path,
        _hash=hashlib.sha256(material.encode("utf-8")).hexdigest()[:16],
    )
