"""Generate the README skill catalog from SKILL.md headings (stdlib only)."""

import argparse
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
START = "<!-- skills:start -->"
END = "<!-- skills:end -->"


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def generate(readme, skills):
    if readme.count(START) != 1 or readme.count(END) != 1:
        raise ValueError("README.md must contain exactly one skills marker pair")
    before, rest = readme.split(START)
    if END not in rest:
        raise ValueError("README.md skills markers are out of order")
    _, after = rest.split(END)
    if not skills:
        raise ValueError("No skills/*/SKILL.md files found")
    rows = ["| Skill | 用途 |", "|---|---|"]
    for path, content in sorted(skills.items()):
        name = path.split("/")[1]
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
            raise ValueError(f"Invalid skill directory name: {name}")
        heading = re.search(r"^#\s+(.+?)\s*$", content, re.MULTILINE)
        if not heading:
            raise ValueError(f"Missing level-one heading: {path}")
        title = heading[1].replace("|", "&#124;")
        rows.append(f"| [`{name}`]({path}) | {title} |")
    return before + START + "\n\n" + "\n".join(rows) + "\n\n" + END + after


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail on catalog drift")
    parser.add_argument("--staged", action="store_true", help="Check the Git index")
    args = parser.parse_args()
    if args.staged and not args.check:
        parser.error("--staged requires --check")
    if args.staged:
        paths = git("ls-files", "--cached", "-z", "--", "skills").decode("utf-8").split("\0")
        skills = {
            path: git("show", f":{path}").decode("utf-8").replace("\r\n", "\n")
            for path in paths
            if re.fullmatch(r"skills/[^/]+/SKILL\.md", path)
        }
        readme = git("show", ":README.md").decode("utf-8").replace("\r\n", "\n")
    else:
        skills = {
            path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
            for path in (ROOT / "skills").glob("*/SKILL.md")
        }
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
    expected = generate(readme, skills)
    if args.check:
        if readme != expected:
            print("README skill catalog is stale. Run: python scripts/update-readme.py", file=sys.stderr)
            print("Then stage README.md together with the skill changes and retry.", file=sys.stderr)
            return 1
        print(f"README catalog matches {len(skills)} skills" + (" in the Git index." if args.staged else "."))
    else:
        (ROOT / "README.md").write_text(expected, encoding="utf-8", newline="\n")
        print(f"Updated README catalog: {len(skills)} skills.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"README catalog check failed: {error}", file=sys.stderr)
        sys.exit(1)
