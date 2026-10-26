"""Free-space scan on a raw image built in Python. Runs on every OS."""
import csv
import os
import random

from helpers import RecoveryTest, fixtures, md5
from test_units import make_mpf_jpeg


class CarveTests(RecoveryTest):
    def build_image(self, blobs):
        """Random filler with each blob at a 64 KB-aligned offset."""
        rnd = random.Random(42)
        img = self.path('raw.img')
        with open(img, 'wb') as f:
            f.write(rnd.randbytes(1 << 20) if hasattr(rnd, 'randbytes') else os.urandom(1 << 20))
            for blob in blobs:
                f.write(blob)
                pad = (-f.tell()) % 65536 + 65536
                f.write(os.urandom(pad))
        return img

    def test_scan_recovers_every_type_and_drops_duplicates(self):
        blobs = []
        for path in fixtures().values():
            with open(path, 'rb') as fh:
                blobs.append(fh.read())
        blobs.append(blobs[0])  # a second deleted copy of the same photo
        img = self.build_image(blobs)
        out = self.path('out')
        res = self.run_ok(['--source', img, '--out', out, '--mode', 'carve'])
        self.assertRecovered(out, output=res.stdout)
        with open(os.path.join(out, 'report.csv'), encoding='utf-8') as fh:
            statuses = [row['status'] for row in csv.DictReader(fh)]
        self.assertEqual(statuses.count('duplicate'), 1, statuses)

    def test_scan_keeps_multi_picture_jpeg_whole(self):
        with open(fixtures()['beach sunset.jpg'], 'rb') as fh:
            main = fh.read()
        mpf, _ = make_mpf_jpeg(main, main)
        img = self.build_image([mpf])
        out = self.path('out')
        self.run_ok(['--source', img, '--out', out, '--mode', 'carve'])
        mpf_path = self.path('mpf.jpg')
        with open(mpf_path, 'wb') as fh:
            fh.write(mpf)
        found = [p for root, _, files in os.walk(out) for p in (os.path.join(root, f) for f in files)
                 if p.endswith('.jpg')]
        self.assertIn(md5(mpf_path), [md5(p) for p in found])

    def test_resume_continues_after_stop(self):
        img = self.build_image([b'filler'])
        out = self.path('out')
        os.makedirs(out)
        with open(os.path.join(out, '.carve_position'), 'w') as fh:
            fh.write(str(os.path.getsize(img) // 2))
        res = self.run_ok(['--source', img, '--out', out, '--mode', 'carve', '--resume'])
        self.assertIn('Resuming from', res.stdout)
