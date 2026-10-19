#!/usr/bin/env python3
"""Turn registration issue forms into registry pull requests."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlencode, urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry

try:
    from .validate_registry import KIND_DIRECTORIES, SCHEMA_PATH, read_json, source_identity, validate_registry
except ImportError:
    from validate_registry import KIND_DIRECTORIES, SCHEMA_PATH, read_json, source_identity, validate_registry


# Form input ids and the labels GitHub renders as issue headings.
FIELDS = {"url": "Source URL", "ref": "Git revision", "path": "Repository path", "issues-url": "Issue tracker URL"}
LABELS = ("Kind", "Name", *FIELDS.values(), "Remove", "Reason")
KINDS = {**{kind: kind for kind in KIND_DIRECTORIES}, **{folder: kind for kind, folder in KIND_DIRECTORIES.items()}}
GUIDANCE = {
    "name": "Name must use lowercase letters and numbers, with single hyphens or underscores between words.",
    "issues_url": "Issue tracker URL must be an HTTPS URL without credentials.",
    "source/url": "Source URL must be an HTTPS URL without credentials; a Git URL cannot include ? or #.",
    "source/ref": "Git revision must be a branch, tag, or commit without spaces that does not start with -.",
    "source/path": "Repository path must be a relative directory such as applications/my-app, or . for the root.",
}
FEEDBACK = "<!-- holohub-registry-feedback -->"
BOT = "github-actions[bot]"


class RequestError(ValueError):
    """A problem the requester can fix by editing the issue."""


def parse_issue(body):
    """Read the sections GitHub renders for the form; Reason, the last one, is free text."""
    if not isinstance(body, str):
        raise RequestError("Use the registration form and keep its headings.")
    if len(body) > 65536:
        raise RequestError("Shorten the issue; its body is limited to 65,536 characters.")
    body = body.replace("\r\n", "\n").strip()
    headings = list(re.finditer(r"^### (.+)\n", body, re.MULTILINE))
    if not headings or headings[0].start():
        raise RequestError("Use the registration form and keep its headings.")
    sections = {}
    for heading, following in zip(headings, [*headings[1:], None]):
        label = heading[1].strip()
        if label in sections:
            raise RequestError(f"The {label} heading appears more than once.")
        end = following.start() if following and label != "Reason" else len(body)
        value = body[heading.end():end].strip()
        sections[label] = "" if value == "_No response_" else value
        if label == "Reason":
            break
    if sections.keys() != set(LABELS):
        missing = ", ".join(label for label in LABELS if label not in sections) or "none"
        unexpected = ", ".join(label for label in sections if label not in LABELS) or "none"
        raise RequestError(f"Keep the form's headings. Missing: {missing}; unexpected: {unexpected}.")
    for label in LABELS[:-2]:
        if "\n" in sections[label]:
            raise RequestError(f"{label} must be a single line.")
    if not sections["Reason"]:
        raise RequestError("Give a reason for this request.")
    kind = KINDS.get(sections["Kind"].lower())
    if kind is None:
        raise RequestError(f"Kind must be one of: {', '.join(KIND_DIRECTORIES)}.")
    if not sections["Name"]:
        raise RequestError("Name is required.")
    return {
        "kind": kind,
        "name": sections["Name"].lower(),
        "fields": {field: sections[label] for field, label in FIELDS.items()},
        "remove": bool(re.search(r"^- \[[xX]\] ", sections["Remove"], re.MULTILINE)),
    }


def flatten(record):
    """Map a record onto the form's source fields."""
    source = record["source"]
    return {"url": source["url"], "ref": source.get("ref"), "path": source.get("path"), "issues-url": record.get("issues_url")}


def build(kind, name, fields):
    """Create a record in canonical key order; a Git revision makes it a Git source."""
    record = {"name": name, "kind": kind}
    if fields["issues-url"]:
        record["issues_url"] = fields["issues-url"]
    record["source"] = {"type": "git" if fields["ref"] else "url", "url": fields["url"]}
    for key in ("ref", "path"):
        if fields[key]:
            record["source"][key] = fields[key]
    return record


def new_record(kind, name, fields):
    fields = {field: None if value.lower() == "none" else value or None for field, value in fields.items()}
    if not fields["url"]:
        raise RequestError(f"{kind}/{name} is not registered yet. Check its kind and name, or add a Source URL to register it.")
    return build(kind, name, fields)


