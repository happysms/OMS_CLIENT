import json
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oms_client import routing


class RoutingTests(unittest.TestCase):
    def test_extracts_only_https_quick_tunnel_hostnames(self):
        self.assertEqual(routing.extract_url('INF | https://quiet-otter-123.trycloudflare.com |'), 'https://quiet-otter-123.trycloudflare.com')
        for value in ['http://quiet.trycloudflare.com', 'https://example.com',
                      'https://quiet.trycloudflare.com.evil.test', 'https://quiet.trycloudflare.com:443',
                      'https://quiet.trycloudflare.com_evil', 'https://-quiet.trycloudflare.com']:
            self.assertIsNone(routing.extract_url(value), value)

    def test_origin_is_fixed_to_loopback_and_valid_port(self):
        self.assertEqual(routing.origin_url(8765), 'http://127.0.0.1:8765')
        for value in [0, -1, 65536, True, '8765', 'http://10.0.0.1:80']:
            with self.assertRaises(ValueError):
                routing.origin_url(value)

    def test_state_is_atomic_private_and_stopped_hides_url(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state = {'manager_pid': 99999999, 'manager_start_ticks': '123',
                     'status': 'connected', 'url': 'https://quiet.trycloudflare.com'}
            routing.write_state(directory, state)
            self.assertEqual(routing.read_state(directory), state)
            self.assertEqual(stat.S_IMODE((directory / 'routing.json').stat().st_mode), 0o600)
            self.assertFalse(list(directory.glob('.routing-*.tmp')))
            self.assertFalse(routing.status(directory)['active'])
            self.assertIsNone(routing.status(directory)['url'])
            (directory / 'routing.json').write_text('[]')
            with self.assertRaises(ValueError):
                routing.read_state(directory)

    def test_pid_reuse_does_not_target_unrelated_process(self):
        state = {'manager_pid': 123, 'manager_start_ticks': '10'}
        with patch.object(routing, 'process_identity', return_value={'pid': 123, 'start_ticks': '11'}):
            self.assertFalse(routing.is_manager(state))
        with patch.object(routing, 'process_identity', return_value=None):
            self.assertFalse(routing.is_manager(state))
        self.assertIsNone(routing.process_identity(os.getpid()))

    def test_stop_never_signals_pid_when_identity_mismatches(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            routing.write_state(directory, {'manager_pid': 123, 'manager_start_ticks': '10'})
            with patch.object(routing, 'process_identity', return_value=None), patch.object(routing.os, 'kill') as kill:
                routing.stop(directory)
                kill.assert_not_called()

    def test_logging_is_bounded_and_tokens_redacted(self):
        self.assertNotIn('very-secret-token', routing.redact('bad token=very-secret-token', 'very-secret-token'))
        self.assertNotIn('abc', routing.redact('token: abc'))
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            logger = routing.make_logger(directory)
            try:
                for _ in range(800):
                    logger.info('x' * 4096)
                files = list(directory.glob('routing.log*'))
                self.assertLessEqual(len(files), 3)
                self.assertLess(sum(f.stat().st_size for f in files), 3 * 1024 * 1024)
            finally:
                for handler in logger.handlers:
                    handler.close()

    def test_named_token_is_read_from_private_file_never_raw_argv(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'token'
            path.write_text('very-secret-token')
            path.chmod(0o600)
            with patch.dict(os.environ, {'CLOUDFLARE_TUNNEL_TOKEN_FILE': str(path)}):
                command, token = routing.command('named', 8765, Path('/cloudflared'))
                self.assertEqual(token, 'very-secret-token')
                self.assertNotIn(token, command)
                self.assertIn('--token-file', command)
                path.chmod(0o644)
                with self.assertRaises(ValueError):
                    routing.command('named', 8765, Path('/cloudflared'))

    def test_quick_command_uses_loopback_and_http2(self):
        command, token = routing.command('quick', 8765, Path('/cloudflared'))
        self.assertEqual(token, '')
        self.assertIn('http://127.0.0.1:8765', command)
        self.assertIn('http2', command)
        self.assertIn('127.0.0.1:0', command)

    def test_supervisor_accepts_legacy_application_health(self):
        self.test_supervisor_persists_url_and_cleans_state_after_child_exits(health_service="midprice-lab")

    def test_supervisor_persists_url_and_cleans_state_after_child_exits(self, health_service="oms-client"):
        class Child:
            pid = 54321
            stdout = io.StringIO('INF https://quiet-otter.trycloudflare.com\nINF Registered tunnel connection\n')
            def poll(self):
                return 0
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            binary = directory / 'cloudflared'
            binary.write_text('fake executable')
            binary.chmod(0o700)
            with (patch.dict(os.environ, {'DASHBOARD_STATE_DIR': str(directory), 'DASHBOARD_CLOUDFLARED': str(binary)}),
                  patch.object(routing, 'process_identity', return_value={'pid': os.getpid(), 'start_ticks': '123'}),
                  patch.object(routing, 'urlopen', return_value=io.StringIO(json.dumps({"ok": True, "service": health_service}))),
                  patch.object(routing.subprocess, 'Popen', return_value=Child()) as popen,
                  patch.object(routing.signal, 'signal'), patch('sys.stdout', new_callable=io.StringIO) as stdout):
                routing.run('quick', 8765)
                self.assertIn('https://quiet-otter.trycloudflare.com', stdout.getvalue())
                self.assertIn('http://127.0.0.1:8765', popen.call_args.args[0])
            state = routing.read_state(directory)
            self.assertEqual(state['status'], 'stopped')
            self.assertIsNone(state['url'])
            self.assertEqual(state['last_url'], 'https://quiet-otter.trycloudflare.com')
            self.assertIn('Registered tunnel connection', (directory / 'routing.log').read_text())


if __name__ == '__main__':
    unittest.main()
