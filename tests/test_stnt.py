import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "stnt", Path(__file__).resolve().parents[1] / "src/stnt.py"
)
stnt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stnt)

DEFAULT_TEST_STATE = tempfile.TemporaryDirectory()
os.environ["STNT_STATE_HOME"] = DEFAULT_TEST_STATE.name
stnt.ensure_state_layout()


def tearDownModule():
    DEFAULT_TEST_STATE.cleanup()


THREAD_ID = "T-12345678-1234-1234-1234-123456789abc"
THREAD_ADAPTER = Path(__file__).resolve().parents[1] / "bin/amp-thread"


def record(repo: Path):
    return {
        "schemaVersion": 1,
        "threadID": THREAD_ID,
        "runtime": "docker-sandbox",
        "sandbox": "stnt-fixture-12345678",
        "sandboxID": "sandbox-id-1",
        "repositoryPath": str(repo),
        "baseSHA": "a" * 40,
        "baseBranch": "master",
        "branch": "stnt/12345678",
        "preservationBranch": "stnt-preserved/phase1a-12345678",
        "sandboxPort": 8000,
        "status": "paused",
        "createdAt": "2026-08-11T00:00:00Z",
    }


def workspace_record(repo: Path):
    current = dict(
        record(repo),
        schemaVersion=stnt.SCHEMA_VERSION,
        workspaceID="W-aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        lifecycleOwner="workspace",
    )
    del current["threadID"]
    return current


