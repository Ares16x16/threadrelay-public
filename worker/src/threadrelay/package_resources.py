"""Explicit frozen-resource diagnostic. No process/service or acceptance grant."""

from importlib.resources import files
import json


def verify_resources():
    schema = json.loads(
        files("threadrelay").joinpath("protocol.schema.json").read_text()
    )
    if "Command" not in schema.get("definitions", {}):
        raise ValueError("Bundled contract unavailable")
    migrations = files("threadrelay.storage").joinpath("migrations")
    names = sorted(p.name for p in migrations.iterdir() if p.name.endswith(".sql"))
    if not names or any(
        not migrations.joinpath(name).read_text().strip() for name in names
    ):
        raise ValueError("Bundled migrations unavailable")
    from .inference.provision import load_catalog

    if (
        load_catalog()["runtime"]["commit"]
        != "8ca4060b7e30e76baaa5670ee1ec1b760b7f8552"
    ):
        raise ValueError("Bundled runtime catalog unavailable")
    if not files("jsonschema_specifications").joinpath("schemas").is_dir():
        raise ValueError("Bundled JSON Schema resources unavailable")
    from .collector.owned_playwright import _verify_playwright_pin

    _verify_playwright_pin()
    return {"resources": "verified", "native_acceptance": "Not run"}
