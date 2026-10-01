import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from single_instance import AlreadyRunningError, SingleInstance


@unittest.skipUnless(os.name == 'posix', 'Linux/POSIX process lock')
class SingleInstanceTests(unittest.TestCase):
    def test_second_process_rejected_and_lock_released(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'config.json'
            code = 'from pathlib import Path; from single_instance import SingleInstance; import sys\nwith SingleInstance(Path(sys.argv[1])): pass'
            with SingleInstance(config):
                result = subprocess.run([sys.executable, '-c', code, str(config)], capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b'AlreadyRunningError', result.stderr)
            result = subprocess.run([sys.executable, '-c', code, str(config)], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0)
            self.assertTrue(config.with_name('config.json.lock').exists())

    def test_distinct_configs_are_independent_and_exception_releases_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            first, second = Path(folder) / 'a.json', Path(folder) / 'b.json'
            with SingleInstance(first), SingleInstance(second):
                with self.assertRaises(AlreadyRunningError):
                    with SingleInstance(first):
                        pass
            with self.assertRaises(RuntimeError):
                with SingleInstance(first):
                    raise RuntimeError('test')
            with SingleInstance(first):
                pass

    def test_abrupt_process_exit_releases_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'config.json'
            code = 'from pathlib import Path; from single_instance import SingleInstance; import sys,os\nwith SingleInstance(Path(sys.argv[1])): os._exit(0)'
            result = subprocess.run([sys.executable, '-c', code, str(config)], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0)
            with SingleInstance(config):
                pass

    def test_symlinked_config_uses_the_same_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'target.json'
            target.write_text('{}')
            link = Path(folder) / 'alias.json'
            link.symlink_to(target)
            with SingleInstance(target):
                with self.assertRaises(AlreadyRunningError):
                    with SingleInstance(link):
                        pass
