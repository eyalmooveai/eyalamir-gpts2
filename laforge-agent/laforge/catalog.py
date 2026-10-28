import json
import re

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def check_ident(*names: str) -> None:
    for n in names:
        if not _IDENT.fullmatch(n or ""):
            raise ValueError(f"unsafe identifier: {n!r}")


class Catalog:
    """Allow-list of features: name -> plain-language definition."""

    def __init__(self, features: dict[str, str]):
        self.features = features

    def check(self, *names: str) -> None:
        check_ident(*names)
        for n in names:
            if n not in self.features:
                raise ValueError(f"feature not in catalog: {n!r}")

    @classmethod
    def from_json(cls, path: str) -> "Catalog":
        with open(path) as f:
            data = json.load(f)
        return cls(data if isinstance(data, dict) else {n: "" for n in data})