class ThreadAdapterTests(unittest.TestCase):
    def write_amp_stub(self, directory: Path, body: str) -> Path:
        stub = directory / "amp"
        stub.write_text("#!/usr/bin/env python3\n" + body)
        stub.chmod(0o755)
        return stub

    def test_list_paginates_active_and_inclusive_and_normalizes_missing_title(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.write_amp_stub(directory, """
import json, sys
args = sys.argv[1:]
offset = int(args[args.index('--offset') + 1])
inclusive = '--include-archived' in args
total = 501 if inclusive else 500
items = []
for index in range(offset, min(offset + 500, total)):
    item = {'id': f'T-{index:08x}-0000-0000-0000-000000000000'}
    if index != 500:
        item['title'] = f'Title {index}'
    items.append(item)
print(json.dumps(items))
""")
            environment = dict(
                os.environ, PATH=f"{directory}:{Path(sys.executable).parent}:{os.environ['PATH']}"
            )
            completed = subprocess.run(
                [str(THREAD_ADAPTER), "list"], text=True, capture_output=True, env=environment
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            listed = json.loads(completed.stdout)
            self.assertEqual(len(listed), 501)
            self.assertEqual(listed[499]["status"], "active")
            self.assertEqual(listed[500], {
                "id": "T-000001f4-0000-0000-0000-000000000000",
                "title": "",
                "status": "archived",
            })

    def test_list_rejects_duplicate_amp_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.write_amp_stub(directory, """
import json
item = {'id': 'T-12345678-1234-1234-1234-123456789abc', 'title': 'duplicate'}
print(json.dumps([item, item]))
""")
            environment = dict(
                os.environ, PATH=f"{directory}:{Path(sys.executable).parent}:{os.environ['PATH']}"
            )
            completed = subprocess.run(
                [str(THREAD_ADAPTER), "list"], text=True, capture_output=True, env=environment
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("invalid or duplicate", completed.stderr)

    def test_status_resolves_list_omitted_archived_empty_thread_from_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.write_amp_stub(directory, f"""
import json, sys
args = sys.argv[1:]
if args[:2] == ['threads', 'list']:
    print('[]')
elif args[:2] == ['threads', 'export']:
    print(json.dumps({{'id': '{THREAD_ID}', 'archived': True, 'messages': []}}))
else:
    raise SystemExit(2)
""")
            environment = dict(
                os.environ, PATH=f"{directory}:{Path(sys.executable).parent}:{os.environ['PATH']}"
            )
            completed = subprocess.run(
                [str(THREAD_ADAPTER), "status", THREAD_ID],
                text=True,
                capture_output=True,
                env=environment,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), "archived")

    def test_status_resolves_list_omitted_unarchived_empty_export_without_archive_field(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.write_amp_stub(directory, f"""
import json, sys
args = sys.argv[1:]
if args[:2] == ['threads', 'list']:
    print('[]')
elif args[:2] == ['threads', 'export']:
    print(json.dumps({{'id': '{THREAD_ID}', 'messages': []}}))
else:
    raise SystemExit(2)
""")
            environment = dict(
                os.environ, PATH=f"{directory}:{Path(sys.executable).parent}:{os.environ['PATH']}"
            )
            completed = subprocess.run(
                [str(THREAD_ADAPTER), "status", THREAD_ID],
                text=True,
                capture_output=True,
                env=environment,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), "empty")


class LauncherTests(unittest.TestCase):
    def test_project_sdk_environment_cannot_redirect_python_to_asdf(self):
        launcher = Path(__file__).resolve().parents[1] / "bin/stnt"
        completed = subprocess.run(
            [str(launcher), "--help"],
            text=True,
            capture_output=True,
            env=dict(
                os.environ,
                DEVELOPER_DIR="/nonexistent/project-sdk",
                SDKROOT="/nonexistent/project-sdk/MacOSX.sdk",
            ),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("usage: stnt", completed.stdout)

    def test_launcher_resolves_repository_when_invoked_through_symlink(self):
        launcher = Path(__file__).resolve().parents[1] / "bin/stnt"
        with tempfile.TemporaryDirectory() as temporary:
            installed = Path(temporary) / "stnt"
            installed.symlink_to(launcher)
            completed = subprocess.run(
                [str(installed), "--help"],
                text=True,
                capture_output=True,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("usage: stnt", completed.stdout)


class TimingTests(unittest.TestCase):
    def test_recorder_appends_secret_free_fixed_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "golden.jsonl"
            recorder = stnt.TimingRecorder(path, "start")
            with recorder.stage("start.service"):
                pass
            recorder.milestone("start.serviceReady")
            recorder.write("success")
            payload = json.loads(path.read_text())
            mode = path.stat().st_mode & 0o777

        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["operation"], "start")
        self.assertEqual(payload["outcome"], "success")
        self.assertEqual(set(payload), {
            "schemaVersion", "operation", "startedAt", "outcome", "durationMs",
            "stagesMs", "milestonesMs",
        })
        self.assertIn("start.service", payload["stagesMs"])
        self.assertIn("start.serviceReady", payload["milestonesMs"])
        self.assertEqual(mode, 0o600)

    def test_recorder_rejects_symlink_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.write_text("")
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaisesRegex(stnt.StntError, "symbolic link"):
                stnt.TimingRecorder(link, "start").write("failed")

    def test_global_timings_option_is_explicit(self):
        parsed = stnt.parse_args(["--timings", "/tmp/golden.jsonl", "pause"])
        self.assertEqual(parsed.timings, "/tmp/golden.jsonl")
        self.assertEqual(parsed.command, "pause")


class AtomicStateTests(unittest.TestCase):
    def test_default_roots_reuse_populated_pre_rename_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            legacy_state = home / "Library/Application Support/ampx"
            legacy_config = home / ".config/ampx"
            legacy_cache = home / "Library/Caches/ampx"
            for path in (legacy_state, legacy_config, legacy_cache):
                path.mkdir(parents=True)
                (path / "retained").write_text("fixture")

            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                stnt.Path, "home", return_value=home
            ), mock.patch.object(stnt.sys, "platform", "darwin"), mock.patch.object(
                stnt.platform, "system", return_value="Darwin"
            ):
                self.assertEqual(stnt.state_root(), legacy_state)
                self.assertEqual(stnt.config_root(), legacy_config)
                self.assertEqual(stnt.cache_home(), legacy_cache)

                new_state = home / "Library/Application Support/stnt"
                new_config = home / ".config/stnt"
                new_cache = home / "Library/Caches/stnt"
                for path in (new_state, new_config, new_cache):
                    path.mkdir(parents=True)
                self.assertEqual(stnt.state_root(), new_state)
                self.assertEqual(stnt.config_root(), new_config)
                self.assertEqual(stnt.cache_home(), new_cache)

    def test_legacy_migration_is_atomic_before_and_after_rename(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, repo = Path(temporary) / "state", Path("/fixture")
            (root / "sessions").mkdir(parents=True)
            original = record(repo)
            with mock.patch.dict(os.environ, {"STNT_STATE_HOME": str(root)}):
                old = stnt.legacy_session_path(repo)
                old.write_text(json.dumps(original))
                with mock.patch.dict(os.environ, {"STNT_TEST_INTERRUPT_AT": "migration-before-rename"}):
                    with self.assertRaises(stnt.StntError):
                        stnt.migrate_legacy_session(repo)
                upgraded = json.loads(old.read_text())
                self.assertEqual(upgraded["workspaceID"], stnt.migrated_workspace_id(THREAD_ID))
                self.assertEqual(upgraded["lifecycleOwner"], "thread")
                target = stnt.workspace_session_path(repo, upgraded["workspaceID"])
                with mock.patch.dict(os.environ, {"STNT_TEST_INTERRUPT_AT": "migration-after-rename"}):
                    with self.assertRaises(stnt.StntError):
                        stnt.migrate_legacy_session(repo)
                self.assertFalse(old.exists())
                self.assertEqual(json.loads(target.read_text()), upgraded)

    def test_loader_rejects_filename_and_duplicate_sandbox_associations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, repo = Path(temporary), Path("/fixture")
            (root / "sessions").mkdir()
            bad = root / "sessions/wrong.json"
            bad.write_text(json.dumps(record(repo)))
            with mock.patch.dict(os.environ, {"STNT_STATE_HOME": str(root)}):
                with self.assertRaisesRegex(stnt.StntError, "filename"):
                    stnt.load_sessions()

    def test_interruption_cannot_replace_valid_state_with_partial_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sessions/session.json"
            original = record(Path("/fixture"))
            stnt.atomic_write(path, original)
            changed = dict(original, status="archived")
            with mock.patch.dict(os.environ, {"STNT_TEST_INTERRUPT_AT": "before-replace"}):
                with self.assertRaisesRegex(stnt.StntError, "synthetic interruption"):
                    stnt.atomic_write(path, changed)
            self.assertEqual(json.loads(path.read_text()), original)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])

    def test_workspace_id_is_durable_before_next_operation_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "fixture"
            repo.mkdir()
            state = Path(temporary) / "state"
            responses = [
                subprocess.CompletedProcess([], 0, "a" * 40 + "\n", ""),
                subprocess.CompletedProcess([], 0, "master\n", ""),
            ]
            with mock.patch.dict(os.environ, {"STNT_STATE_HOME": str(state)}), mock.patch.object(
                stnt, "run", side_effect=responses
            ), mock.patch.object(
                stnt, "repository_default_branch", return_value=("master", "a" * 40)
            ):
                created = stnt.create_record(repo)
            with mock.patch.dict(os.environ, {"STNT_STATE_HOME": str(state)}):
                durable = stnt.load_state(stnt.session_path(repo))
                self.assertEqual(stnt.session_path(repo), stnt.workspace_session_path(repo, durable["workspaceID"]))
            self.assertNotIn("threadID", durable)
            self.assertEqual(durable["schemaVersion"], stnt.SCHEMA_VERSION)
            self.assertRegex(durable["workspaceID"], stnt.WORKSPACE_RE)
            self.assertEqual(durable["lifecycleOwner"], "workspace")
            self.assertEqual(created["status"], "creating")
            self.assertEqual(created["branch"], "master")
            self.assertLessEqual(len(created["sandbox"]), 63)

    def test_schema_one_without_service_fields_remains_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            original = record(Path("/fixture"))
            path.write_text(json.dumps(original))
            self.assertEqual(stnt.load_state(path), original)

    def test_schema_two_requires_thread_id_only_for_thread_owned_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            current = workspace_record(Path("/fixture"))
            path.write_text(json.dumps(current))
            self.assertEqual(stnt.load_state(path), current)

            current["lifecycleOwner"] = "thread"
            path.write_text(json.dumps(current))
            with self.assertRaisesRegex(stnt.StntError, "missing its Amp thread ID"):
                stnt.load_state(path)

    def test_active_state_requires_a_strict_editor_authorization_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            current = dict(
                workspace_record(Path("/fixture")),
                status="active",
                editorAuthorization="a" * 32,
            )
            path.write_text(json.dumps(current))
            self.assertEqual(stnt.load_state(path), current)
            for authorization in ("short", 3):
                current["editorAuthorization"] = authorization
                path.write_text(json.dumps(current))
                with self.assertRaisesRegex(stnt.StntError, "editor authorization"):
                    stnt.load_state(path)
            current["editorAuthorization"] = "a" * 32
            current["status"] = "paused"
            path.write_text(json.dumps(current))
            with self.assertRaisesRegex(stnt.StntError, "editor authorization"):
                stnt.load_state(path)

    def test_service_url_is_a_strict_origin_matching_the_sandbox_port(self):
        self.assertEqual(
            stnt.parse_service_url("https://app.example.test:8010"),
            ("https", "app.example.test", 8010),
        )
        for invalid in (
            "https://app.example.test",
            "ftp://app.example.test:8010",
            "https://app.example.test:8010/path",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(stnt.StntError):
                stnt.parse_service_url(invalid)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            current = dict(
                record(Path("/fixture")),
                sandboxPort=8010,
                serviceCommand="bin/dev",
                serviceURL="https://app.example.test:8010",
            )
            path.write_text(json.dumps(current))
            self.assertEqual(stnt.load_state(path), current)
            current["sandboxPort"] = 8000
            path.write_text(json.dumps(current))
            with self.assertRaisesRegex(stnt.StntError, "disagree"):
                stnt.load_state(path)

    def test_source_branch_fields_are_optional_but_atomic_and_strict(self):
        original = record(Path("/fixture"))
        for changes in (
            {"sourceBranch": "feature/example-work"},
            {"sourceSHA": "b" * 40},
            {"sourceBranch": "bad branch", "sourceSHA": "b" * 40},
            {"sourceBranch": "feature/example-work", "sourceSHA": "bad"},
        ):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "session.json"
                path.write_text(json.dumps(dict(original, **changes)))
                with self.assertRaises(stnt.StntError):
                    stnt.load_state(path)

    def test_create_only_atomic_write_preserves_existing_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            path.write_bytes(b"existing bytes\n")
            with self.assertRaisesRegex(stnt.StntError, "refusing to replace"):
                stnt.atomic_write(path, record(Path("/fixture")), create_only=True)
            self.assertEqual(path.read_bytes(), b"existing bytes\n")

    def test_create_only_fsync_failure_leaves_only_a_complete_private_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sessions" / "session.json"
            path.parent.mkdir()
            original = record(Path("/fixture"))
            with mock.patch.object(
                stnt, "fsync_directory", side_effect=stnt.StntError("synthetic fsync failure")
            ):
                with self.assertRaisesRegex(stnt.StntError, "fsync failure"):
                    stnt.atomic_write(path, original, create_only=True)
            self.assertEqual(json.loads(path.read_text()), original)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])

    def test_migration_after_fsync_is_complete_and_mode_is_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, repo = Path(temporary), Path("/fixture")
            (root / "sessions").mkdir()
            with mock.patch.dict(os.environ, {"STNT_STATE_HOME": str(root)}):
                old = stnt.legacy_session_path(repo)
                payload = (json.dumps(record(repo), sort_keys=True) + "\n").encode()
                old.write_bytes(payload)
                os.chmod(old, 0o600)
                with mock.patch.dict(os.environ, {"STNT_TEST_INTERRUPT_AT": "migration-after-fsync"}):
                    with self.assertRaises(stnt.StntError):
                        stnt.migrate_legacy_session(repo)
                target = stnt.workspace_session_path(repo, stnt.migrated_workspace_id(THREAD_ID))
                migrated = json.loads(target.read_text())
                self.assertEqual(migrated["schemaVersion"], stnt.SCHEMA_VERSION)
                self.assertEqual(migrated["workspaceID"], stnt.migrated_workspace_id(THREAD_ID))
                self.assertEqual(migrated["lifecycleOwner"], "thread")
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_workspace_record_path_and_lock_do_not_require_a_thread(self):
        repo = Path("/fixture")
        current = workspace_record(repo)
        self.assertNotIn("threadID", current)
        self.assertEqual(
            stnt.record_session_path(current),
            stnt.workspace_session_path(repo, current["workspaceID"]),
        )
        self.assertEqual(stnt.record_lock_identity(current), current["workspaceID"])

    def test_workspace_owned_pause_never_looks_up_or_finishes_a_host_thread(self):
        current = workspace_record(Path("/fixture"))
        stopped = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(stnt, "run_lifecycle", return_value=stopped), mock.patch.object(
            stnt, "thread_status"
        ) as status, mock.patch.object(stnt, "command_finish") as finish, mock.patch.object(
            stnt, "atomic_write"
        ) as write, redirect_stdout(io.StringIO()):
            stnt.pause_after_exit(current, Path("/state"))
        status.assert_not_called()
        finish.assert_not_called()
        self.assertEqual(current["status"], "paused")
        write.assert_called_once_with(Path("/state"), current)

    def test_workspace_owned_finish_requires_interactive_destructive_confirmation(self):
        current = workspace_record(Path("/fixture"))
        with mock.patch.object(stnt, "validate_record_repository"), mock.patch.object(
            sys.stdin, "isatty", return_value=False
        ), mock.patch.object(stnt, "thread_status") as status, mock.patch.object(
            stnt, "runtime_find"
        ) as runtime:
            with self.assertRaisesRegex(stnt.StntError, "interactive terminal"):
                stnt.command_finish(Path("/fixture"), (Path("/state"), current))
        status.assert_not_called()
        runtime.assert_not_called()

    def test_loader_rejects_conflicting_and_partial_migration_shapes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, repo = Path(temporary), Path("/fixture")
            (root / "sessions").mkdir()
            with mock.patch.dict(os.environ, {"STNT_STATE_HOME": str(root)}):
                old, new = stnt.legacy_session_path(repo), stnt.session_path(repo, THREAD_ID)
                old.write_text(json.dumps(record(repo)))
                new.write_text(json.dumps(record(repo)))
                with self.assertRaisesRegex(stnt.StntError, "duplicate thread|conflicting"):
                    stnt.load_sessions()
                new.unlink()
                (root / "sessions" / ".partial.json.tmp").write_text("x")
                with self.assertRaisesRegex(stnt.StntError, "partial"):
                    stnt.load_sessions()

    def test_malformed_schema_fields_always_raise_stnt_error(self):
        for field, value in (("threadID", None), ("sandboxPort", "8000"),
                             ("repositoryPath", 3), ("createdAt", []),
                             ("sandboxID", ""), ("baseSHA", "no")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "state.json"
                path.write_text(json.dumps(dict(record(Path("/fixture")), **{field: value})))
                with self.assertRaises(stnt.StntError):
                    stnt.load_state(path)

    def test_loader_rejects_duplicate_nonempty_sandbox_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, repo = Path(temporary), Path("/fixture")
            (root / "sessions").mkdir()
            second_id = "T-22345678-1234-1234-1234-123456789abc"
            one, two = record(repo), dict(record(Path("/other")), threadID=second_id, sandbox="other")
            with mock.patch.dict(os.environ, {"STNT_STATE_HOME": str(root)}):
                stnt.session_path(repo, THREAD_ID).write_text(json.dumps(one))
                stnt.session_path(Path("/other"), second_id).write_text(json.dumps(two))
                with self.assertRaisesRegex(stnt.StntError, "duplicate sandbox ID"):
                    stnt.load_sessions()

    def test_loader_fails_closed_if_record_vanishes_during_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, repo = Path(temporary), Path("/fixture")
            (root / "sessions").mkdir()
            with mock.patch.dict(os.environ, {"STNT_STATE_HOME": str(root)}):
                path = stnt.session_path(repo, THREAD_ID)
                path.write_text(json.dumps(record(repo)))
                with mock.patch.object(stnt, "load_state", return_value=None):
                    with self.assertRaisesRegex(stnt.StntError, "changed concurrently"):
                        stnt.load_sessions()


class RepositoryProfileTests(unittest.TestCase):
    class TTY(io.StringIO):
        def isatty(self):
            return True

    def make_repo(self, root: Path, files, *, remote="git@github.com:example/project.git"):
        repo = root / "project"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Stnt Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "stnt@example.invalid"], cwd=repo, check=True)
        if remote:
            subprocess.run(["git", "remote", "add", "origin", remote], cwd=repo, check=True)
        for relative, content in files.items():
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "fixture"], cwd=repo, check=True, capture_output=True)
        return repo

    def fixture_files(self):
        return {
            "flake.nix": "{ outputs = { self }: { devShells.default = {}; }; }\n",
            "flake.lock": json.dumps({
                "version": 7,
                "root": "root",
                "nodes": {
                    "root": {"inputs": {"nixpkgs": "nixpkgs"}},
                    "nixpkgs": {"locked": {
                        "type": "github", "owner": "NixOS", "repo": "nixpkgs",
                        "rev": "a" * 40, "narHash": "sha256-fixture",
                    }},
                },
            }),
            ".tool-versions": "nodejs 20.0.0\n",
            "package.json": json.dumps({
                "packageManager": "yarn@4.12.0",
                "engines": {"node": "22.x"},
                "scripts": {"dev": "vite"},
                "devDependencies": {"vite": "7.0.0"},
            }),
            "yarn.lock": "lock\n",
            ".env.example": "DO_NOT_READ_REAL_ENV=\n",
            "vite.config.ts": """
throw new Error('this project code must never execute');
const certFile = './certs/app.local.example.pem';
const keyFile = './certs/app.local.example-key.pem';
const server = { host: 'app.local.example', port: 8010 };
if (existsSync(certFile) && existsSync(keyFile)) {
  server.https = {cert: readFileSync(certFile), key: readFileSync(keyFile)};
}
export default {server};
""",
        }

    def service_less_fixture_files(self):
        files = self.fixture_files()
        for name in ("package.json", "yarn.lock", ".env.example", "vite.config.ts"):
            del files[name]
        return files

    def approved_profile(self, repo):
        profile = stnt.build_profile(repo)

        def approve(value):
            if isinstance(value, dict):
                if "unresolved" in value:
                    value["unresolved"] = []
                for item in value.values():
                    approve(item)
            elif isinstance(value, list):
                for item in value:
                    approve(item)

        approve(profile)
        return profile

    def test_static_discovery_selects_nix_before_asdf_and_package_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self.make_repo(root, self.fixture_files())
            (repo / ".env").write_text("TOP_SECRET=must-not-appear\n")
            (repo / "certs").mkdir()
            (repo / "certs/app.local.example.pem").write_text("private certificate bytes")
            (repo / "certs/app.local.example-key.pem").write_text("private key bytes")

            discovered = stnt.discover_repository(repo)

            self.assertEqual(discovered["toolchain"]["provider"], "nix")
            self.assertEqual(discovered["toolchain"]["confidence"], "confirmed")
            self.assertEqual(discovered["secretCapabilities"], [])
            self.assertEqual(discovered["setup"][0]["argv"], ["yarn", "install", "--immutable"])
            service = discovered["services"][0]
            self.assertEqual(service["origin"], "https://app.local.example:8010")
            self.assertEqual(service["argv"], ["yarn", "dev", "--host", "0.0.0.0", "--strictPort"])
            self.assertIn("HTTPS is conditional", service["unresolved"][0])
            self.assertEqual(
                [item["source"] for item in discovered["localInputs"]],
                [".env", "certs/app.local.example.pem", "certs/app.local.example-key.pem"],
            )
            serialized = json.dumps(discovered)
            self.assertNotIn("must-not-appear", serialized)
            self.assertNotIn("private key bytes", serialized)
            self.assertEqual(discovered["evidence"]["flake.nix"]["blobID"], subprocess.run(
                ["git", "rev-parse", "HEAD:flake.nix"], cwd=repo, check=True,
                text=True, capture_output=True,
            ).stdout.strip())

            profile = stnt.build_profile(repo)
            self.assertEqual(profile["secretCapabilities"], [stnt.GITHUB_PUSH_CAPABILITY])
            self.assertIn("github.com:443", profile["network"]["domains"])

    def test_absent_nix_selects_asdf_without_falling_through_to_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            files = self.fixture_files()
            del files["flake.nix"]
            del files["flake.lock"]
            repo = self.make_repo(Path(temporary), files)
            discovered = stnt.discover_repository(repo)
            self.assertEqual(discovered["toolchain"]["provider"], "asdf")
            self.assertEqual(discovered["toolchain"]["versions"], [["nodejs", "20.0.0"]])

    def test_noninteractive_init_fails_with_exact_guidance_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self.make_repo(root, self.fixture_files())
            config, state = root / "config", root / "state"
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, {"STNT_CONFIG_HOME": str(config), "STNT_STATE_HOME": str(state)}), mock.patch.object(
                stnt, "configuration_repository", return_value=repo
            ), redirect_stdout(io.StringIO()), mock.patch("sys.stderr", stderr):
                self.assertEqual(stnt.main(["init"]), 1)
            self.assertIn("run stnt init in an interactive terminal", stderr.getvalue())
            self.assertEqual(list(config.rglob("*.json")), [])

    def test_staged_review_cancellation_and_interruption_never_publish_a_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self.make_repo(root, self.fixture_files())
            environment = {"STNT_CONFIG_HOME": str(root / "config"), "STNT_STATE_HOME": str(root / "state")}
            profile = stnt.build_profile(repo)
            with mock.patch.dict(os.environ, environment), mock.patch("sys.stdin", self.TTY()), mock.patch(
                "sys.stdout", self.TTY()
            ), mock.patch("builtins.input", side_effect=["y", "n"]):
                with self.assertRaisesRegex(stnt.StntError, "cancelled"):
                    stnt.review_profile(profile)
            self.assertFalse(stnt.profile_path(profile["repository"]).exists())

            with mock.patch.dict(os.environ, {**environment, "STNT_TEST_INTERRUPT_AT": "before-replace"}), mock.patch.object(
                stnt, "configuration_repository", return_value=repo
            ), mock.patch.object(stnt, "review_profile"), redirect_stdout(io.StringIO()):
                self.assertEqual(stnt.main(["init"]), 1)
            self.assertFalse(stnt.profile_path(profile["repository"]).exists())

    def test_interactive_review_does_not_run_under_a_progress_indicator(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self.make_repo(root, self.service_less_fixture_files())
            environment = {
                "STNT_CONFIG_HOME": str(root / "config"),
                "STNT_STATE_HOME": str(root / "state"),
            }
            with mock.patch.dict(os.environ, environment), mock.patch.object(
                stnt, "configuration_repository", return_value=repo
            ), mock.patch.object(stnt, "review_profile"), mock.patch.object(
                stnt, "ProgressIndicator"
            ) as indicator:
                self.assertEqual(stnt.command_profile_review("init"), 0)

            self.assertEqual(
                [call.args[0] for call in indicator.call_args_list],
                ["discovering repository configuration", "saving reviewed profile"],
            )

    def test_init_is_creation_only_and_unchanged_reconfigure_does_not_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self.make_repo(root, self.service_less_fixture_files())
            environment = {
                "STNT_CONFIG_HOME": str(root / "config"),
                "STNT_STATE_HOME": str(root / "state"),
            }
            profile = self.approved_profile(repo)
            with mock.patch.dict(os.environ, environment):
                stnt.ensure_state_layout()
                path = stnt.profile_path(profile["repository"])
                stnt.atomic_write(path, profile)
                original = path.read_bytes()
                with mock.patch.object(
                    stnt, "configuration_repository", return_value=repo
                ), mock.patch.object(stnt, "build_profile") as discover, mock.patch.object(
                    stnt, "review_profile"
                ) as review:
                    with self.assertRaisesRegex(stnt.StntError, "stnt reconfigure"):
                        stnt.command_init()
                discover.assert_not_called()
                review.assert_not_called()
                self.assertEqual(path.read_bytes(), original)

                output = io.StringIO()
                with mock.patch.object(
                    stnt, "configuration_repository", return_value=repo
                ), mock.patch.object(stnt, "review_profile") as review, redirect_stdout(output):
                    self.assertEqual(stnt.main(["reconfigure"]), 0)
                review.assert_not_called()
                self.assertEqual(path.read_bytes(), original)
                self.assertIn("unchanged; no review or write", output.getvalue())

    def test_reconfigure_cancellation_and_interruption_preserve_current_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self.make_repo(root, self.service_less_fixture_files())
            environment = {
                "STNT_CONFIG_HOME": str(root / "config"),
                "STNT_STATE_HOME": str(root / "state"),
            }
            current = self.approved_profile(repo)
            proposed = json.loads(json.dumps(current))
            proposed["resources"]["cpus"] = 5
            with mock.patch.dict(os.environ, environment):
                stnt.ensure_state_layout()
                path = stnt.profile_path(current["repository"])
                stnt.atomic_write(path, current)
                original = path.read_bytes()
                with mock.patch.object(
                    stnt, "configuration_repository", return_value=repo
                ), mock.patch.object(
                    stnt, "build_profile", return_value=proposed
                ), mock.patch.object(
                    stnt, "review_profile",
                    side_effect=stnt.StntError("configuration review cancelled"),
                ):
                    with self.assertRaisesRegex(stnt.StntError, "cancelled"):
                        stnt.command_reconfigure()
                self.assertEqual(path.read_bytes(), original)

            interrupted_environment = dict(environment, STNT_TEST_INTERRUPT_AT="before-replace")
            with mock.patch.dict(os.environ, interrupted_environment), mock.patch.object(
                stnt, "configuration_repository", return_value=repo
            ), mock.patch.object(
                stnt, "build_profile", return_value=proposed
            ), mock.patch.object(stnt, "review_profile"), redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(stnt.StntError, "synthetic interruption"):
                    stnt.command_reconfigure()
            self.assertEqual(path.read_bytes(), original)

    def test_reconfigure_replaces_future_policy_without_invalidating_retained_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self.make_repo(root, self.service_less_fixture_files())
            environment = {
                "STNT_CONFIG_HOME": str(root / "config"),
                "STNT_STATE_HOME": str(root / "state"),
            }
            current_profile = self.approved_profile(repo)
            source_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                text=True, capture_output=True,
            ).stdout.strip()
            kit_digest = "b" * 64
            with mock.patch.dict(os.environ, environment):
                stnt.ensure_state_layout()
                path = stnt.profile_path(current_profile["repository"])
                stnt.atomic_write(path, current_profile)
                with mock.patch.object(
                    stnt, "repository_default_branch", return_value=("main", source_sha)
                ), mock.patch.object(
                    stnt, "ensure_profile_kit", return_value=(Path("/kit"), kit_digest)
                ):
                    retained = stnt.create_record(repo, repository_profile=current_profile)

                (repo / "flake.nix").write_text(
                    "{ outputs = { self }: { devShells.default = {}; }; } # reviewed change\n"
                )
                subprocess.run(["git", "add", "flake.nix"], cwd=repo, check=True)
                subprocess.run([
                    "git", "commit", "-m", "change reviewed evidence",
                ], cwd=repo, check=True, capture_output=True)

                def approve(profile, **_kwargs):
                    def clear(value):
                        if isinstance(value, dict):
                            if "unresolved" in value:
                                value["unresolved"] = []
                            for item in value.values():
                                clear(item)
                        elif isinstance(value, list):
                            for item in value:
                                clear(item)
                    clear(profile)

                output = io.StringIO()
                with mock.patch.object(
                    stnt, "configuration_repository", return_value=repo
                ), mock.patch.object(
                    stnt, "review_profile", side_effect=approve
                ) as review, redirect_stdout(output):
                    self.assertEqual(stnt.command_reconfigure(), 0)

                replaced = stnt.load_profile(repo)[1]
                self.assertEqual(replaced["status"], "reviewed")
                self.assertNotIn("proofs", replaced)
                self.assertNotIn("activeApprovalDigest", replaced)
                self.assertIn('"evidence"', output.getvalue())
                review.assert_called_once()
                with mock.patch.object(
                    stnt, "load_profile", side_effect=AssertionError("workspace must not load current profile")
                ), mock.patch.object(
                    stnt, "ensure_profile_kit", return_value=(Path("/kit"), kit_digest)
                ):
                    self.assertEqual(stnt.verify_profile_record(retained), (Path("/kit"), kit_digest))

    def test_reviewed_profile_is_atomic_collision_safe_and_reports_focused_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self.make_repo(root, self.fixture_files())
            environment = {"STNT_CONFIG_HOME": str(root / "config"), "STNT_STATE_HOME": str(root / "state")}
            with mock.patch.dict(os.environ, environment), mock.patch.object(
                stnt, "configuration_repository", return_value=repo
            ), mock.patch.object(stnt, "review_profile"), redirect_stdout(io.StringIO()):
                self.assertEqual(stnt.command_init(), 0)
                path, profile = stnt.load_profile(repo)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(profile["status"], "reviewed")
            self.assertTrue(all(value == "unchanged" for value in stnt.profile_drift(repo, profile).values()))

            profile["secretCapabilities"] = [{
                "name": "registry", "provider": "keychain", "reference": "private-reference",
            }]
            stnt.atomic_write(path, profile)
            shown = io.StringIO()
            with mock.patch.dict(os.environ, environment), mock.patch.object(
                stnt, "configuration_repository", return_value=repo
            ), redirect_stdout(shown):
                self.assertEqual(stnt.command_config_show(), 0)
            displayed = json.loads(shown.getvalue())
            self.assertEqual(displayed["secretCapabilities"], [{
                "name": "registry", "provider": "keychain", "capability": "<redacted>",
            }])
            self.assertNotIn("private-reference", shown.getvalue())
            self.assertEqual(displayed["localInputStatus"][".env"], "missing")

            (repo / "package.json").write_text(json.dumps({"packageManager": "yarn@4.13.0"}))
            subprocess.run(["git", "add", "package.json"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "drift"], cwd=repo, check=True, capture_output=True)
            drift = stnt.profile_drift(repo, profile)
            self.assertEqual(drift["package.json"], "changed")
            self.assertEqual(drift["flake.nix"], "unchanged")

            other = dict(profile["repository"], path=str(root / "other"), key="f" * 64)
            with self.assertRaisesRegex(stnt.StntError, "collision"):
                stnt.validate_profile(profile, path, other)

    def test_remote_normalization_path_identity_and_secret_redaction(self):
        self.assertEqual(
            stnt.normalize_repository_remote("git@GitHub.com:example/example-app.git"),
            "github.com/example/example-app",
        )
        self.assertEqual(
            stnt.normalize_repository_remote("https://github.com/example/example-app.git"),
            "github.com/example/example-app",
        )
        first = {"remote": "github.com/example/repo", "path": "/one", "key": "a" * 64}
        second = {"remote": "github.com/example/repo", "path": "/two", "key": "b" * 64}
        self.assertNotEqual(stnt.profile_path(first), stnt.profile_path(second))
        redacted = stnt.redact_profile({
            "secretCapabilities": [{"provider": "keychain", "reference": "secret-id"}],
        })
        self.assertEqual(redacted["secretCapabilities"][0], {
            "provider": "keychain", "capability": "<redacted>",
        })

    def test_provision_plan_is_source_bound_complete_and_pinned(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = self.make_repo(Path(temporary), self.fixture_files())
            profile = self.approved_profile(repo)
            source_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
            ).stdout.strip()
            plan = stnt.provision_plan(repo, profile, source_sha)
            self.assertEqual(plan["toolchain"]["bootstrap"], {
                "version": stnt.NIX_VERSION,
                "url": stnt.NIX_AARCH64_LINUX_URL,
                "sha256": stnt.NIX_AARCH64_LINUX_SHA256,
            })
            self.assertEqual(plan["resources"], {"cpus": 4, "memoryGiB": 8})
            self.assertEqual(plan["sourceSHA"], source_sha)
            self.assertEqual(plan["secretCapabilities"], [stnt.GITHUB_PUSH_CAPABILITY])
            self.assertEqual(plan["gitRemote"], {
                "normalized": "github.com/example/project",
                "httpsURL": "https://github.com/example/project.git",
            })
            self.assertEqual(plan["digest"], stnt.canonical_digest({
                key: value for key, value in plan.items() if key != "digest"
            }))
            self.assertEqual(plan["networkDomains"], sorted([
                *stnt.NIX_BOOTSTRAP_DOMAINS, *stnt.GITHUB_FLAKE_DOMAINS,
                *stnt.PACKAGE_MANAGER_DOMAINS["yarn"],
            ]))

            profile["network"]["domains"].append("invented.invalid:443")
            with self.assertRaisesRegex(stnt.StntError, "exactly match"):
                stnt.provision_plan(repo, profile, source_sha)

    def test_zero_setup_zero_service_github_profile_has_a_source_bound_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = self.make_repo(Path(temporary), self.service_less_fixture_files())
            profile = self.approved_profile(repo)
            source_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
            ).stdout.strip()

            plan = stnt.provision_plan(repo, profile, source_sha)

        self.assertEqual(profile["setup"], [])
        self.assertEqual(profile["services"], [])
        self.assertEqual(plan["setup"], [])
        self.assertNotIn("service", plan)
        self.assertEqual(plan["sourceSHA"], source_sha)
        self.assertEqual(plan["gitRemote"], {
            "normalized": "github.com/example/project",
            "httpsURL": "https://github.com/example/project.git",
        })
        self.assertIn("github.com:443", plan["networkDomains"])
        self.assertEqual(plan["digest"], stnt.canonical_digest({
            key: value for key, value in plan.items() if key != "digest"
        }))

    def test_profile_rejects_multiple_services_and_service_only_inputs_without_a_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self.make_repo(root, self.fixture_files())
            profile = self.approved_profile(repo)
            source_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
            ).stdout.strip()
            profile["services"].append(dict(profile["services"][0]))
            with self.assertRaisesRegex(stnt.StntError, "more than one service"):
                stnt.validate_profile(profile, Path("/profile"), profile["repository"])
            with self.assertRaisesRegex(stnt.StntError, "more than one service"):
                stnt.provision_plan(repo, profile, source_sha)

            service_less_root = root / "service-less"
            service_less_root.mkdir()
            service_less_repo = self.make_repo(
                service_less_root, self.service_less_fixture_files(),
                remote="git@github.com:example/cli.git",
            )
            service_less = self.approved_profile(service_less_repo)
            service_less["localInputs"] = [{
                "source": ".env", "exposure": "service-env", "consumer": "web",
            }]
            with self.assertRaisesRegex(stnt.StntError, "service-only local input"):
                stnt.validate_profile(
                    service_less, Path("/profile"), service_less["repository"]
                )
            service_less_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=service_less_repo, check=True,
                text=True, capture_output=True,
            ).stdout.strip()
            with self.assertRaisesRegex(stnt.StntError, "service-only local input"):
                stnt.provision_plan(service_less_repo, service_less, service_less_sha)

    def test_service_less_profile_record_omits_service_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self.make_repo(root, self.service_less_fixture_files())
            profile = self.approved_profile(repo)
            source_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
            ).stdout.strip()
            with mock.patch.dict(os.environ, {"STNT_STATE_HOME": str(root / "state")}), mock.patch.object(
                stnt, "repository_default_branch", return_value=("main", source_sha)
            ), mock.patch.object(
                stnt, "ensure_profile_kit", return_value=(Path("/kit"), "b" * 64)
            ):
                current = stnt.create_record(repo, repository_profile=profile)
                durable = stnt.load_state(stnt.record_session_path(current))

        self.assertEqual(durable, current)
        self.assertIn("profilePlan", current)
        self.assertNotIn("service", current["profilePlan"])
        for field in ("sandboxPort", "serviceArgv", "serviceURL", "healthPath"):
            self.assertNotIn(field, current)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            path.write_text(json.dumps(dict(current, sandboxPort=8000)))
            with self.assertRaisesRegex(stnt.StntError, "service-less profile state"):
                stnt.load_state(path)

    def test_old_unresolved_profile_blocks_before_thread_or_sandbox_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = self.make_repo(Path(temporary), self.fixture_files())
            profile = stnt.build_profile(repo)
            with mock.patch.object(stnt, "ensure_profile_kit") as kit:
                with self.assertRaisesRegex(stnt.StntError, "unresolved.*stnt reconfigure"):
                    stnt.provision_plan(repo, profile, subprocess.run(
                        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                        text=True, capture_output=True,
                    ).stdout.strip())
            kit.assert_not_called()

    def test_generated_kit_is_checksum_pinned_and_has_only_reviewed_domains(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = self.make_repo(Path(temporary), self.fixture_files())
            profile = self.approved_profile(repo)
            source_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
            ).stdout.strip()
            rendered = stnt.render_profile_kit(stnt.provision_plan(repo, profile, source_sha))
            self.assertIn(stnt.NIX_AARCH64_LINUX_SHA256, rendered)
            self.assertIn(stnt.NIX_AARCH64_LINUX_URL, rendered)
            for domain in [
                *stnt.NIX_BOOTSTRAP_DOMAINS, *stnt.GITHUB_FLAKE_DOMAINS,
                *stnt.PACKAGE_MANAGER_DOMAINS["yarn"],
            ]:
                self.assertIn(domain, rendered)
            self.assertNotIn("invented.invalid", rendered)
            self.assertIn("--no-daemon --yes --no-channel-add --no-modify-profile", rendered)
            self.assertIn("import lzma, shutil, sys", rendered)
            self.assertNotIn("tar -xJf", rendered)
            self.assertIn("experimental-features = nix-command flakes", rendered)
            self.assertIn("/home/agent/.config/nix/nix.conf", rendered)
            self.assertIn("service: github", rendered)
            self.assertIn("scheme: basic", rendered)
            self.assertIn("username: x-access-token", rendered)
            self.assertNotIn("gho_", rendered)

    def test_non_github_profile_has_no_push_capability_or_git_remote_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = self.make_repo(
                Path(temporary), self.fixture_files(), remote="https://git.example.test/example/project.git"
            )
            profile = self.approved_profile(repo)
            source_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
            ).stdout.strip()
            plan = stnt.provision_plan(repo, profile, source_sha)
        self.assertEqual(profile["secretCapabilities"], [])
        self.assertIsNone(plan["gitRemote"])
        self.assertNotIn("service: github", stnt.render_profile_kit(plan))

    def test_github_origin_is_normalized_to_https_without_credential_material(self):
        current = dict(
            record(Path("/fixture")),
            profilePlan={"gitRemote": {
                "normalized": "github.com/example/project",
                "httpsURL": "https://github.com/example/project.git",
            }},
        )
        responses = [
            subprocess.CompletedProcess([], 0, "git@github.com:example/project.git\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "https://github.com/example/project.git\n", ""),
        ]
        with mock.patch.object(stnt, "run_lifecycle", side_effect=responses) as invoked:
            stnt.configure_profile_git_remote(current)
        arguments = [call.args[0] for call in invoked.call_args_list]
        self.assertIn(
            ["git", "remote", "set-url", "origin", "https://github.com/example/project.git"],
            [args[-5:] for args in arguments],
        )
        self.assertNotIn("GITHUB_TOKEN", json.dumps(arguments))
        self.assertNotIn("x-access-token", json.dumps(arguments))

    def test_github_origin_mismatch_is_retained_without_mutation(self):
        current = dict(
            record(Path("/fixture")),
            profilePlan={"gitRemote": {
                "normalized": "github.com/example/project",
                "httpsURL": "https://github.com/example/project.git",
            }},
        )
        with mock.patch.object(
            stnt, "run_lifecycle",
            return_value=subprocess.CompletedProcess([], 0, "git@github.com:other/project.git\n", ""),
        ) as invoked:
            with self.assertRaisesRegex(stnt.StntError, "does not match.*retained"):
                stnt.configure_profile_git_remote(current)
        invoked.assert_called_once()

    def test_profile_service_uses_argv_and_local_env_path_without_secret_contents(self):
        plan = {
            "service": {},
            "localInputs": [{"source": ".env", "exposure": "service-env", "consumer": "web"}],
        }
        current = dict(
            record(Path("/fixture")),
            sandboxPort=8010,
            serviceArgv=["nix", "develop", "--command", "yarn", "dev"],
            profilePlan=plan,
            healthPath="/",
            serviceURL="https://app.local.example:8010",
        )
        with mock.patch.object(
            stnt, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
        ) as invoked, mock.patch.object(stnt, "wait_for_health"):
            stnt.restart_service(current, 8010)
        args = invoked.call_args_list[0].args[0]
        self.assertEqual(args[1], "service-start-argv")
        self.assertIn("/run/sandbox/source/.env", args)
        self.assertIn("/usr/bin/env", args)
        self.assertIn("__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=app.local.example", args)
        self.assertNotIn("SECRET", json.dumps(args))
        self.assertEqual(args[-5:], current["serviceArgv"])
        self.assertIn(
            "[[ $line =~ ^[A-Za-z_][A-Za-z0-9_]*=.*$ ]] || continue",
            stnt.RUNTIME.read_text(),
        )

    def test_workspace_provisioning_runs_setup_and_service_proof_without_mutating_profile(self):
        approval = "d" * 64
        plan_body = {
            "schemaVersion": 1,
            "sourceSHA": "a" * 40,
            "profileApprovalDigest": approval,
            "toolchain": {"bootstrap": {"version": stnt.NIX_VERSION}},
            "setup": [["yarn", "install", "--immutable"]],
            "service": {
                "argv": ["yarn", "dev"], "origin": "https://app.local.example:8010",
                "port": 8010, "healthPath": "/",
            },
            "localInputs": [], "networkDomains": [],
            "resources": {"cpus": 4, "memoryGiB": 8},
        }
        plan = dict(plan_body, digest=stnt.canonical_digest(plan_body))
        current = dict(
            record(Path("/fixture")), sandboxPort=8010, sourceSHA="a" * 40,
            profilePlan=plan, profilePlanDigest=plan["digest"],
            profileApprovalDigest=approval, profileKitDigest="b" * 64,
            serviceArgv=stnt.nix_argv(["yarn", "dev"]),
            serviceURL="https://app.local.example:8010", healthPath="/",
        )

        def successful_run(args, **kwargs):
            if args[-1:] == ["--version"]:
                return subprocess.CompletedProcess(args, 0, f"nix (Nix) {stnt.NIX_VERSION}\n", "")
            if "port" in args:
                return subprocess.CompletedProcess(args, 0, '{"host_port":8010}', "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(stnt, "run", side_effect=successful_run), mock.patch.object(
            stnt, "require_loopback_service_host"
        ), mock.patch.object(stnt, "wait_for_health"):
            stnt.provision_profile(current)

        def setup_failure(args, **kwargs):
            if args[-1:] == ["--version"]:
                return subprocess.CompletedProcess(args, 0, f"nix (Nix) {stnt.NIX_VERSION}\n", "")
            return subprocess.CompletedProcess(args, 1, "", "setup failed")

        with mock.patch.object(stnt, "run", side_effect=setup_failure):
            with self.assertRaisesRegex(stnt.StntError, "setup command failed"):
                stnt.provision_profile(current)

    def test_service_less_workspace_provisions_without_service_or_profile_operations(self):
        approval = "d" * 64
        plan_body = {
            "schemaVersion": 1,
            "sourceSHA": "a" * 40,
            "profileApprovalDigest": approval,
            "toolchain": {"bootstrap": {"version": stnt.NIX_VERSION}},
            "setup": [], "localInputs": [], "networkDomains": [],
            "resources": {"cpus": 4, "memoryGiB": 8},
        }
        plan = dict(plan_body, digest=stnt.canonical_digest(plan_body))
        current = dict(
            record(Path("/fixture")), sourceSHA="a" * 40,
            profilePlan=plan, profilePlanDigest=plan["digest"],
            profileApprovalDigest=approval, profileKitDigest="b" * 64,
        )
        current.pop("sandboxPort")

        version = subprocess.CompletedProcess([], 0, f"nix (Nix) {stnt.NIX_VERSION}\n", "")
        with mock.patch.object(stnt, "run", return_value=version) as invoked, mock.patch.object(
            stnt, "run_lifecycle"
        ) as lifecycle, mock.patch.object(
            stnt, "require_loopback_service_host"
        ) as loopback, mock.patch.object(
            stnt, "restart_service"
        ) as restart, mock.patch.object(
            stnt, "wait_for_health"
        ) as health:
            stnt.provision_profile(current)

        invoked.assert_called_once()
        self.assertIn("project-exec", invoked.call_args.args[0])
        lifecycle.assert_not_called()
        loopback.assert_not_called()
        restart.assert_not_called()
        health.assert_not_called()


class LockingAndCreationTests(unittest.TestCase):
    def test_authoritative_default_branch_requires_agreeing_remote_heads_and_exact_local_ref(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Stnt Test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "stnt@example.invalid"], cwd=repo, check=True)
            (repo / "README.md").write_text("base\n")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
            main_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
            ).stdout.strip()

            with self.assertRaisesRegex(stnt.StntError, "no authoritative"):
                stnt.repository_default_branch(repo)
            original_run = stnt.run
            calls = []

            def recording_run(args, **kwargs):
                calls.append(args)
                return original_run(args, **kwargs)

            with mock.patch.dict(os.environ, {"STNT_STATE_HOME": str(Path(temporary) / "state")}), mock.patch.object(
                stnt, "run", side_effect=recording_run
            ):
                with self.assertRaisesRegex(stnt.StntError, "no authoritative"):
                    stnt.create_record(repo)
            self.assertNotIn([str(stnt.THREADS), "new"], calls)
            self.assertFalse(any(command in call for call in calls for command in ("fetch", "pull", "ls-remote")))

            subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=repo, check=True)
            subprocess.run([
                "git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"
            ], cwd=repo, check=True)
            self.assertEqual(stnt.repository_default_branch(repo), ("main", main_sha))

            subprocess.run([
                "git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/other/back"
            ], cwd=repo, check=True)
            subprocess.run([
                "git", "symbolic-ref", "refs/remotes/other/back", "refs/remotes/origin/main"
            ], cwd=repo, check=True)
            with self.assertRaisesRegex(stnt.StntError, "malformed"):
                stnt.repository_default_branch(repo)
            subprocess.run([
                "git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"
            ], cwd=repo, check=True)

            subprocess.run(["git", "branch", "develop"], cwd=repo, check=True)
            subprocess.run(["git", "remote", "add", "upstream", str(repo)], cwd=repo, check=True)
            subprocess.run([
                "git", "symbolic-ref", "refs/remotes/upstream/HEAD", "refs/remotes/upstream/main"
            ], cwd=repo, check=True)
            self.assertEqual(stnt.repository_default_branch(repo), ("main", main_sha))
            subprocess.run([
                "git", "symbolic-ref", "refs/remotes/upstream/HEAD", "refs/remotes/upstream/develop"
            ], cwd=repo, check=True)
            with self.assertRaisesRegex(stnt.StntError, "conflict"):
                stnt.repository_default_branch(repo)

            subprocess.run(["git", "remote", "remove", "upstream"], cwd=repo, check=True)
            subprocess.run(["git", "switch", "develop"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "branch", "-D", "main"], cwd=repo, check=True, capture_output=True)
            with self.assertRaisesRegex(stnt.StntError, "no exact local branch"):
                stnt.repository_default_branch(repo)

    def test_repository_validation_ignores_host_checkout_branch_and_sha(self):
        current = record(Path("/fixture"))
        with mock.patch.object(stnt, "run") as invoked:
            stnt.validate_record_repository(current, Path("/fixture"))
        invoked.assert_not_called()
        with self.assertRaisesRegex(stnt.StntError, "path"):
            stnt.validate_record_repository(current, Path("/other"))

    def test_repository_accepts_dirty_host_checkout_with_a_committed_head(self):
        repo = Path("/fixture")

        def fake_run(args, **kwargs):
            if args == ["git", "rev-parse", "--show-toplevel"]:
                return subprocess.CompletedProcess(args, 0, str(repo) + "\n", "")
            if args[-3:] == ["status", "--porcelain=v1", "--untracked-files=all"]:
                return subprocess.CompletedProcess(args, 0, " M local-change\n", "")
            if args[-2:] == ["branch", "--show-current"]:
                return subprocess.CompletedProcess(args, 0, "feature\n", "")
            if args[-2:] == ["rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")
            raise AssertionError(f"unexpected command: {args}")

        with mock.patch.object(stnt.shutil, "which", return_value="/bin/git"), mock.patch.object(
            stnt, "run", side_effect=fake_run
        ), mock.patch.object(Path, "is_dir", return_value=True):
            self.assertEqual(stnt.repository(), repo)

    def test_default_creation_uses_remote_default_not_current_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Stnt Test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "stnt@example.invalid"], cwd=repo, check=True)
            (repo / "README.md").write_text("base\n")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
            main_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
            ).stdout.strip()
            subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=repo, check=True)
            subprocess.run([
                "git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"
            ], cwd=repo, check=True)
            subprocess.run(["git", "switch", "-c", "feature"], cwd=repo, check=True, capture_output=True)
            (repo / "feature.txt").write_text("feature\n")
            subprocess.run(["git", "add", "feature.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "feature"], cwd=repo, check=True, capture_output=True)
            feature_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
            ).stdout.strip()

            def fake_run(args, **kwargs):
                if args == [str(stnt.THREADS), "new"]:
                    return subprocess.CompletedProcess(args, 0, THREAD_ID + "\n", "")
                return subprocess.run(
                    args, check=kwargs.get("check", True), text=True,
                    capture_output=kwargs.get("capture", True),
                )

            with mock.patch.dict(os.environ, {"STNT_STATE_HOME": str(root / "state")}), mock.patch.object(
                stnt, "run", side_effect=fake_run
            ):
                current = stnt.create_record(repo)
            self.assertEqual(current["baseBranch"], "feature")
            self.assertEqual(current["baseSHA"], feature_sha)
            self.assertEqual(current["sourceBranch"], "main")
            self.assertEqual(current["sourceSHA"], main_sha)
            self.assertEqual(current["branch"], "main")
            self.assertEqual(subprocess.run(
                ["git", "branch", "--show-current"], cwd=repo, check=True,
                text=True, capture_output=True,
            ).stdout.strip(), "feature")

    def test_from_branch_is_pinned_before_workspace_persistence_without_switching_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Stnt Test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "stnt@example.invalid"], cwd=repo, check=True)
            (repo / "README.md").write_text("base\n")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "switch", "-c", "feature/example-work"], cwd=repo, check=True, capture_output=True)
            (repo / "work.txt").write_text("work\n")
            subprocess.run(["git", "add", "work.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "work"], cwd=repo, check=True, capture_output=True)
            source_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
            ).stdout.strip()
            subprocess.run(["git", "switch", "main"], cwd=repo, check=True, capture_output=True)

            calls = []
            def fake_run(args, **kwargs):
                calls.append(args)
                if args == [str(stnt.THREADS), "new"]:
                    return subprocess.CompletedProcess(args, 0, THREAD_ID + "\n", "")
                return subprocess.run(
                    args, check=kwargs.get("check", True), text=True,
                    capture_output=kwargs.get("capture", True),
                )

            with mock.patch.dict(os.environ, {"STNT_STATE_HOME": str(root / "state")}), mock.patch.object(
                stnt, "run", side_effect=fake_run
            ):
                current = stnt.create_record(repo, from_branch="feature/example-work")
            self.assertEqual(current["sourceBranch"], "feature/example-work")
            self.assertEqual(current["sourceSHA"], source_sha)
            self.assertEqual(current["branch"], "feature/example-work")
            self.assertEqual(
                subprocess.run(["git", "branch", "--show-current"], cwd=repo, check=True, text=True, capture_output=True).stdout.strip(),
                "main",
            )
            self.assertNotIn("threadID", current)
            self.assertNotIn([str(stnt.THREADS), "new"], calls)

            calls.clear()
            with mock.patch.dict(os.environ, {"STNT_STATE_HOME": str(root / "other-state")}), mock.patch.object(
                stnt, "run", side_effect=fake_run
            ):
                with self.assertRaisesRegex(stnt.StntError, "does not exist"):
                    stnt.create_record(repo, from_branch="missing-branch")
            self.assertNotIn([str(stnt.THREADS), "new"], calls)

            for invalid in ("--detach", "-foo"):
                calls.clear()
                with mock.patch.dict(os.environ, {"STNT_STATE_HOME": str(root / f"invalid-{invalid[1:]}")}), mock.patch.object(
                    stnt, "run", side_effect=fake_run
                ):
                    with self.assertRaisesRegex(stnt.StntError, "invalid local source branch"):
                        stnt.create_record(repo, from_branch=invalid)
                self.assertNotIn([str(stnt.THREADS), "new"], calls)

    def test_process_level_default_and_new_creation_races_create_at_most_one_identity(self):
        worker = r'''
import importlib.util, json, os, time
from pathlib import Path
from unittest import mock

source = Path(os.environ["STNT_TEST_SOURCE"])
spec = importlib.util.spec_from_file_location("stnt_worker", source)
stnt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stnt)
repo = Path("/fixture")
thread_id = "T-32345678-1234-1234-1234-123456789abc"

def make_record(*args, **kwargs):
    Path(os.environ["STNT_TEST_MARKER"]).write_text("entered")
    time.sleep(0.5)
    identity = f"{stnt.repo_key(repo)[:8]}-{stnt.compact_thread_id(thread_id)}"
    value = {
        "schemaVersion": 1, "threadID": thread_id, "runtime": "docker-sandbox",
        "sandbox": f"stnt-fixture-{identity}", "repositoryPath": str(repo),
        "baseSHA": "a" * 40, "baseBranch": "master", "branch": f"stnt/{identity}",
        "preservationBranch": f"stnt-preserved/phase1c-{identity}", "sandboxPort": 8000,
        "status": "creating", "createdAt": "2026-08-12T00:00:00Z",
    }
    stnt.atomic_write(stnt.session_path(repo, thread_id), value, create_only=True)
    return value

def finish_creation(value, path):
    value["sandboxID"] = "sandbox-process-id"
    value["status"] = "paused"
    stnt.atomic_write(path, value)

argv = ["new"] if os.environ["STNT_TEST_MODE"] == "new" else []
with mock.patch.object(stnt, "repository", return_value=repo), \
     mock.patch.object(stnt, "critical_preflight"), \
     mock.patch.object(stnt, "create_record", side_effect=make_record), \
     mock.patch.object(stnt, "ensure_creation", side_effect=finish_creation), \
     mock.patch.object(stnt, "command_start"):
    raise SystemExit(stnt.main(argv))
'''
        for mode in ("default", "new"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "state"
                marker = Path(temporary) / "entered"
                with mock.patch.dict(os.environ, {"STNT_STATE_HOME": str(root)}):
                    stnt.ensure_state_layout()
                environment = dict(
                    os.environ,
                    STNT_STATE_HOME=str(root),
                    STNT_TEST_SOURCE=str(Path(__file__).resolve().parents[1] / "src/stnt.py"),
                    STNT_TEST_MARKER=str(marker),
                    STNT_TEST_MODE=mode,
                )
                first = subprocess.Popen(
                    [sys.executable, "-c", worker], text=True, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, env=environment,
                )
                deadline = time.monotonic() + 3
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists(), "first creator did not acquire the lock")
                second = subprocess.Popen(
                    [sys.executable, "-c", worker], text=True, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, env=environment,
                )
                first_output = first.communicate(timeout=5)
                second_output = second.communicate(timeout=5)
                self.assertEqual(first.returncode, 0, first_output)
                self.assertEqual(second.returncode, 1, second_output)
                records = list((root / "sessions").glob("*.json"))
                self.assertEqual(len(records), 1)
                self.assertEqual(json.loads(records[0].read_text())["threadID"], "T-32345678-1234-1234-1234-123456789abc")

    def test_same_session_lock_rejects_second_but_different_session_coexists(self):
        second = "T-22345678-1234-1234-1234-123456789abc"
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {"STNT_STATE_HOME": temporary}):
            stnt.ensure_state_layout()
            with stnt.session_lock(THREAD_ID):
                with self.assertRaisesRegex(stnt.StntError, "already in use"):
                    with stnt.session_lock(THREAD_ID):
                        pass
                with stnt.session_lock(second):
                    pass

    def test_lock_is_released_after_exception(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {"STNT_STATE_HOME": temporary}):
            stnt.ensure_state_layout()
            with self.assertRaisesRegex(RuntimeError, "boom"):
                with stnt.repository_lock(Path("/fixture")):
                    raise RuntimeError("boom")
            with stnt.repository_lock(Path("/fixture")):
                pass

    def test_global_creation_lock_serializes_repositories(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {"STNT_STATE_HOME": temporary}):
            stnt.ensure_state_layout()
            with stnt.repository_lock(Path("/one")), stnt.creation_lock():
                with stnt.repository_lock(Path("/two")):
                    with self.assertRaisesRegex(stnt.StntError, "creating"):
                        with stnt.creation_lock():
                            pass

    def test_duplicate_workspace_id_preserves_bytes_and_never_calls_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, repo, existing_repo = Path(temporary) / "state", Path("/fixture"), Path("/other")
            stnt.ensure_directory(root / "sessions")
            existing = workspace_record(existing_repo)
            with mock.patch.dict(os.environ, {"STNT_STATE_HOME": str(root)}):
                path = stnt.workspace_session_path(existing_repo, existing["workspaceID"])
                path.write_text(json.dumps(existing))
                before = path.read_bytes()
                responses = [subprocess.CompletedProcess([], 0, "a" * 40, ""),
                             subprocess.CompletedProcess([], 0, "master", "")]
                with mock.patch.object(stnt, "run", side_effect=responses), mock.patch.object(
                    stnt, "repository_default_branch", return_value=("master", "a" * 40)
                ), mock.patch.object(
                    stnt, "new_workspace_id", return_value=existing["workspaceID"]
                ):
                    with self.assertRaisesRegex(stnt.StntError, "workspace identity collision"):
                        stnt.create_record(repo)
                self.assertEqual(path.read_bytes(), before)

    def test_repository_lock_contention_blocks_second_default_creation_decision(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"STNT_STATE_HOME": temporary}
        ):
            stnt.ensure_state_layout()
            repo = Path("/fixture")
            with stnt.repository_lock(repo), mock.patch.object(
                stnt, "repository", return_value=repo
            ), mock.patch.object(stnt, "create_record") as create:
                self.assertEqual(stnt.main([]), 1)
            create.assert_not_called()

    def test_creation_lock_contention_blocks_concurrent_new_without_reuse(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"STNT_STATE_HOME": temporary}
        ):
            stnt.ensure_state_layout()
            repo = Path("/fixture")
            with stnt.creation_lock(), mock.patch.object(
                stnt, "repository", return_value=repo
            ), mock.patch.object(stnt, "load_sessions", return_value=[]), mock.patch.object(
                stnt, "critical_preflight"
            ), mock.patch.object(stnt, "create_record") as create:
                self.assertEqual(stnt.main(["new"]), 1)
            create.assert_not_called()

    def test_explicit_new_creates_second_session_but_ordinary_start_resumes(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"STNT_STATE_HOME": temporary}
        ):
            stnt.ensure_state_layout()
            repo = Path("/fixture")
            current = record(repo)
            second_id = "T-22345678-1234-1234-1234-123456789abc"
            second = dict(current, threadID=second_id, sandbox="stnt-fixture-second")
            current_pair = (stnt.session_path(repo, THREAD_ID), current)
            second_pair = (stnt.session_path(repo, second_id), second)
            current_pair[0].write_text(json.dumps(current))
            with mock.patch.object(stnt, "repository", return_value=repo), mock.patch.object(
                stnt, "migrate_legacy_session"
            ), mock.patch.object(stnt, "load_sessions", return_value=[current_pair]), mock.patch.object(
                stnt, "command_start"
            ) as start, mock.patch.object(stnt, "create_record", return_value=second) as create, mock.patch.object(
                stnt, "critical_preflight"
            ) as preflight, mock.patch.object(stnt, "ensure_creation"):
                self.assertEqual(stnt.main([]), 0)
                create.assert_not_called()
                self.assertEqual(start.call_args.kwargs["selected"][1], current)
                start.reset_mock()
                self.assertEqual(stnt.main(["new", "--from", "feature/example-work"]), 0)
                create.assert_called_once()
                self.assertEqual(create.call_args.kwargs["from_branch"], "feature/example-work")
                self.assertEqual(start.call_args.kwargs["selected"], second_pair)
                self.assertTrue(preflight.call_args.kwargs["offer_credentials"])


