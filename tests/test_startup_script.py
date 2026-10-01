"""Run the real launcher in a temporary clone with local UI/network commands stubbed."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell launcher')
class LauncherTests(unittest.TestCase):
    def test_existing_service_never_changes_logs_or_requests_shutdown(self):
        for healthy in (True, False):
            with self.subTest(healthy=healthy), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                shutil.copyfile(Path(__file__).resolve().parents[1] / 'start_daily_coolpapers.ps1', root / 'start_daily_coolpapers.ps1')
                logs = root / 'logs'
                logs.mkdir()
                for name in ('startup.log', 'server.stdout.log', 'server.stderr.log', 'current.log'):
                    (logs / name).write_text('sentinel', encoding='utf-8')
                wrapper = root / 'wrapper.ps1'
                wrapper.write_text('''
function Invoke-WebRequest {
    param([switch]$UseBasicParsing, [string]$Uri, [int]$TimeoutSec, [string]$Method)
    if ($Method -eq 'Post') { throw 'UNEXPECTED_SHUTDOWN' }
    if ($Uri -like '*api/health') {
        return @{StatusCode=200; Content='HEALTH_JSON'}
    }
    return @{StatusCode=200; Content='Daily Cool Papers'}
}
function Start-Process { param($FilePath); Write-Output 'OPEN_EXISTING_BROWSER' }
function Read-Host { throw 'UNEXPECTED_PROMPT' }
& (Join-Path $PSScriptRoot 'start_daily_coolpapers.ps1')
exit $LASTEXITCODE
'''.replace('HEALTH_JSON', '{"ok":true,"service":"daily-coolpapers"}' if healthy else '{}'), encoding='utf-8')
                result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(wrapper)],
                                        capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(result.returncode, 0 if healthy else 1, result.stdout + result.stderr)
                self.assertNotIn('UNEXPECTED', result.stdout + result.stderr)
                self.assertEqual(len(list(logs.iterdir())), 4)
                self.assertTrue(all(p.read_text(encoding='utf-8') == 'sentinel' for p in logs.iterdir()))
