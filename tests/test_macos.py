"""macOS: disk images made with hdiutil, plus real raw-device access when sudo works."""
import os
import re
import shutil
import unittest

from helpers import IS_MAC, RecoveryTest, can_sudo, fixtures, have, run_tool, sh

FOLDER = 'DCIM/Holiday Trip'


@unittest.skipUnless(IS_MAC and have('hdiutil'), 'needs macOS hdiutil')
class MacDriveTests(RecoveryTest):
    def make_image(self, name, fs, layout='NONE', delete_folder=True, keep_attached=False):
        """Create an image, copy the fixtures into DCIM/Holiday Trip, delete them, detach."""
        img, mnt = self.path(name + '.dmg'), self.path('mnt_' + name)
        sh(['hdiutil', 'create', '-quiet', '-size', '64m', '-fs', fs, '-layout', layout,
            '-volname', 'SMARA', img])
        sh(['hdiutil', 'attach', '-quiet', '-nobrowse', img, '-mountpoint', mnt])
        folder = os.path.join(mnt, *FOLDER.split('/'))
        os.makedirs(folder)
        for path in fixtures().values():
            shutil.copyfile(path, os.path.join(folder, os.path.basename(path)))
        os.sync()
        if delete_folder:
            shutil.rmtree(os.path.join(mnt, 'DCIM'))
        else:
            # macOS's FAT driver blanks a folder's records when the folder itself is deleted,
            # so for FAT only the files are deleted (as a camera or Windows would leave it).
            for n in fixtures():
                os.remove(os.path.join(folder, n))
        os.sync()
        if keep_attached:
            self.addCleanup(sh, ['hdiutil', 'detach', '-force', mnt], check=False)
            return img, mnt
        sh(['hdiutil', 'detach', '-quiet', mnt])
        return img

    def attach_raw(self, img):
        """Attach without mounting and return the whole-disk name, e.g. disk7."""
        out = sh(['hdiutil', 'attach', '-nomount', '-nobrowse', img]).stdout
        disk = re.search(r'/dev/(disk\d+)\b', out).group(1)
        self.addCleanup(sh, ['hdiutil', 'detach', '-force', disk], check=False)
        return disk

    def test_exfat_deleted_folder(self):
        img = self.make_image('exfat', 'ExFAT')
        out = self.path('out')
        res = self.run_ok(['--source', img, '--out', out])
        self.assertRecovered(out, folder=FOLDER, output=res.stdout)

    def test_fat32_deleted_files(self):
        img = self.make_image('fat32', 'MS-DOS FAT32', delete_folder=False)
        out = self.path('out')
        res = self.run_ok(['--source', img, '--out', out])
        self.assertRecovered(out, folder=FOLDER, output=res.stdout)

    def test_whole_disk_image_with_gpt(self):
        img = self.make_image('gpt', 'ExFAT', layout='GPTSPUD')
        out = self.path('out')
        res = self.run_ok(['--source', img, '--out', out])
        self.assertIn('Using partition', res.stdout)
        self.assertRecovered(out, folder=FOLDER, output=res.stdout)

    @unittest.skipUnless(can_sudo(), 'needs passwordless sudo for raw disk access')
    def test_raw_device_whole_disk_and_partition(self):
        img = self.make_image('raw', 'ExFAT', layout='GPTSPUD')
        disk = self.attach_raw(img)
        for source in (disk, disk + 's1'):
            out = self.path('out_' + source)
            res = self.run_ok(['--source', source, '--out', out], root=True)
            self.assertRecovered(out, folder=FOLDER, output=res.stdout)

    @unittest.skipUnless(can_sudo(), 'needs passwordless sudo for raw disk access')
    def test_mounted_volume_refuses_then_unmounts(self):
        img, mnt = self.make_image('mounted', 'ExFAT', keep_attached=True)
        bad = run_tool(['--source', mnt, '--out', os.path.join(mnt, 'out'), '--keep-mounted'], root=True)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn('Refusing', bad.stdout + bad.stderr)
        out = self.path('out')
        res = self.run_ok(['--source', mnt, '--out', out], root=True)
        self.assertNotIn(mnt, sh(['mount']).stdout, 'tool should have unmounted the volume')
        self.assertRecovered(out, folder=FOLDER, output=res.stdout)
