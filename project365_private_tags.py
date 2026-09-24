"""Read and update digiKam's Private tag on Project365 working-copy sidecars."""

from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path


PRIVATE_TAG = "Private"
_TAG_NAMES = (
    "XMP-digiKam:TagsList",
    "XMP-lr:HierarchicalSubject",
    "XMP-dc:Subject",
)
_RDF_LI = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}li"
_TAG_QNAMES = (
    "{http://www.digikam.org/ns/1.0/}TagsList",
    "{http://ns.adobe.com/lightroom/1.0/}hierarchicalSubject",
    "{http://purl.org/dc/elements/1.1/}subject",
)


def private_sidecar_path(photo: Path) -> Path:
    return photo.with_name(photo.name + ".xmp")


def read_private_tag(photo: Path) -> bool:
    """Return True if any digiKam-compatible XMP tag array has root-level Private.

    A missing image or sidecar is unknown, not public, and raises an error.
    """
    if not photo.is_file():
        raise FileNotFoundError(f"Missing working copy: {photo}")
    sidecar = private_sidecar_path(photo)
    if not sidecar.is_file():
        raise FileNotFoundError(f"Missing working-copy sidecar: {sidecar}")
    root = ET.parse(sidecar).getroot()
    return any(
        (node.text or "").strip() == PRIVATE_TAG
        for tag_name in _TAG_QNAMES
        for tag in root.iter(tag_name)
        for node in tag.iter(_RDF_LI)
    )


def write_private_tag(photo: Path, private: bool) -> bool:
    """Set or remove Private, creating a digiKam-compatible sidecar when enabling."""
    if not photo.is_file():
        raise FileNotFoundError(f"Missing working copy: {photo}")
    sidecar = private_sidecar_path(photo)
    if sidecar.exists() and not sidecar.is_file():
        raise OSError(f"Working-copy sidecar is not a file: {sidecar}")
    sidecar_exists = sidecar.is_file()
    if sidecar_exists:
        current = read_private_tag(photo)
        if current == private:
            return current
    elif not private:
        return False
    exiftool = shutil.which("exiftool")
    if exiftool is None:
        raise RuntimeError("ExifTool is required to change Private metadata")
    operation = "+=" if private else "-="
    command = [exiftool, "-overwrite_original"]
    if not sidecar_exists:
        command.extend(["-o", str(sidecar)])
    command.extend(f"-{tag}{operation}{PRIVATE_TAG}" for tag in _TAG_NAMES)
    command.append(str(sidecar if sidecar_exists else photo))
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "ExifTool failed")
    updated = read_private_tag(photo)
    if updated != private:
        raise RuntimeError("Private tag did not round-trip through the sidecar")
    return updated
