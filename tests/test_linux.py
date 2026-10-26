"""Linux: FAT images built with mtools (deletes like Windows does), and NTFS/exFAT
on real loop block devices when sudo works."""
import os
import subprocess
import sys
import unittest

from helpers import IS_LINUX, RecoveryTest, can_sudo, fixtures, have, md5, recovered, run_tool, sh

FOLDER = 'DCIM/Holiday Trip'
# FAT keeps long names in full but loses the first letter of a deleted 8.3 name (DCIM -> _CIM).
FAT_FOLDER = 'Camera Roll/Holiday Trip'


def kernel_fs(name):
    """True if the kernel can mount `name`, loading its module if needed."""
    if not IS_LINUX:
        return False
    with open('/proc/filesystems') as fh:
        if name in fh.read().split():
            return True
    if can_sudo():
        sh(['modprobe', name], root=True, check=False)
    with open('/proc/filesystems') as fh:
        return name in fh.read().split()


MTOOLS_ENV = dict(os.environ, MTOOLS_SKIP_CHECK='1')


def mtools(*args):
    subprocess.run(list(args), check=True, capture_output=True, env=MTOOLS_ENV)


@unittest.skipUnless(IS_LINUX and have('mkfs.vfat', 'mcopy', 'sfdisk'), 'needs dosfstools, mtools, sfdisk')
class LinuxFatImageTests(RecoveryTest):
    def fat_image(self, bits, size_kb=131072, cluster_sectors=8):
        img = self.path(f'fat{bits}.img')
        sh(['mkfs.vfat', '-F', str(bits), '-s', str(cluster_sectors), '-C', img, str(size_kb)])
        return img, img

    def fill(self, target):
        mtools('mmd', '-i', target, '::/Camera Roll', '::/' + FAT_FOLDER)
        mtools('mcopy', '-i', target, *fixtures().values(), '::/' + FAT_FOLDER + '/')

    def test_fat32_deleted_folder_tree(self):
        img, target = self.fat_image(32)
        self.fill(target)
        mtools('mdeltree', '-i', target, '::/Camera Roll')
        out = self.path('out')
        res = self.run_ok(['--source', img, '--out', out])
        self.assertRecovered(out, folder=FAT_FOLDER, output=res.stdout)

    def test_fat16_deleted_files(self):
        img, target = self.fat_image(16, 65536)
        self.fill(target)
        mtools('mdel', '-i', target, '::/' + FAT_FOLDER + '/*')
        out = self.path('out')
        res = self.run_ok(['--source', img, '--out', out])
        self.assertRecovered(out, folder=FAT_FOLDER, output=res.stdout)

    def test_fat32_folder_split_across_clusters(self):
        # With 512-byte clusters the folder's entry list outgrows its first cluster and
        # continues after the files' data, with one long name cut across the gap.
        # Every file must still come back with its name and folder.
        img, target = self.fat_image(32, cluster_sectors=1)
        self.fill(target)
        mtools('mdeltree', '-i', target, '::/Camera Roll')
        out = self.path('out')
        res = self.run_ok(['--source', img, '--out', out])
        self.assertRecovered(out, folder=FAT_FOLDER, output=res.stdout)
        leftovers = [p for ps in recovered(out).values() for p in ps if p.startswith('carved/')]
        self.assertEqual(leftovers, [], 'named copies should replace generic carved ones')

    def test_fat32_orphaned_folder(self):
        img, target = self.fat_image(32)
        self.fill(target)
        mtools('mdeltree', '-i', target, '::/Camera Roll')
        # New entries reuse the deleted 'Camera Roll' slot, so its subfolder can only be found
        # by the scan. The notes are empty so they take no clusters and overwrite no data.
        for i in range(6):
            note = self.path(f'note{i}.txt')
            open(note, 'w').close()
            mtools('mcopy', '-i', target, note, '::/')
        out = self.path('out')
        res = self.run_ok(['--source', img, '--out', out])
        self.assertRecovered(out, output=res.stdout)
        paths = [p for ps in recovered(out).values() for p in ps]
        for name in fixtures():
            self.assertTrue(any('_orphaned_folders/' in p and p.endswith('/Holiday Trip/' + name) for p in paths),
                            f'{name} not restored by name from the orphaned folder: {paths}')

    def test_duplicate_copies_kept_once(self):
        img, target = self.fat_image(32)
        mtools('mmd', '-i', target, '::/A', '::/B')
        for d in ('::/A/', '::/B/'):
            mtools('mcopy', '-i', target, *fixtures().values(), d)
        mtools('mdeltree', '-i', target, '::/A', '::/B')
        out = self.path('out')
        res = self.run_ok(['--source', img, '--out', out])
        self.assertRecovered(out, output=res.stdout)
        self.assertEqual(sum(len(v) for v in recovered(out).values()), len(fixtures()))

    def test_whole_disk_image_with_gpt(self):
        img = self.path('disk.img')
        with open(img, 'wb') as fh:
            fh.truncate(160 << 20)
        subprocess.run(['sfdisk', '-q', img], input='label: gpt\n,,L\n', text=True, check=True)
        dump = sh(['sfdisk', '-d', img]).stdout
        line = [l for l in dump.splitlines() if 'start=' in l][0]
        start = int(line.split('start=')[1].split(',')[0])
        size = int(line.split('size=')[1].split(',')[0])
        sh(['mkfs.vfat', '-F', '32', '--offset', str(start), img, str(size // 2)])
        target = f'{img}@@{start * 512}'
        self.fill(target)
        mtools('mdel', '-i', target, '::/' + FAT_FOLDER + '/*')
        out = self.path('out')
        res = self.run_ok(['--source', img, '--out', out])
        self.assertIn('Using partition', res.stdout)
        self.assertRecovered(out, folder=FAT_FOLDER, output=res.stdout)


@unittest.skipUnless(IS_LINUX and can_sudo() and have('losetup', 'mkntfs'), 'needs sudo, losetup, ntfs-3g')
class LinuxBlockDeviceTests(RecoveryTest):
    def loop(self, img):
        dev = sh(['losetup', '-f', '--show', img], root=True).stdout.strip()
        self.addCleanup(sh, ['losetup', '-d', dev], root=True, check=False)
        return dev

    def mount(self, dev, kind):
        mnt = self.path('mnt')
        os.makedirs(mnt, exist_ok=True)
        if kind == 'ntfs-3g':
            sh(['ntfs-3g', dev, mnt], root=True)
        else:
            sh(['mount', '-t', kind, dev, mnt], root=True)
        self.addCleanup(sh, ['umount', mnt], root=True, check=False)
        return mnt

    def ntfs(self, size_mb=200):
        img = self.path('ntfs.img')
        with open(img, 'wb') as fh:
            fh.truncate(size_mb << 20)
        sh(['mkntfs', '-Q', '-F', '-q', img])
        return img

    def copy_fixtures(self, mnt, folder):
        dest = os.path.join(mnt, *folder.split('/'))
        sh(['mkdir', '-p', dest], root=True)
        sh(['cp', *fixtures().values(), dest + '/'], root=True)
        sh(['sync'])

    def test_ntfs_deleted_folder_keeps_names(self):
        dev = self.loop(self.ntfs())
        mnt = self.mount(dev, 'ntfs-3g')
        folder = 'Users/Me/Pictures/Holiday Trip'
        self.copy_fixtures(mnt, folder)
        sh(['rm', '-rf', os.path.join(mnt, 'Users', 'Me', 'Pictures')], root=True)
        sh(['umount', mnt], root=True)
        out = self.path('out')
        res = self.run_ok(['--source', dev, '--out', out], root=True)
        self.assertRecovered(out, folder=folder, output=res.stdout)

    @unittest.skipUnless(kernel_fs('ntfs3'), 'kernel ntfs3 driver not available')
    def test_ntfs_fragmented_video(self):
        dev = self.loop(self.ntfs())
        mnt = self.mount(dev, 'ntfs3')
        big = self.path('big.avi')
        with open(fixtures()['garden walk.avi'], 'rb') as fh:
            data = fh.read() * 12
        with open(big, 'wb') as fh:
            fh.write(data)
        # Interleave appends with another file so the video ends up in many pieces.
        for k in range(0, len(data), 65536):
            for name, src in (('trip.avi', big), ('other.bin', '/dev/urandom')):
                sh(['dd', f'if={src}', f'of={os.path.join(mnt, name)}', 'bs=65536', 'count=1',
                    f'skip={k // 65536}' if src == big else 'skip=0', 'oflag=append,sync', 'conv=notrunc'],
                   root=True)
        pieces = sh([sys.executable, '-c', (
            'import sys; sys.path.insert(0, %r); import smara_sukma as s\n'
            'fs = s.NTFS(s.Source(%r))\n'
            'for n, r in fs.records():\n'
            '    f, b, names, d, m, a = fs.parse_record(r)\n'
            '    if names and names[0][1] == "trip.avi": print(len(d["runs"]))'
        ) % (os.path.dirname(os.path.dirname(os.path.abspath(__file__))), dev)], root=True).stdout.split()
        self.assertTrue(pieces and int(pieces[0]) > 1, f'video was not fragmented: {pieces}')
        sh(['rm', os.path.join(mnt, 'trip.avi')], root=True)
        sh(['umount', mnt], root=True)
        out = self.path('out')
        res = self.run_ok(['--source', dev, '--out', out, '--mode', 'undelete'], root=True)
        self.assertIn(md5(big), recovered(out), f'fragmented video not recovered intact\n{res.stdout}')

    @unittest.skipUnless(have('mkfs.exfat') and kernel_fs('exfat'), 'needs exfatprogs and kernel exfat')
    def test_exfat_kernel_driver(self):
        img = self.path('exfat.img')
        with open(img, 'wb') as fh:
            fh.truncate(128 << 20)
        sh(['mkfs.exfat', img])
        dev = self.loop(img)
        mnt = self.mount(dev, 'exfat')
        self.copy_fixtures(mnt, FOLDER)
        sh(['rm', '-rf', os.path.join(mnt, 'DCIM')], root=True)
        sh(['umount', mnt], root=True)
        out = self.path('out')
        res = self.run_ok(['--source', dev, '--out', out], root=True)
        self.assertRecovered(out, folder=FOLDER, output=res.stdout)

    def test_mounted_source_refuses_then_unmounts(self):
        dev = self.loop(self.ntfs())
        mnt = self.mount(dev, 'ntfs-3g')
        self.copy_fixtures(mnt, FOLDER)
        sh(['rm', '-rf', os.path.join(mnt, 'DCIM')], root=True)
        sh(['sync'])
        bad = run_tool(['--source', dev, '--out', os.path.join(mnt, 'out'), '--keep-mounted'], root=True)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn('Refusing', bad.stdout + bad.stderr)
        out = self.path('out')
        res = self.run_ok(['--source', mnt, '--out', out], root=True)
        with open('/proc/mounts') as fh:
            self.assertNotIn(f' {mnt} ', fh.read(), 'tool should have unmounted the drive')
        self.assertRecovered(out, folder=FOLDER, output=res.stdout)
