"""Windows: real virtual hard disks created with diskpart, formatted NTFS / FAT32 / exFAT,
then read by drive letter and by physical disk number. Needs an Administrator prompt."""
import os
import shutil
import subprocess
import tempfile
import unittest

from helpers import IS_WIN, RecoveryTest, fixtures, run_tool, smara_sukma

FOLDER = 'DCIM/Holiday Trip'


def diskpart(script, check=True):
    fd, path = tempfile.mkstemp(suffix='.txt')
    with os.fdopen(fd, 'w') as fh:
        fh.write(script)
    try:
        res = subprocess.run(['diskpart', '/s', path], capture_output=True, text=True)
        if check and res.returncode:
            raise RuntimeError(f'diskpart failed:\n{script}\n{res.stdout}\n{res.stderr}')
        return res
    finally:
        os.remove(path)


def powershell(cmd):
    return subprocess.run(['powershell', '-NoProfile', '-Command', cmd],
                          capture_output=True, text=True, check=True).stdout.strip()


def free_letter():
    for letter in 'RSTUVWXYZ':
        if not os.path.exists(f'{letter}:\\'):
            return letter
    raise RuntimeError('no free drive letter')


@unittest.skipUnless(IS_WIN, 'Windows only')
class WindowsDriveTests(RecoveryTest):
    def setUp(self):
        super().setUp()
        if not smara_sukma.is_admin():
            self.skipTest('needs an Administrator prompt')

    def make_drive(self, fs):
        """Attach a new virtual disk, format it, copy the fixtures in, then delete them."""
        vhd, letter = self.path(f'{fs}.vhd'), free_letter()
        diskpart(f'create vdisk file="{vhd}" maximum=256 type=expandable\n'
                 f'select vdisk file="{vhd}"\nattach vdisk\nconvert mbr\n'
                 f'create partition primary\nformat fs={fs} quick label=SMARA\nassign letter={letter}\n')
        self.addCleanup(diskpart, f'select vdisk file="{vhd}"\ndetach vdisk\n', False)
        root = f'{letter}:\\'
        folder = os.path.join(root, *FOLDER.split('/'))
        os.makedirs(folder)
        for path in fixtures().values():
            shutil.copyfile(path, os.path.join(folder, os.path.basename(path)))
        powershell(f'Write-VolumeCache -DriveLetter {letter}')
        shutil.rmtree(os.path.join(root, 'DCIM'))
        powershell(f'Write-VolumeCache -DriveLetter {letter}')
        disk = int(powershell(f"(Get-DiskImage -ImagePath '{vhd}' | Get-Disk).Number"))
        return letter, disk

    def check_drive_letter(self, fs):
        letter, _ = self.make_drive(fs)
        out = self.path('out')
        res = self.run_ok(['--source', f'{letter}:', '--out', out])
        self.assertRecovered(out, folder=FOLDER, output=res.stdout)
        return res

    def test_ntfs_drive_letter(self):
        res = self.check_drive_letter('ntfs')
        self.assertIn('NTFS volume', res.stdout)

    def test_fat32_drive_letter(self):
        res = self.check_drive_letter('fat32')
        self.assertIn('FAT32 volume', res.stdout)

    def test_exfat_drive_letter(self):
        res = self.check_drive_letter('exfat')
        self.assertIn('exFAT volume', res.stdout)

    def test_physical_drive_with_partition_table(self):
        _, disk = self.make_drive('ntfs')
        out = self.path('out')
        res = self.run_ok(['--source', f'PhysicalDrive{disk}', '--partition', '1', '--out', out])
        self.assertIn('Using partition 1', res.stdout)
        self.assertRecovered(out, folder=FOLDER, output=res.stdout)

    def test_refuses_output_on_source_drive(self):
        letter, _ = self.make_drive('ntfs')
        res = run_tool(['--source', f'{letter}:', '--out', f'{letter}:\\out', '--keep-mounted'])
        self.assertNotEqual(res.returncode, 0)
        self.assertIn('Refusing', res.stdout + res.stderr)

    def test_lists_drives(self):
        smara_sukma.list_drives()  # must not raise on a real Windows machine


if __name__ == '__main__':
    unittest.main()