class DoctorTests(unittest.TestCase):
    def test_each_required_tool_is_reported_without_invoking_it(self):
        for missing in ("amp", "sbx", "git", "jq"):
            with self.subTest(missing=missing), mock.patch.object(
                stnt.shutil, "which", side_effect=lambda name, absent=missing: None if name == absent else f"/bin/{name}"
            ):
                checks = {check["id"]: check for check in stnt.tool_checks()}
            self.assertEqual(checks[f"tool.{missing}"]["status"], "blocked")
            self.assertIn("nextCommand", checks[f"tool.{missing}"])

    def test_blocked_preflight_cannot_create_workspace(self):
        repo = Path("/fixture")
        blocked = [stnt.result("tool.sbx", "blocked", "missing", next_command="install sbx")]
        with mock.patch.object(stnt, "load_state", return_value=None), mock.patch.object(
            stnt, "doctor_results", return_value=blocked
        ), mock.patch.object(stnt, "create_record") as create:
            with self.assertRaisesRegex(stnt.StntError, "before workspace creation"):
                stnt.command_start(repo)
        create.assert_not_called()

    def test_workspace_preflight_does_not_require_host_amp_but_stack_preflight_does(self):
        repo = Path("/fixture")
        blocked = [stnt.result("tool.amp", "blocked", "missing", next_command="install amp")]
        with mock.patch.object(stnt, "doctor_results", return_value=blocked):
            stnt.critical_preflight(repo)
            with self.assertRaisesRegex(stnt.StntError, "tool.amp"):
                stnt.critical_preflight(repo, require_host_amp=True)

    def test_github_secret_is_required_only_for_reviewed_push_capability(self):
        repo = Path("/fixture")
        warnings = [
            stnt.result(
                "docker.github-secret", "warning", "not registered",
                next_command="gh auth token | sbx secret set github",
            ),
            stnt.result(
                "docker.github-binding", "warning", "not approved",
                next_command="stnt setup",
            ),
        ]
        with mock.patch.object(stnt, "doctor_results", return_value=warnings):
            stnt.critical_preflight(repo)
            with self.assertRaisesRegex(stnt.StntError, "(?s)docker.github-secret.*docker.github-binding"):
                stnt.critical_preflight(repo, require_github=True)

    def test_interactive_preflight_offers_required_credentials_and_rechecks(self):
        repo = Path("/fixture")
        missing = [stnt.result(
            "docker.secret", "blocked", "not registered",
            next_command="sbx secret set-custom --host ampcode.com --env AMP_API_KEY",
        ), stnt.result(
            "docker.github-secret", "warning", "not registered",
            next_command="gh auth token | sbx secret set github",
        )]
        ready = [
            stnt.result("docker.secret", "pass", "registered"),
            stnt.result("docker.github-secret", "pass", "registered"),
        ]
        with mock.patch.object(
            stnt, "doctor_results", side_effect=[missing, ready]
        ) as doctor, mock.patch.object(
            stnt, "configure_amp_secret", return_value=True
        ) as amp, mock.patch.object(
            stnt, "configure_github_secret", return_value=False
        ) as github:
            stnt.critical_preflight(
                repo, require_github=True, offer_credentials=True,
            )

        amp.assert_called_once_with()
        github.assert_called_once_with()
        self.assertEqual(doctor.call_count, 2)

    def test_amp_binding_is_required_for_every_workspace(self):
        blocked = [stnt.result(
            "docker.amp-binding", "blocked", "not approved",
            next_command="stnt setup",
        )]
        with mock.patch.object(stnt, "doctor_results", return_value=blocked):
            with self.assertRaisesRegex(stnt.StntError, "docker.amp-binding"):
                stnt.critical_preflight(Path("/fixture"))

    def test_daemon_and_login_failures_are_distinct(self):
        diagnosis = {
            "checks": [
                {"name": "Daemon", "status": "pass"},
                {"name": "Socket", "status": "pass"},
                {"name": "Authentication", "status": "fail"},
            ]
        }

        def fake_run(args, **kwargs):
            command = args[1]
            values = {
                "version": (0, "sbx version: v0.38.0\n"),
                "daemon-status": (0, "Status: running\n"),
                "diagnose": (0, json.dumps(diagnosis)),
                "list": (0, '{"sandboxes": []}'),
                "policy": (0, '{"rules": []}'),
                "policy-check": (1, "Denied: stnt-doctor.invalid:443\nContext: global\nReason: no matching allow rule (default deny)"),
                "secrets": (0, '[]'),
                "amp-binding-status": (0, '{"approved":true,"fileExists":true}'),
                "github-binding-status": (0, '{"approved":false,"fileExists":false}'),
                "validate-kit": (0, "VALID"),
            }
            code, output = values[command]
            return subprocess.CompletedProcess(args, code, output, "")

        with mock.patch.object(stnt.shutil, "which", return_value="/bin/tool"), mock.patch.object(
            stnt, "run", side_effect=fake_run
        ):
            checks = {check["id"]: check for check in stnt.docker_checks()}
        self.assertEqual(checks["docker.daemon"]["status"], "pass")
        self.assertEqual(checks["docker.login"]["status"], "blocked")
        self.assertEqual(checks["docker.login"]["nextCommand"], "sbx login")

    def test_malformed_contract_and_missing_policy_secret_fail_closed(self):
        diagnosis = {"checks": [
            {"name": "Daemon", "status": "pass"},
            {"name": "Socket", "status": "pass"},
            {"name": "Authentication", "status": "pass"},
        ]}

        def fake_run(args, **kwargs):
            command = args[1]
            outputs = {
                "version": "sbx version: v0.39.0",
                "daemon-status": "Status: running",
                "diagnose": json.dumps(diagnosis),
                "list": "not-json",
                "policy": '{"rules": []}',
                "policy-check": "Allowed: stnt-doctor.invalid:443",
                "secrets": '[{"target":"example.com","name":"OTHER"}]',
                "amp-binding-status": '{"approved":false,"fileExists":false}',
                "github-binding-status": '{"approved":false,"fileExists":false}',
                "validate-kit": "VALID",
            }
            return subprocess.CompletedProcess(args, 0, outputs[command], "")

        with mock.patch.object(stnt.shutil, "which", return_value="/bin/tool"), mock.patch.object(
            stnt, "run", side_effect=fake_run
        ):
            checks = {check["id"]: check for check in stnt.docker_checks()}
        self.assertEqual(checks["docker.version"]["status"], "pass")
        for check_id in ("docker.json", "docker.policy", "docker.secret"):
            self.assertEqual(checks[check_id]["status"], "blocked")

    def test_sandbox_version_below_minimum_fails_closed(self):
        def fake_run(args, **kwargs):
            command = args[1]
            outputs = {
                "version": "sbx version: v0.37.9",
                "validate-kit": "VALID",
                "amp-binding-status": '{"approved":true,"fileExists":true}',
                "github-binding-status": '{"approved":true,"fileExists":true}',
                "daemon-status": "Status: stopped",
            }
            return subprocess.CompletedProcess(args, 0, outputs[command], "")

        with mock.patch.object(stnt.shutil, "which", return_value="/bin/tool"), mock.patch.object(
            stnt, "run", side_effect=fake_run
        ):
            checks = {check["id"]: check for check in stnt.docker_checks()}

        self.assertEqual(checks["docker.version"]["status"], "blocked")
        self.assertIn("minimum supported version is v0.38.0", checks["docker.version"]["summary"])

    def test_sandbox_version_above_minimum_is_supported(self):
        def fake_run(args, **kwargs):
            command = args[1]
            outputs = {
                "version": "sbx version: v1.0.0",
                "validate-kit": "VALID",
                "amp-binding-status": '{"approved":true,"fileExists":true}',
                "github-binding-status": '{"approved":true,"fileExists":true}',
                "daemon-status": "Status: stopped",
            }
            return subprocess.CompletedProcess(args, 0, outputs[command], "")

        with mock.patch.object(stnt.shutil, "which", return_value="/bin/tool"), mock.patch.object(
            stnt, "run", side_effect=fake_run
        ):
            checks = {check["id"]: check for check in stnt.docker_checks()}

        self.assertEqual(checks["docker.version"]["status"], "pass")

    def test_nonzero_diagnosis_still_classifies_healthy_critical_checks(self):
        diagnosis = {"checks": [
            {"name": "Daemon", "status": "pass"},
            {"name": "Socket", "status": "pass"},
            {"name": "Authentication", "status": "pass"},
            {"name": "SSH client config", "status": "fail"},
        ]}

        def fake_run(args, **kwargs):
            command = args[1]
            values = {
                "version": (0, "sbx version: v0.38.0"),
                "validate-kit": (0, "VALID"),
                "daemon-status": (0, "Status: running"),
                "diagnose": (1, json.dumps(diagnosis)),
                "list": (0, '{"sandboxes":[null]}'),
                "policy": (1, '{"rules":[]}'),
                "policy-check": (0, "Allowed: stnt-doctor.invalid:443"),
                "secrets": (1, '[]'),
                "amp-binding-status": (1, 'not-json'),
                "github-binding-status": (1, 'not-json'),
            }
            code, output = values[command]
            return subprocess.CompletedProcess(args, code, output, "provider failed")

        with mock.patch.object(stnt.shutil, "which", return_value="/bin/tool"), mock.patch.object(
            stnt, "run", side_effect=fake_run
        ):
            checks = {check["id"]: check for check in stnt.docker_checks()}
        for check_id in ("docker.daemon", "docker.login"):
            self.assertEqual(checks[check_id]["status"], "pass")
        for check_id in ("docker.json", "docker.policy", "docker.secret"):
            self.assertEqual(checks[check_id]["status"], "blocked")

    def test_explicit_policy_denial_does_not_prove_default_deny(self):
        diagnosis = {"checks": [
            {"name": "Daemon", "status": "pass"},
            {"name": "Socket", "status": "pass"},
            {"name": "Authentication", "status": "pass"},
        ]}

        def fake_run(args, **kwargs):
            values = {
                "version": (0, "sbx version: v0.38.0"),
                "validate-kit": (0, "VALID"),
                "daemon-status": (0, "Status: running"),
                "diagnose": (0, json.dumps(diagnosis)),
                "list": (0, '{"sandboxes":[]}'),
                "policy": (0, '{"rules":[]}'),
                "policy-check": (1, "Denied: stnt-doctor.invalid:443\nContext: global\nReason: explicitly denied by rule"),
                "secrets": (0, '[{"target":"ampcode.com","name":"AMP_API_KEY"}]'),
                "amp-binding-status": (0, '{"approved":true,"fileExists":true}'),
                "github-binding-status": (0, '{"approved":true,"fileExists":true}'),
            }
            code, output = values[args[1]]
            return subprocess.CompletedProcess(args, code, output, "")

        with mock.patch.object(stnt.shutil, "which", return_value="/bin/tool"), mock.patch.object(
            stnt, "run", side_effect=fake_run
        ):
            checks = {check["id"]: check for check in stnt.docker_checks()}
        self.assertEqual(checks["docker.policy"]["status"], "blocked")

    def test_doctor_git_status_disables_optional_index_locks(self):
        repo = Path("/fixture")
        responses = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "master\n", ""),
            subprocess.CompletedProcess([], 0, "a" * 40 + "\n", ""),
        ]
        with mock.patch.object(stnt.shutil, "which", return_value="/bin/git"), mock.patch.object(
            stnt, "run", side_effect=responses
        ) as invoked, mock.patch.object(Path, "is_dir", return_value=True):
            stnt.git_checks(repo)
        self.assertEqual(
            invoked.call_args_list[0].args[0][:4],
            ["git", "--no-optional-locks", "-C", str(repo)],
        )

    def test_dirty_host_is_a_nonblocking_warning_for_pinned_source_creation(self):
        repo = Path("/fixture")
        responses = [
            subprocess.CompletedProcess([], 0, " M local-change\n", ""),
            subprocess.CompletedProcess([], 0, "feature\n", ""),
            subprocess.CompletedProcess([], 0, "a" * 40 + "\n", ""),
        ]
        with mock.patch.object(stnt.shutil, "which", return_value="/bin/git"), mock.patch.object(
            stnt, "run", side_effect=responses
        ), mock.patch.object(Path, "is_dir", return_value=True):
            check = stnt.git_checks(repo)[0]

        self.assertEqual(check["status"], "warning")
        self.assertIn("pinned committed branch", check["summary"])
        with mock.patch.object(stnt, "doctor_results", return_value=[check]):
            stnt.critical_preflight(repo)

    def test_stopped_daemon_skips_daemon_backed_inventory(self):
        invoked = []

        def fake_run(args, **kwargs):
            invoked.append(args[1])
            if args[1] == "version":
                return subprocess.CompletedProcess(args, 0, "sbx version: v0.38.0", "")
            if args[1] == "validate-kit":
                return subprocess.CompletedProcess(args, 0, "VALID", "")
            if args[1] in {"amp-binding-status", "github-binding-status"}:
                return subprocess.CompletedProcess(args, 0, '{"approved":false,"fileExists":false}', "")
            if args[1] == "daemon-status":
                return subprocess.CompletedProcess(args, 1, "Status: stopped", "")
            raise AssertionError(f"unexpected daemon-backed command: {args}")

        with mock.patch.object(stnt.shutil, "which", return_value="/bin/tool"), mock.patch.object(
            stnt, "run", side_effect=fake_run
        ):
            checks = {check["id"]: check for check in stnt.docker_checks()}
        self.assertEqual(invoked, [
            "version", "validate-kit", "amp-binding-status",
            "github-binding-status", "daemon-status",
        ])
        self.assertEqual(checks["docker.daemon"]["status"], "blocked")
        self.assertEqual(checks["docker.secret"]["status"], "ambiguous")

    def test_full_doctor_does_not_reconcile_sessions_when_daemon_is_stopped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sessions").mkdir(mode=0o700)
            (root / "locks").mkdir(mode=0o700)
            current = record(Path("/fixture"))
            state_name = f"{stnt.repo_key(Path('/fixture'))}--{stnt.compact_thread_id(THREAD_ID)}.json"
            (root / "sessions" / state_name).write_text(json.dumps(current))
            stopped = [stnt.result("docker.daemon", "blocked", "stopped", next_command="sbx daemon restart")]
            with mock.patch.dict(os.environ, {"STNT_STATE_HOME": str(root)}), mock.patch.object(
                stnt, "tool_checks", return_value=[]
            ), mock.patch.object(stnt, "host_checks", return_value=[]), mock.patch.object(
                stnt, "git_checks", return_value=[]
            ), mock.patch.object(stnt, "amp_checks", return_value=[]), mock.patch.object(
                stnt, "docker_checks", return_value=stopped
            ), mock.patch.object(stnt, "integration_checks", return_value=[]), mock.patch.object(
                stnt, "runtime_find"
            ) as runtime:
                checks = {check["id"]: check for check in stnt.doctor_results(None)}
            runtime.assert_not_called()
            self.assertEqual(checks["state.sessions"]["status"], "ambiguous")

    def test_stale_session_reconciliation_never_starts_sandbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sessions").mkdir(mode=0o700)
            (root / "locks").mkdir(mode=0o700)
            current = record(Path("/fixture"))
            state_name = f"{stnt.repo_key(Path('/fixture'))}--{stnt.compact_thread_id(THREAD_ID)}.json"
            (root / "sessions" / state_name).write_text(json.dumps(current))
            with mock.patch.dict(os.environ, {"STNT_STATE_HOME": str(root)}), mock.patch.object(
                stnt.shutil, "which", return_value="/bin/tool"
            ), mock.patch.object(stnt, "validate_record_repository"), mock.patch.object(
                stnt, "runtime_find", return_value=None
            ) as inventory, mock.patch.object(
                stnt, "thread_status", return_value="active"
            ) as status:
                checks = {check["id"]: check for check in stnt.state_checks()}
            self.assertEqual(checks["state.sessions"]["status"], "ambiguous")
            inventory.assert_called_once_with(current["sandbox"])
            status.assert_called_once_with(THREAD_ID, allow_empty=True)

    def test_missing_editors_and_ssh_are_informational_only(self):
        with mock.patch.object(stnt.shutil, "which", return_value=None), mock.patch.object(
            Path, "is_file", return_value=False
        ):
            checks = stnt.integration_checks()
        self.assertTrue(all(check["status"] == "warning" for check in checks))

    def test_clipboard_image_paste_diagnostic_reports_consent_and_revocation(self):
        cases = (
            (
                subprocess.CompletedProcess([], 0, '{"value":true}', ""),
                "pass", "sbx settings set clipboard.imagePaste false", None,
            ),
            (
                subprocess.CompletedProcess([], 0, '{"value":false}', ""),
                "warning", "image-only host clipboard reads", "stnt setup",
            ),
            (
                subprocess.CompletedProcess([], 1, "", "unavailable"),
                "warning", "unavailable or unreadable",
                "sbx settings get --json clipboard.imagePaste",
            ),
        )
        for response, expected_status, summary, next_command in cases:
            with self.subTest(expected_status=expected_status, next_command=next_command), mock.patch.object(
                stnt.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}" if name == "sbx" else None
            ), mock.patch.object(
                stnt, "run", side_effect=lambda args, **kwargs: (
                    response if args[1] == "clipboard-image-paste-status"
                    else subprocess.CompletedProcess(args, 1, "", "")
                )
            ), mock.patch.object(stnt, "vscode_command", return_value=None), mock.patch.object(
                Path, "is_file", return_value=False
            ):
                check = {item["id"]: item for item in stnt.integration_checks()}["clipboard.image-paste"]
            self.assertEqual(check["status"], expected_status)
            self.assertIn(summary, check["summary"])
            self.assertEqual(check.get("nextCommand"), next_command)

    def test_clipboard_image_paste_diagnostic_does_not_query_stopped_daemon(self):
        with mock.patch.object(stnt.shutil, "which", return_value="/usr/bin/tool"), mock.patch.object(
            stnt, "run", return_value=subprocess.CompletedProcess([], 1, "", "")
        ) as invoked, mock.patch.object(stnt, "vscode_command", return_value=None), mock.patch.object(
            Path, "is_file", return_value=False
        ):
            check = {
                item["id"]: item for item in stnt.integration_checks(runtime_available=False)
            }["clipboard.image-paste"]
        self.assertEqual(check["status"], "ambiguous")
        self.assertFalse(any(call.args[0][1] == "clipboard-image-paste-status" for call in invoked.call_args_list))

    def test_ssh_diagnostic_uses_daemon_feature_not_obsolete_tcp_port(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / ".ssh").mkdir()
            (home / ".ssh/config").write_text(
                stnt.stnt_ssh_block(Path("/usr/bin/stnt")) + "Host *.sbx\n"
            )

            def available(name):
                return f"/usr/bin/{name}" if name in {"ssh", "sbx"} else None

            with mock.patch.object(Path, "home", return_value=home), mock.patch.object(
                stnt.shutil, "which", side_effect=available
            ), mock.patch.object(
                stnt, "vscode_command", return_value=None
            ), mock.patch.object(
                stnt, "run", return_value=subprocess.CompletedProcess([], 0, '{"enabled":true}', "")
            ) as invoked:
                checks = {check["id"]: check for check in stnt.integration_checks()}

        self.assertEqual(checks["ssh.client"]["status"], "pass")
        self.assertEqual(invoked.call_args_list, [
            mock.call([str(stnt.RUNTIME), "ssh-status"], check=False),
            mock.call([str(stnt.RUNTIME), "clipboard-image-paste-status"], check=False),
        ])

    def test_stnt_ssh_setup_is_atomic_idempotent_and_precedes_docker_wildcard(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            ssh = home / ".ssh"
            ssh.mkdir()
            config = ssh / "config"
            config.write_text(
                "Host example\n    HostName example.com\n\n"
                f"{stnt.DOCKER_SSH_BEGIN}\nHost *.sbx\n"
            )
            with mock.patch.object(Path, "home", return_value=home), mock.patch.object(
                stnt, "stnt_executable", return_value=Path("/usr/local/bin/stnt")
            ):
                self.assertTrue(stnt.install_stnt_ssh_config())
                first = config.read_text()
                self.assertFalse(stnt.install_stnt_ssh_config())

            self.assertLess(first.index("Host *.stnt.sbx"), first.index("Host *.sbx"))
            self.assertIn('ProxyCommand "/usr/local/bin/stnt" ssh-proxy %n', first)
            self.assertIn('KnownHostsCommand "/usr/local/bin/stnt" ssh-known-hosts %H', first)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    def test_stnt_ssh_setup_refuses_a_symlinked_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            ssh = home / ".ssh"
            ssh.mkdir()
            target = home / "shared-config"
            target.write_text("Host shared\n")
            (ssh / "config").symlink_to(target)
            with mock.patch.object(Path, "home", return_value=home):
                with self.assertRaisesRegex(stnt.StntError, "not a regular file"):
                    stnt.install_stnt_ssh_config()
            self.assertEqual(target.read_text(), "Host shared\n")

    def test_setup_only_initializes_local_state(self):
        checks = [stnt.result("docker.login", "blocked", "not logged in", next_command="sbx login")]
        output = io.StringIO()
        with mock.patch.object(stnt, "ensure_state_layout") as initialize, mock.patch.object(
            stnt, "optional_repository", return_value=None
        ), mock.patch.object(stnt, "doctor_results", return_value=checks), mock.patch.object(
            stnt, "configure_amp_secret", return_value=False
        ), mock.patch.object(
            stnt, "configure_github_secret", return_value=False
        ), mock.patch.object(
            stnt, "configure_stnt_bindings", return_value=False
        ), mock.patch.object(
            stnt, "configure_stnt_ssh", return_value=False
        ), mock.patch.object(
            stnt, "run"
        ) as global_command, redirect_stdout(output):
            self.assertEqual(stnt.command_setup(), 1)
        initialize.assert_called_once()
        global_command.assert_not_called()
        self.assertIn("No Docker login, policy, secret, binding, daemon, SSH, editor, or clipboard setting change", output.getvalue())
        self.assertIn("[BLOCKED] docker.login: sbx login", output.getvalue())

    def test_setup_enables_clipboard_image_paste_only_after_interactive_approval(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        checks = [stnt.result("docker.daemon", "pass", "healthy")]
        disabled = subprocess.CompletedProcess([], 0, '{"value":false}', "")
        changed = subprocess.CompletedProcess([], 0, "updated", "")
        enabled = subprocess.CompletedProcess([], 0, '{"value":true}', "")
        output = Terminal()
        with mock.patch.object(stnt, "ensure_state_layout"), mock.patch.object(
            stnt, "optional_repository", return_value=None
        ), mock.patch.object(stnt, "doctor_results", return_value=checks), mock.patch.object(
            stnt, "configure_amp_secret", return_value=False
        ), mock.patch.object(
            stnt, "configure_github_secret", return_value=False
        ), mock.patch.object(
            stnt, "configure_stnt_bindings", return_value=False
        ), mock.patch.object(
            stnt, "configure_stnt_ssh", return_value=False
        ), mock.patch.object(
            stnt, "run", side_effect=[disabled, changed, enabled]
        ) as invoked, mock.patch.object(
            sys.stdin, "isatty", return_value=True
        ), mock.patch("builtins.input", return_value="y") as approval, redirect_stdout(output):
            self.assertEqual(stnt.command_setup(), 0)

        approval.assert_called_once_with("Enable host clipboard image paste for Docker Sandboxes? [y/N] ")
        self.assertEqual([call.args[0] for call in invoked.call_args_list], [
            [str(stnt.RUNTIME), "clipboard-image-paste-status"],
            [str(stnt.RUNTIME), "clipboard-image-paste-enable"],
            [str(stnt.RUNTIME), "clipboard-image-paste-status"],
        ])
        self.assertIn("Only the explicitly approved", output.getvalue())
        self.assertIn("Setup complete; there are no remaining commands", output.getvalue())

    def test_setup_reports_revocation_when_clipboard_image_paste_is_already_enabled(self):
        enabled = subprocess.CompletedProcess([], 0, '{"value":true}', "")
        output = io.StringIO()
        with mock.patch.object(stnt, "run", return_value=enabled), redirect_stdout(output):
            self.assertFalse(stnt.configure_clipboard_image_paste())
        self.assertIn("sbx settings set clipboard.imagePaste false", output.getvalue())

    def test_noninteractive_setup_prints_clipboard_command_without_changing_setting(self):
        checks = [stnt.result("docker.daemon", "pass", "healthy")]
        disabled = subprocess.CompletedProcess([], 0, '{"value":false}', "")
        output = io.StringIO()
        with mock.patch.object(stnt, "ensure_state_layout"), mock.patch.object(
            stnt, "optional_repository", return_value=None
        ), mock.patch.object(stnt, "doctor_results", return_value=checks), mock.patch.object(
            stnt, "configure_amp_secret", return_value=False
        ), mock.patch.object(
            stnt, "configure_github_secret", return_value=False
        ), mock.patch.object(
            stnt, "configure_stnt_bindings", return_value=False
        ), mock.patch.object(
            stnt, "configure_stnt_ssh", return_value=False
        ), mock.patch.object(
            stnt, "run", return_value=disabled
        ) as invoked, mock.patch.object(
            sys.stdin, "isatty", return_value=False
        ), redirect_stdout(output):
            self.assertEqual(stnt.command_setup(), 0)

        invoked.assert_called_once_with([str(stnt.RUNTIME), "clipboard-image-paste-status"], check=False)
        self.assertIn("enable explicitly: sbx settings set clipboard.imagePaste true", output.getvalue())
        self.assertIn("No Docker login, policy, secret, binding, daemon, SSH, editor, or clipboard setting change", output.getvalue())

    def test_setup_delegates_amp_key_entry_to_docker_and_verifies_registration(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        missing = subprocess.CompletedProcess([], 0, "[]", "")
        changed = subprocess.CompletedProcess([], 0, None, None)
        registered = subprocess.CompletedProcess(
            [], 0, '[{"target":"ampcode.com","name":"AMP_API_KEY"}]', ""
        )
        output = Terminal()
        with mock.patch.object(
            stnt, "run", side_effect=[missing, changed, registered]
        ) as invoked, mock.patch.object(
            sys.stdin, "isatty", return_value=True
        ), mock.patch("builtins.input", return_value="y") as approval, redirect_stdout(output):
            self.assertTrue(stnt.configure_amp_secret())

        approval.assert_called_once_with(
            "Configure the Amp API key now using Docker's secure prompt? [y/N] "
        )
        self.assertEqual(invoked.call_args_list, [
            mock.call([str(stnt.RUNTIME), "secrets"], check=False),
            mock.call([str(stnt.RUNTIME), "amp-secret-set"], check=False, capture=False),
            mock.call([str(stnt.RUNTIME), "secrets"], check=False),
        ])
        self.assertIn("did not read or store its value", output.getvalue())
        self.assertIn(
            "https://ampcode.com/settings/security#access-token",
            output.getvalue(),
        )

    def test_noninteractive_setup_prints_amp_secret_command_without_opening_a_prompt(self):
        missing = subprocess.CompletedProcess([], 0, "[]", "")
        output = io.StringIO()
        with mock.patch.object(
            stnt, "run", return_value=missing
        ) as invoked, mock.patch.object(
            sys.stdin, "isatty", return_value=False
        ), redirect_stdout(output):
            self.assertFalse(stnt.configure_amp_secret())

        invoked.assert_called_once_with([str(stnt.RUNTIME), "secrets"], check=False)
        self.assertIn(
            "configure explicitly: sbx secret set-custom --host ampcode.com --env AMP_API_KEY",
            output.getvalue(),
        )

    def test_setup_pipes_github_credential_to_docker_and_verifies_registration(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        missing = subprocess.CompletedProcess([], 0, "[]", "")
        changed = subprocess.CompletedProcess([], 0, None, None)
        registered = subprocess.CompletedProcess(
            [], 0, '[{"target":"github","name":"GITHUB_TOKEN"}]', ""
        )
        output = Terminal()
        with mock.patch.object(
            stnt, "run", side_effect=[missing, changed, registered]
        ) as invoked, mock.patch.object(
            sys.stdin, "isatty", return_value=True
        ), mock.patch("builtins.input", return_value="y") as approval, redirect_stdout(output):
            self.assertTrue(stnt.configure_github_secret())

        approval.assert_called_once_with(
            "Register the GitHub credential from gh auth token now? [y/N] "
        )
        self.assertEqual(invoked.call_args_list, [
            mock.call([str(stnt.RUNTIME), "secrets"], check=False),
            mock.call([str(stnt.RUNTIME), "github-secret-set"], check=False, capture=False),
            mock.call([str(stnt.RUNTIME), "secrets"], check=False),
        ])
        self.assertIn("did not read or store its value", output.getvalue())

    def test_setup_reruns_diagnostics_and_prints_current_remaining_actions(self):
        initial = [stnt.result(
            "docker.secret", "blocked", "not registered",
            next_command="sbx secret set-custom --host ampcode.com --env AMP_API_KEY",
        )]
        ready = [stnt.result("docker.secret", "pass", "registered")]
        output = io.StringIO()
        with mock.patch.object(stnt, "ensure_state_layout"), mock.patch.object(
            stnt, "optional_repository", return_value=None
        ), mock.patch.object(
            stnt, "doctor_results", side_effect=[initial, ready]
        ) as doctor, mock.patch.object(
            stnt, "configure_amp_secret", return_value=True
        ), mock.patch.object(
            stnt, "configure_github_secret", return_value=False
        ), mock.patch.object(
            stnt, "configure_stnt_bindings", return_value=False
        ), mock.patch.object(
            stnt, "configure_stnt_ssh", return_value=False
        ), redirect_stdout(output):
            self.assertEqual(stnt.command_setup(), 0)

        self.assertEqual(doctor.call_count, 2)
        self.assertIn("Setup complete; there are no remaining commands", output.getvalue())
        self.assertNotIn("Review each remaining command above", output.getvalue())

    def test_setup_creates_stnt_bindings_only_after_interactive_approval(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        statuses = [
            {"approved": False, "fileExists": False},
            {"approved": False, "fileExists": False},
            {"approved": True, "fileExists": True},
            {"approved": True, "fileExists": True},
        ]
        changed = subprocess.CompletedProcess([], 0, "", "")
        output = Terminal()
        with mock.patch.object(
            stnt, "credential_binding_status", side_effect=statuses
        ), mock.patch.object(
            stnt, "run", return_value=changed
        ) as invoked, mock.patch.object(
            sys.stdin, "isatty", return_value=True
        ), mock.patch("builtins.input", return_value="y") as approval, redirect_stdout(output):
            self.assertTrue(stnt.configure_stnt_bindings())

        approval.assert_called_once_with(
            "Approve proxy-managed Amp and GitHub credentials for Stnt kits on their exact domains? [y/N] "
        )
        invoked.assert_called_once_with([str(stnt.RUNTIME), "stnt-bindings-enable"], check=False)
        self.assertIn("created for ampcode.com and github.com only", output.getvalue())


class EditorTransportTests(unittest.TestCase):
    class ProxyProcess:
        def __init__(self):
            self.returncode = None
            self.started = threading.Event()
            self.terminated = threading.Event()
            self.started.set()

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15
            self.terminated.set()

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9
            self.terminated.set()

    def test_alias_is_exact_workspace_and_sandbox_identity(self):
        selected = workspace_record(Path("/selected"))
        sibling = dict(
            workspace_record(Path("/sibling")),
            workspaceID="W-bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
            sandbox="stnt-sibling",
            sandboxID="sandbox-id-2",
        )
        self.assertRegex(stnt.editor_alias(selected), stnt.EDITOR_ALIAS_RE)
        self.assertNotEqual(stnt.editor_alias(selected), stnt.editor_alias(sibling))
        for invalid in (selected["sandbox"] + ".sbx", "*.stnt.sbx", "bad.stnt.sbx"):
            with self.subTest(alias=invalid), self.assertRaisesRegex(
                stnt.StntError, "invalid Stnt editor alias"
            ):
                stnt.resolve_editor_alias(invalid)

    def test_proxy_is_revoked_and_drained_before_exact_sandbox_stop(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"STNT_STATE_HOME": temporary}
        ):
            stnt.ensure_state_layout()
            current = workspace_record(Path("/fixture"))
            path = stnt.record_session_path(current)
            stnt.atomic_write(path, current)
            authorization = stnt.EditorAuthorization(current, path)
            alias = stnt.editor_alias(current)
            process = self.ProxyProcess()
            proxy_error = []
            sandbox = {
                "name": current["sandbox"], "id": current["sandboxID"],
                "workspaces": [current["repositoryPath"]], "state": "running",
            }

            def proxy():
                try:
                    stnt.command_ssh_proxy(alias)
                except stnt.StntError as error:
                    proxy_error.append(str(error))

            def stop(args, **kwargs):
                self.assertTrue(process.terminated.is_set())
                return subprocess.CompletedProcess(args, 0, "", "")

            with mock.patch.object(stnt, "runtime_find", return_value=sandbox), mock.patch.object(
                stnt.shutil, "which", return_value="/opt/homebrew/bin/sbx"
            ), mock.patch.object(
                stnt.subprocess, "Popen", return_value=process
            ) as spawned, mock.patch.object(stnt, "run_lifecycle", side_effect=stop):
                worker = threading.Thread(target=proxy)
                worker.start()
                self.assertTrue(process.started.wait(timeout=1))
                deadline = time.monotonic() + 1
                while not spawned.called and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(spawned.called)
                stnt.command_pause(Path("/fixture"), (path, current))
                worker.join(timeout=2)

            authorization.close()
            self.assertFalse(worker.is_alive())
            self.assertTrue(any("revoked" in error for error in proxy_error))
            spawned.assert_called_once_with([
                "/opt/homebrew/bin/sbx", "ssh", "proxy", f"{current['sandbox']}.sbx",
            ])
            self.assertEqual(current["status"], "paused")
            durable = stnt.load_state(path)
            self.assertEqual(durable["status"], "paused")
            self.assertNotIn("editorAuthorization", durable)


class LifecycleSafetyTests(unittest.TestCase):
    def test_terminal_menu_uses_arrow_keys_and_enter(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        output = Terminal()
        with mock.patch.object(stnt, "_read_menu_key", side_effect=["down", "down", "up", "enter"]), \
                redirect_stdout(output):
            selected = stnt.terminal_menu("Sessions", ["one", "two", "three"])
        self.assertEqual(selected, 1)
        self.assertIn("↑/↓ select", output.getvalue())

    def test_workspace_exit_inspection_reports_refreshed_upstream_state(self):
        current = workspace_record(Path("/fixture"))
        inspected = subprocess.CompletedProcess([], 0, "2\n1\nfeature\norigin/feature\n3\n4\n", "")
        with mock.patch.object(stnt, "require_sandbox", return_value={"state": "running"}), mock.patch.object(
            stnt, "run", return_value=inspected
        ) as invoked:
            result = stnt.inspect_workspace_exit(current)
        self.assertEqual(result, {
            "tracked": 2, "untracked": 1, "branch": "feature",
            "upstream": "origin/feature", "ahead": 3, "behind": 4,
        })
        script = invoked.call_args.args[0][-3]
        self.assertIn("git fetch --quiet --no-tags --no-write-fetch-head --force", script)
        self.assertIn("git update-ref -d", script)

    def test_workspace_exit_inspection_executes_against_a_real_upstream(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            upstream, clone = root / "upstream.git", root / "clone"
            subprocess.run(["git", "init", "--bare", "-q", "-b", "main", upstream], check=True)
            subprocess.run(["git", "clone", "-q", upstream, clone], check=True)
            subprocess.run(["git", "config", "user.name", "Stnt Test"], cwd=clone, check=True)
            subprocess.run(["git", "config", "user.email", "stnt@example.invalid"], cwd=clone, check=True)
            (clone / "tracked.txt").write_text("baseline\n")
            subprocess.run(["git", "add", "tracked.txt"], cwd=clone, check=True)
            subprocess.run(["git", "commit", "-qm", "baseline"], cwd=clone, check=True)
            subprocess.run(["git", "push", "-qu", "origin", "main"], cwd=clone, check=True)
            (clone / "ahead.txt").write_text("committed locally\n")
            subprocess.run(["git", "add", "ahead.txt"], cwd=clone, check=True)
            subprocess.run(["git", "commit", "-qm", "ahead"], cwd=clone, check=True)
            (clone / "tracked.txt").write_text("changed\n")
            (clone / "untracked.txt").write_text("untracked\n")
            current = workspace_record(clone)

            def execute_in_clone(args, **kwargs):
                self.assertEqual(args[1:3], ["exec", current["sandbox"]])
                return subprocess.run(
                    args[3:], cwd=clone, text=True, capture_output=True, check=False
                )

            with mock.patch.object(
                stnt, "require_sandbox", return_value={"state": "running"}
            ), mock.patch.object(stnt, "run", side_effect=execute_in_clone):
                result = stnt.inspect_workspace_exit(current)
            self.assertEqual(result["tracked"], 1)
            self.assertEqual(result["untracked"], 1)
            self.assertEqual(result["upstream"], "origin/main")
            self.assertEqual(result["ahead"], 1)
            self.assertEqual(result["behind"], 0)
            refs = subprocess.run([
                "git", "for-each-ref", "--format=%(refname)", "refs/stnt/upstream-check",
            ], cwd=clone, check=True, text=True, capture_output=True)
            self.assertEqual(refs.stdout, "")

            subprocess.run([
                "git", "remote", "set-url", "origin", str(root / "missing.git"),
            ], cwd=clone, check=True)
            with mock.patch.object(
                stnt, "require_sandbox", return_value={"state": "running"}
            ), mock.patch.object(stnt, "run", side_effect=execute_in_clone):
                unknown = stnt.inspect_workspace_exit(current)
            self.assertEqual(unknown["upstream"], "origin/main")
            self.assertIsNone(unknown["ahead"])
            self.assertIsNone(unknown["behind"])

    def test_workspace_exit_inspection_restores_a_paused_sandbox(self):
        current = workspace_record(Path("/fixture"))
        inspected = subprocess.CompletedProcess([], 0, "0\n0\nmain\n\nunknown\nunknown\n", "")
        stopped = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(
            stnt, "require_sandbox", return_value={"state": "stopped"}
        ), mock.patch.object(
            stnt, "run", return_value=inspected
        ), mock.patch.object(
            stnt, "run_lifecycle", return_value=stopped
        ) as lifecycle:
            result = stnt.inspect_workspace_exit(current)
        self.assertIsNone(result["upstream"])
        self.assertIsNone(result["ahead"])
        self.assertEqual(lifecycle.call_args.args[0], [
            str(stnt.RUNTIME), "stop", current["sandbox"],
        ])

    def test_workspace_exit_summary_never_calls_stale_tracking_state_unpushed(self):
        summary = stnt.workspace_exit_summary({
            "tracked": 1, "untracked": 2, "branch": "feature",
            "upstream": "origin/feature", "ahead": None, "behind": None,
        })
        self.assertIn("remote status unknown", summary)
        self.assertNotIn("unpushed", summary)

    def test_noninteractive_workspace_exit_pauses_without_inspection(self):
        current = workspace_record(Path("/fixture"))
        with mock.patch.object(sys.stdin, "isatty", return_value=False), mock.patch.object(
            stnt, "inspect_workspace_exit"
        ) as inspect, mock.patch.object(stnt, "pause_after_exit") as pause:
            self.assertEqual(stnt.workspace_exit_decision(current, Path("/state")), "pause")
        inspect.assert_not_called()
        pause.assert_called_once_with(current, Path("/state"))

    def test_workspace_exit_pause_is_safe_default_and_cancel_does_not_mutate(self):
        current = workspace_record(Path("/fixture"))
        inspection = {
            "tracked": 0, "untracked": 0, "branch": "main",
            "upstream": None, "ahead": None, "behind": None,
        }
        with mock.patch.object(sys.stdin, "isatty", return_value=True), mock.patch.object(
            sys.stdout, "isatty", return_value=True
        ), mock.patch.object(stnt, "inspect_workspace_exit", return_value=inspection), mock.patch.object(
            stnt, "terminal_menu", side_effect=[0, 2]
        ), mock.patch.object(stnt, "pause_after_exit") as pause:
            self.assertEqual(stnt.workspace_exit_decision(current, Path("/state")), "pause")
            self.assertEqual(stnt.workspace_exit_decision(current, Path("/state")), "cancel")
        pause.assert_called_once_with(current, Path("/state"))

    def test_workspace_exit_finish_requires_exact_identity_then_discards(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        current = workspace_record(Path("/fixture"))
        inspection = {
            "tracked": 1, "untracked": 2, "branch": "feature",
            "upstream": "origin/feature", "ahead": 3, "behind": 0,
        }
        output = Terminal()
        with mock.patch.object(sys.stdin, "isatty", return_value=True), mock.patch.object(
            stnt, "inspect_workspace_exit", return_value=inspection
        ), mock.patch.object(
            stnt, "terminal_menu", return_value=1
        ), mock.patch("builtins.input", return_value=f"finish {current['workspaceID']}") as confirm, mock.patch.object(
            stnt, "remove_workspace_without_preservation"
        ) as remove, redirect_stdout(output):
            self.assertEqual(stnt.workspace_exit_decision(current, Path("/state")), "finish")
        self.assertIn(current["workspaceID"], confirm.call_args.args[0])
        remove.assert_called_once_with(Path("/state"), current)
        self.assertIn("3 commits not on upstream", output.getvalue())

    def test_rejected_finish_confirmation_returns_to_workspace(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        current = workspace_record(Path("/fixture"))
        inspection = {
            "tracked": 0, "untracked": 0, "branch": "main",
            "upstream": "origin/main", "ahead": 0, "behind": 0,
        }
        with mock.patch.object(sys.stdin, "isatty", return_value=True), mock.patch.object(
            stnt, "inspect_workspace_exit", return_value=inspection
        ), mock.patch.object(
            stnt, "terminal_menu", return_value=1
        ), mock.patch("builtins.input", return_value="finish"), mock.patch.object(
            stnt, "remove_workspace_without_preservation"
        ) as remove, redirect_stdout(Terminal()):
            self.assertEqual(stnt.workspace_exit_decision(current, Path("/state")), "cancel")
        remove.assert_not_called()

    def test_destructive_finish_removes_state_only_after_runtime_removal(self):
        current = workspace_record(Path("/fixture"))
        removed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(stnt, "require_sandbox"), mock.patch.object(
            stnt, "run_lifecycle", return_value=removed
        ) as lifecycle, mock.patch.object(
            stnt, "remove_state"
        ) as remove, redirect_stdout(io.StringIO()):
            stnt.remove_workspace_without_preservation(Path("/state"), current)
        lifecycle.assert_called_once_with(
            [str(stnt.RUNTIME), "remove", current["sandbox"]], check=False
        )
        remove.assert_called_once_with(Path("/state"))

    def test_failed_destructive_finish_retains_state_and_rechecks_identity(self):
        current = workspace_record(Path("/fixture"))
        failed = subprocess.CompletedProcess([], 1, "", "failed")
        found = {
            "name": current["sandbox"], "id": current["sandboxID"],
            "workspaces": [current["repositoryPath"]], "state": "running",
        }
        with mock.patch.object(stnt, "require_sandbox", return_value=found) as required, mock.patch.object(
            stnt, "run_lifecycle", return_value=failed
        ), mock.patch.object(stnt, "runtime_find", return_value=found), mock.patch.object(
            stnt, "remove_state"
        ) as remove:
            with self.assertRaisesRegex(stnt.StntError, "workspace.*state were retained"):
                stnt.remove_workspace_without_preservation(Path("/state"), current)
        self.assertEqual(required.call_count, 2)
        remove.assert_not_called()

    def test_discard_inspection_stops_a_previously_stopped_sandbox(self):
        current = dict(record(Path("/fixture")), sourceSHA="a" * 40)
        inspected = subprocess.CompletedProcess([], 0, "2\n1\n", "")
        stopped = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(stnt, "runtime_find", return_value={
            "name": current["sandbox"], "id": current["sandboxID"],
            "workspaces": [current["repositoryPath"]], "state": "stopped",
        }), mock.patch.object(
            stnt, "run_lifecycle", side_effect=[inspected, stopped]
        ) as lifecycle:
            self.assertEqual(stnt.inspect_discard_changes(current), (2, 1))
        self.assertEqual(lifecycle.call_args_list[1].args[0], [
            str(stnt.RUNTIME), "stop", current["sandbox"],
        ])

    def test_delete_requires_typed_confirmation_when_clone_has_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            path.write_text("state")
            current = record(Path("/fixture"))
            removed = subprocess.CompletedProcess([], 0, "", "")
            output = io.StringIO()
            with mock.patch.object(
                stnt, "inspect_discard_changes", return_value=(1, 2)
            ), mock.patch("builtins.input", return_value="delete") as prompt, mock.patch.object(
                stnt, "run_lifecycle", return_value=removed
            ), redirect_stdout(output):
                stnt.command_delete_session(path, current)
            self.assertFalse(path.exists())
            self.assertIn('Type "delete"', prompt.call_args.args[0])
            self.assertIn("Amp thread", output.getvalue())

    def test_show_selects_a_session_and_dispatches_start(self):
        current = record(Path("/fixture"))
        pair = (Path("/state/session.json"), current)
        threads = {current["threadID"]: {"title": "Useful work", "status": "active"}}
        sandboxes = {current["sandbox"]: {
            "name": current["sandbox"], "id": current["sandboxID"],
            "workspaces": [current["repositoryPath"]], "state": "stopped",
        }}
        with mock.patch.object(sys.stdin, "isatty", return_value=True), mock.patch.object(
            sys.stdout, "isatty", return_value=True
        ), mock.patch.object(stnt, "load_sessions", return_value=[pair]), mock.patch.object(
            stnt, "read_only_inventory", return_value=(threads, True, sandboxes, True, [])
        ), mock.patch.object(stnt, "terminal_menu", side_effect=[0, 0]), mock.patch.object(
            stnt, "lifecycle_gate", return_value=nullcontext()
        ), mock.patch.object(stnt, "session_lock", return_value=nullcontext()), mock.patch.object(
            stnt, "load_state", return_value=current
        ), mock.patch.object(stnt, "start_session") as start:
            stnt.command_show()
        start.assert_called_once_with(current, pair[0])

    def test_prune_removes_only_recorded_and_stnt_prefixed_sandboxes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session_path = root / "session.json"
            stack_path = root / "stack.json"
            session_path.write_text("session")
            stack_path.write_text("stack")
            sessions = [(session_path, {"sandbox": "custom-recorded-sandbox"})]
            stacks = [(stack_path, {"sandbox": "stnt-stack-proof"})]
            inventory = subprocess.CompletedProcess([], 0, json.dumps({"sandboxes": [
                {"name": "custom-recorded-sandbox"},
                {"name": "stnt-stack-proof"},
                {"name": "stnt-orphan"},
                {"name": "unrelated-sandbox"},
            ]}), "")
            removed = subprocess.CompletedProcess([], 0, "provider chatter", "")
            output = io.StringIO()
            with mock.patch.object(stnt, "load_sessions", return_value=sessions), mock.patch.object(
                stnt, "load_stack_states", return_value=stacks
            ), mock.patch.object(stnt, "run", return_value=inventory), mock.patch.object(
                stnt, "run_lifecycle", return_value=removed
            ) as lifecycle, redirect_stdout(output):
                stnt.command_prune(force=True)

            self.assertEqual([call.args[0][-1] for call in lifecycle.call_args_list], [
                "custom-recorded-sandbox", "stnt-orphan", "stnt-stack-proof",
            ])
            self.assertFalse(session_path.exists())
            self.assertFalse(stack_path.exists())
            self.assertNotIn("unrelated-sandbox", output.getvalue())
            self.assertIn("pruned 3 Stnt sandbox(es) and 2 state record(s)", output.getvalue())

    def test_prune_requires_confirmation_and_retains_state_on_remove_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            path.write_text("session")
            sessions = [(path, {"sandbox": "stnt-failing"})]
            inventory = subprocess.CompletedProcess(
                [], 0, '{"sandboxes":[{"name":"stnt-failing"}]}', ""
            )
            failed = subprocess.CompletedProcess([], 1, "", "remove failed")
            with mock.patch.object(stnt, "load_sessions", return_value=sessions), mock.patch.object(
                stnt, "load_stack_states", return_value=[]
            ), mock.patch.object(stnt, "run", return_value=inventory), mock.patch.object(
                stnt, "run_lifecycle", return_value=failed
            ), mock.patch.object(sys.stdin, "isatty", return_value=False), redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(stnt.StntError, "prune --force"):
                    stnt.command_prune()
                with self.assertRaisesRegex(stnt.StntError, "state records were retained"):
                    stnt.command_prune(force=True)
            self.assertTrue(path.exists())

    def test_prune_exclusively_blocks_lifecycle_operations(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"STNT_STATE_HOME": temporary}
        ):
            stnt.ensure_state_layout()
            with stnt.lifecycle_gate(prune=True):
                with self.assertRaisesRegex(stnt.StntError, "prune is active"):
                    with stnt.lifecycle_gate():
                        pass

    def test_interactive_stage_animates_and_finishes_on_one_terminal_line(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        errors = Terminal()
        with mock.patch.object(stnt, "VERBOSE", False), mock.patch.object(
            stnt.ProgressIndicator, "INTERVAL_SECONDS", 0.001
        ), redirect_stderr(errors):
            with stnt.timed("test.stage", "doing useful work"):
                time.sleep(0.004)

        self.assertIn("⠋ stnt: doing useful work", errors.getvalue())
        self.assertIn("✓ stnt: doing useful work", errors.getvalue())
        self.assertIn("\033[2K", errors.getvalue())

    def test_redirected_stage_output_remains_stable(self):
        errors = io.StringIO()
        with redirect_stderr(errors):
            with stnt.timed("test.stage", "doing useful work"):
                pass
        self.assertEqual(errors.getvalue(), "stnt: doing useful work...\n")

    def test_lifecycle_commands_hide_success_and_replay_failure_output(self):
        success = subprocess.CompletedProcess(["provider"], 0, "provider detail\n", "warning\n")
        failure = subprocess.CompletedProcess(["provider"], 1, "failure context\n", "failure detail\n")
        output = io.StringIO()
        errors = io.StringIO()
        with mock.patch.object(stnt, "VERBOSE", False), mock.patch.object(
            stnt, "run", side_effect=[success, failure]
        ) as invoked, redirect_stdout(output), redirect_stderr(errors):
            self.assertIs(stnt.run_lifecycle(["provider"]), success)
            self.assertIs(stnt.run_lifecycle(["provider"], check=False), failure)

        self.assertEqual(output.getvalue(), "failure context\n")
        self.assertEqual(errors.getvalue(), "failure detail\n")
        self.assertEqual(invoked.call_args_list, [
            mock.call(["provider"], check=True, capture=True),
            mock.call(["provider"], check=False, capture=True),
        ])

    def test_captured_commands_cannot_open_hidden_interactive_prompts(self):
        completed = subprocess.CompletedProcess(["provider"], 0, "result\n", "")
        with mock.patch.object(subprocess, "run", return_value=completed) as invoked:
            self.assertIs(stnt.run(["provider"], check=False), completed)

        invoked.assert_called_once_with(
            ["provider"],
            check=False,
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )

    def test_streamed_commands_retain_terminal_input(self):
        completed = subprocess.CompletedProcess(["provider"], 0, None, None)
        with mock.patch.object(subprocess, "run", return_value=completed) as invoked:
            self.assertIs(stnt.run(["provider"], capture=False), completed)

        invoked.assert_called_once_with(
            ["provider"],
            check=True,
            text=True,
            capture_output=False,
            stdin=None,
        )

    def test_verbose_lifecycle_commands_stream_provider_output(self):
        completed = subprocess.CompletedProcess(["provider"], 0, None, None)
        with mock.patch.object(stnt, "VERBOSE", True), mock.patch.object(
            stnt, "run", return_value=completed
        ) as invoked:
            self.assertIs(stnt.run_lifecycle(["provider"]), completed)
        invoked.assert_called_once_with(["provider"], check=True, capture=False)

    def test_profile_creation_passes_only_reviewed_kit_and_resources_to_runtime(self):
        current = dict(
            record(Path("/fixture")),
            status="creating",
            sourceSHA="a" * 40,
            profilePlan={"resources": {"cpus": 6, "memoryGiB": 12}},
            profileKitDigest="b" * 64,
        )
        current.pop("sandboxID")
        found = {
            "name": current["sandbox"], "id": "created-sandbox-id",
            "workspaces": [current["repositoryPath"]],
        }

        def fake_run(args, **kwargs):
            if args[:2] == [str(stnt.RUNTIME), "port"]:
                return subprocess.CompletedProcess(args, 0, '{"host_port":49177}', "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(
            stnt, "verify_profile_record",
            return_value=(Path("/reviewed-kit"), "c" * 64),
        ), mock.patch.object(
            stnt, "runtime_find", side_effect=[None, found, found]
        ), mock.patch.object(
            stnt, "run", side_effect=fake_run
        ) as invoked, mock.patch.object(
            stnt, "provision_profile"
        ) as provision, mock.patch.object(stnt, "atomic_write"):
            stnt.ensure_creation(current, Path("/state"))

        self.assertIn(
            [
                str(stnt.RUNTIME), "create", current["sandbox"], current["repositoryPath"],
                "/reviewed-kit", "6", "12",
            ],
            [call.args[0] for call in invoked.call_args_list],
        )
        self.assertEqual(current["profileKitDigest"], "c" * 64)
        provision.assert_called_once()

    def test_service_less_profile_creation_runs_git_and_provision_gates_without_port_operations(self):
        current = dict(
            record(Path("/fixture")),
            status="creating",
            sourceSHA="a" * 40,
            profilePlan={"resources": {"cpus": 6, "memoryGiB": 12}},
            profileKitDigest="c" * 64,
        )
        current.pop("sandboxPort")
        found = {
            "name": current["sandbox"], "id": current["sandboxID"],
            "workspaces": [current["repositoryPath"]],
        }

        def fake_run(args, **kwargs):
            if args[-4:] == ["git", "symbolic-ref", "--quiet", "HEAD"]:
                return subprocess.CompletedProcess(args, 0, "refs/heads/master\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        operations = []
        with mock.patch.object(
            stnt, "verify_profile_record",
            return_value=(Path("/reviewed-kit"), current["profileKitDigest"]),
        ), mock.patch.object(
            stnt, "runtime_find", return_value=found
        ), mock.patch.object(
            stnt, "run", side_effect=fake_run
        ) as invoked, mock.patch.object(
            stnt, "configure_profile_git_remote", side_effect=lambda record: operations.append("git")
        ) as configure, mock.patch.object(
            stnt, "provision_profile", side_effect=lambda *args: operations.append("provision")
        ) as provision, mock.patch.object(stnt, "atomic_write"):
            stnt.ensure_creation(current, Path("/state"))

        arguments = [call.args[0] for call in invoked.call_args_list]
        self.assertFalse(any("port" in args or "publish" in args for args in arguments))
        self.assertEqual(operations, ["git", "provision"])
        configure.assert_called_once_with(current)
        provision.assert_called_once()
        self.assertEqual(current["status"], "paused")

    def test_profile_provision_failure_stops_and_retains_creation_for_retry(self):
        current = dict(
            record(Path("/fixture")),
            status="creating",
            profilePlan={"resources": {"cpus": 4, "memoryGiB": 8}},
            profileKitDigest="c" * 64,
        )
        found = {
            "name": current["sandbox"], "id": current["sandboxID"],
            "workspaces": [current["repositoryPath"]],
        }

        def fake_run(args, **kwargs):
            if args[:2] == [str(stnt.RUNTIME), "port"]:
                return subprocess.CompletedProcess(args, 0, '{"host_port":49177}', "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(
            stnt, "verify_profile_record",
            return_value=(Path("/reviewed-kit"), current["profileKitDigest"]),
        ), mock.patch.object(
            stnt, "runtime_find", return_value=found
        ), mock.patch.object(
            stnt, "run", side_effect=fake_run
        ) as invoked, mock.patch.object(
            stnt, "provision_profile", side_effect=stnt.StntError("setup failed")
        ), mock.patch.object(stnt, "atomic_write") as write:
            with self.assertRaisesRegex(stnt.StntError, "setup failed"):
                stnt.ensure_creation(current, Path("/state"))

        self.assertIn(
            [str(stnt.RUNTIME), "stop", current["sandbox"]],
            [call.args[0] for call in invoked.call_args_list],
        )
        self.assertEqual(current["status"], "creating")
        write.assert_not_called()

    def test_service_url_creation_publishes_the_exact_loopback_port(self):
        current = dict(
            record(Path("/fixture")),
            status="creating",
            sandboxPort=8010,
            serviceCommand="bin/dev",
            serviceURL="https://app.example.test:8010",
        )
        found = {
            "name": current["sandbox"], "id": current["sandboxID"],
            "workspaces": [current["repositoryPath"]],
        }
        port_calls = 0

        def fake_run(args, **kwargs):
            nonlocal port_calls
            if args[:2] == [str(stnt.RUNTIME), "port"]:
                port_calls += 1
                if port_calls == 1:
                    return subprocess.CompletedProcess(args, 4, "", "")
                return subprocess.CompletedProcess(
                    args, 0, '{"host_port":8010,"sandbox_port":8010}', ""
                )
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(stnt, "runtime_find", return_value=found), mock.patch.object(
            stnt, "run", side_effect=fake_run
        ) as invoked, mock.patch.object(stnt, "atomic_write"):
            stnt.ensure_creation(current, Path("/state"))

        self.assertIn(
            [str(stnt.RUNTIME), "publish", current["sandbox"], "8010:8010"],
            [call.args[0] for call in invoked.call_args_list],
        )

    def test_default_source_drops_divergent_clone_scaffold_and_recovers_idempotently(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host, clone = root / "host", root / "clone"
            host.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=host, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Stnt Test"], cwd=host, check=True)
            subprocess.run(["git", "config", "user.email", "stnt@example.invalid"], cwd=host, check=True)
            (host / "README.md").write_text("main\n")
            subprocess.run(["git", "add", "README.md"], cwd=host, check=True)
            subprocess.run(["git", "commit", "-m", "main"], cwd=host, check=True, capture_output=True)
            main_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=host, check=True, text=True, capture_output=True
            ).stdout.strip()
            subprocess.run(["git", "switch", "-c", "feature"], cwd=host, check=True, capture_output=True)
            (host / "feature.txt").write_text("feature\n")
            subprocess.run(["git", "add", "feature.txt"], cwd=host, check=True)
            subprocess.run(["git", "commit", "-m", "feature"], cwd=host, check=True, capture_output=True)
            feature_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=host, check=True, text=True, capture_output=True
            ).stdout.strip()
            subprocess.run([
                "git", "clone", "--no-local", "--single-branch", "--branch", "feature", str(host), str(clone)
            ], check=True, capture_output=True)
            current = dict(
                record(clone), repositoryPath=str(clone), baseBranch="feature", baseSHA=feature_sha,
                branch="main", sourceBranch="main", sourceSHA=main_sha, status="creating",
            )
            found = {
                "name": current["sandbox"], "id": current["sandboxID"],
                "workspaces": [str(clone)],
            }

            def fake_run(args, **kwargs):
                if args[:3] == [str(stnt.RUNTIME), "exec", current["sandbox"]] and args[-1] == "true":
                    return subprocess.CompletedProcess(args, 0, "", "")
                if args[:3] == [str(stnt.RUNTIME), "exec", current["sandbox"]] and "bash" in args:
                    script = args[-1].replace("/run/sandbox/source", stnt.shell_quote(str(host)))
                    return subprocess.run(
                        ["bash", "--noprofile", "--norc", "-c", script], cwd=clone,
                        text=True, capture_output=kwargs.get("capture", True),
                    )
                if "port" in args:
                    return subprocess.CompletedProcess(args, 0, '{"host_port":49177}', "")
                return subprocess.CompletedProcess(args, 0, "", "")

            with mock.patch.object(stnt, "runtime_find", return_value=found), mock.patch.object(
                stnt, "run", side_effect=fake_run
            ):
                stnt.ensure_creation(current, root / "state.json")
                stnt.ensure_creation(current, root / "state.json")
            self.assertEqual(subprocess.run(
                ["git", "branch", "--show-current"], cwd=clone, check=True,
                text=True, capture_output=True,
            ).stdout.strip(), "main")
            self.assertEqual(subprocess.run(
                ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"],
                cwd=clone, check=True, text=True, capture_output=True,
            ).stdout.splitlines(), ["main"])
            branches = subprocess.run([
                "git", "for-each-ref", "--format=%(objectname)", "refs/heads"
            ], cwd=clone, check=True, text=True, capture_output=True).stdout.splitlines()
            self.assertTrue(all(subprocess.run([
                "git", "merge-base", "--is-ancestor", sha, "HEAD"
            ], cwd=clone).returncode == 0 for sha in branches))

    def test_from_branch_imports_exact_pinned_commit_from_read_only_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host, clone = root / "host", root / "clone"
            host.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=host, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Stnt Test"], cwd=host, check=True)
            subprocess.run(["git", "config", "user.email", "stnt@example.invalid"], cwd=host, check=True)
            (host / "README.md").write_text("base\n")
            subprocess.run(["git", "add", "README.md"], cwd=host, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=host, check=True, capture_output=True)
            subprocess.run(["git", "switch", "-c", "ten;safe"], cwd=host, check=True, capture_output=True)
            (host / "work.txt").write_text("work\n")
            subprocess.run(["git", "add", "work.txt"], cwd=host, check=True)
            subprocess.run(["git", "commit", "-m", "source"], cwd=host, check=True, capture_output=True)
            source_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=host, check=True, text=True, capture_output=True
            ).stdout.strip()
            subprocess.run(["git", "switch", "main"], cwd=host, check=True, capture_output=True)
            subprocess.run(
                ["git", "clone", "--no-local", "--single-branch", "--branch", "main", str(host), str(clone)],
                check=True, capture_output=True,
            )
            self.assertNotEqual(subprocess.run(
                ["git", "cat-file", "-e", f"{source_sha}^{{commit}}"], cwd=clone, capture_output=True
            ).returncode, 0)
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=clone, check=True, text=True, capture_output=True
            ).stdout.strip()
            current = dict(
                record(clone), repositoryPath=str(clone), baseSHA=base_sha,
                baseBranch="main", branch="ten;safe", sourceBranch="ten;safe",
                sourceSHA=source_sha, status="creating",
            )
            state_path = root / "sessions" / "state.json"
            found = {
                "name": current["sandbox"], "id": current["sandboxID"],
                "workspaces": [str(clone)],
            }

            def fake_run(args, **kwargs):
                if args[:3] == [str(stnt.RUNTIME), "exec", current["sandbox"]] and args[-1] == "true":
                    return subprocess.CompletedProcess(args, 0, "", "")
                if args[:3] == [str(stnt.RUNTIME), "exec", current["sandbox"]] and "bash" in args:
                    script = args[-1].replace("/run/sandbox/source", stnt.shell_quote(str(host)))
                    return subprocess.run(
                        ["bash", "--noprofile", "--norc", "-c", script], cwd=clone,
                        text=True, capture_output=kwargs.get("capture", True),
                    )
                if "port" in args:
                    return subprocess.CompletedProcess(args, 0, '{"host_port":49177}', "")
                return subprocess.CompletedProcess(args, 0, "", "")

            with mock.patch.object(stnt, "runtime_find", return_value=found), mock.patch.object(
                stnt, "run", side_effect=fake_run
            ):
                stnt.ensure_creation(current, state_path)
            self.assertEqual(subprocess.run(
                ["git", "branch", "--show-current"], cwd=clone, check=True,
                text=True, capture_output=True,
            ).stdout.strip(), "ten;safe")
            self.assertEqual(subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=clone, check=True,
                text=True, capture_output=True,
            ).stdout.strip(), source_sha)
            self.assertEqual(subprocess.run(
                ["git", "for-each-ref", "--format=%(refname)", "refs/stnt/source-pin"],
                cwd=clone, check=True, text=True, capture_output=True,
            ).stdout, "")

            # A moved source ref must not be hidden by the already-present old
            # object or by the clone already being checked out at the pin.
            moved_sha = subprocess.run(
                ["git", "commit-tree", f"{source_sha}^{{tree}}", "-p", source_sha, "-m", "moved"],
                cwd=host, check=True, text=True, capture_output=True,
            ).stdout.strip()
            subprocess.run(["git", "branch", "-f", "ten;safe", moved_sha], cwd=host, check=True)
            with mock.patch.object(stnt, "runtime_find", return_value=found), mock.patch.object(
                stnt, "run", side_effect=fake_run
            ):
                with self.assertRaisesRegex(stnt.StntError, "branch is ambiguous"):
                    stnt.ensure_creation(current, state_path)
            self.assertEqual(subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=clone, check=True,
                text=True, capture_output=True,
            ).stdout.strip(), source_sha)

            # Restoring the exact source ref makes retry safe even if the failed
            # attempt left a temporary pin before its EXIT trap ran.
            subprocess.run(["git", "branch", "-f", "ten;safe", source_sha], cwd=host, check=True)
            subprocess.run(
                ["git", "update-ref", f"refs/stnt/source-pin/{source_sha}", moved_sha],
                cwd=clone, check=True,
            )
            with mock.patch.object(stnt, "runtime_find", return_value=found), mock.patch.object(
                stnt, "run", side_effect=fake_run
            ):
                stnt.ensure_creation(current, state_path)
            self.assertEqual(subprocess.run(
                ["git", "for-each-ref", "--format=%(refname)", "refs/stnt/source-pin"],
                cwd=clone, check=True, text=True, capture_output=True,
            ).stdout, "")

    def test_creation_keeps_default_on_base_supports_from_and_recovers_legacy(self):
        for initial_branch, source_branch, expected_current, expected_branches in (
            ("main", None, "main", ["main"]),
            ("stnt/legacy-session", None, "stnt/legacy-session", ["main", "stnt/legacy-session"]),
            ("feature/example-work", "feature/example-work", "feature/example-work", ["feature/example-work", "main"]),
        ):
            with self.subTest(initial_branch=initial_branch, source_branch=source_branch), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                clone = root / "clone"
                clone.mkdir()
                subprocess.run(["git", "init", "-b", "main"], cwd=clone, check=True, capture_output=True)
                subprocess.run(["git", "config", "user.name", "Stnt Test"], cwd=clone, check=True)
                subprocess.run(["git", "config", "user.email", "stnt@example.invalid"], cwd=clone, check=True)
                (clone / "README.md").write_text("base\n")
                subprocess.run(["git", "add", "README.md"], cwd=clone, check=True)
                subprocess.run(["git", "commit", "-m", "base"], cwd=clone, check=True, capture_output=True)
                base_sha = subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=clone, check=True, text=True, capture_output=True
                ).stdout.strip()
                source_sha = None
                if source_branch:
                    source_sha = subprocess.run(
                        ["git", "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "source"],
                        cwd=clone, check=True, text=True, capture_output=True,
                    ).stdout.strip()
                    subprocess.run(["git", "branch", source_branch, source_sha], cwd=clone, check=True)
                current = dict(
                    record(clone), baseSHA=base_sha, baseBranch="main", branch=initial_branch,
                    repositoryPath=str(clone), status="creating",
                )
                if source_branch:
                    current.update(sourceBranch=source_branch, sourceSHA=source_sha)
                state_path = root / "sessions" / "state.json"

                def fake_run(args, **kwargs):
                    if args[:3] == [str(stnt.RUNTIME), "exec", current["sandbox"]] and args[-1] == "true":
                        return subprocess.CompletedProcess(args, 0, "", "")
                    if args[:3] == [str(stnt.RUNTIME), "exec", current["sandbox"]] and "bash" in args:
                        script = args[-1].replace("/run/sandbox/source", stnt.shell_quote(str(clone)))
                        return subprocess.run(
                            ["bash", "--noprofile", "--norc", "-c", script], cwd=clone, text=True,
                            capture_output=kwargs.get("capture", True),
                        )
                    if "port" in args:
                        return subprocess.CompletedProcess(args, 0, '{"host_port":49177}', "")
                    return subprocess.CompletedProcess(args, 0, "", "")

                found = {
                    "name": current["sandbox"], "id": current["sandboxID"],
                    "workspaces": [str(clone)],
                }
                with mock.patch.object(stnt, "runtime_find", return_value=found), mock.patch.object(
                    stnt, "run", side_effect=fake_run
                ):
                    stnt.ensure_creation(current, state_path)
                branches = subprocess.run(
                    ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"],
                    cwd=clone, check=True, text=True, capture_output=True,
                ).stdout.splitlines()
                self.assertEqual(branches, expected_branches)
                self.assertEqual(
                    subprocess.run(
                        ["git", "branch", "--show-current"], cwd=clone, check=True,
                        text=True, capture_output=True,
                    ).stdout.strip(),
                    expected_current,
                )

    def test_deterministic_noninteractive_selection_shows_exact_commands(self):
        repo = Path("/fixture")
        second_id = "T-22345678-1234-1234-1234-123456789abc"
        first, second = record(repo), dict(record(repo), threadID=second_id, createdAt="2026-08-12T00:00:00Z")
        with mock.patch.object(
            stnt, "read_only_inventory",
            return_value=({THREAD_ID: {"title": "Same", "status": "active"}, second_id: {"title": "Same", "status": "active"}}, True, {}, True, []),
        ), mock.patch.object(
            stnt.sys.stdin, "isatty", return_value=False
        ):
            with self.assertRaises(stnt.StntError) as raised:
                stnt.select_session(repo, None, [(Path("b"), second), (Path("a"), first)])
        message = str(raised.exception)
        self.assertLess(message.index(THREAD_ID), message.index(second_id))
        self.assertIn(f"stnt --session {THREAD_ID}", message)

    def test_exact_selector_does_not_depend_on_title_lookup(self):
        repo = Path("/fixture")
        current = record(repo)
        with mock.patch.object(stnt, "read_only_inventory") as discovery:
            selected = stnt.select_session(repo, THREAD_ID, [(Path("state"), current)])
        self.assertIs(selected[1], current)
        discovery.assert_not_called()

    def test_workspace_selector_does_not_depend_on_host_thread_lookup(self):
        repo = Path("/fixture")
        current = workspace_record(repo)
        with mock.patch.object(stnt, "read_only_inventory") as discovery:
            selected = stnt.select_session(
                repo, current["workspaceID"], [(Path("state"), current)]
            )
        self.assertIs(selected[1], current)
        discovery.assert_not_called()

    def test_workspace_list_does_not_consult_host_threads_for_lifecycle_guidance(self):
        current = workspace_record(Path("/fixture"))
        threads = {THREAD_ID: {"title": "unrelated", "status": "archived"}}
        sandboxes = {current["sandbox"]: {
            "name": current["sandbox"],
            "id": current["sandboxID"],
            "workspaces": [current["repositoryPath"]],
            "state": "stopped",
        }}
        with mock.patch.object(stnt, "load_sessions", return_value=[(Path("state"), current)]), mock.patch.object(
            stnt, "read_only_inventory", return_value=(threads, True, sandboxes, True, [])
        ), redirect_stdout(io.StringIO()) as output:
            stnt.command_list()
        text = output.getvalue()
        self.assertIn(f"workspace={current['workspaceID']}", text)
        self.assertIn(f"next=stnt --session {current['workspaceID']} start", text)
        self.assertNotIn(" finish", text)

    def test_main_exact_selector_forms_dispatch_only_the_selected_sibling(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"STNT_STATE_HOME": str(Path(temporary) / "state")}
        ):
            stnt.ensure_state_layout()
            repo = Path("/fixture")
            selected = record(repo)
            sibling = dict(
                selected,
                threadID="T-22345678-1234-1234-1234-123456789abc",
                sandbox="stnt-sibling",
                sandboxID="sandbox-id-2",
                branch="stnt/sibling",
                preservationBranch="stnt-preserved/sibling",
            )
            selected_path = stnt.session_path(repo, selected["threadID"])
            sibling_path = stnt.session_path(repo, sibling["threadID"])
            selected_path.write_text(json.dumps(selected))
            sibling_path.write_text(json.dumps(sibling))
            sibling_bytes = sibling_path.read_bytes()
            sessions = [(selected_path, selected), (sibling_path, sibling)]
            cases = (
                (["--session", THREAD_ID], "start"),
                (["start", "--session", THREAD_ID], "start"),
                (["--session", THREAD_ID, "pause"], "pause"),
                (["recover-create", "--session", THREAD_ID], "recover"),
                (["finish", "--session", THREAD_ID], "finish"),
                (["detach", "--session", THREAD_ID], "detach"),
                (["open", "--session", THREAD_ID], "open"),
            )
            for argv, expected in cases:
                with self.subTest(argv=argv), mock.patch.object(
                    stnt, "repository", return_value=repo
                ), mock.patch.object(stnt, "migrate_legacy_session"), mock.patch.object(
                    stnt, "load_sessions", return_value=sessions
                ), mock.patch.object(stnt, "command_start") as start, mock.patch.object(
                    stnt, "command_pause"
                ) as pause, mock.patch.object(stnt, "command_recover_create") as recover, mock.patch.object(
                    stnt, "command_finish"
                ) as finish, mock.patch.object(stnt, "command_detach") as detach, mock.patch.object(
                    stnt, "command_open"
                ) as open_service:
                    self.assertEqual(stnt.main(argv), 0)
                called = {
                    "start": start, "pause": pause, "recover": recover,
                    "finish": finish, "detach": detach, "open": open_service,
                }
                for name, function in called.items():
                    self.assertEqual(function.call_count, 1 if name == expected else 0)
                call = called[expected].call_args
                chosen = call.kwargs["selected"] if expected == "start" else call.args[1]
                self.assertEqual(chosen[1], selected)
                self.assertEqual(sibling_path.read_bytes(), sibling_bytes)

    def test_session_removed_between_selection_and_lock_is_never_resurrected(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"STNT_STATE_HOME": str(Path(temporary) / "state")}
        ):
            stnt.ensure_state_layout()
            repo = Path("/fixture")
            current = record(repo)
            selected_path = stnt.session_path(repo, THREAD_ID)
            selected_path.write_text(json.dumps(current))

            @stnt.contextmanager
            def removing_lock(thread_id):
                self.assertEqual(thread_id, THREAD_ID)
                selected_path.unlink()
                yield

            with mock.patch.object(stnt, "repository", return_value=repo), mock.patch.object(
                stnt, "migrate_legacy_session"
            ), mock.patch.object(
                stnt, "load_sessions", return_value=[(selected_path, current)]
            ), mock.patch.object(stnt, "session_lock", side_effect=removing_lock), mock.patch.object(
                stnt, "command_start"
            ) as start:
                self.assertEqual(stnt.main(["--session", THREAD_ID]), 1)
            start.assert_not_called()
            self.assertFalse(selected_path.exists())

    def test_list_empty_omission_export_checks_without_runtime_mutation(self):
        current = record(Path("/fixture"))
        calls = []
        def fake_run(args, **kwargs):
            calls.append(args)
            if args[0] == str(stnt.RUNTIME):
                return subprocess.CompletedProcess(args, 0, '{"sandboxes":[]}', "")
            if args[0] == str(stnt.THREADS):
                return subprocess.CompletedProcess(args, 0, "[]", "")
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"id": THREAD_ID, "messages": []}), ""
            )
        with mock.patch.object(stnt, "load_sessions", return_value=[(Path("state"), current)]), mock.patch.object(
            stnt, "run", side_effect=fake_run
        ), redirect_stdout(io.StringIO()) as output:
            stnt.command_list()
        self.assertIn("threadState=empty", output.getvalue())
        self.assertIn(f"source={current['baseBranch']}@{current['baseSHA']}", output.getvalue())
        self.assertFalse(any(any(word in arg for word in ("run", "exec", "remove", "stop")) for call in calls for arg in call[1:]))

    def test_list_omitted_archived_empty_thread_is_authoritatively_archived(self):
        responses = [
            subprocess.CompletedProcess([], 3, "missing\n", ""),
            subprocess.CompletedProcess(
                [], 0, json.dumps({"id": THREAD_ID, "archived": True, "messages": []}), ""
            ),
        ]
        with mock.patch.object(stnt, "run", side_effect=responses):
            self.assertEqual(stnt.thread_status(THREAD_ID), "archived")

    def test_failed_or_malformed_amp_lookup_never_becomes_empty_or_mutates_runtime(self):
        current = record(Path("/fixture"))
        for code, payload in ((2, ""), (0, "not-json"), (0, json.dumps([
            {"id": THREAD_ID, "title": "one", "status": "active"},
            {"id": THREAD_ID, "title": "two", "status": "active"},
        ]))):
            with self.subTest(code=code, payload=payload[:8]):
                calls = []
                def fake_run(args, **kwargs):
                    calls.append(args)
                    if args[0] == str(stnt.THREADS):
                        return subprocess.CompletedProcess(args, code, payload, "failed")
                    if args[0] == str(stnt.RUNTIME):
                        return subprocess.CompletedProcess(args, 0, '{"sandboxes":[]}', "")
                    raise AssertionError(f"unexpected export after invalid list: {args}")
                with mock.patch.object(stnt, "load_sessions", return_value=[(Path("state"), current)]), mock.patch.object(
                    stnt, "run", side_effect=fake_run
                ), redirect_stdout(io.StringIO()) as output:
                    stnt.command_list()
                self.assertIn("threadState=ambiguous", output.getvalue())
                self.assertFalse(any(call[0] == "amp" for call in calls))

    def test_malformed_or_wrong_export_is_ambiguous(self):
        current = record(Path("/fixture"))
        malformed_exports = (
            "not-json",
            json.dumps({"id": "T-22345678-1234-1234-1234-123456789abc", "messages": []}),
            json.dumps({"id": THREAD_ID, "messages": [{}]}),
            json.dumps({"id": THREAD_ID, "archived": None, "messages": []}),
        )
        for exported in malformed_exports:
            with self.subTest(exported=exported[:12]):
                def fake_run(args, **kwargs):
                    if args[0] == str(stnt.THREADS):
                        return subprocess.CompletedProcess(args, 0, "[]", "")
                    if args[0] == str(stnt.RUNTIME):
                        return subprocess.CompletedProcess(args, 0, '{"sandboxes":[]}', "")
                    return subprocess.CompletedProcess(args, 0, exported, "")
                with mock.patch.object(stnt, "load_sessions", return_value=[(Path("state"), current)]), mock.patch.object(
                    stnt, "run", side_effect=fake_run
                ), redirect_stdout(io.StringIO()) as output:
                    stnt.command_list()
                self.assertIn("threadState=ambiguous", output.getvalue())

    def test_sandbox_provider_ambiguities_are_reported_without_mutation(self):
        current = record(Path("/fixture"))
        thread_json = json.dumps([{"id": THREAD_ID, "title": "Title", "status": "active"}])
        inventories = {
            "malformed": "not-json",
            "duplicate-name": json.dumps({"sandboxes": [
                {"name": current["sandbox"], "id": "one", "workspaces": [current["repositoryPath"]]},
                {"name": current["sandbox"], "id": "two", "workspaces": [current["repositoryPath"]]},
            ]}),
            "duplicate-id": json.dumps({"sandboxes": [
                {"name": current["sandbox"], "id": current["sandboxID"], "workspaces": [current["repositoryPath"]]},
                {"name": "other", "id": current["sandboxID"], "workspaces": ["/other"]},
            ]}),
            "missing-workspaces": json.dumps({"sandboxes": [
                {"name": current["sandbox"], "id": current["sandboxID"]},
            ]}),
            "missing": '{"sandboxes":[]}',
            "changed-id": json.dumps({"sandboxes": [
                {"name": current["sandbox"], "id": "replacement", "state": "running", "workspaces": [current["repositoryPath"]]},
            ]}),
            "wrong-workspace": json.dumps({"sandboxes": [
                {"name": current["sandbox"], "id": current["sandboxID"], "state": "running", "workspaces": ["/other"]},
            ]}),
        }
        for label, inventory in inventories.items():
            with self.subTest(label=label):
                calls = []
                def fake_run(args, **kwargs):
                    calls.append(args)
                    if args[0] == str(stnt.THREADS):
                        return subprocess.CompletedProcess(args, 0, thread_json, "")
                    if args[0] == str(stnt.RUNTIME):
                        return subprocess.CompletedProcess(args, 0, inventory, "")
                    raise AssertionError(args)
                with mock.patch.object(stnt, "load_sessions", return_value=[(Path("state"), current)]), mock.patch.object(
                    stnt, "run", side_effect=fake_run
                ), redirect_stdout(io.StringIO()) as output:
                    stnt.command_list()
                text = output.getvalue()
                self.assertTrue(any(status in text for status in (
                    "lookup-ambiguous", "missing-stale", "identity-mismatch", "workspace-mismatch"
                )))
                self.assertIn("next=stnt list", text)
                self.assertEqual([call[1] for call in calls if call[0] == str(stnt.RUNTIME)], ["list"])

    def test_main_list_works_outside_git_without_initializing_or_locking(self):
        with mock.patch.object(stnt, "command_list") as listed, mock.patch.object(
            stnt, "ensure_state_layout"
        ) as layout, mock.patch.object(stnt, "repository") as repository:
            self.assertEqual(stnt.main(["list"]), 0)
        listed.assert_called_once()
        layout.assert_not_called()
        repository.assert_not_called()

    def test_finish_removes_only_selected_record(self):
        repo = Path("/fixture")
        current = record(repo)
        selected, sibling = Path("/selected"), Path("/sibling")
        with mock.patch.object(stnt, "validate_record_repository"), mock.patch.object(
            stnt, "thread_status", return_value="archived"
        ), mock.patch.object(stnt, "runtime_find", return_value={"name": current["sandbox"], "id": current["sandboxID"], "workspaces": [str(repo)]}), mock.patch.object(
            stnt, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
        ) as invoked, mock.patch.object(stnt, "remove_state") as remove:
            stnt.command_finish(repo, (selected, current))
        remove.assert_called_once_with(selected)
        self.assertNotEqual(remove.call_args.args[0], sibling)
        finish_call = next(call.args[0] for call in invoked.call_args_list if call.args[0][0] == str(stnt.FINISH))
        self.assertEqual(finish_call[-2:], ["--current-branch", current["preservationBranch"]])

    def test_finish_active_thread_names_archive_only_recovery(self):
        repo = Path("/fixture")
        current = record(repo)
        with mock.patch.object(stnt, "validate_record_repository"), mock.patch.object(
            stnt, "thread_status", return_value="active"
        ), mock.patch.object(stnt, "runtime_find") as runtime:
            with self.assertRaises(stnt.StntError) as raised:
                stnt.command_finish(repo, (Path("/state"), current))

        message = str(raised.exception)
        self.assertIn(f"amp threads archive {THREAD_ID}", message)
        self.assertIn(f"stnt --session {THREAD_ID} finish", message)
        self.assertNotIn("pause", message)
        runtime.assert_not_called()

    def test_finish_retains_existing_archived_creation_recovery_path(self):
        repo = Path("/fixture")
        current = dict(record(repo), status="creating")
        with mock.patch.object(stnt, "validate_record_repository"), mock.patch.object(
            stnt, "thread_status", return_value="archived"
        ), mock.patch.object(stnt, "runtime_find", return_value={
            "name": current["sandbox"], "id": current["sandboxID"], "workspaces": [str(repo)]
        }), mock.patch.object(
            stnt, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
        ) as invoked, mock.patch.object(stnt, "remove_state") as remove:
            stnt.command_finish(repo, (Path("/selected"), current))

        self.assertTrue(any(
            call.args[0][:2] == [str(stnt.FINISH), "finish"] for call in invoked.call_args_list
        ))
        remove.assert_called_once_with(Path("/selected"))

    def test_detach_preserves_and_removes_only_selected_active_session(self):
        repo = Path("/fixture")
        current = record(repo)
        selected, sibling = Path("/selected"), Path("/sibling")
        with mock.patch.object(stnt, "validate_record_repository"), mock.patch.object(
            stnt, "thread_status"
        ) as status, mock.patch.object(stnt, "runtime_find", return_value={
            "name": current["sandbox"], "id": current["sandboxID"], "workspaces": [str(repo)]
        }), mock.patch.object(
            stnt, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
        ) as invoked, mock.patch.object(stnt, "remove_state") as remove:
            stnt.command_detach(repo, (selected, current))

        status.assert_not_called()
        remove.assert_called_once_with(selected)
        self.assertNotEqual(remove.call_args.args[0], sibling)
        detach_call = next(
            call.args[0] for call in invoked.call_args_list if call.args[0][0] == str(stnt.FINISH)
        )
        self.assertEqual(detach_call, [
            str(stnt.FINISH), "detach", current["sandbox"], str(repo), THREAD_ID,
            "--current-branch", current["preservationBranch"],
        ])

    def test_workspace_detach_uses_workspace_identity_without_a_host_thread(self):
        repo = Path("/fixture")
        current = workspace_record(repo)
        with mock.patch.object(stnt, "validate_record_repository"), mock.patch.object(
            stnt, "runtime_find", return_value={
                "name": current["sandbox"], "id": current["sandboxID"], "workspaces": [str(repo)]
            }
        ), mock.patch.object(
            stnt, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
        ) as invoked, mock.patch.object(stnt, "remove_state"):
            stnt.command_detach(repo, (Path("/selected"), current))

        detach_call = next(
            call.args[0] for call in invoked.call_args_list if call.args[0][0] == str(stnt.FINISH)
        )
        self.assertEqual(detach_call[1:5], [
            "detach", current["sandbox"], str(repo), current["workspaceID"],
        ])

    def test_failed_detach_retains_record_and_names_exact_retry(self):
        repo = Path("/fixture")
        current = record(repo)
        selected = Path("/selected")
        with mock.patch.object(stnt, "validate_record_repository"), mock.patch.object(
            stnt, "runtime_find", return_value={
                "name": current["sandbox"], "id": current["sandboxID"], "workspaces": [str(repo)]
            }
        ), mock.patch.object(
            stnt, "run", return_value=subprocess.CompletedProcess([], 1, "", "failed")
        ), mock.patch.object(stnt, "remove_state") as remove:
            with self.assertRaises(stnt.StntError) as raised:
                stnt.command_detach(repo, (selected, current))

        self.assertIn(f"stnt --session {THREAD_ID} detach", str(raised.exception))
        remove.assert_not_called()

    def test_detach_rejects_incomplete_creation_and_changed_sandbox_id(self):
        repo = Path("/fixture")
        creating = dict(record(repo), status="creating")
        with mock.patch.object(stnt, "validate_record_repository"), mock.patch.object(
            stnt, "runtime_find"
        ) as runtime:
            with self.assertRaisesRegex(stnt.StntError, "recover-create"):
                stnt.command_detach(repo, (Path("/creating"), creating))
        runtime.assert_not_called()

        current = record(repo)
        with mock.patch.object(stnt, "validate_record_repository"), mock.patch.object(
            stnt, "runtime_find", return_value={
                "name": current["sandbox"], "id": "replacement", "workspaces": [str(repo)]
            }
        ), mock.patch.object(stnt, "run") as invoked:
            with self.assertRaisesRegex(stnt.StntError, "identity changed"):
                stnt.command_detach(repo, (Path("/selected"), current))
        invoked.assert_not_called()

    def test_selected_start_attaches_only_selected_identity(self):
        repo = Path("/fixture")
        selected = record(repo)
        sibling = dict(
            selected,
            threadID="T-22345678-1234-1234-1234-123456789abc",
            sandbox="stnt-sibling",
            sandboxID="sandbox-id-2",
            branch="stnt/sibling",
        )
        def fake_run(args, **kwargs):
            if args[-4:] == ["git", "symbolic-ref", "--quiet", "HEAD"]:
                return subprocess.CompletedProcess(args, 0, "refs/heads/feature/example-work\n", "")
            if "port" in args:
                return subprocess.CompletedProcess(args, 0, '{"host_port":49177}', "")
            return subprocess.CompletedProcess(args, 0, "", "")
        with mock.patch.object(stnt, "validate_record_repository"), mock.patch.object(
            stnt, "thread_status", return_value="active"
        ), mock.patch.object(stnt, "runtime_find", return_value={
            "name": selected["sandbox"], "id": selected["sandboxID"], "workspaces": [str(repo)]
        }), mock.patch.object(stnt, "run", side_effect=fake_run) as invoked, mock.patch.object(
            stnt, "pause_after_exit"
        ), mock.patch.object(stnt, "sandbox_hold") as hold, mock.patch.object(
            stnt, "atomic_write"
        ):
            stnt.start_session(selected, Path("/selected"))
        flattened = "\n".join(" ".join(map(str, call.args[0])) for call in invoked.call_args_list)
        self.assertIn(selected["threadID"], flattened)
        self.assertIn(selected["sandbox"], flattened)
        self.assertNotIn(sibling["threadID"], flattened)
        self.assertNotIn(sibling["sandbox"], flattened)
        invoked.assert_any_call([
            str(stnt.RUNTIME), "runner-start", selected["sandbox"], stnt.runner_id(selected),
            selected["threadID"], str(repo),
        ])
        invoked.assert_any_call(
            [str(stnt.THREADS), "continue", selected["threadID"]],
            check=False, capture=False,
        )
        hold.assert_called_once_with(selected["sandbox"])

    def test_workspace_start_attaches_guest_tui_without_host_thread_or_runner(self):
        repo = Path("/fixture")
        selected = workspace_record(repo)

        def fake_run(args, **kwargs):
            if args[-4:] == ["git", "symbolic-ref", "--quiet", "HEAD"]:
                return subprocess.CompletedProcess(args, 0, "refs/heads/master\n", "")
            if "port" in args:
                return subprocess.CompletedProcess(args, 0, '{"host_port":49177}', "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(stnt, "validate_record_repository"), mock.patch.object(
            stnt, "thread_status"
        ) as status, mock.patch.object(stnt, "runtime_find", return_value={
            "name": selected["sandbox"], "id": selected["sandboxID"], "workspaces": [str(repo)]
        }), mock.patch.object(stnt, "run", side_effect=fake_run) as invoked, mock.patch.object(
            stnt, "workspace_exit_decision", side_effect=["cancel", "pause"]
        ) as decision, mock.patch.object(stnt, "sandbox_hold") as hold, mock.patch.object(
            stnt, "atomic_write"
        ), redirect_stdout(io.StringIO()):
            stnt.start_session(selected, Path("/selected"))

        status.assert_not_called()
        hold.assert_not_called()
        self.assertEqual(decision.call_count, 2)
        self.assertEqual(invoked.call_args_list.count(mock.call(
            [str(stnt.RUNTIME), "tui-start", selected["sandbox"], str(repo)],
            check=False,
            capture=False,
        )), 2)
        flattened = "\n".join(" ".join(map(str, call.args[0])) for call in invoked.call_args_list)
        self.assertNotIn("runner-start", flattened)
        self.assertNotIn(str(stnt.THREADS), flattened)

    def test_service_less_profile_start_and_resume_use_nix_tui_without_service_or_url(self):
        repo = Path("/fixture")
        selected = dict(
            workspace_record(repo),
            profilePlan={},
            profilePlanDigest="c" * 64,
            profileApprovalDigest="d" * 64,
            profileKitDigest="b" * 64,
            sourceSHA="a" * 40,
        )
        selected.pop("sandboxPort")
        def fake_run(args, **kwargs):
            if args[-4:] == ["git", "symbolic-ref", "--quiet", "HEAD"]:
                return subprocess.CompletedProcess(args, 0, "refs/heads/main\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(stnt, "validate_record_repository"), mock.patch.object(
            stnt, "verify_profile_record", return_value=(
                Path("/kit"), selected["profileKitDigest"]
            )
        ), mock.patch.object(stnt, "runtime_find", return_value={
            "name": selected["sandbox"], "id": selected["sandboxID"], "workspaces": [str(repo)]
        }), mock.patch.object(
            stnt, "run", side_effect=fake_run
        ) as invoked, mock.patch.object(
            stnt, "restart_service"
        ) as restart, mock.patch.object(
            stnt, "wait_for_health"
        ) as health, mock.patch.object(
            stnt, "workspace_exit_decision", return_value="pause"
        ), mock.patch.object(
            stnt, "vscode_editor_command", return_value="/Applications/Code"
        ), mock.patch.object(
            stnt, "open_vscode_editor"
        ) as editor, mock.patch.object(stnt, "atomic_write"), redirect_stdout(output), redirect_stderr(errors):
            stnt.start_session(selected, Path("/selected"))
            stnt.start_session(selected, Path("/selected"))

        tui_call = mock.call(
            [str(stnt.RUNTIME), "tui-start", selected["sandbox"], str(repo), "nix"],
            check=False,
            capture=False,
        )
        self.assertEqual(invoked.call_args_list.count(tui_call), 2)
        self.assertFalse(any("port" in call.args[0] for call in invoked.call_args_list))
        restart.assert_not_called()
        health.assert_not_called()
        self.assertEqual(editor.call_args_list, [
            mock.call(selected, "/Applications/Code"),
            mock.call(selected, "/Applications/Code"),
        ])
        self.assertNotIn("url=", output.getvalue())
        self.assertNotIn("service", errors.getvalue())

    def test_profile_start_launches_runner_in_nix_environment(self):
        repo = Path("/fixture")
        selected = dict(
            record(repo),
            profilePlan={},
            profilePlanDigest="c" * 64,
            profileApprovalDigest="d" * 64,
            profileKitDigest="b" * 64,
            sourceSHA="a" * 40,
        )
        def fake_run(args, **kwargs):
            if args[-4:] == ["git", "symbolic-ref", "--quiet", "HEAD"]:
                return subprocess.CompletedProcess(args, 0, "refs/heads/feature/example-work\n", "")
            if "port" in args:
                return subprocess.CompletedProcess(args, 0, '{"host_port":49177}', "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(stnt, "validate_record_repository"), mock.patch.object(
            stnt, "thread_status", return_value="active"
        ), mock.patch.object(stnt, "verify_profile_record", return_value=(
            Path("/kit"), selected["profileKitDigest"]
        )), mock.patch.object(stnt, "runtime_find", return_value={
            "name": selected["sandbox"], "id": selected["sandboxID"], "workspaces": [str(repo)]
        }), mock.patch.object(stnt, "run", side_effect=fake_run) as invoked, mock.patch.object(
            stnt, "pause_after_exit"
        ), mock.patch.object(stnt, "sandbox_hold"), mock.patch.object(stnt, "atomic_write"):
            stnt.start_session(selected, Path("/selected"))

        invoked.assert_any_call([
            str(stnt.RUNTIME), "runner-start", selected["sandbox"], stnt.runner_id(selected),
            selected["threadID"], str(repo), "nix",
        ])

    def test_runtime_hold_keeps_a_docker_exec_session_attached(self):
        adapter = stnt.RUNTIME.read_text()
        self.assertIn("exec sbx exec \"$sandbox\" -- sh -c", adapter)
        self.assertIn("exec sleep infinity", adapter)

    def test_editor_opens_selected_private_clone_over_sandbox_ssh(self):
        repo = Path("/fixture project")
        current = record(repo)

        def fake_run(args, **kwargs):
            if args[:2] == ["/Applications/Code", "--new-window"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            raise AssertionError(args)

        with mock.patch.object(
            stnt, "prepare_vscode_server"
        ), mock.patch.object(
            stnt, "run", side_effect=fake_run
        ) as invoked:
            stnt.open_vscode_editor(current, "/Applications/Code")

        invoked.assert_any_call([
            "/Applications/Code", "--new-window", "--folder-uri",
            f"vscode-remote://ssh-remote+{stnt.editor_alias(current)}/fixture%20project",
        ], check=False, capture=False)

    def test_explicit_editor_uses_full_managed_session_lifetime(self):
        repo = Path("/fixture")
        current = workspace_record(repo)
        with mock.patch.object(
            stnt, "vscode_editor_command", return_value="/Applications/Code"
        ), mock.patch.object(stnt, "start_session") as start:
            stnt.command_editor(repo, (Path("/state"), current))
        start.assert_called_once_with(current, Path("/state"))

    def test_automatic_editor_prerequisites_skip_without_mutating_setup(self):
        extension = subprocess.CompletedProcess([], 0, "other.extension\n", "")
        with mock.patch.object(stnt, "vscode_command", return_value=None), mock.patch.object(
            stnt, "run"
        ) as invoked:
            self.assertIsNone(stnt.vscode_editor_command(required=False))
        invoked.assert_not_called()

        with mock.patch.object(
            stnt, "vscode_command", return_value="/Applications/Code"
        ), mock.patch.object(stnt, "run", return_value=extension), mock.patch.object(
            stnt, "vscode_remote_ssh_offline_ready"
        ) as settings:
            self.assertIsNone(stnt.vscode_editor_command(required=False))
        settings.assert_not_called()

        remote_ssh = subprocess.CompletedProcess([], 0, "ms-vscode-remote.remote-ssh\n", "")
        with mock.patch.object(
            stnt, "vscode_command", return_value="/Applications/Code"
        ), mock.patch.object(stnt, "run", return_value=remote_ssh), mock.patch.object(
            stnt, "vscode_remote_ssh_offline_ready", return_value=False
        ):
            self.assertIsNone(stnt.vscode_editor_command(required=False))

    def test_automatic_editor_failure_continues_with_explicit_retry(self):
        current = workspace_record(Path("/fixture"))
        errors = io.StringIO()
        with mock.patch.object(
            stnt, "vscode_editor_command", return_value="/Applications/Code"
        ), mock.patch.object(
            stnt, "open_vscode_editor", side_effect=stnt.StntError("launch failed")
        ), redirect_stderr(errors):
            stnt.automatic_editor_handoff(current)

        self.assertIn("continuing terminal-only", errors.getvalue())
        self.assertIn(f"stnt --session {current['workspaceID']} editor", errors.getvalue())

    def test_editor_requires_explicit_remote_ssh_installation(self):
        repo = Path("/fixture")
        current = record(repo)
        with mock.patch.object(stnt, "runtime_find", return_value={
            "name": current["sandbox"], "id": current["sandboxID"], "workspaces": [str(repo)]
        }), mock.patch.object(stnt, "vscode_command", return_value="/Applications/Code"), mock.patch.object(
            stnt, "run", return_value=subprocess.CompletedProcess([], 0, "other.extension\n", "")
        ) as invoked:
            with self.assertRaisesRegex(stnt.StntError, "--install-extension"):
                stnt.command_editor(repo, (Path("/state"), current))

        self.assertEqual(invoked.call_count, 1)

    def test_editor_server_cache_is_build_specific_and_archive_is_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            commit = "d" * 40
            archive = cache / "editor/vscode" / commit / "vscode-server-linux-arm64.tar.gz"
            archive.parent.mkdir(parents=True)
            source = root / "vscode-server-linux-arm64/bin/code-server"
            source.parent.mkdir(parents=True)
            source.write_text("#!/bin/sh\n")
            source.chmod(0o755)
            with tarfile.open(archive, "w:gz") as bundle:
                bundle.add(source.parents[1], arcname="vscode-server-linux-arm64")
            completed = subprocess.CompletedProcess([], 0, f"1.132.0\n{commit}\narm64\n", "")
            with mock.patch.dict(os.environ, {"STNT_CACHE_HOME": str(cache)}), mock.patch.object(
                stnt, "run", return_value=completed
            ):
                observed_commit, observed_archive = stnt.cached_vscode_server("/Applications/Code")
        self.assertEqual(observed_commit, commit)
        self.assertEqual(observed_archive, archive)

    def test_editor_server_prewarm_reuses_an_installed_matching_build(self):
        current = record(Path("/fixture"))
        commit = "d" * 40
        responses = [
            subprocess.CompletedProcess([], 0, f"1.132.0\n{commit}\narm64\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        with mock.patch.object(stnt, "run", side_effect=responses) as invoked, mock.patch.object(
            stnt, "cached_vscode_server"
        ) as cache:
            stnt.prepare_vscode_server(current, "/Applications/Code")
        cache.assert_not_called()
        self.assertEqual(invoked.call_args_list[-1].args[0], [
            str(stnt.RUNTIME), "editor-server-status", current["sandbox"], commit,
        ])

    def test_editor_requires_host_side_remote_server_download(self):
        repo = Path("/fixture")
        current = record(repo)
        with mock.patch.object(stnt, "runtime_find", return_value={
            "name": current["sandbox"], "id": current["sandboxID"], "workspaces": [str(repo)]
        }), mock.patch.object(stnt, "vscode_command", return_value="/Applications/Code"), mock.patch.object(
            stnt, "vscode_remote_ssh_offline_ready", return_value=False
        ), mock.patch.object(
            stnt, "run", return_value=subprocess.CompletedProcess(
                [], 0, "ms-vscode-remote.remote-ssh\n", ""
            )
        ) as invoked:
            with self.assertRaisesRegex(stnt.StntError, "localServerDownload"):
                stnt.command_editor(repo, (Path("/state"), current))

        self.assertEqual(invoked.call_count, 1)

    def test_resume_rejects_detached_head_without_attaching_amp(self):
        repo = Path("/fixture")
        current = record(repo)

        def fake_run(args, **kwargs):
            if args[-4:] == ["git", "symbolic-ref", "--quiet", "HEAD"]:
                return subprocess.CompletedProcess(args, 1, "", "detached")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(stnt, "validate_record_repository"), mock.patch.object(
            stnt, "thread_status", return_value="active"
        ), mock.patch.object(stnt, "runtime_find", return_value={
            "name": current["sandbox"], "id": current["sandboxID"], "workspaces": [str(repo)]
        }), mock.patch.object(stnt, "run", side_effect=fake_run) as invoked, mock.patch.object(
            stnt, "atomic_write"
        ):
            with self.assertRaisesRegex(stnt.StntError, "detached"):
                stnt.start_session(current, Path("/state"))
        flattened = [call.args[0] for call in invoked.call_args_list]
        self.assertFalse(any("threads" in args for args in flattened))
        self.assertTrue(any("stop" in args for args in flattened))
        symbolic_ref_call = next(
            call for call in invoked.call_args_list
            if call.args[0][-4:] == ["git", "symbolic-ref", "--quiet", "HEAD"]
        )
        self.assertFalse(symbolic_ref_call.kwargs["check"])

    def test_selected_service_command_and_health_path_do_not_use_sibling(self):
        selected = dict(record(Path("/fixture")), serviceCommand="selected-command", healthPath="/selected")
        sibling = dict(selected, serviceCommand="sibling-command", healthPath="/sibling")
        with mock.patch.object(
            stnt, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
        ) as invoked, mock.patch.object(stnt, "wait_for_health") as health:
            stnt.restart_service(selected, 49177)
        command = invoked.call_args_list[0].args[0]
        self.assertIn("selected-command", command[-1])
        self.assertNotIn(sibling["serviceCommand"], command[-1])
        health.assert_called_once_with(49177, "/selected")

    def test_open_uses_only_the_selected_healthy_reviewed_service_url(self):
        repo = Path("/fixture")
        selected = dict(
            workspace_record(repo), sandboxPort=8010, serviceArgv=["bin/dev"],
            serviceURL="https://app.local.example:8010", healthPath="/ready", status="active",
        )
        sibling_url = "https://sibling.local.example:8010"
        responses = [
            subprocess.CompletedProcess([], 0, json.dumps({
                "host_port": 8010, "sandbox_port": 8010,
            }), ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        with mock.patch.object(stnt, "require_loopback_service_host"), mock.patch.object(
            stnt, "require_sandbox", return_value={"state": "running"}
        ), mock.patch.object(stnt, "run", side_effect=responses) as invoked, mock.patch.object(
            stnt, "wait_for_health"
        ) as health, mock.patch.object(stnt, "open_browser") as browser:
            stnt.command_open(repo, (Path("/selected"), selected))

        self.assertEqual(invoked.call_args_list, [
            mock.call([
                str(stnt.RUNTIME), "port", selected["sandbox"], "8010",
            ], check=False),
            mock.call([str(stnt.RUNTIME), "service-status", selected["sandbox"]], check=False),
        ])
        health.assert_called_once_with(
            8010, "/ready", scheme="https", hostname="app.local.example",
        )
        browser.assert_called_once_with(selected["serviceURL"])
        self.assertNotIn(sibling_url, str(browser.call_args_list))

    def test_browser_boundary_invokes_only_macos_open_with_a_loopback_origin(self):
        service_url = "http://127.0.0.1:8010"
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(stnt, "run", return_value=completed) as invoked:
            stnt.open_browser(service_url)
        invoked.assert_called_once_with(
            ["/usr/bin/open", service_url], check=False, capture=False,
        )

    def test_open_rejects_service_less_and_paused_workspaces_without_host_action(self):
        repo = Path("/fixture")
        service_less = workspace_record(repo)
        service_less.pop("sandboxPort")
        with mock.patch.object(stnt, "require_sandbox") as sandbox, mock.patch.object(
            stnt, "open_browser"
        ) as browser:
            with self.assertRaisesRegex(stnt.StntError, "service-less"):
                stnt.command_open(repo, (Path("/service-less"), service_less))
        sandbox.assert_not_called()
        browser.assert_not_called()

        unreviewed_url = dict(workspace_record(repo), serviceCommand="bin/dev")
        with mock.patch.object(stnt, "require_sandbox") as sandbox, mock.patch.object(
            stnt, "open_browser"
        ) as browser:
            with self.assertRaisesRegex(stnt.StntError, "no reviewed fixed service URL"):
                stnt.command_open(repo, (Path("/unreviewed"), unreviewed_url))
        sandbox.assert_not_called()
        browser.assert_not_called()

        paused = dict(
            workspace_record(repo), sandboxPort=8010, serviceArgv=["bin/dev"],
            serviceURL="https://app.local.example:8010",
        )
        with mock.patch.object(stnt, "require_loopback_service_host"), mock.patch.object(
            stnt, "require_sandbox", return_value={"state": "running"}
        ) as sandbox, mock.patch.object(stnt, "run") as invoked, mock.patch.object(
            stnt, "open_browser"
        ) as browser:
            with self.assertRaisesRegex(stnt.StntError, "paused.*stnt --session"):
                stnt.command_open(repo, (Path("/paused"), paused))
        sandbox.assert_not_called()
        invoked.assert_not_called()
        browser.assert_not_called()

        ambiguous = dict(paused, status="ambiguous")
        with mock.patch.object(stnt, "require_sandbox") as sandbox, mock.patch.object(
            stnt, "open_browser"
        ) as browser:
            with self.assertRaisesRegex(stnt.StntError, "ambiguous.*stnt list"):
                stnt.command_open(repo, (Path("/ambiguous"), ambiguous))
        sandbox.assert_not_called()
        browser.assert_not_called()

    def test_open_rejects_invalid_stale_and_unverified_service_urls(self):
        repo = Path("/fixture")
        base = dict(
            workspace_record(repo), sandboxPort=8010, serviceArgv=["bin/dev"],
            serviceURL="https://app.local.example:8010", status="active",
        )
        for service_url, expected in (
            ("https://app.local.example:8010/path", "HTTP or HTTPS origin"),
            ("http://192.0.2.1:8010", "loopback"),
        ):
            current = dict(base, serviceURL=service_url)
            with self.subTest(service_url=service_url), mock.patch.object(
                stnt, "require_sandbox"
            ) as sandbox, mock.patch.object(stnt, "open_browser") as browser:
                with self.assertRaisesRegex(stnt.StntError, expected):
                    stnt.command_open(repo, (Path("/state"), current))
            sandbox.assert_not_called()
            browser.assert_not_called()

        stale = subprocess.CompletedProcess([], 0, json.dumps({
            "host_port": 8011, "sandbox_port": 8010,
        }), "")
        with mock.patch.object(stnt, "require_loopback_service_host"), mock.patch.object(
            stnt, "require_sandbox", return_value={"state": "running"}
        ), mock.patch.object(stnt, "run", return_value=stale), mock.patch.object(
            stnt, "open_browser"
        ) as browser:
            with self.assertRaisesRegex(stnt.StntError, "stale or unverified"):
                stnt.command_open(repo, (Path("/state"), base))
        browser.assert_not_called()

        responses = [
            subprocess.CompletedProcess([], 0, json.dumps({
                "host_port": 8010, "sandbox_port": 8010,
            }), ""),
            subprocess.CompletedProcess([], 1, "", "missing identity"),
        ]
        with mock.patch.object(stnt, "require_loopback_service_host"), mock.patch.object(
            stnt, "require_sandbox", return_value={"state": "running"}
        ), mock.patch.object(stnt, "run", side_effect=responses), mock.patch.object(
            stnt, "wait_for_health"
        ), mock.patch.object(stnt, "open_browser"
        ) as browser:
            with self.assertRaisesRegex(stnt.StntError, "process identity is unverified"):
                stnt.command_open(repo, (Path("/state"), base))
        browser.assert_not_called()

        healthy_responses = [
            subprocess.CompletedProcess([], 0, json.dumps({
                "host_port": 8010, "sandbox_port": 8010,
            }), ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        with mock.patch.object(stnt, "require_loopback_service_host"), mock.patch.object(
            stnt, "require_sandbox", return_value={"state": "running"}
        ), mock.patch.object(stnt, "run", side_effect=healthy_responses), mock.patch.object(
            stnt, "wait_for_health", side_effect=stnt.StntError("health timeout")
        ), mock.patch.object(stnt, "open_browser") as browser:
            with self.assertRaisesRegex(stnt.StntError, "health timeout"):
                stnt.command_open(repo, (Path("/state"), base))
        browser.assert_not_called()

        for failure in (
            stnt.StntError("sandbox is missing; do not create a replacement"),
            stnt.StntError("sandbox identity changed; retained"),
        ):
            with self.subTest(failure=str(failure)), mock.patch.object(
                stnt, "require_loopback_service_host"
            ), mock.patch.object(
                stnt, "require_sandbox", side_effect=failure
            ), mock.patch.object(stnt, "run") as invoked, mock.patch.object(
                stnt, "open_browser"
            ) as browser:
                with self.assertRaises(stnt.StntError):
                    stnt.command_open(repo, (Path("/state"), base))
            invoked.assert_not_called()
            browser.assert_not_called()

    def test_start_opens_reviewed_url_only_after_successful_health(self):
        repo = Path("/fixture")
        current = dict(
            workspace_record(repo), sandboxPort=8010, serviceArgv=["bin/dev"],
            serviceURL="https://app.local.example:8010",
        )
        events = []

        def fake_run(args, **kwargs):
            if args[-4:] == ["git", "symbolic-ref", "--quiet", "HEAD"]:
                return subprocess.CompletedProcess(args, 0, "refs/heads/main\n", "")
            if "port" in args:
                return subprocess.CompletedProcess(
                    args, 0, json.dumps({"host_port": 8010, "sandbox_port": 8010}), ""
                )
            if len(args) > 1 and args[1] == "tui-start":
                events.append("tui")
            return subprocess.CompletedProcess(args, 0, "", "")

        sandbox = {
            "name": current["sandbox"], "id": current["sandboxID"],
            "workspaces": [str(repo)], "state": "running",
        }
        durable_statuses = []
        with mock.patch.object(stnt, "require_loopback_service_host"), mock.patch.object(
            stnt, "runtime_find", return_value=sandbox
        ), mock.patch.object(stnt, "run", side_effect=fake_run), mock.patch.object(
            stnt, "restart_service", side_effect=lambda *_: events.append("healthy")
        ), mock.patch.object(
            stnt, "open_browser", side_effect=lambda *_: events.append("opened")
        ) as browser, mock.patch.object(
            stnt, "vscode_editor_command", return_value="/Applications/Code"
        ), mock.patch.object(
            stnt, "open_vscode_editor", side_effect=lambda *_: events.append("editor")
        ) as editor, mock.patch.object(
            stnt, "workspace_exit_decision", return_value="pause"
        ), mock.patch.object(
            stnt, "atomic_write",
            side_effect=lambda _, value: durable_statuses.append(
                (value["status"], "editorAuthorization" in value)
            ),
        ), redirect_stdout(io.StringIO()):
            stnt.start_session(current, Path("/state"))
        self.assertEqual(events, ["healthy", "opened", "editor", "tui"])
        self.assertEqual(durable_statuses, [("starting", False), ("active", True)])
        browser.assert_called_once_with(current["serviceURL"])
        editor.assert_called_once_with(current, "/Applications/Code")

        with mock.patch.object(stnt, "require_loopback_service_host"), mock.patch.object(
            stnt, "runtime_find", return_value=sandbox
        ), mock.patch.object(stnt, "run", side_effect=fake_run), mock.patch.object(
            stnt, "restart_service", side_effect=stnt.StntError("health failed")
        ), mock.patch.object(stnt, "open_browser") as browser, mock.patch.object(
            stnt, "vscode_editor_command"
        ) as editor, mock.patch.object(
            stnt, "atomic_write"
        ):
            with self.assertRaisesRegex(stnt.StntError, "health failed"):
                stnt.start_session(current, Path("/state"))
        browser.assert_not_called()
        editor.assert_not_called()

    def test_pause_and_recover_write_only_selected_path(self):
        repo = Path("/fixture")
        selected_path, sibling_path = Path("/selected"), Path("/sibling")
        paused = record(repo)
        with mock.patch.object(stnt, "thread_status", return_value="active"), mock.patch.object(
            stnt, "require_sandbox"
        ), mock.patch.object(
            stnt, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
        ), mock.patch.object(stnt, "atomic_write") as write:
            stnt.command_pause(repo, (selected_path, paused))
        write.assert_called_once_with(selected_path, paused)
        self.assertNotEqual(write.call_args.args[0], sibling_path)

        creating = dict(record(repo), status="creating")
        with mock.patch.object(stnt, "thread_status", return_value="empty"), mock.patch.object(
            stnt, "ensure_creation"
        ) as recover, mock.patch.object(stnt, "load_state", return_value=creating), mock.patch.object(
            stnt, "repository_lock"
        ):
            stnt.command_recover_create(repo, (selected_path, creating))
        recover.assert_called_once_with(creating, selected_path)

    def test_failed_finish_retains_selected_and_sibling_records(self):
        repo = Path("/fixture")
        current = record(repo)
        with tempfile.TemporaryDirectory() as temporary:
            selected_path = Path(temporary) / "selected.json"
            sibling_path = Path(temporary) / "sibling.json"
            selected_path.write_text("selected")
            sibling_path.write_text("sibling")
            with mock.patch.object(stnt, "validate_record_repository"), mock.patch.object(
                stnt, "thread_status", return_value="archived"
            ), mock.patch.object(stnt, "runtime_find", return_value={
                "name": current["sandbox"], "id": current["sandboxID"], "workspaces": [str(repo)]
            }), mock.patch.object(
                stnt, "run", return_value=subprocess.CompletedProcess([], 1, "", "failed")
            ), mock.patch.object(stnt, "remove_state") as remove:
                with self.assertRaisesRegex(stnt.StntError, "retained session"):
                    stnt.command_finish(repo, (selected_path, current))
            remove.assert_not_called()
            self.assertEqual(selected_path.read_text(), "selected")
            self.assertEqual(sibling_path.read_text(), "sibling")

    def test_health_accepts_redirect_status_without_following_location(self):
        connection = mock.Mock()
        connection.getresponse.return_value.status = 302
        with mock.patch.object(stnt, "HTTPConnection", return_value=connection) as constructor:
            stnt.wait_for_health(49177, "/health", timeout=1)
        constructor.assert_called_once_with("127.0.0.1", 49177, timeout=mock.ANY)
        connection.request.assert_called_once_with("GET", "/health")
        connection.close.assert_called_once()

    def test_https_health_uses_the_service_hostname_and_accepts_self_signed_tls(self):
        connection = mock.Mock()
        connection.getresponse.return_value.status = 200
        with mock.patch.object(stnt, "HTTPSConnection", return_value=connection) as constructor:
            stnt.wait_for_health(
                8010, "/health", timeout=1,
                scheme="https", hostname="app.example.test",
            )

        constructor.assert_called_once_with(
            "app.example.test", 8010, timeout=mock.ANY, context=mock.ANY
        )
        context = constructor.call_args.kwargs["context"]
        self.assertFalse(context.check_hostname)
        self.assertEqual(context.verify_mode, stnt.ssl.CERT_NONE)
        connection.request.assert_called_once_with("GET", "/health")

    def test_incomplete_creation_blocks_duplicate_default_session(self):
        repo = Path("/fixture")
        existing = dict(record(repo), status="creating")
        with mock.patch.object(stnt, "load_state", return_value=existing), mock.patch.object(
            stnt, "create_record"
        ) as create:
            with self.assertRaisesRegex(stnt.StntError, "recover-create"):
                stnt.command_start(repo)
        create.assert_not_called()

    def test_missing_sandbox_fails_closed_with_retained_identity(self):
        repo = Path("/fixture")
        current = record(repo)
        with mock.patch.object(stnt, "validate_record_repository"), mock.patch.object(
            stnt, "thread_status", return_value="active"
        ), mock.patch.object(stnt, "runtime_find", return_value=None):
            with self.assertRaisesRegex(
                stnt.StntError, f"session {THREAD_ID} is retained.*do not create a replacement"
            ):
                stnt.start_session(current, Path("/state"))

    def test_inventory_failure_is_not_treated_as_absence(self):
        failed = subprocess.CompletedProcess([], 1, "", "daemon unavailable")
        with mock.patch.object(stnt, "run", return_value=failed):
            with self.assertRaisesRegex(stnt.StntError, "inventory lookup failed"):
                stnt.runtime_find("sandbox")

    def test_recovery_never_replaces_a_missing_persisted_sandbox_id(self):
        current = dict(record(Path("/fixture")), status="creating")
        with mock.patch.object(stnt, "runtime_find", return_value=None), mock.patch.object(
            stnt, "run"
        ) as runtime:
            with self.assertRaisesRegex(stnt.StntError, "do not create a replacement"):
                stnt.ensure_creation(current, Path("/state"))
        runtime.assert_not_called()

    def test_changed_sandbox_id_blocks_mutation(self):
        current = record(Path("/fixture"))
        with mock.patch.object(
            stnt, "runtime_find", return_value={
                "name": current["sandbox"], "id": "replacement", "workspaces": ["/fixture"]
            }
        ):
            with self.assertRaisesRegex(stnt.StntError, "identity changed"):
                stnt.require_sandbox(current)

    def test_pause_rejects_incomplete_creation_before_lookup(self):
        repo = Path("/fixture")
        current = dict(record(repo), status="creating")
        with mock.patch.object(stnt, "load_state", return_value=current), mock.patch.object(
            stnt, "thread_status"
        ) as lookup:
            with self.assertRaisesRegex(stnt.StntError, "recover-create"):
                stnt.command_pause(repo)
        lookup.assert_not_called()

    def test_failed_amp_lookup_cannot_touch_runtime(self):
        repo = Path("/fixture")
        current = record(repo)
        with mock.patch.object(stnt, "validate_record_repository"), mock.patch.object(
            stnt, "thread_status", side_effect=stnt.StntError("lookup failed")
        ), mock.patch.object(stnt, "runtime_find") as runtime:
            with self.assertRaisesRegex(stnt.StntError, "lookup failed"):
                stnt.start_session(current, Path("/state"))
        runtime.assert_not_called()

    def test_ambiguous_record_with_authoritative_empty_export_can_resume(self):
        repo = Path("/fixture")
        current = dict(record(repo), status="ambiguous")

        def fake_run(args, **kwargs):
            if args[:2] == [str(stnt.THREADS), "status"]:
                return subprocess.CompletedProcess(args, 3, "missing\n", "")
            if args[:3] == ["amp", "threads", "export"]:
                return subprocess.CompletedProcess(
                    args, 0, json.dumps({"id": THREAD_ID, "messages": []}), ""
                )
            if args[-4:] == ["git", "symbolic-ref", "--quiet", "HEAD"]:
                return subprocess.CompletedProcess(args, 0, "refs/heads/feature/example-work\n", "")
            if "port" in args:
                return subprocess.CompletedProcess(args, 0, '{"host_port": 49177}', "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(stnt, "validate_record_repository"), mock.patch.object(
            stnt, "runtime_find", return_value={
                "name": current["sandbox"], "id": current["sandboxID"],
                "workspaces": [str(repo)],
            }
        ), mock.patch.object(stnt, "run", side_effect=fake_run) as invoked, mock.patch.object(
            stnt, "pause_after_exit"
        ), mock.patch.object(stnt, "sandbox_hold"), mock.patch.object(
            stnt, "atomic_write"
        ), redirect_stdout(io.StringIO()):
            stnt.start_session(current, Path("/state"))

        self.assertTrue(any(call.args[0] == [str(stnt.THREADS), "continue", THREAD_ID]
                            for call in invoked.call_args_list))

    def test_each_resume_uses_current_port_without_requiring_git_bridge(self):
        repo = Path("/fixture")
        current = record(repo)
        ports = iter((49175, 49177))

        def fake_run(args, **kwargs):
            if args[-4:] == ["git", "symbolic-ref", "--quiet", "HEAD"]:
                return subprocess.CompletedProcess(args, 0, "refs/heads/feature/example-work\n", "")
            if "port" in args:
                return subprocess.CompletedProcess(args, 0, json.dumps({"host_port": next(ports)}), "")
            return subprocess.CompletedProcess(args, 0, "", "")

        output = io.StringIO()
        with mock.patch.object(stnt, "validate_record_repository"
        ), mock.patch.object(stnt, "thread_status", return_value="active"), mock.patch.object(
            stnt, "runtime_find", return_value={
                "name": current["sandbox"], "id": current["sandboxID"],
                "workspaces": [str(repo)],
            }
        ), mock.patch.object(stnt, "run", side_effect=fake_run) as invoked, mock.patch.object(
            stnt, "pause_after_exit"
        ), mock.patch.object(stnt, "sandbox_hold"), mock.patch.object(
            stnt, "atomic_write"
        ), redirect_stdout(output):
            stnt.start_session(current, Path("/state"))
            stnt.start_session(current, Path("/state"))

        self.assertIn("http://127.0.0.1:49175", output.getvalue())
        self.assertIn("http://127.0.0.1:49177", output.getvalue())
        remote_calls = [call for call in invoked.call_args_list if "remote" in call.args[0]]
        self.assertEqual(remote_calls, [])
        self.assertNotIn("hostPort", current)

    def test_service_restarts_and_health_uses_each_new_host_port(self):
        repo = Path("/fixture")
        current = dict(record(repo), serviceCommand="python server.py", healthPath="/health")
        ports = iter((49175, 49177))

        def fake_run(args, **kwargs):
            if args[-4:] == ["git", "symbolic-ref", "--quiet", "HEAD"]:
                return subprocess.CompletedProcess(args, 0, "refs/heads/feature/example-work\n", "")
            if "port" in args:
                return subprocess.CompletedProcess(args, 0, json.dumps({"host_port": next(ports)}), "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(stnt, "validate_record_repository"), mock.patch.object(
            stnt, "thread_status", return_value="active"
        ), mock.patch.object(stnt, "runtime_find", return_value={
            "name": current["sandbox"], "id": current["sandboxID"], "workspaces": [str(repo)]
        }), mock.patch.object(stnt, "run", side_effect=fake_run) as invoked, mock.patch.object(
            stnt, "wait_for_health"
        ) as health, mock.patch.object(stnt, "pause_after_exit"), mock.patch.object(
            stnt, "sandbox_hold"
        ), mock.patch.object(stnt, "atomic_write"):
            stnt.start_session(current, Path("/state"))
            stnt.start_session(current, Path("/state"))

        service_calls = [call for call in invoked.call_args_list if "service-start" in call.args[0]]
        self.assertEqual(len(service_calls), 2)
        self.assertIn("STNT_HOST=0.0.0.0", service_calls[0].args[0][-1])
        self.assertEqual(
            [call.args[:2] for call in health.call_args_list],
            [(49175, "/health"), (49177, "/health")],
        )
        self.assertNotIn("hostPort", current)

    def test_service_health_failure_stops_and_retains_without_attaching(self):
        repo = Path("/fixture")
        current = dict(record(repo), serviceCommand="python server.py", healthPath="/health")

        def fake_run(args, **kwargs):
            if args[-4:] == ["git", "symbolic-ref", "--quiet", "HEAD"]:
                return subprocess.CompletedProcess(args, 0, "refs/heads/feature/example-work\n", "")
            if "port" in args:
                return subprocess.CompletedProcess(args, 0, '{"host_port": 49177}', "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(stnt, "validate_record_repository"), mock.patch.object(
            stnt, "thread_status", return_value="active"
        ), mock.patch.object(stnt, "runtime_find", return_value={
            "name": current["sandbox"], "id": current["sandboxID"], "workspaces": [str(repo)]
        }), mock.patch.object(stnt, "run", side_effect=fake_run) as invoked, mock.patch.object(
            stnt, "wait_for_health", side_effect=stnt.StntError("health timeout")
        ), mock.patch.object(stnt, "atomic_write") as write:
            with self.assertRaisesRegex(stnt.StntError, "retry: stnt"):
                stnt.start_session(current, Path("/state"))

        self.assertEqual(current["status"], "paused")
        self.assertEqual(write.call_count, 2)
        self.assertTrue(all(call.args[0] == Path("/state") for call in write.call_args_list))
        self.assertTrue(any("stop" in call.args[0] for call in invoked.call_args_list))
        attach_calls = [call for call in invoked.call_args_list if "threads" in call.args[0]]
        self.assertEqual(attach_calls, [])

    def test_stop_failure_records_ambiguous_not_paused(self):
        current = record(Path("/fixture"))
        stop_failed = subprocess.CompletedProcess([], 1, "", "stop failed")
        with mock.patch.object(stnt, "run", return_value=stop_failed), mock.patch.object(
            stnt, "atomic_write"
        ) as write, mock.patch.object(stnt, "thread_status") as lookup:
            with self.assertRaisesRegex(stnt.StntError, "ambiguous runtime state"):
                stnt.pause_after_exit(current, Path("/state"))
        self.assertEqual(current["status"], "ambiguous")
        write.assert_called_once()
        lookup.assert_not_called()

    def test_quitting_before_first_message_keeps_verified_empty_session_paused(self):
        current = dict(record(Path("/fixture")), status="ambiguous")
        with mock.patch.object(
            stnt, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
        ), mock.patch.object(
            stnt, "thread_status", return_value="empty"
        ) as lookup, mock.patch.object(stnt, "atomic_write") as write, redirect_stdout(io.StringIO()):
            stnt.pause_after_exit(current, Path("/state"))

        lookup.assert_called_once_with(THREAD_ID, allow_empty=True)
        self.assertEqual(current["status"], "paused")
        write.assert_called_once_with(Path("/state"), current)

    def test_archive_and_quit_stops_then_safely_finishes_session(self):
        repo = Path("/fixture")
        current = record(repo)
        output = io.StringIO()
        with mock.patch.object(
            stnt, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
        ) as invoked, mock.patch.object(
            stnt, "thread_status", return_value="archived"
        ), mock.patch.object(stnt, "atomic_write") as write, mock.patch.object(
            stnt, "command_finish"
        ) as finish, redirect_stdout(output):
            stnt.pause_after_exit(current, Path("/state"))

        invoked.assert_called_once_with(
            [str(stnt.RUNTIME), "stop", current["sandbox"]], check=False, capture=True
        )
        self.assertEqual(current["status"], "archived")
        write.assert_called_once_with(Path("/state"), current)
        finish.assert_called_once_with(repo, (Path("/state"), current))
        self.assertIn("preserved work and removed sandbox", output.getvalue())


class StackProofTests(unittest.TestCase):
    def make_repository(self, path: Path, fixture: str) -> None:
        path.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
        source = Path(__file__).parent / "fixtures" / "stack" / fixture
        (path / fixture).write_text(source.read_text())
        subprocess.run(["git", "-C", str(path), "add", fixture], check=True)
        subprocess.run([
            "git", "-C", str(path), "-c", "user.name=Stnt Test",
            "-c", "user.email=stnt@example.invalid", "commit", "-qm", "fixture",
        ], check=True)

    def profile(self, root: Path, name: str = "proof-one", host_port: int = 18010):
        frontend, backend = root / "frontend", root / "backend"
        self.make_repository(frontend, "frontend.py")
        self.make_repository(backend, "backend.py")
        return {
            "schemaVersion": 1,
            "name": name,
            "repositories": [
                stnt.stack_repository(
                    str(frontend), "frontend", ["python3", "frontend.py"], str(frontend.resolve())
                ),
                stnt.stack_repository(
                    str(backend), "backend", ["python3", "backend.py"],
                    f"/home/agent/stnt-stacks/{name}/backend",
                ),
            ],
            "ingress": {"hostPort": host_port, "sandboxPort": 8000, "healthPath": "/health"},
            "internalPorts": {"http": 4000, "websocket": 4500},
        }

    def stack_record(self, profile, status="paused"):
        return {
            "schemaVersion": 1, "name": profile["name"],
            "profileDigest": stnt.canonical_digest(profile), "threadID": THREAD_ID,
            "runtime": "docker-sandbox", "sandbox": "stnt-stack-proof", "sandboxID": "stack-id",
            "repositories": stnt.stack_state_repositories(profile, THREAD_ID),
            "ingress": profile["ingress"], "status": status,
            "createdAt": "2026-08-14T00:00:00Z",
        }

    def test_stack_init_requires_explicit_paths_commands_and_interactive_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = self.profile(root)
            config = root / "config"
            with mock.patch.dict(os.environ, {"STNT_CONFIG_HOME": str(config)}), mock.patch.object(
                sys.stdin, "isatty", return_value=True
            ), mock.patch("builtins.input", return_value="yes"), redirect_stdout(io.StringIO()):
                result = stnt.command_stack_init(
                    profile["name"], profile["repositories"][0]["path"],
                    profile["repositories"][1]["path"], profile["ingress"]["hostPort"],
                    '["python3","frontend.py"]', '["python3","backend.py"]',
                )
            saved_path = config / "stacks" / "proof-one.json"
            saved = json.loads(saved_path.read_text())
            self.assertEqual(result, 0)
            self.assertEqual(saved["repositories"], profile["repositories"])
            self.assertEqual(saved["internalPorts"], {"http": 4000, "websocket": 4500})
            self.assertEqual(saved_path.stat().st_mode & 0o777, 0o600)

    def test_noninteractive_stack_init_and_source_drift_fail_without_authorizing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = self.profile(root)
            config = root / "config"
            with mock.patch.dict(os.environ, {"STNT_CONFIG_HOME": str(config)}), mock.patch.object(
                sys.stdin, "isatty", return_value=False
            ), redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(stnt.StntError, "interactive review"):
                    stnt.command_stack_init(
                        profile["name"], profile["repositories"][0]["path"],
                        profile["repositories"][1]["path"], 18010,
                        '["python3","frontend.py"]', '["python3","backend.py"]',
                    )
            self.assertFalse((config / "stacks" / "proof-one.json").exists())
            backend = Path(profile["repositories"][1]["path"])
            (backend / "backend.py").write_text("changed")
            with self.assertRaisesRegex(stnt.StntError, "must be clean"):
                stnt.require_stack_sources(profile)

    def test_stack_creation_uses_read_only_second_source_and_only_publishes_ingress(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = self.profile(root)
            state_root = root / "state"
            record_value = {
                "schemaVersion": 1, "name": profile["name"],
                "profileDigest": stnt.canonical_digest(profile), "threadID": THREAD_ID,
                "runtime": "docker-sandbox", "sandbox": "stnt-stack-proof",
                "repositories": profile["repositories"], "ingress": profile["ingress"],
                "status": "creating", "createdAt": "2026-08-14T00:00:00Z",
            }
            found = {
                "name": record_value["sandbox"], "id": "stack-id",
                "workspaces": [item["path"] for item in profile["repositories"]],
            }

            def fake_run(args, **kwargs):
                if args[:2] == [str(stnt.RUNTIME), "port"]:
                    if not hasattr(fake_run, "seen"):
                        fake_run.seen = True
                        return subprocess.CompletedProcess(args, 4, "", "")
                    return subprocess.CompletedProcess(args, 0, '{"host_port":18010}', "")
                return subprocess.CompletedProcess(args, 0, "", "")

            with mock.patch.dict(os.environ, {"STNT_STATE_HOME": str(state_root)}), mock.patch.object(
                stnt, "runtime_find", side_effect=[None, found, found]
            ), mock.patch.object(stnt, "run", side_effect=fake_run) as invoked, mock.patch.object(
                stnt, "atomic_write"
            ):
                stnt.ensure_stack_creation(record_value, profile)
            calls = [call.args[0] for call in invoked.call_args_list]
            self.assertIn([
                str(stnt.RUNTIME), "create-stack", record_value["sandbox"],
                profile["repositories"][0]["path"], profile["repositories"][1]["path"],
            ], calls)
            self.assertIn([str(stnt.RUNTIME), "publish", record_value["sandbox"], "18010:8000"], calls)
            self.assertTrue(any(call[1] == "stack-prepare" for call in calls))
            self.assertFalse(any("4000" in call or "4500" in call for call in calls if call[1] == "publish"))
            adapter = stnt.RUNTIME.read_text()
            self.assertIn('"${backend}:ro"', adapter)
            self.assertIn("git clone --no-local --no-hardlinks", adapter)

    def test_stack_start_restores_services_ingress_and_one_frontend_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = self.profile(root)
            record_value = {
                "schemaVersion": 1, "name": profile["name"],
                "profileDigest": stnt.canonical_digest(profile), "threadID": THREAD_ID,
                "runtime": "docker-sandbox", "sandbox": "stnt-stack-proof", "sandboxID": "stack-id",
                "repositories": profile["repositories"], "ingress": profile["ingress"],
                "status": "paused", "createdAt": "2026-08-14T00:00:00Z",
            }

            def fake_run(args, **kwargs):
                if args[:2] == [str(stnt.RUNTIME), "port"]:
                    return subprocess.CompletedProcess(args, 0, '{"host_port":18010}', "")
                return subprocess.CompletedProcess(args, 0, "", "")

            with mock.patch.object(stnt, "thread_status", return_value="active"), mock.patch.object(
                stnt, "runtime_find", return_value={
                    "name": record_value["sandbox"], "id": "stack-id",
                    "workspaces": [item["path"] for item in profile["repositories"]],
                }
            ), mock.patch.object(stnt, "run", side_effect=fake_run) as invoked, mock.patch.object(
                stnt, "wait_for_health"
            ) as health, mock.patch.object(stnt, "sandbox_hold"), mock.patch.object(
                stnt, "pause_stack_after_exit"
            ):
                stnt.start_stack(record_value, profile)
            calls = [call.args[0] for call in invoked.call_args_list]
            self.assertTrue(any(call[1] == "stack-verify" for call in calls))
            self.assertTrue(any(call[1] == "stack-start" for call in calls))
            runner = next(call for call in calls if call[1] == "runner-start")
            self.assertEqual(runner[-1], profile["repositories"][0]["guestPath"])
            health.assert_called_once_with(18010, "/health")

    def test_stack_record_durably_names_both_collision_resistant_preservation_branches(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = self.profile(Path(temporary))
            repositories = stnt.stack_state_repositories(profile, THREAD_ID)
        self.assertEqual({item["role"] for item in repositories}, {"frontend", "backend"})
        self.assertEqual(len({item["preservationBranch"] for item in repositories}), 2)
        for item in repositories:
            self.assertIn(stnt.compact_thread_id(THREAD_ID), item["preservationBranch"])
            self.assertEqual(subprocess.run([
                "git", "check-ref-format", f"refs/heads/{item['preservationBranch']}"
            ]).returncode, 0)

    def test_existing_stack_records_preservation_intent_before_next_runtime_mutation(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"STNT_STATE_HOME": str(Path(temporary) / "state")}
        ):
            repositories = Path(temporary) / "repositories"
            repositories.mkdir()
            profile = self.profile(repositories)
            current = self.stack_record(profile)
            current["repositories"] = profile["repositories"]
            with mock.patch.object(stnt, "atomic_write") as write, mock.patch.object(
                stnt, "run"
            ) as runtime:
                stnt.ensure_stack_preservation_intent(current, profile)
            write.assert_called_once_with(stnt.stack_state_path(profile["name"]), current)
            runtime.assert_not_called()
            self.assertTrue(all(item.get("preservationBranch") for item in current["repositories"]))

    def test_stack_finish_requires_archive_then_removes_state_only_after_transaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = self.profile(Path(temporary))
            current = self.stack_record(profile)
            found = {
                "name": current["sandbox"], "id": current["sandboxID"],
                "workspaces": [item["path"] for item in profile["repositories"]],
            }
            with mock.patch.object(stnt, "load_stack_state", return_value=current), mock.patch.object(
                stnt, "load_stack_profile", return_value=(Path("/profile"), profile)
            ), mock.patch.object(stnt, "thread_status", return_value="archived") as status, mock.patch.object(
                stnt, "runtime_find", return_value=found
            ), mock.patch.object(
                stnt, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
            ) as invoked, mock.patch.object(stnt, "remove_state") as remove:
                stnt.command_stack_finish(profile["name"])
            status.assert_called_once_with(THREAD_ID)
            transaction = next(
                call.args[0] for call in invoked.call_args_list if call.args[0][0] == str(stnt.STACK_FINISH)
            )
            self.assertEqual(transaction[1:5], ["finish", current["sandbox"], profile["name"], THREAD_ID])
            self.assertIn(current["repositories"][0]["preservationBranch"], transaction)
            self.assertIn(current["repositories"][1]["preservationBranch"], transaction)
            remove.assert_called_once_with(stnt.stack_state_path(profile["name"]))

    def test_stack_detach_never_invokes_amp_and_failed_transaction_retains_complete_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = self.profile(Path(temporary))
            current = self.stack_record(profile)
            found = {
                "name": current["sandbox"], "id": current["sandboxID"],
                "workspaces": [item["path"] for item in profile["repositories"]],
            }
            with mock.patch.object(stnt, "load_stack_state", return_value=current), mock.patch.object(
                stnt, "load_stack_profile", return_value=(Path("/profile"), profile)
            ), mock.patch.object(stnt, "thread_status") as status, mock.patch.object(
                stnt, "runtime_find", return_value=found
            ), mock.patch.object(
                stnt, "run", return_value=subprocess.CompletedProcess([], 1, "", "failed")
            ), mock.patch.object(stnt, "remove_state") as remove:
                with self.assertRaisesRegex(stnt.StntError, "Safe retry: stnt stack detach proof-one") as raised:
                    stnt.command_stack_detach(profile["name"])
            status.assert_not_called()
            remove.assert_not_called()
            message = str(raised.exception)
            self.assertIn(current["sandbox"], message)
            self.assertIn(current["repositories"][0]["preservationBranch"], message)
            self.assertIn(current["repositories"][1]["preservationBranch"], message)


if __name__ == "__main__":
    unittest.main()