def updated_record(current, fields):
    """Blank fields keep their registered values, and none removes an optional value."""
    merged = flatten(current)
    for field, value in fields.items():
        if value.lower() == "none":
            if field == "url":
                raise RequestError("Source URL cannot be removed; enter the new URL instead.")
            merged[field] = None
        elif value:
            merged[field] = value
    if not merged["ref"] and not fields["path"]:
        merged["path"] = None  # Hosted content has no repository path.
    return build(current["kind"], current["name"], merged)


def check(root, record, others):
    """Validate a proposed record against the schema and the other registrations."""
    source = record["source"]
    if source["type"] == "url" and "path" in source:
        raise RequestError("Repository path applies only to Git sources; add a Git revision or leave the path blank.")
    validator = Draft202012Validator(read_json(root / SCHEMA_PATH), format_checker=FormatChecker(), registry=Registry())
    problems = []
    for error in validator.iter_errors(record):
        pointer = "/".join(map(str, error.absolute_path))
        problems.append(GUIDANCE.get(pointer, f"{pointer or 'record'}: {error.message}"))
    if problems:
        raise RequestError("\n".join(dict.fromkeys(problems)))
    url = urlsplit(source["url"])
    if source["type"] == "url" and (url.path.endswith(".git") or (url.hostname == "github.com" and url.path.strip("/").count("/") == 1)):
        raise RequestError("Source URL looks like a Git repository; add a Git revision (branch, tag, or commit).")
    if source["type"] == "git" and url.path.endswith(("/metadata.json", "/SKILL.md")):
        raise RequestError("Source URL links to a hosted metadata.json or SKILL.md, which takes no Git revision. "
                           "Leave Git revision blank for a new source, or enter none to switch a registered one.")
    identity = source_identity(record)
    for other in others:
        if source_identity(other) == identity:
            raise RequestError(f"This source is already registered as {other['kind']}/{other['name']}; update that registration instead.")


def summarize(current, record):
    """List the form fields a request sets or changes."""
    if record is None:
        return []
    before = flatten(current) if current else {}
    lines = []
    for field, value in flatten(record).items():
        old = before.get(field)
        if value != old:
            lines.append(f"{FIELDS[field]}: {value}" if current is None else f"{FIELDS[field]}: {old or '(none)'} → {value or '(none)'}")
    return lines


def load_records(root):
    records, errors = validate_registry(root)
    if errors:
        raise ValueError("Registry validation failed:\n" + "\n".join(errors))
    return records


def prepare(root, body):
    """Apply a valid request to the working tree; an invalid one changes nothing."""
    request = parse_issue(body)
    kind, name = request["kind"], request["name"]
    key = f"{kind}/{name}"
    records = load_records(root)
    current = next((record for record in records if record["kind"] == kind and record["name"] == name), None)
    others = [record for record in records if record is not current]
    if request["remove"]:
        if current is None:
            raise RequestError(f"{key} is not registered, so there is nothing to remove.")
        action, record = "Remove", None
    elif current is None:
        action, record = "Register", new_record(kind, name, request["fields"])
    else:
        action, record = "Update", updated_record(current, request["fields"])
        if record == current:
            return {"status": "noop", "registration": key}
    if record is not None:
        check(root, record, others)
    # The name is validated or already registered, so it always maps inside its kind's directory.
    path = root / KIND_DIRECTORIES[kind] / f"{name}.json"
    if record is None:
        path.unlink()
        if not any(path.parent.iterdir()):
            # Git does not keep empty directories, and the registry requires each one.
            (path.parent / ".gitkeep").touch()
    else:
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {
        "status": "ready", "registration": key, "title": f"{action} {key}",
        "directory": KIND_DIRECTORIES[kind], "changes": summarize(current, record),
    }


def pull_request_body(result, number):
    changes = "".join(f"    {line}\n" for line in result["changes"])
    return (f"{changes}\n" if changes else "") + f"Closes #{number}.\n"


