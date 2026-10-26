"""Fast checks of individual parsers. Run on every OS, no tools or admin rights needed."""
import os
import struct
import tempfile
import unittest

from helpers import IS_WIN, fixtures, smara_sukma as ss


class MemSource:
    """In-memory stand-in for smara_sukma.Source."""

    def __init__(self, data):
        self.data, self.size, self.align, self.bad_reads = data, len(data), 512, 0

    def read(self, off, n):
        return self.data[off:off + n] if off >= 0 else b''


def make_mpf_jpeg(main, extra, gap=777):
    """Phone-style Multi-Picture JPEG: main image + MPF index + padding + extra image."""
    # APP2 'MPF\0' + TIFF header + IFD with one MPEntry tag holding 2 entries.
    ifd_off, entries_off = 8, 8 + 2 + 12 + 4
    def build(main_size, extra_off):
        tiff = b'II*\0' + struct.pack('<I', ifd_off)
        tiff += struct.pack('<H', 1) + struct.pack('<HHII', 0xB002, 7, 32, entries_off) + b'\0' * 4
        tiff += struct.pack('<III', 0x20030000, main_size, 0) + b'\0' * 4
        tiff += struct.pack('<III', 0x00010001, len(extra), extra_off) + b'\0' * 4
        body = b'MPF\0' + tiff
        return b'\xff\xd8' + b'\xff\xe2' + struct.pack('>H', len(body) + 2) + body + main[2:]
    first = build(0, 0)
    base = 2 + 4 + 4  # SOI + APP2 marker/length + 'MPF\0' -> TIFF header offset
    main_size = len(first)
    return build(main_size, main_size + gap - base) + os.urandom(gap) + extra, main_size


class UnitTests(unittest.TestCase):
    def test_ntfs_runlist(self):
        a = bytearray(64)
        struct.pack_into('<H', a, 32, 64)
        a += bytes([0x11, 16, 100,            # 16 clusters at LCN 100
                    0x11, 8, 0xCE,            # 8 clusters at 100 - 50 = 50
                    0x01, 4,                  # 4 sparse clusters
                    0x22, 0x00, 0x02, 0xE8, 0x03,  # 512 clusters at 50 + 1000
                    0x00])
        self.assertEqual(ss.ntfs_runlist(bytes(a)), [(100, 16), (50, 8), (None, 4), (1050, 512)])

    def test_lfn_checksum_matches_real_volume(self):
        # From a real FAT32 volume: '.fseventsd' long name, 8.3 alias FSEVEN~1, checksum 0xDA.
        self.assertEqual(ss.lfn_checksum(b'FSEVEN~1   '), 0xDA)

    def test_sniff_ext_on_fixtures(self):
        want = {'jpg': 'jpg', 'png': 'png', 'mp4': 'mp4', 'mov': 'mov', 'avi': 'avi', 'heic': 'heic'}
        for name, path in fixtures().items():
            with open(path, 'rb') as f:
                head = f.read(16)
            self.assertEqual(ss.sniff_ext(head), want[ss.ext_of(name)], name)
            self.assertIs(ss.header_ok(ss.ext_of(name), head), True, name)

    def test_bitmap(self):
        bm = ss.Bitmap(bytes([0xFF, 0xFF, 0x0F, 0x00]), 2)  # clusters 2..21 set
        self.assertTrue(bm.all(2, 22))
        self.assertFalse(bm.all(2, 23))
        self.assertTrue(bm.any(20, 30))
        self.assertFalse(bm.any(22, 34))
        self.assertEqual(bm.used(), 20)

    def test_mbr_and_gpt_partitions(self):
        mbr = bytearray(512)
        mbr[446 + 4] = 0x07
        struct.pack_into('<II', mbr, 446 + 8, 2048, 4096)
        mbr[510:512] = b'\x55\xaa'
        self.assertEqual(ss.partitions(MemSource(bytes(mbr) + bytes(4 << 20))),
                         [(2048 * 512, 4096 * 512, 'MBR type 0x07')])

        disk = bytearray(4 << 20)
        disk[446 + 4] = 0xEE
        disk[510:512] = b'\x55\xaa'
        hdr = bytearray(92)
        hdr[:8] = b'EFI PART'
        struct.pack_into('<QII', hdr, 72, 2, 128, 128)
        disk[512:604] = hdr
        entry = bytearray(128)
        entry[:16] = b'\x01' * 16
        struct.pack_into('<QQ', entry, 32, 2048, 6143)
        entry[56:68] = 'Photos'.encode('utf-16le')
        disk[1024:1152] = entry
        self.assertEqual(ss.partitions(MemSource(bytes(disk))), [(2048 * 512, 4096 * 512, 'Photos')])

    def test_multi_picture_jpeg_keeps_extra_images(self):
        f = fixtures()
        with open(f['beach sunset.jpg'], 'rb') as fh:
            main = fh.read()
        mpf, _ = make_mpf_jpeg(main, main[:])
        img = os.urandom(4096) + mpf + os.urandom(4096)
        w = ss.Window(MemSource(img))
        self.assertEqual(ss.carve_jpeg(w, 4096, 50 << 20), (len(mpf), 'jpg'))

    def test_partition_slice(self):
        src = MemSource(bytes(range(256)) * 16)
        part = ss.Partition(src, 1024, 512)
        self.assertEqual(part.read(0, 4), src.data[1024:1028])
        self.assertEqual(part.read(510, 10), src.data[1534:1536])

    def test_source_reads_unaligned_ranges(self):
        data = os.urandom(10000)
        fd, path = tempfile.mkstemp()
        os.write(fd, data)
        os.close(fd)
        try:
            src = ss.Source(path)
            for off, n in [(0, 10), (511, 2), (1000, 3000), (9990, 50)]:
                self.assertEqual(src.read(off, n), data[off:off + n])
            os.close(src.fd)
        finally:
            os.remove(path)

    @unittest.skipUnless(IS_WIN, 'Windows file-name rules')
    def test_windows_names(self):
        self.assertEqual(ss.safe_name('CON.jpg'), '_CON.jpg')
        self.assertEqual(ss.safe_name('a:b?c*.jpg'), 'a_b_c_.jpg')
        self.assertEqual(ss.resolve_source('e:')[0], '\\\\.\\E:')
        self.assertTrue(ss.fs_path('C:\\' + 'x' * 300).startswith('\\\\?\\'))


if __name__ == '__main__':
    unittest.main()
