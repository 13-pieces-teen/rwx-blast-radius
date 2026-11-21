"""Fetch only the competition-pinned SQLGlot source during the image build."""
import io
from pathlib import Path, PurePosixPath
import tarfile
import urllib.request

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
url = f"https://codeload.github.com/tobymao/sqlglot/tar.gz/{COMMIT}"
with urllib.request.urlopen(url, timeout=120) as response:
    content = response.read()
root = Path("/opt/sqlglot")
with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
    for member in archive.getmembers():
        parts = PurePosixPath(member.name).parts[1:]
        if not member.isfile() or not parts or parts[0] not in ("sqlglot", "tests", "LICENSE"):
            continue
        if ".." in parts or any("\\" in p or ":" in p for p in parts):
            raise ValueError("unsafe source archive")
        target = root.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.extractfile(member).read())
(root / "PINNED_COMMIT").write_text(COMMIT + "\n")
