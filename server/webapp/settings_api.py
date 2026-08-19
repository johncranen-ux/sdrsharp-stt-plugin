"""The settings form: schema-driven rendering, and the one rule that outranks everything else
here -- a secret's value must never leave this process.

`form()` builds what the browser is allowed to see: every field's type, label and current
value, except a SECRET, which is reduced to whether it is set. `apply()` validates a submission
through the existing schema coercion (webapp.settings_schema.validate_value) and reports what
changed and which managed processes need restarting to pick it up -- a setting a process only
reads at its own startup is not in effect until that process restarts, and a form that does not
say so lies about what is currently running.
"""
from __future__ import annotations

from dataclasses import dataclass

from webapp.settings_schema import BY_KEY, SETTINGS, SettingSpec, SettingType, validate_value

# Submitting this for a SECRET field clears the stored value. An EMPTY field cannot mean the
# same thing: the form never shows a secret's value, so an empty box is indistinguishable from
# "the operator left it alone" and must be read that way.
CLEAR = "__CLEAR__"


class Invalid(ValueError):
    """A submitted value that failed schema validation, or a key outside the catalogue.

    Always names the offending key, both as an attribute (for callers that want it structured)
    and in the message (for callers -- including the API layer -- that just show str(exc)).
    """

    def __init__(self, key: str, message: str):
        self.key = key
        # validate_value's own ValueError already names the key (settings_schema.py does this
        # deliberately, since Phase 2 renders these messages into an HTTP response). Naming it
        # again here would read as "PROXY_PORT: PROXY_PORT: expected a whole number...".
        super().__init__(message if message.startswith(f"{key}:") else f"{key}: {message}")


@dataclass(frozen=True)
class Applied:
    values: dict[str, str]
    changed: set[str]
    restart_needed: set[str]


def _restart_targets(key: str, spec: SettingSpec) -> set[str]:
    """Which managed processes carry this key in the environment (or argv) they were started
    with, and so must restart for a change to take effect.

    Not a simple `if spec.exported` gate: AIS_STATION_* is passed to the counter as argv, not
    env, and WEBAPP_* is never passed to a child at all (settings_schema.SettingSpec.exported
    says so explicitly) -- the panel itself reads those at ITS OWN startup, so they still need a
    restart, just of a different process. Prefix wins over the schema's `exported` flag; that
    flag only decides the fallback, "does the proxy read this from its environment".
    """
    if key.startswith("AIS_STATION_"):
        return {"counter"}
    if key.startswith("WEBAPP_"):
        return {"panel"}
    if spec.exported:
        return {"proxy"}
    return set()


def form(values: dict[str, str]) -> dict:
    """Groups in schema order, each carrying its keys; fields keyed by setting, each with type,
    label, description and current value -- a SECRET replaced by whether it is set, never what
    it is set to.
    """
    groups: list[dict] = []
    by_group: dict[str, dict] = {}
    fields: dict[str, dict] = {}

    for spec in SETTINGS:
        group = by_group.get(spec.group)
        if group is None:
            group = {"name": spec.group, "keys": []}
            by_group[spec.group] = group
            groups.append(group)
        group["keys"].append(spec.key)

        entry: dict = {
            "type": spec.type.value,
            "label": spec.key,
            "description": spec.description,
            "group": spec.group,
        }
        if spec.type is SettingType.SECRET:
            entry["set"] = bool(values.get(spec.key))
        else:
            entry["value"] = values.get(spec.key, spec.default)
            if spec.choices:
                entry["choices"] = list(spec.choices)
            if spec.minimum is not None:
                entry["minimum"] = spec.minimum
            if spec.maximum is not None:
                entry["maximum"] = spec.maximum
        fields[spec.key] = entry

    return {"groups": groups, "fields": fields}


def apply(values: dict[str, str], submitted: dict[str, str]) -> Applied:
    """Validate `submitted` against the catalogue and merge it over `values`.

    Raises Invalid, naming the key, on an unknown key or one that fails
    settings_schema.validate_value -- an unknown key is refused outright rather than stored, and
    a bad value must never reach config_store.save.
    """
    unknown = sorted(set(submitted) - set(BY_KEY))
    if unknown:
        raise Invalid(unknown[0], "not a setting")

    new_values = dict(values)
    changed: set[str] = set()
    restart_needed: set[str] = set()

    for key, raw in submitted.items():
        spec = BY_KEY[key]

        if spec.type is SettingType.SECRET and raw == "":
            # The form cannot show the current value, so an empty box cannot mean "clear it" --
            # only the CLEAR sentinel can. Leave the stored value untouched and unreported.
            continue

        candidate = "" if raw == CLEAR else raw
        try:
            new_value = validate_value(spec, candidate)
        except ValueError as exc:
            raise Invalid(key, str(exc)) from exc

        new_values[key] = new_value
        if new_value != values.get(key, spec.default):
            changed.add(key)
            restart_needed |= _restart_targets(key, spec)

    return Applied(values=new_values, changed=changed, restart_needed=restart_needed)
