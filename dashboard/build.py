"""Build the study dashboard from GitHub or a local snapshot."""

import argparse
from base64 import b64decode
from collections import Counter
from datetime import date, datetime, timedelta
from html import unescape
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
from tempfile import NamedTemporaryFile
import unicodedata
from urllib.parse import quote, unquote, urlsplit
from zoneinfo import ZoneInfo

from markdown_it import MarkdownIt

from comparison import compare_topic
from presentation import analyze_pdf


ROOT = Path(__file__).parent
DEFAULT_REPO = "cloud-club/10th-system-design-study"
PRESENTATION = re.compile(r"week\d+/presentation/.+\.(?:pdf|html)", re.I)
WEEK_FILE = re.compile(r"week(\d+)/.+", re.I)
FIELDS = {
    "architecture": r"아키텍처|설계도|시스템 구성|데이터 흐름",
    "decision": r"기술 선택|설계 선택|선택 근거|설계 결정",
    "tradeoff": r"트레이드\s*오프|trade[\s-]*off|장단점",
    "common": r"공통\s*고려|고려\s*사항",
}
IMAGE_LINK = re.compile(
    r"""!\[(?P<alt>[^\]\n]*)\]\(\s*(?P<target><[^>\n]+>|(?:[^\s()]|\([^()\n]*\))+)"""
    r"""(?:[ \t]+(?:"[^"\n]*"|'[^'\n]*'))?\s*\)|<img\b[^>]*>""", re.I
)
IMAGE_ATTRIBUTE = re.compile(r"""\s(src|alt)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".avif"}
MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"]).disable("image")
DESIGN_MARKDOWN_FIELDS = ("architecture", "decision", "tradeoff")


def normalized(text):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


def is_presentation(path, pr):
    return bool(PRESENTATION.fullmatch(path) or (
        WEEK_FILE.fullmatch(path) and path.lower().endswith(".pdf") and "발표" in pr.get("title", "")
    ))


def resolve_topic(topics, pr, paths):
    ids = {topic["id"] for topic in topics}
    labels = [label["name"].casefold().removeprefix("topic:") for label in pr.get("labels", [])
              if label["name"].casefold().startswith("topic:")]
    explicit = labels or re.findall(r"<!--\s*study-topic:\s*([\w-]+)\s*-->", pr.get("body") or "", re.I)
    if explicit:
        return explicit[0] if len(set(explicit)) == 1 and explicit[0] in ids else None

    title = pr.get("title", "")
    weeks = {int(week) for week in re.findall(r"\bweek[\s_-]*(\d+)", title, re.I)}
    weeks.update(int(match[1]) for path in paths if (match := WEEK_FILE.fullmatch(path)))
    if len(weeks) > 1:
        return None
    candidates = [topic for topic in topics if not weeks or topic["week"] in weeks]
    def matches(values):
        return [topic["id"] for topic in candidates
                if any(normalized(alias) in value for alias in topic["aliases"] for value in values)]

    filenames = [normalized(PurePosixPath(path).stem) for path in paths]
    if len(paths) == 1 and paths[0].lower().endswith(".pdf"):
        file_matches = matches(filenames)
        if file_matches:
            return file_matches[0] if len(file_matches) == 1 else None
    matched = matches([normalized(title), *filenames])
    if len(matched) == 1:
        return matched[0]
    if not matched and weeks and len(candidates) == 1:
        return candidates[0]["id"]
    return None


def design_sections(body):
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    lines = body.splitlines(keepends=True)
    tokens = MARKDOWN.parse(body)
    headings = [(token.map[0], token.map[1], int(token.tag[1]), tokens[index + 1].content)
                for index, token in enumerate(tokens) if token.type == "heading_open" and token.level == 0]
    consumed_until = 0
    for index, (start, end, level, heading) in enumerate(headings):
        if start < consumed_until:
            continue
        key = next((key for key, pattern in FIELDS.items() if re.search(pattern, heading, re.I)), None)
        if key:
            consumed_until = next((following[0] for following in headings[index + 1:]
                                   if following[2] <= level), len(lines))
            yield key, "".join(lines[end:consumed_until])


def design_fields(body):
    fields = {}
    for key, contents in design_sections(body):
        lines = contents.splitlines()
        literal_lines = {line for token in MARKDOWN.parse(contents) if token.type in {"fence", "code_block"}
                         for line in range(*token.map)}
        for index, line in enumerate(lines):
            if index in literal_lines:
                continue
            if key == "architecture":
                line = IMAGE_LINK.sub("", line)
            if line.strip().startswith(">") or line.strip() in {
                "-", "---", "**내용**", "**설계도**", "**설계 설명**", "내용",
            }:
                line = ""
            lines[index] = line
        markdown = "\n".join(lines).strip("\n")
        if markdown.strip() and key not in fields:
            fields[key] = markdown
    return fields


def branch_image_url(repo, ref, path):
    return f"https://raw.githubusercontent.com/{repo}/{quote(ref, safe='')}/{quote(path)}"


def image_url(target, repo, ref, directory):
    target = unescape(target)
    try:
        link = urlsplit(target)
    except ValueError:
        return None
    if link.scheme or link.netloc:
        if link.scheme == "https" and (
            link.netloc in {"user-images.githubusercontent.com", "raw.githubusercontent.com"} or
            (link.netloc == "github.com" and link.path.startswith("/user-attachments/assets/"))
        ):
            return target
        return None
    path = unquote(link.path)
    relative = posixpath.normpath(path.lstrip("/") if path.startswith("/") else posixpath.join(directory, path))
    if relative.startswith("../") or PurePosixPath(relative).suffix.lower() not in IMAGE_EXTENSIONS:
        return None
    return branch_image_url(repo, ref, relative)


def design_images(body, repo, ref, directory=""):
    for key, contents in design_sections(body):
        if key != "architecture":
            continue
        for match in IMAGE_LINK.finditer(contents):
            target, alt = match.group("target", "alt")
            if target is None:
                attributes = {name.lower(): next((value for value in values if value), "")
                              for name, *values in IMAGE_ATTRIBUTE.findall(match[0])}
                target, alt = attributes.get("src"), attributes.get("alt")
            if target and (url := image_url(target.strip("<>"), repo, ref, directory)):
                yield {"url": url, "alt": unescape(alt or "아키텍처 설계도")}


def github(endpoint, *options):
    return subprocess.check_output(["gh", "api", endpoint, *options], timeout=60)


def github_items(endpoint):
    return [item for page in json.loads(github(endpoint, "--paginate", "--slurp")) for item in page]


def read_github(repo, branch):
    pulls = github_items(f"repos/{repo}/pulls?state=all&base={quote(branch, safe='')}&per_page=100")
    active = [pr for pr in pulls if pr["state"] == "open" or pr.get("merged_at")]
    for pr in active:
        pr["files"] = github_items(f"repos/{repo}/pulls/{pr['number']}/files?per_page=100")
    tree = json.loads(github(f"repos/{repo}/git/trees/{quote(branch, safe='')}?recursive=1"))
    if tree.get("truncated"):
        raise ValueError("repository tree is truncated")
    base_files = [item["path"] for item in tree["tree"] if item["type"] == "blob" and
                  item["path"].lower().endswith((".pdf", ".html"))]
    return {"repo": repo, "branch": branch, "base_files": base_files, "pulls": active}


def source_reader(snapshot_path):
    def read(repo, ref, file):
        if snapshot_path:
            local = file.get("local")
            return (snapshot_path.parent / local).read_bytes() if local else None
        endpoint = f"repos/{repo}/contents/{quote(file['filename'])}?ref={quote(ref, safe='')}"
        if file["filename"].lower().endswith(".pdf"):
            metadata = json.loads(github(endpoint))
            if metadata["size"] > 20 * 1024 * 1024:
                return None
            if metadata["encoding"] != "base64":
                metadata = json.loads(github(f"repos/{repo}/git/blobs/{metadata['sha']}"))
            return b64decode(metadata["content"])
        return github(endpoint, "-H", "Accept: application/vnd.github.raw+json")
    return read


def source_location(repo, branch, pr):
    head = pr.get("head") or {}
    head_repo = head.get("repo") or {}
    if head_repo.get("full_name") and head.get("sha"):
        return head_repo["full_name"], head["sha"]
    return repo, pr.get("merge_commit_sha") or branch


def read_file(read, repo, ref, file):
    try:
        return read(repo, ref, file)
    except (OSError, subprocess.SubprocessError):
        print(f"Could not read {file['filename']}")
        return None


def meeting_date(week):
    return date(2026, 11, 4) + timedelta(weeks=week - 2)


def shared_considerations(designs):
    count = Counter()
    labels = {}
    for design in designs:
        seen = set()
        for line in design.get("common", "").splitlines():
            label = re.sub(r"^\s*[-*]\s*(?:\[[xX]\]\s*)?", "", line).strip()
            if label.startswith("[ ]") or not label:
                continue
            key = normalized(label)
            if key and key not in seen:
                seen.add(key)
                count[key] += 1
                labels.setdefault(key, label)
    return [labels[key] for key, total in count.items() if total >= 2]


def build_model(topics, snapshot, read, today, cache_dir=None):
    repo, branch = snapshot["repo"], snapshot["branch"]
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError("invalid repository name")
    result = {}
    for topic in topics:
        week = topic["week"]
        presentation = meeting_date(week)
        result[topic["id"]] = {
            **topic, "presentationDate": presentation.isoformat(),
            "deadline": (presentation + timedelta(days=5)).isoformat(),
            "reviewDate": (presentation + timedelta(days=7)).isoformat(),
            "presentations": [], "designs": [],
        }
    assets = {}
    base_files = {item if isinstance(item, str) else item["filename"]:
                  {"filename": item} if isinstance(item, str) else item
                  for item in snapshot.get("base_files", [])}
    for path, file in base_files.items():
        if is_presentation(path, {}):
            assets[path] = (repo, branch, file, {}, True)

    unassigned = []
    trusted_designs = set()
    pulls = sorted(snapshot["pulls"], key=lambda pr: (pr["state"] == "open", pr["number"]))
    for pr in pulls:
        is_open = pr["state"] == "open"
        if not is_open and not pr.get("merged_at"):
            continue
        files = [file for file in pr["files"] if file.get("status") != "removed"]
        pdfs = [file for file in files if is_presentation(file["filename"], pr)]
        docs = [file for file in files if WEEK_FILE.fullmatch(file["filename"]) and
                file["filename"].lower().endswith((".md", ".markdown")) and
                PurePosixPath(file["filename"]).parts[1].casefold() != "presentation"]
        head_repo, head_ref = source_location(repo, branch, pr)
        # Only repository members' open contributions can spend API credits.
        head = (pr.get("head") or {}).get("repo") or {}
        trusted = (not is_open or head.get("full_name") == repo or
                   pr.get("author_association") in {"OWNER", "MEMBER", "COLLABORATOR"})
        for file in pdfs:
            path = file["filename"]
            if is_open:
                assets[path] = (head_repo, head_ref, file, pr, trusted)
            elif path in base_files:
                assets[path] = (repo, branch, base_files[path], pr, True)

        body = pr.get("body") or ""
        fields = design_fields(body)
        images = list(design_images(body, head_repo, head_ref))
        if (pdfs and not docs) or not (fields or images or docs or "설계" in pr["title"]):
            continue
        topic_id = resolve_topic(topics, pr, [file["filename"] for file in docs])
        url = f"https://github.com/{repo}/pull/{int(pr['number'])}"
        if topic_id is None:
            unassigned.append({"title": pr["title"], "url": url})
            continue
        for file in docs:
            data = read_file(read, head_repo, head_ref, file)
            if data:
                markdown = data.decode("utf-8-sig", errors="replace")
                for key, value in design_fields(markdown).items():
                    fields.setdefault(key, value)
                images.extend(design_images(markdown, head_repo, head_ref, posixpath.dirname(file["filename"])))
        if not images:
            for file in files:
                path = PurePosixPath(file["filename"])
                week = WEEK_FILE.fullmatch(path.as_posix())
                if (week and int(week[1]) == result[topic_id]["week"] and
                        path.suffix.lower() in IMAGE_EXTENSIONS and
                        re.search(r"architecture|diagram|설계도|아키텍처", path.stem, re.I)):
                    images.append({"url": branch_image_url(head_repo, head_ref, path.as_posix()), "alt": path.stem})
        unique_images = {}
        for diagram in images:
            unique_images.setdefault(diagram["url"], diagram)
        user = (pr.get("user") or {}).get("login") or "작성자 정보 없음"
        author = re.match(r"^\[[^\]]+\]\s*(.+?)\s+-\s+", pr["title"])
        design_dirs = {PurePosixPath(file["filename"]).parent for file in docs}
        materials = []
        for file in files:
            path = PurePosixPath(file["filename"])
            if (path.parent in design_dirs and path.suffix.lower() == ".html" and
                    re.search(r"설계|architecture|design", path.stem, re.I)):
                materials.append({
                    "title": path.stem.replace("-", " "),
                    "url": f"https://github.com/{head_repo}/blob/{quote(head_ref, safe='')}/{quote(path.as_posix())}",
                })
        if trusted:
            trusted_designs.add(int(pr["number"]))
        result[topic_id]["designs"].append({
            **fields, "images": list(unique_images.values()), "materials": materials, "author": author[1] if author else user,
            "login": user, "number": int(pr["number"]),
            "title": pr["title"], "url": url, "state": "draft" if is_open and pr.get("draft") else
            ("open" if is_open else "merged"),
        })

    for path, (source_repo, ref, file, pr, allow_ai) in sorted(assets.items()):
        url = f"https://github.com/{source_repo}/blob/{quote(ref, safe='')}/{quote(path)}"
        topic_id = resolve_topic(topics, pr, [path])
        if topic_id is None:
            unassigned.append({"title": PurePosixPath(path).name, "url": url})
            continue
        if path.lower().endswith(".pdf"):
            data = read_file(read, source_repo, ref, file)
            status, summary = analyze_pdf(data, cache_dir, allow_ai) if data else ("원본 PDF 확인", {})
        else:
            status, summary = "HTML 발표 자료 · 원문 확인", {}
        result[topic_id]["presentations"].append({
            "title": PurePosixPath(path).stem, "url": url, "status": status,
            "format": "PDF" if path.lower().endswith(".pdf") else "HTML", **summary,
        })

    for topic in result.values():
        topic["designs"].sort(key=lambda design: (design["state"] == "merged", design["number"]))
        topic["common"] = {
            "open": shared_considerations([design for design in topic["designs"] if design["state"] != "merged"]),
            "all": shared_considerations(topic["designs"]),
        }
        topic["comparison"] = compare_topic(topic["title"], topic["designs"], trusted_designs, cache_dir)
    # Keep the agenda available through the end of the meeting's local day.
    meeting_week = max(2, (today - meeting_date(2)).days // 7 + 2)
    if meeting_date(meeting_week) < today:
        meeting_week += 1
    return {
        "repoUrl": f"https://github.com/{repo}", "meetingWeek": meeting_week,
        "meetingDate": meeting_date(meeting_week).isoformat(), "topics": list(result.values()),
        "unassigned": unassigned, "capturedAt": snapshot.get("captured_at"), "today": today.isoformat(),
    }


def write_dashboard(output, data):
    body = (ROOT / "template.html").read_text(encoding="utf-8")
    topics = []
    for topic in data["topics"]:
        designs = [
            {**design, "markdownHtml": {field: MARKDOWN.render(design[field])
                                        for field in DESIGN_MARKDOWN_FIELDS if design.get(field)}}
            for design in topic["designs"]
        ]
        topics.append({**topic, "designs": designs})
    payload = json.dumps({**data, "topics": topics}, ensure_ascii=False).replace("<", "\\u003c")
    assert body.count("__DASHBOARD_DATA__") == 1
    page = (
        '<!doctype html>\n<html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        '<title>시스템 디자인 스터디 대시보드</title></head><body style="margin:0">'
        + body.replace("__DASHBOARD_DATA__", payload) + "</body></html>\n"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False) as temp:
        temp.write(page)
    Path(temp.name).chmod(0o644)
    Path(temp.name).replace(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="output HTML file")
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", DEFAULT_REPO))
    parser.add_argument("--branch", default=os.getenv("BASE_BRANCH", "main"))
    parser.add_argument("--snapshot", type=Path, help="use a local GitHub data snapshot")
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(ZoneInfo("Asia/Seoul")).date())
    parser.add_argument("--cache-dir", type=Path)
    args = parser.parse_args()
    topics = json.loads((ROOT / "topics.json").read_text(encoding="utf-8"))
    if args.snapshot:
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    else:
        snapshot = read_github(args.repo, args.branch)
    model = build_model(topics, snapshot, source_reader(args.snapshot), args.date, args.cache_dir)
    write_dashboard(args.output, model)
    print(f"Generated {args.output}")


if __name__ == "__main__":
    main()
