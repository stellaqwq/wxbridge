"""Archive an explicit source allowlist; never include runtime data or environments."""

import tomllib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def main():
    project = Path(__file__).resolve().parents[1]
    version = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    output = project / "dist" / f"wxbridge-{version}-source.zip"
    output.parent.mkdir(exist_ok=True)
    sources = [
        project / name
        for name in (
            "pyproject.toml",
            "uv.lock",
            "README.md",
            ".gitignore",
            "ONEBOT.md",
            "onebot.example.json",
            "start-onebot.ps1",
            "start-onebot.cmd",
        )
    ]
    for directory in ("src", "tests", "examples", "tools"):
        sources.extend((project / directory).rglob("*.py"))
    sources.extend((project / "src/wxbridge/onebot/static").glob("*"))
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for source in sorted(sources):
            relative = source.relative_to(project)
            archive.write(source, f"wxbridge-{version}/" + relative.as_posix())
    print(output)


if __name__ == "__main__":
    main()
