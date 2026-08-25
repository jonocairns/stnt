import os
import pathlib
import pty
import signal
import subprocess
import tempfile
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = ROOT / "assets" / "amp-kit" / "spec.yaml"


class AmpKitTests(unittest.TestCase):
    def test_amp_remains_the_fixed_entrypoint_executable(self):
        text = SPEC.read_text()

        self.assertIn('schemaVersion: "2"', text)
        self.assertIn("entrypoint: [amp]", text)
        self.assertNotIn("entrypoint: [env,", text)

    def test_live_golden_proves_push_upstream_and_destructive_finish(self):
        text = (ROOT / "bin" / "golden-e2e-test").read_text()

        self.assertIn('push --set-upstream origin "HEAD:$remote_ref"', text)
        self.assertIn('"@{upstream}...HEAD"', text)
        self.assertIn('[[ $ahead == 0 && $behind == 0 ]]', text)
        self.assertIn('"$stnt" config show', text)
        self.assertIn('len(services) > 1', text)
        self.assertIn('re.fullmatch(', text)
        self.assertIn('/usr/bin/script -q "$finish_transcript"', text)
        self.assertIn('--session "$workspace_id" finish', text)
        self.assertNotIn("GOLDEN stage=preservation-detach", text)
        self.assertIn('refs/heads/$preservation^{commit}', text)
        self.assertIn("gh api --method DELETE", text)

    def test_runtime_reduces_github_service_inventory_without_secret_preview(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            sbx = directory / "sbx"
            sbx.write_text("""#!/bin/sh
cat <<'EOF'
(global) ampcode.com AMP_API_KEY sgamp-masked
(global) service github gho_must-not-escape
(global) service openai sk-must-not-escape
EOF
""")
            sbx.chmod(0o755)
            completed = subprocess.run(
                [str(ROOT / "bin" / "docker-sandbox"), "secrets"],
                text=True,
                capture_output=True,
                env=dict(os.environ, PATH=f"{directory}:{os.environ['PATH']}"),
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.strip(),
            '[{"target":"ampcode.com","name":"AMP_API_KEY"},{"target":"github","name":"GITHUB_TOKEN"}]',
        )
        self.assertNotIn("must-not-escape", completed.stdout)

    def test_runtime_delegates_amp_secret_entry_to_docker_without_a_command_line_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            marker = directory / "args"
            sbx = directory / "sbx"
            sbx.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" >\"$STNT_TEST_MARKER\"\n")
            sbx.chmod(0o755)
            completed = subprocess.run(
                [str(ROOT / "bin" / "docker-sandbox"), "amp-secret-set"],
                text=True,
                capture_output=True,
                env=dict(
                    os.environ,
                    PATH=f"{directory}:{os.environ['PATH']}",
                    STNT_TEST_MARKER=str(marker),
                ),
            )
            arguments = marker.read_text()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            arguments.splitlines(),
            ["secret", "set-custom", "--host", "ampcode.com", "--env", "AMP_API_KEY"],
        )
        self.assertNotIn("--value", arguments)
        self.assertNotIn("--token", arguments)

    def test_runtime_pipes_github_token_to_docker_without_exposing_it_in_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            arguments_marker = directory / "args"
            input_marker = directory / "input"
            gh = directory / "gh"
            gh.write_text("#!/bin/sh\nprintf 'github-secret\\n'\n")
            gh.chmod(0o755)
            sbx = directory / "sbx"
            sbx.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$@\" >\"$STNT_TEST_ARGS\"\n"
                "cat >\"$STNT_TEST_INPUT\"\n"
            )
            sbx.chmod(0o755)
            completed = subprocess.run(
                [str(ROOT / "bin" / "docker-sandbox"), "github-secret-set"],
                text=True,
                capture_output=True,
                env=dict(
                    os.environ,
                    PATH=f"{directory}:{os.environ['PATH']}",
                    STNT_TEST_ARGS=str(arguments_marker),
                    STNT_TEST_INPUT=str(input_marker),
                ),
            )
            arguments = arguments_marker.read_text()
            secret_input = input_marker.read_text()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(arguments.splitlines(), ["secret", "set", "github"])
        self.assertEqual(secret_input, "github-secret\n")
        self.assertNotIn("github-secret", arguments)
        self.assertNotIn("github-secret", completed.stdout)

    def test_runtime_creates_and_recognizes_only_the_narrow_stnt_bindings(self):
        with tempfile.TemporaryDirectory() as temporary:
            binding = pathlib.Path(temporary) / "sbx" / "credentials.yaml"
            environment = dict(os.environ, STNT_SBX_CREDENTIALS_FILE=str(binding))
            adapter = str(ROOT / "bin" / "docker-sandbox")

            missing = subprocess.run(
                [adapter, "github-binding-status"], text=True, capture_output=True,
                env=environment,
            )
            created = subprocess.run(
                [adapter, "stnt-bindings-enable"], text=True, capture_output=True,
                env=environment,
            )
            github_approved = subprocess.run(
                [adapter, "github-binding-status"], text=True, capture_output=True,
                env=environment,
            )
            amp_approved = subprocess.run(
                [adapter, "amp-binding-status"], text=True, capture_output=True,
                env=environment,
            )
            original = binding.read_text()
            mode = binding.stat().st_mode & 0o777
            refused = subprocess.run(
                [adapter, "stnt-bindings-enable"], text=True, capture_output=True,
                env=environment,
            )

        self.assertEqual(missing.stdout.strip(), '{"approved":false,"fileExists":false}')
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertEqual(github_approved.stdout.strip(), '{"approved":true,"fileExists":true}')
        self.assertEqual(amp_approved.stdout.strip(), '{"approved":true,"fileExists":true}')
        self.assertEqual(mode, 0o600)
        self.assertEqual(original, """bindings:
    amp:
        apiKey:
            domains:
                - ampcode.com
    github:
        apiKey:
            domains:
                - github.com
""")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("refusing to overwrite", refused.stderr)

    def test_runtime_binding_status_does_not_emit_unrelated_bindings(self):
        with tempfile.TemporaryDirectory() as temporary:
            binding = pathlib.Path(temporary) / "credentials.yaml"
            binding.write_text("""bindings:
  unrelated-service:
    apiKey:
      domains: [secret.example]
  github:
    apiKey:
      domains: [api.github.com, github.com]
""")
            completed = subprocess.run(
                [str(ROOT / "bin" / "docker-sandbox"), "github-binding-status"],
                text=True,
                capture_output=True,
                env=dict(os.environ, STNT_SBX_CREDENTIALS_FILE=str(binding)),
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), '{"approved":true,"fileExists":true}')
        self.assertNotIn("unrelated", completed.stdout)
        self.assertNotIn("secret.example", completed.stdout)

    def test_runtime_scopes_clipboard_setting_operations_to_image_paste(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            marker = directory / "args"
            sbx = directory / "sbx"
            sbx.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" >\"$STNT_TEST_MARKER\"\n")
            sbx.chmod(0o755)
            environment = dict(
                os.environ,
                PATH=f"{directory}:{os.environ['PATH']}",
                STNT_TEST_MARKER=str(marker),
            )

            cases = (
                ("clipboard-image-paste-status", ["settings", "get", "--json", "clipboard.imagePaste"]),
                ("clipboard-image-paste-enable", ["settings", "set", "clipboard.imagePaste", "true"]),
            )
            for command, expected in cases:
                with self.subTest(command=command):
                    completed = subprocess.run(
                        [str(ROOT / "bin" / "docker-sandbox"), command],
                        text=True,
                        capture_output=True,
                        env=environment,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertEqual(marker.read_text().splitlines(), expected)

    def test_runtime_starts_a_stable_no_tui_runner_in_the_private_clone(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            marker = directory / "args"
            sbx = directory / "sbx"
            sbx.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" >\"$STNT_TEST_MARKER\"\n")
            sbx.chmod(0o755)

            completed = subprocess.run(
                [
                    str(ROOT / "bin" / "docker-sandbox"), "runner-start", "sandbox-name",
                    "stnt-12345678123412341234123456789abc",
                    "T-12345678-1234-1234-1234-123456789abc", "/workspace/project",
                ],
                text=True,
                capture_output=True,
                env=dict(
                    os.environ,
                    PATH=f"{directory}:{os.environ['PATH']}",
                    STNT_TEST_MARKER=str(marker),
                ),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            arguments = marker.read_text().splitlines()
            self.assertEqual(arguments[:11], [
                "exec", "-u", "1000", "-w", "/workspace/project", "sandbox-name", "--",
                "bash", "--noprofile", "--norc", "-c",
            ])
            self.assertIn("exec /home/agent/.local/bin/amp --no-tui", "\n".join(arguments[11:-4]))
            self.assertIn("for attempt in 1 2", "\n".join(arguments[11:-4]))
            self.assertEqual(arguments[-4:], [
                "stnt-runner", "stnt-12345678123412341234123456789abc",
                "T-12345678-1234-1234-1234-123456789abc", "plain",
            ])

    def test_runtime_attaches_the_guest_tui_in_the_private_clone(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            marker = directory / "args"
            sbx = directory / "sbx"
            sbx.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" >\"$STNT_TEST_MARKER\"\n")
            sbx.chmod(0o755)

            completed = subprocess.run(
                [
                    str(ROOT / "bin" / "docker-sandbox"), "tui-start", "sandbox-name",
                    "/workspace/project", "nix",
                ],
                text=True,
                capture_output=True,
                env=dict(
                    os.environ,
                    PATH=f"{directory}:{os.environ['PATH']}",
                    STNT_TEST_MARKER=str(marker),
                ),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            arguments = marker.read_text().splitlines()
            self.assertEqual(arguments[:12], [
                "exec", "-it", "-u", "1000", "-w", "/workspace/project", "sandbox-name", "--",
                "bash", "--noprofile", "--norc", "-c",
            ])
            script = "\n".join(arguments[12:-2])
            self.assertIn("print-dev-env --no-write-lock-file", script)
            self.assertIn("exec /home/agent/.local/bin/amp --dangerously-allow-all", script)
            self.assertNotIn("--no-tui", script)
            self.assertEqual(arguments[-2:], ["stnt-tui", "nix"])

    def test_guest_tui_transport_preserves_terminal_input_and_resize(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            marker = directory / "terminal"
            sbx = directory / "sbx"
            payload = b"ordinary text\n\x1b[200~bracketed \xe2\x9c\x93\n\x1b[201~\x01\x03\x1b[A"
            sbx.write_text("""#!/usr/bin/python3
import os, signal, termios, tty

marker = os.environ['STNT_TEST_MARKER']
def resized(_signal, _frame):
    with open(marker, 'a') as output:
        output.write('resize=received\\n')

signal.signal(signal.SIGWINCH, resized)
original = termios.tcgetattr(0)
try:
    tty.setraw(0)
    with open(marker, 'w') as output:
        output.write(f'tty={os.isatty(0)}\\n')
        output.flush()
    expected = int(os.environ['STNT_TEST_BYTES'])
    data = b''
    while len(data) < expected:
        data += os.read(0, expected - len(data))
    with open(marker, 'a') as output:
        output.write(f'input={data.hex()}\\n')
finally:
    termios.tcsetattr(0, termios.TCSANOW, original)
""")
            sbx.chmod(0o755)
            master, slave = pty.openpty()
            process = subprocess.Popen(
                [
                    str(ROOT / "bin" / "docker-sandbox"), "tui-start", "sandbox-name",
                    "/workspace/project",
                ],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                env=dict(
                    os.environ,
                    PATH=f"{directory}:{os.environ['PATH']}",
                    STNT_TEST_MARKER=str(marker),
                    STNT_TEST_BYTES=str(len(payload)),
                ),
            )
            os.close(slave)
            try:
                for _ in range(100):
                    if marker.exists():
                        break
                    time.sleep(0.01)
                self.assertTrue(marker.exists(), "guest transport did not acquire the terminal")
                os.killpg(process.pid, signal.SIGWINCH)
                os.write(master, payload)
                self.assertEqual(process.wait(timeout=5), 0)
            finally:
                os.close(master)
                if process.poll() is None:
                    process.kill()

            observed = marker.read_text()
            self.assertIn("tty=True", observed)
            self.assertIn("resize=received", observed)
            self.assertIn(f"input={payload.hex()}", observed)

    def test_profile_runner_loads_the_pinned_nix_development_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            marker = directory / "args"
            sbx = directory / "sbx"
            sbx.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" >\"$STNT_TEST_MARKER\"\n")
            sbx.chmod(0o755)

            completed = subprocess.run(
                [
                    str(ROOT / "bin" / "docker-sandbox"), "runner-start", "sandbox-name",
                    "stnt-12345678123412341234123456789abc",
                    "T-12345678-1234-1234-1234-123456789abc", "/workspace/project", "nix",
                ],
                text=True,
                capture_output=True,
                env=dict(
                    os.environ,
                    PATH=f"{directory}:{os.environ['PATH']}",
                    STNT_TEST_MARKER=str(marker),
                ),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            arguments = marker.read_text().splitlines()
            script = "\n".join(arguments[11:-4])
            self.assertIn("print-dev-env --no-write-lock-file", script)
            self.assertIn('/home/agent/.nix-profile/bin:$PATH', script)
            self.assertEqual(arguments[-4:], [
                "stnt-runner", "stnt-12345678123412341234123456789abc",
                "T-12345678-1234-1234-1234-123456789abc", "nix",
            ])

    def test_kit_does_not_copy_host_amp_state_or_credentials(self):
        text = SPEC.read_text()

        for forbidden in (
            ".config/amp",
            ".amp/session.json",
            "settings.json",
            "AMP_SETTINGS_FILE",
            "AMP_API_KEY=",
        ):
            self.assertNotIn(forbidden, text)

    def test_base_kit_uses_proxy_managed_amp_credential_without_github_access(self):
        text = SPEC.read_text()

        self.assertIn("credentials:\n  - service: amp", text)
        self.assertIn('name: ""', text)
        self.assertIn("format: \"Bearer %s\"", text)
        self.assertNotIn("service: github", text)

    def test_agent_context_distinguishes_the_mirrored_private_clone(self):
        text = SPEC.read_text()

        for required in (
            "sandbox-private writable Git clone",
            "same absolute path as the host repository",
            "is not the host checkout",
            'call it the\n    "sandbox-private clone"',
            "`/run/sandbox/source` only as a\n    read-only source mount",
        ):
            self.assertIn(required, text)


if __name__ == "__main__":
    unittest.main()
