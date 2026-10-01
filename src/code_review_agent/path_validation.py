"""Portable validation for untrusted paths received in a patch."""

import re
from pathlib import Path, PurePosixPath, PureWindowsPath


class UnsafeDiffPathError(ValueError):
    """The patch contains a path that cannot be confined to a workspace."""


_DEVICE = re.compile(
    r"^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])$", re.IGNORECASE
)


def validate_diff_path(filename: str) -> str:
    """Reject unsafe syntax on every host, returning a relative POSIX path.

    Do not normalize away traversal or Windows aliases before checking them.
    /dev/null is a diff metadata sentinel, handled by the parser, never a path.
    """
    if not isinstance(filename, str) or not filename:
        raise UnsafeDiffPathError("Unsafe diff path: empty filename")
    normalized = filename.replace("\\", "/")
    parts = normalized.split("/")
    if (
        PurePosixPath(normalized).is_absolute()
        or PureWindowsPath(filename).drive
        or any(ord(char) < 32 or ord(char) == 127 for char in filename)
        or any(char in filename for char in ':*?"<>|')
        or any(
            part in ("", ".", "..")
            or part.endswith((" ", "."))
            or _DEVICE.fullmatch(part.split(".")[0].rstrip(" "))
            for part in parts
        )
    ):
        raise UnsafeDiffPathError(
            "Unsafe diff path: expected a portable relative filename"
        )
    return normalized


def safe_diff_path(root: Path, filename: str) -> Path:
    """Resolve a validated path and reject symlinks or junctions escaping root.

    Call immediately before filesystem access in private scanner workspaces.
    This does not provide atomic protection against a hostile local process
    replacing directories while the service is writing.
    """
    normalized = validate_diff_path(filename)
    boundary = Path(root).resolve()
    destination = (boundary / normalized).resolve()
    if not destination.is_relative_to(boundary) or destination == boundary:
        raise UnsafeDiffPathError("Unsafe diff path: workspace escape")
    return destination
