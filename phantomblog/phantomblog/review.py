"""Read-only, complete source comparison for a human reviewing a proposal."""

import re

from .core import Invalid, encoded

MISSING = object()
LABELS = {
    "en": "English",
    "es": "Spanish",
    "imageAlt": "Image description",
    "bodyFile": "Body source file",
    "publishedAt": "Publication date",
    "socialImage": "Social preview image",
    "templateFile": "HTML template",
    "css": "CSS",
    "url": "URL",
}


def compare(before, after):
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise Invalid("Proposal source must be an object")
    changes = []

    def label(key):
        return LABELS.get(
            key,
            re.sub(r"([a-z])([A-Z])", r"\1 \2", str(key))
            .replace("_", " ")
            .capitalize(),
        )

    def walk(old, new, path=(), group="Publication"):
        if old is new or (
            old is not MISSING and new is not MISSING and encoded(old) == encoded(new)
        ):
            return
        if (
            path in (("articles",), ("categories",))
            and isinstance(old, list)
            and isinstance(new, list)
            and all(
                isinstance(v, dict) and isinstance(v.get("slug"), str)
                for v in old + new
            )
        ):
            previous = {v["slug"]: v for v in old}
            proposed = {v["slug"]: v for v in new}
            if len(previous) == len(old) and len(proposed) == len(new):
                for key in sorted(previous.keys() | proposed.keys()):
                    walk(
                        previous.get(key, MISSING),
                        proposed.get(key, MISSING),
                        (),
                        ("Article" if path[0] == "articles" else "Category")
                        + ": "
                        + key,
                    )
                # Source order also matters for the catalogue's presentation.
                if previous.keys() == proposed.keys() and list(previous) != list(
                    proposed
                ):
                    walk(list(previous), list(proposed), ("Order",), label(path[0]))
                return
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(old.keys() | new.keys()):
                next_group = "Connection: " + key if path == ("connections",) else group
                next_path = () if path == ("connections",) else path
                walk(
                    old.get(key, MISSING),
                    new.get(key, MISSING),
                    next_path + (key,),
                    next_group,
                )
            return
        changes.append(
            {
                "group": group,
                "field": " / ".join(label(k) for k in path) or "Record",
                "kind": "added"
                if old is MISSING
                else "removed"
                if new is MISSING
                else "changed",
                "beforePresent": old is not MISSING,
                "afterPresent": new is not MISSING,
                "before": None if old is MISSING else old,
                "after": None if new is MISSING else new,
            }
        )

    walk(before, after)
    return changes