def github_api(endpoint, method="GET", data=None):
    """Call the GitHub REST API with the runner's authenticated GitHub CLI."""
    command = ["gh", "api", "--method", method, endpoint]
    if data is not None:
        command.extend(["--input", "-"])
    result = subprocess.run(
        command, input=None if data is None else json.dumps(data),
        text=True, capture_output=True, timeout=60, check=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def report(repository, number, result, branch, pull_url="", run_url="", failed=False, api=github_api):
    """Keep one feedback comment on the issue, and withdraw a proposal that no longer matches it."""
    status = result.get("status", "error")
    failed = failed or (status == "ready" and not pull_url)
    withdrawn = []
    if not failed and status in ("noop", "error", "closed"):
        query = urlencode({"state": "open", "head": f"{repository.split('/')[0]}:{branch}"})
        withdrawn = [pull for pull in api(f"repos/{repository}/pulls?{query}") if pull["user"]["login"] == BOT]
        for pull in withdrawn:
            api(f"repos/{repository}/pulls/{pull['number']}", "PATCH", {"state": "closed"})
    if failed:
        parts = [f"The registry automation did not finish; a maintainer can check [the workflow run]({run_url}). "
                 "Edit or reopen this issue to retry."]
        if pull_url:
            parts.append(f"Pull request: {pull_url}")
    elif status == "ready":
        parts = [f"**{result['title']}** is ready for review in {pull_url}."]
        if result["changes"]:
            # Indented code keeps requester-supplied values from rendering as Markdown or mentions.
            parts.append("\n".join(f"    {line}" for line in result["changes"]))
        parts.append("The change takes effect when a maintainer merges the pull request. "
                     "To revise the request, edit this issue.")
    elif status == "noop":
        parts = [f"**{result['registration']}** already matches this request, so nothing needs to change. "
                 "Blank fields keep their current values; enter `none` to remove an optional value."]
    elif status == "closed":
        if not withdrawn:
            return  # Merged or never proposed: keep the latest feedback.
        parts = ["This request was closed, so its pull request was closed too. Reopen the issue to try again."]
    else:
        parts = [
            "This request needs a change before it can be proposed. Edit the issue to fix the following:",
            "\n".join(f"    {line}" for line in result.get("message", "Invalid request.")[:6000].splitlines()),
        ]
    if withdrawn and status != "closed":
        parts.append("The previous pull request was closed.")
    body = FEEDBACK + "\n" + "\n\n".join(parts)
    comment = None
    page = 1
    while comment is None:
        comments = api(f"repos/{repository}/issues/{number}/comments?per_page=100&page={page}")
        comment = next((item for item in comments if item["user"]["login"] == BOT and (item.get("body") or "").startswith(FEEDBACK)), None)
        if len(comments) < 100:
            break
        page += 1
    if comment is None:
        api(f"repos/{repository}/issues/{number}/comments", "POST", {"body": body})
    elif comment["body"] != body:
        api(f"repos/{repository}/issues/comments/{comment['id']}", "PATCH", {"body": body})
    if withdrawn:
        api(f"repos/{repository}/git/refs/heads/{branch}", "DELETE")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    registry = argparse.ArgumentParser(add_help=False)
    registry.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="registry root (default: this repository)")
    request = commands.add_parser("prepare", parents=[registry], help="apply a registration issue to the working tree")
    request.add_argument("--issue", type=Path, required=True, help="issue JSON from the GitHub REST API")
    request.add_argument("--result", type=Path, required=True, help="where to write the outcome as JSON")
    request.add_argument("--pull-request-body", type=Path, required=True, help="where to write the pull request description")
    feedback = commands.add_parser("report", help="comment on the issue with the outcome of prepare")
    feedback.add_argument("--repository", required=True, help="owner/name")
    feedback.add_argument("--issue-number", type=int, required=True)
    feedback.add_argument("--branch", required=True, help="the request's pull request branch")
    feedback.add_argument("--result", type=Path, required=True, help="outcome JSON from prepare")
    feedback.add_argument("--pull-request", default="", help="URL of the request's pull request")
    feedback.add_argument("--run-url", default="", help="URL of the workflow run")
    feedback.add_argument("--job-status", default="success", help="the job status so far")
    args = parser.parse_args(argv)

    if args.command == "prepare":
        issue = read_json(args.issue)
        if issue["state"] == "closed":
            result = {"status": "closed"}
        else:
            try:
                result = prepare(args.root, issue["body"])
            except RequestError as error:
                result = {"status": "error", "message": str(error)}
            if result["status"] == "ready":
                args.pull_request_body.write_text(pull_request_body(result, int(issue["number"])), encoding="utf-8")
        args.result.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if output := os.environ.get("GITHUB_OUTPUT"):
            with open(output, "a", encoding="utf-8") as stream:
                for key in ("status", "title", "directory"):
                    if key in result:
                        print(f"{key}={result[key]}", file=stream)
        return 0

    try:
        result = json.loads(args.result.read_text(encoding="utf-8")) if args.result.is_file() else {}
        report(args.repository, args.issue_number, result, args.branch, args.pull_request, args.run_url, args.job_status != "success")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Could not report the registry request: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
