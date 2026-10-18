"""DWG in and out through a free converter, and handing a written file to the OS.

ezdxf reads and writes DXF only. A DWG goes through the first converter found:

1. the ODA File Converter (free, opendesign.com): PLOMADA_ODA (the .exe, or the
   folder holding it), else the newest ``C:\\Program Files\\ODA\\ODAFileConverter*``.
   Its command line is ``ODAFileConverter <in_dir> <out_dir> <version> <type>
   <recurse> <audit> <filter>``: Plomada converts one file, ACAD2018, no
   recursion, with audit;
2. LibreDWG's ``dwg2dxf`` / ``dxf2dwg`` on PATH (GPL; dxf2dwg is experimental).

A converted DXF lands in a fresh temporary folder named TEMP_PREFIX..., which
the caller removes once it has read it.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .dxf import DxfPlanError

ODA_GLOB = r"C:\Program Files\ODA\ODAFileConverter*\ODAFileConverter.exe"
ODA_EXE = "ODAFileConverter.exe"
ODA_VERSION = "ACAD2018"
TEMP_PREFIX = "plomada-cad-"
TIMEOUT_S = 120.0  # s a conversion may take
HELP = (
    "install the free ODA File Converter (https://www.opendesign.com/guestfiles/oda_file_converter; set "
    "PLOMADA_ODA to its ODAFileConverter.exe if it is not under C:\\Program Files\\ODA) or put LibreDWG's "
    "dwg2dxf and dxf2dwg on PATH (https://www.gnu.org/software/libredwg/)"
)


def _version(exe: str) -> tuple[int, ...]:
    """The version numbers in an install folder's name: "ODAFileConverter 25.12.0" is (25, 12, 0)."""
    return tuple(int(n) for n in re.findall(r"\d+", Path(exe).parent.name))


def find_oda() -> Path | None:
    """The ODA File Converter: PLOMADA_ODA (an .exe or the folder holding it), else the newest install under
    C:\\Program Files\\ODA by the version in its folder name, compared as numbers (25.12 is newer than 9.12 and
    25.4); None when there is none."""
    env = os.environ.get("PLOMADA_ODA")
    if env:
        p = Path(env)
        p = p / ODA_EXE if p.is_dir() else p
        return p if p.is_file() else None
    found = sorted(glob.glob(ODA_GLOB), key=lambda exe: (_version(exe), exe))
    return Path(found[-1]) if found else None


def _run(args: list[str], what: str) -> None:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DxfPlanError(f"{what} failed to run {Path(args[0]).name}: {exc}") from None
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-1:] or ["no output"]
        raise DxfPlanError(f"{what} failed: {Path(args[0]).name} exited with {done.returncode} ({tail[0]})")


def convert(src: Path, out_dir: Path, to: str) -> Path:
    """Converts src (.dwg or .dxf) to the other format (to = "DXF" or "DWG") in out_dir; returns the file
    written there, named like src. Raises DxfPlanError naming both free converters when none is found."""
    suffix = "." + to.lower()
    out = out_dir / (src.stem + suffix)
    what = f"converting {src.name} to {to}"
    if (oda := find_oda()) is not None:
        _run([str(oda), str(src.parent), str(out_dir), ODA_VERSION, to, "0", "1", src.name], what)
    elif (tool := shutil.which("dwg2dxf" if to == "DXF" else "dxf2dwg")) is not None:
        _run([tool, "-y", "-o", str(out), str(src)], what)
    else:
        raise DxfPlanError(f"{src.name} is a {src.suffix[1:].upper()} and no converter was found; {HELP}")
    if not out.is_file():
        raise DxfPlanError(f"{what} wrote nothing: {out.name} is missing (is {src.name} a valid drawing?)")
    return out


def as_dxf(path: str | Path) -> Path:
    """A DXF for the drawing at path: a .dxf as it is, a .dwg converted into a temporary folder (TEMP_PREFIX,
    the caller removes it). Any other suffix is refused."""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".dxf":
        return p
    if suffix != ".dwg":
        raise DxfPlanError(f"{p.name} is not a .dxf or .dwg file")
    out_dir = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    try:
        return convert(p, out_dir, "DXF")
    except DxfPlanError:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise


def to_dwg(dxf: Path, dwg: Path) -> Path:
    """Converts a written DXF into the DWG at dwg (ACAD2018), replacing it; returns dwg."""
    out_dir = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    try:
        made = convert(dxf, out_dir, "DWG")
        dwg.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(made), str(dwg))
        return dwg
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def open_file(path: Path) -> None:
    """Opens the file with the OS default application (AutoCAD for .dwg and .dxf where it is installed)."""
    if sys.platform == "win32":
        os.startfile(path)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
