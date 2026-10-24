"""Snapshots of an instance: what `cointoss snapshot` writes and `cointoss restore` reads back.

A snapshot is a directory holding `cointoss.db`, whichever of `cointoss.yaml` and
`woodglue.yaml` the instance has, and `manifest.json`. It never holds `auth.db`, woodglue's
`mounts/`, logs or the pid file: a snapshot is data, not credentials, and a restored instance
gets its own token from `cointoss init`. Naming snapshots and pruning old ones is the caller's.

`take` holds the sweep lock for the whole copy, waiting for it as a scheduled sweep does, so no
sweep writes between the database and the configs. It copies the database with SQLite's online
backup API, which is consistent while `wgl` serves. The manifest is written last, so an
interrupted snapshot has none and `restore` refuses it.

A rollback has to restore the database together with the code that wrote it, since the store
migrates a database on open and an older build refuses a newer one. So `restore` refuses a
snapshot whose schema is newer than this build reads, before writing anything. It never
replaces a database: a target that already holds `cointoss.db` is refused, and the user moves
it aside. Configs from the snapshot replace the target's. A `woodglue.yaml` written before
woodglue 0.0.7 names `init.data_dir` on the cointoss fragment entry, which would point the
restored instance at the old directory, so `restore` removes it and the fragment takes the
directory `wgl --data` gives it.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import os
import shutil
from contextlib import ExitStack
from importlib.metadata import version
from pathlib import Path
from typing import Any, ClassVar, cast

from pydantic import BaseModel, ConfigDict, ValidationError
from ruamel.yaml import YAML
from woodglue.config import CONFIG_FILENAME as WOODGLUE_CONFIG

from cointoss.app import FRAGMENT_GREF, SWEEP_LOCK_WAIT, SweepInProgress, sweep_lock
from cointoss.config import CONFIG_FILENAME, DB_FILENAME
from cointoss.store import SCHEMA_VERSION, backup_database

__all__ = ["CONFIGS", "MANIFEST", "Manifest", "SnapshotError", "read_manifest", "restore", "take"]

log = logging.getLogger(__name__)

MANIFEST = "manifest.json"
CONFIGS = (CONFIG_FILENAME, WOODGLUE_CONFIG)


class SnapshotError(Exception):
    """A snapshot that cannot be taken or restored, with the reason on one line."""


class Manifest(BaseModel):
    """What took a snapshot, and when; `schema_version` is the copied database's own."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    cointoss_version: str
    schema_version: int
    taken_at: dt.datetime


def take(data_dir: Path, dest: Path, *, wait: float = SWEEP_LOCK_WAIT) -> Manifest:
    """Snapshot the instance in `data_dir` into `dest`, which must not exist or must be empty."""
    db = data_dir / DB_FILENAME
    if not db.exists():
        raise SnapshotError(f"no database at {db}; nothing to snapshot")
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise SnapshotError(f"{dest} exists and is not an empty directory")
    dest.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        try:
            stack.enter_context(sweep_lock(data_dir))
        except SweepInProgress:
            log.info("the sweep lock is held; waiting up to %gs for it", wait)
            try:
                stack.enter_context(sweep_lock(data_dir, wait))
            except SweepInProgress as exc:
                raise SnapshotError(f"{exc}; no snapshot taken") from exc
        schema_version = backup_database(db, dest / DB_FILENAME)
        for name in CONFIGS:
            if (data_dir / name).exists():
                shutil.copy2(data_dir / name, dest / name)
    if schema_version is None:
        raise SnapshotError(f"{db} records no schema version; not a cointoss database")
    manifest = Manifest(
        cointoss_version=version("cointoss"),
        schema_version=schema_version,
        taken_at=dt.datetime.now(dt.UTC),
    )
    (dest / MANIFEST).write_text(manifest.model_dump_json(indent=2) + "\n")
    return manifest


def read_manifest(src: Path) -> Manifest:
    """The manifest of the snapshot in `src`, refusing one this build cannot restore."""
    path = src / MANIFEST
    if not path.is_file():
        raise SnapshotError(f"no {MANIFEST} in {src}; not a cointoss snapshot")
    try:
        manifest = Manifest.model_validate(json.loads(path.read_text()))
    except (ValueError, ValidationError) as exc:
        raise SnapshotError(f"{path}: not a snapshot manifest") from exc
    if not (src / DB_FILENAME).is_file():
        raise SnapshotError(f"no {DB_FILENAME} in {src}")
    if manifest.schema_version > SCHEMA_VERSION:
        raise SnapshotError(
            f"snapshot is at schema version {manifest.schema_version} (cointoss "
            f"{manifest.cointoss_version}), this build reads {SCHEMA_VERSION}; restore it with "
            f"that cointoss"
        )
    return manifest


def restore(src: Path, data_dir: Path) -> list[str]:
    """Copy the snapshot in `src` into `data_dir`; what was written, one line per file.

    Everything is checked before anything is written, and the database goes in last, under a
    temporary name first, so a failed copy leaves no `cointoss.db` behind to refuse a retry.
    """
    read_manifest(src)
    db = data_dir / DB_FILENAME
    if db.exists():
        raise SnapshotError(f"{db} exists; move it aside to restore over it")
    data_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name in CONFIGS:
        if not (src / name).exists():
            continue
        replaced = (data_dir / name).exists()
        text = (src / name).read_text()
        stripped = _without_data_dir(text) if name == WOODGLUE_CONFIG else None
        (data_dir / name).write_text(text if stripped is None else stripped)
        note = "; removed init.data_dir" if stripped is not None else ""
        written.append(f"{name}: {'replaced' if replaced else 'restored'}{note}")
    partial = db.with_name(db.name + ".restoring")
    shutil.copyfile(src / DB_FILENAME, partial)
    os.replace(partial, db)
    written.append(f"{DB_FILENAME}: restored")
    return written


def _without_data_dir(text: str) -> str | None:
    """`woodglue.yaml` text without `init.data_dir` on the cointoss fragment entry.

    None if no entry carries one. ruamel's round trip keeps the file's comments and layout.
    """
    yaml = YAML()
    doc = _mapping(cast(object, yaml.load(text)))
    changed = False
    for namespace in _mapping(doc.get("namespaces")).values():
        entries: object = _mapping(namespace).get("entries")
        for entry in cast("list[object]", entries) if isinstance(entries, list) else []:
            fragment = _mapping(entry)
            init = _mapping(fragment.get("init"))
            if fragment.get("gref") == FRAGMENT_GREF and "data_dir" in init:
                del init["data_dir"]
                if not init:
                    del fragment["init"]
                changed = True
    if not changed:
        return None
    out = io.StringIO()
    yaml.dump(doc, out)
    return out.getvalue()


def _mapping(value: object) -> dict[str, Any]:
    """`value` if it is a YAML mapping, else an empty one."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}
