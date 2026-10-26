"""Shared helpers for the Smara Sukma test suite (standard library only)."""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, 'smara_sukma.py')
FIXTURES = os.path.join(ROOT, 'tests', 'fixtures')

sys.path.insert(0, ROOT)
import smara_sukma  # noqa: E402

__all__ = ['ROOT', 'TOOL', 'FIXTURES', 'IS_WIN', 'IS_MAC', 'IS_LINUX', 'smara_sukma', 'md5', 'fixtures',
           'is_root', 'can_sudo', 'have', 'sh', 'run_tool', 'recovered', 'RecoveryTest']

IS_WIN = os.name == 'nt'
IS_MAC = sys.platform == 'darwin'
IS_LINUX = sys.platform.startswith('linux')


def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def fixtures():
    """{file name: path} of the test photos and videos."""
    return {n: os.path.join(FIXTURES, n) for n in sorted(os.listdir(FIXTURES)) if not n.startswith('.')}


def is_root():
    return not IS_WIN and os.geteuid() == 0


def can_sudo():
    if IS_WIN:
        return False
    if is_root():
        return True
    return shutil.which('sudo') is not None and \
        subprocess.run(['sudo', '-n', 'true'], capture_output=True).returncode == 0


def have(*commands):
    return all(shutil.which(c) for c in commands)


def sh(cmd, root=False, check=True, **kw):
    """Run a shell command, with sudo when root is needed and we aren't root."""
    if root and not is_root():
        cmd = ['sudo', '-n'] + cmd
    return subprocess.run(cmd, check=check, capture_output=True, text=True, **kw)


def run_tool(args, root=False):
    cmd = [sys.executable, TOOL, '-y'] + args
    if root and not is_root():
        cmd = ['sudo', '-n', '--preserve-env=PYTHONIOENCODING'] + cmd
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    return subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                          errors='replace', env=env, timeout=900)


def recovered(out):
    """{md5: [relative paths]} of every file the tool saved."""
    found = {}
    for root, _, files in os.walk(out):
        for f in files:
            if f.startswith('.') or f == 'report.csv':
                continue
            p = os.path.join(root, f)
            found.setdefault(md5(p), []).append(os.path.relpath(p, out).replace(os.sep, '/'))
    return found


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='smara-test-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def run_ok(self, args, root=False):
        res = run_tool(args, root=root)
        self.assertEqual(res.returncode, 0, f'tool failed:\n{res.stdout}\n{res.stderr}')
        return res

    def assertRecovered(self, out, folder=None, names=None, output=''):
        """Every fixture must come back byte-identical. With `folder`, each must
        also be restored by name at undeleted/<folder>/<name>."""
        got = recovered(out)
        names = names or fixtures()
        for name, src in names.items():
            paths = got.get(md5(src), [])
            self.assertTrue(paths, f'{name} not recovered byte-identical.\nFound: {got}\n{output}')
            if folder is not None:
                want = f'undeleted/{folder}/{name}'.replace('//', '/')
                self.assertIn(want, paths, f'{name} not restored by name.\nFound: {paths}\n{output}')
