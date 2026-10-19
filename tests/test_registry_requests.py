"""Registration issues change only validated records, and the form matches what the bot parses."""

import copy
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

from utilities import registry_requests


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "utilities" / "registry_requests.py"
FORM = Path(".github/ISSUE_TEMPLATE/registry.yml")
KINDS = ("application", "module", "tutorial", "skill", "benchmark")
BOT = registry_requests.BOT


def form_elements():
    return yaml.safe_load((ROOT / FORM).read_text(encoding="utf-8"))["body"]


def render(values):
    """Render an issue body the way GitHub does when the form is submitted."""
    sections = []
    for element in form_elements():
        if element["type"] == "markdown":
            continue
        attributes = element["attributes"]
        if element["type"] == "checkboxes":
            mark = "X" if values.get(element["id"]) else " "
            value = "\n".join(f"- [{mark}] {option['label']}" for option in attributes["options"])
        else:
            value = values.get(element["id"]) or "_No response_"
        sections.append(f"### {attributes['label']}\n\n{value}")
    return "\n\n".join(sections)


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "utilities").mkdir()
        shutil.copyfile(ROOT / "utilities/source.schema.json", self.root / "utilities/source.schema.json")
        for kind in KINDS:
            (self.root / f"{kind}s").mkdir()
        self.path = self.root / "applications/camera.json"
        self.record = {
            "name": "camera", "kind": "application",
            "issues_url": "https://example.org/support?kind=camera#report",
            "source": {"type": "git", "url": "https://example.org/camera.git", "ref": "v1", "path": "apps/camera"},
        }

    def seed(self, record=None):
        record = record or self.record
        (self.root / f"{record['kind']}s/{record['name']}.json").write_text(json.dumps(record, indent=2) + "\n")

    def registered(self, kind="application", name="camera"):
        path = self.root / f"{kind}s/{name}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def request(self, values, state="open"):
        """Prepare an issue the way the workflow does and return the outcome."""
        issue = self.root / "issue.json"
        body = render(values) if isinstance(values, dict) else values
        issue.write_text(json.dumps({"number": 42, "state": state, "body": body}))
        result = self.root / "result.json"
        process = subprocess.run([
            sys.executable, str(SCRIPT), "prepare", "--root", str(self.root), "--issue", str(issue),
            "--result", str(result), "--pull-request-body", str(self.root / "pull-request.md"),
        ], capture_output=True, text=True)
        self.assertEqual(0, process.returncode, process.stderr)
        return json.loads(result.read_text())

    def snapshot(self):
        """Contents of every registry record."""
        paths = [*(path for kind in KINDS for path in (self.root / f"{kind}s").iterdir())]
        return {path: path.read_bytes() for path in paths}

    def test_form_has_no_dropdowns_and_matches_the_parsed_fields(self):
        fields = [element for element in form_elements() if element["type"] != "markdown"]
        self.assertNotIn("dropdown", {element["type"] for element in fields})
        self.assertEqual(list(registry_requests.LABELS), [element["attributes"]["label"] for element in fields])
        text_ids = {element["id"] for element in fields if element["type"] in ("input", "textarea")}
        self.assertLessEqual(set(registry_requests.FIELDS) | {"kind", "name"}, text_ids)

    def test_workflow_recognizes_the_form_headings(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/registry-request.yml").read_text(encoding="utf-8"))
        condition = workflow["jobs"]["request"]["if"]
        headings = set(re.findall(r"'### ([^']+)'", condition))
        self.assertEqual({registry_requests.LABELS[0], "Git revision"}, headings)
        self.assertLessEqual(headings, set(registry_requests.LABELS))
        self.assertIn(f"startsWith(github.event.issue.body, '### {registry_requests.LABELS[0]}')", condition)

    def test_register_each_kind(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                result = self.request({
                    "kind": f"{kind.title()}s", "name": "Camera", "url": "https://example.org/camera.git",
                    "ref": "v2", "reason": "Publish it.",
                })
                self.assertEqual("ready", result["status"])
                self.assertEqual(f"Register {kind}/camera", result["title"])
                self.assertEqual(f"{kind}s", result["directory"])
                self.assertEqual({
                    "name": "camera", "kind": kind,
                    "source": {"type": "git", "url": "https://example.org/camera.git", "ref": "v2"},
                }, self.registered(kind))

    def test_new_record_matches_registry_style(self):
        result = self.request({
            "kind": "application", "name": "camera", "url": "https://example.org/camera.git", "ref": "v1",
            "path": "apps/camera", "issues-url": "https://example.org/support?kind=camera#report", "reason": "Publish it.",
        })
        self.assertEqual(json.dumps(self.record, indent=2) + "\n", self.path.read_text())
        self.assertEqual([
            "Source URL: https://example.org/camera.git", "Git revision: v1", "Repository path: apps/camera",
            "Issue tracker URL: https://example.org/support?kind=camera#report",
        ], result["changes"])

    def test_hosted_source_needs_no_revision(self):
        url = "https://example.org/metadata.json?version=2#application"
        self.assertEqual("ready", self.request({"kind": "application", "name": "camera", "url": url, "reason": "Hosted."})["status"])
        self.assertEqual({"type": "url", "url": url}, self.registered()["source"])

    def test_update_by_name_changes_only_given_fields(self):
        self.seed()
        result = self.request({"kind": "application", "name": "camera", "ref": "v2", "reason": "New release."})
        expected = copy.deepcopy(self.record)
        expected["source"]["ref"] = "v2"
        self.assertEqual("Update application/camera", result["title"])
        self.assertEqual(["Git revision: v1 → v2"], result["changes"])
        self.assertEqual(expected, self.registered())
        self.assertEqual("    Git revision: v1 → v2\n\nCloses #42.\n", (self.root / "pull-request.md").read_text())

    def test_none_removes_optional_values(self):
        self.seed()
        result = self.request({"kind": "application", "name": "camera", "path": "none", "issues-url": "None", "reason": "Use the root."})
        expected = copy.deepcopy(self.record)
        del expected["issues_url"], expected["source"]["path"]
        self.assertEqual(["Repository path: apps/camera → (none)", "Issue tracker URL: https://example.org/support?kind=camera#report → (none)"], result["changes"])
        self.assertEqual(expected, self.registered())

    def test_removing_the_revision_switches_to_hosted_content(self):
        self.seed()
        url = "https://example.org/camera/metadata.json"
        self.assertEqual("ready", self.request({"kind": "application", "name": "camera", "url": url, "ref": "none", "reason": "Hosted."})["status"])
        self.assertEqual({"type": "url", "url": url}, self.registered()["source"])

    def test_register_update_and_remove_round_trip(self):
        values = {"kind": "application", "name": "camera"}
        result = self.request({**values, "url": "https://example.org/camera.git", "ref": "v1", "reason": "New."})
        self.assertEqual("ready", result["status"])
        result = self.request({**values, "ref": "v2", "reason": "New release."})
        self.assertEqual(["Git revision: v1 → v2"], result["changes"])
        self.assertEqual("v2", self.registered()["source"]["ref"])
        result = self.request({**values, "remove": True, "reason": "Retired upstream."})
        self.assertEqual("Remove application/camera", result["title"])
        self.assertIsNone(self.registered())

    def test_unchanged_request_is_noop(self):
        self.seed()
        before = self.snapshot()
        prefilled = {"kind": "application", "name": "camera", **registry_requests.flatten(self.record)}
        for values in ({"kind": "application", "name": "camera"}, prefilled):
            with self.subTest(values=values):
                self.assertEqual({"status": "noop", "registration": "application/camera"}, self.request({**values, "reason": "Check."}))
                self.assertEqual(before, self.snapshot())

    def test_remove_keeps_other_records_and_only_empty_directories(self):
        self.seed()
        other = {"name": "other", "kind": "application", "source": {"type": "url", "url": "https://example.org/other"}}
        self.seed(other)
        self.assertEqual("ready", self.request({"kind": "application", "name": "camera", "remove": True, "reason": "Retired."})["status"])
        self.assertIsNone(self.registered())
        self.assertEqual(other, self.registered(name="other"))
        self.assertFalse((self.root / "applications/.gitkeep").exists())
        self.assertEqual("ready", self.request({"kind": "application", "name": "other", "remove": True, "reason": "Retired."})["status"])
        self.assertEqual([".gitkeep"], [path.name for path in (self.root / "applications").iterdir()])

    def test_invalid_requests_change_nothing(self):
        self.seed()
        before = self.snapshot()
        new = {"kind": "application", "name": "new", "url": "https://example.org/new.git", "ref": "v1", "reason": "New."}
        valid = render(new)
        cases = [
            ({**new, "kind": "plugin"}, "Kind must be one of: application, module"),
            ({**new, "url": ""}, "application/new is not registered yet. Check its kind and name, or add a Source URL"),
            ({**new, "name": "My App"}, "Name must use lowercase letters"),
            ({**new, "name": "../escape"}, "Name must use lowercase letters"),
            ({**new, "url": "http://example.org/new.git"}, "Source URL must be an HTTPS URL"),
            ({**new, "url": "https://example.org/new.git?branch=main"}, "a Git URL cannot include ? or #"),
            ({**new, "ref": ""}, "looks like a Git repository; add a Git revision"),
            ({**new, "url": "https://github.com/owner/new", "ref": ""}, "looks like a Git repository"),
            ({**new, "url": "https://example.org/metadata.json", "ref": "", "path": "apps"}, "applies only to Git sources"),
            ({**new, "path": "../apps"}, "Repository path must be a relative directory"),
            ({**new, "ref": "-v1"}, "Git revision must be a branch, tag, or commit"),
            ({**new, "issues-url": "mailto:team@example.org"}, "Issue tracker URL must be an HTTPS URL"),
            ({**new, "url": "https://EXAMPLE.org/camera", "path": "apps/camera"}, "already registered as application/camera"),
            ({"kind": "application", "name": "camera", "url": "none", "reason": "x"}, "Source URL cannot be removed"),
            ({**new, "url": "https://example.org/new/metadata.json"}, "which takes no Git revision"),
            ({"kind": "application", "name": "camera", "url": "https://example.org/camera/SKILL.md", "reason": "x"}, "enter none to switch"),
            ("", "Use the registration form and keep its headings"),
            (None, "Use the registration form and keep its headings"),  # The API's body for an emptied issue.
            ({"kind": "application", "name": "missing", "remove": True, "reason": "x"}, "application/missing is not registered"),
            ({**new, "reason": ""}, "Give a reason"),
            ("Please register my project.", "Use the registration form"),
            (valid.replace("### Remove", "### Delete"), "Missing: Remove; unexpected: Delete"),
            (valid.replace("### Reason", "### Git revision\n\nv2\n\n### Reason"), "Git revision heading appears more than once"),
            (valid.replace("\n\nv1\n\n", "\n\nv1\nv2\n\n"), "Git revision must be a single line"),
        ]
        for values, message in cases:
            with self.subTest(message=message):
                result = self.request(values)
                self.assertEqual("error", result["status"])
                self.assertIn(message, result["message"])
                self.assertNotIn("(?!", result["message"])
                self.assertEqual(before, self.snapshot())

    def test_reason_is_free_text_and_never_executed(self):
        marker = self.root / "unexpected"
        reason = f"Release notes\n### Details\n$(touch {marker}) `echo bad`"
        result = self.request({"kind": "application", "name": "camera", "url": "https://example.org/camera.git", "ref": "v1", "reason": reason})
        self.assertEqual("ready", result["status"])
        self.assertFalse(marker.exists())

    def test_closed_issue_changes_nothing(self):
        self.seed()
        before = self.snapshot()
        self.assertEqual({"status": "closed"}, self.request("", state="closed"))
        self.assertEqual(before, self.snapshot())

    def test_proposal_stages_only_registry_paths(self):
        self.seed()

        def git(*args):
            return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True)

        git("init", "--quiet")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--quiet", "-m", "Registry")
        result = self.request({"kind": "application", "name": "camera", "remove": True, "reason": "Retired."})
        git("add", "--", result["directory"])
        self.assertEqual([
            "A\tapplications/.gitkeep", "D\tapplications/camera.json",
        ], git("diff", "--cached", "--name-status").stdout.splitlines())


class ReportTests(unittest.TestCase):
    """Exercise issue feedback with a fake GitHub API, never live writes."""

    def setUp(self):
        self.comment = {"id": 8, "body": registry_requests.FEEDBACK + "\nOld result", "user": {"login": BOT}}
        self.pull = {"number": 12, "state": "open", "user": {"login": BOT}}
        self.comments = [self.comment]
        self.calls = []

    def api(self, endpoint, method="GET", data=None):
        self.calls.append((method, endpoint))
        if method == "GET" and "/issues/42/comments?" in endpoint:
            page = int(re.search(r"[?&]page=(\d+)", endpoint)[1])
            return self.comments[(page - 1) * 100:page * 100]
        if method == "GET" and endpoint == "repos/owner/repo/pulls?state=open&head=owner%3Aautomation%2Fregistry-issue-42":
            return [self.pull] if self.pull["state"] == "open" else []
        if method == "PATCH" and endpoint == "repos/owner/repo/pulls/12":
            self.pull.update(data)
            return self.pull
        if method == "PATCH" and endpoint == "repos/owner/repo/issues/comments/8":
            self.comment.update(data)
            return self.comment
        if method == "POST" and endpoint == "repos/owner/repo/issues/42/comments":
            self.comments.append({"id": 9, **data, "user": {"login": BOT}})
            return self.comments[-1]
        if method == "DELETE" and endpoint == "repos/owner/repo/git/refs/heads/automation/registry-issue-42":
            return None
        self.fail(f"Unexpected API request: {method} {endpoint} {data}")

    def report(self, result, pull_url="", failed=False):
        registry_requests.report(
            "owner/repo", 42, result, "automation/registry-issue-42", pull_url, "https://example.org/run", failed, self.api,
        )

    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]

    def test_ready_request_updates_one_comment_with_its_pull_request(self):
        changes = ["Git revision: v1 → @maintainers"]
        self.report({"status": "ready", "title": "Update application/camera", "changes": changes}, "https://github.com/owner/repo/pull/12")
        self.assertEqual(1, len(self.comments))
        self.assertIn("**Update application/camera** is ready for review in https://github.com/owner/repo/pull/12.", self.comment["body"])
        self.assertIn("\n    Git revision: v1 → @maintainers\n", self.comment["body"])
        self.assertEqual([("PATCH", "repos/owner/repo/issues/comments/8")], self.writes())

    def test_invalid_edit_withdraws_the_pull_request(self):
        self.report({"status": "error", "message": "Source URL must be an HTTPS URL.\nThanks @maintainers"})
        self.assertEqual("closed", self.pull["state"])
        self.assertIn("\n    Thanks @maintainers", self.comment["body"])
        self.assertIn("The previous pull request was closed.", self.comment["body"])
        self.assertIn(("DELETE", "repos/owner/repo/git/refs/heads/automation/registry-issue-42"), self.calls)

    def test_noop_withdraws_the_pull_request(self):
        self.report({"status": "noop", "registration": "application/camera"})
        self.assertEqual("closed", self.pull["state"])
        self.assertIn("**application/camera** already matches this request", self.comment["body"])

    def test_closing_the_issue_withdraws_an_open_pull_request(self):
        self.report({"status": "closed"})
        self.assertEqual("closed", self.pull["state"])
        self.assertIn("Reopen the issue to try again.", self.comment["body"])

    def test_closing_after_merge_keeps_the_last_feedback(self):
        self.pull["state"] = "closed"
        self.report({"status": "closed"})
        self.assertEqual([], self.writes())
        self.assertEqual(registry_requests.FEEDBACK + "\nOld result", self.comment["body"])

    def test_automation_failure_keeps_the_pull_request_and_links_the_run(self):
        self.report({"status": "ready", "title": "Update application/camera", "changes": []}, "https://github.com/owner/repo/pull/12", failed=True)
        self.assertEqual("open", self.pull["state"])
        self.assertIn("[the workflow run](https://example.org/run)", self.comment["body"])
        self.assertIn("Pull request: https://github.com/owner/repo/pull/12", self.comment["body"])
        self.report({"status": "ready", "title": "Update application/camera", "changes": []})
        self.assertIn("did not finish", self.comment["body"])

    def test_human_comments_and_pull_requests_are_untouched(self):
        self.comment["user"] = {"login": "contributor"}
        self.pull["user"] = {"login": "contributor"}
        self.report({"status": "error", "message": "Invalid source."})
        self.assertEqual("open", self.pull["state"])
        self.assertEqual(registry_requests.FEEDBACK + "\nOld result", self.comment["body"])
        self.assertEqual(2, len(self.comments))
        self.assertNotIn("previous pull request", self.comments[-1]["body"])

    def test_feedback_comment_is_found_on_later_pages(self):
        self.comments[:0] = [{"id": index, "body": "Thanks!", "user": {"login": "contributor"}} for index in range(100, 200)]
        self.report({"status": "noop", "registration": "application/camera"})
        self.assertEqual(101, len(self.comments))
        self.assertIn("already matches", self.comment["body"])


if __name__ == "__main__":
    unittest.main()
