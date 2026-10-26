#!/usr/bin/env python3
"""
Smara Sukma — photo & video recovery, strictly read-only on the source.
(Malay from Sanskrit: smara "memory", sukma "soul". Bringing back what was lost.)
Runs on macOS, Linux and Windows with plain Python 3.8+ (no extra packages).

Two passes:
  1. undelete  – reads the filesystem's own records of deleted files and
                 restores them with original names, folders and dates.
                 Supports exFAT, FAT12/16/32 and NTFS.
  2. carve     – scans free space for photo/video signatures (JPEG, PNG,
                 HEIC/AVIF, MP4/MOV/3GP/CR3, AVI, WebP) and rebuilds files
                 even when no record survives (any filesystem, or after a format).

The source is opened read-only and nothing is ever written to it. Recovered
files must go to a DIFFERENT drive.

Usage (raw disks need admin rights):
  macOS / Linux:  sudo python3 smara_sukma.py           # guided mode
  Windows:        python smara_sukma.py                 # from an Administrator prompt
  Any OS:         python3 smara_sukma.py --source disk.img --out out
"""
import argparse
import array
import csv
import datetime
import errno
import hashlib
import os
import plistlib
import re
import stat
import struct
import subprocess
import sys
import time

IS_WIN = os.name == 'nt'
IS_MAC = sys.platform == 'darwin'
IS_LINUX = sys.platform.startswith('linux')
if not IS_WIN:
    import fcntl

SECTOR = 512
MB = 1 << 20

MEDIA_EXT = {
    'jpg', 'jpeg', 'jpe', 'heic', 'heif', 'avif', 'png', 'gif', 'bmp', 'tif', 'tiff', 'webp',
    'dng', 'cr2', 'cr3', 'nef', 'arw', 'raf', 'orf', 'rw2', 'srw', 'pef',
    'mp4', 'mov', 'm4v', '3gp', '3g2', 'avi', 'mkv', 'mts', 'm2ts', 'wmv', 'mpg', 'mpeg',
    'insv', 'insp', 'lrv', 'webm',
}


def u16(b, o): return struct.unpack_from('<H', b, o)[0]
def u32(b, o): return struct.unpack_from('<I', b, o)[0]
def u64(b, o): return struct.unpack_from('<Q', b, o)[0]
def be32(b, o): return struct.unpack_from('>I', b, o)[0]
def be64(b, o): return struct.unpack_from('>Q', b, o)[0]


def human(n):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{n:.1f} {unit}' if unit != 'B' else f'{n} B'
        n /= 1024


def ext_of(name):
    return name.rsplit('.', 1)[-1].lower() if '.' in name else ''


def is_media(name):
    return ext_of(name) in MEDIA_EXT


def popcount(b):
    n = int.from_bytes(b, 'little')
    return n.bit_count() if hasattr(n, 'bit_count') else bin(n).count('1')


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True).stdout
    except (FileNotFoundError, OSError):
        return ''


# --------------------------------------------------------------------------
# Platform: finding, opening and unmounting drives
# --------------------------------------------------------------------------

if IS_WIN:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _k32.DeviceIoControl.restype = wintypes.BOOL
    _k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                     wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                     wintypes.LPVOID]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _INVALID_HANDLE = wintypes.HANDLE(-1).value
    IOCTL_DISK_GET_LENGTH_INFO = 0x7405C
    IOCTL_DISK_GET_DRIVE_GEOMETRY = 0x70000
    IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x2D1080
    FSCTL_LOCK_VOLUME = 0x90018
    FSCTL_DISMOUNT_VOLUME = 0x90020

    def win_open(path):
        # GENERIC_READ only, shared read/write, OPEN_EXISTING.
        h = _k32.CreateFileW(path, 0x80000000, 3, None, 3, 0, None)
        if h == _INVALID_HANDLE:
            err = ctypes.get_last_error()
            if err == 5:
                raise PermissionError(f'Access denied opening {path}')
            raise ctypes.WinError(err)
        return h

    def win_ioctl(h, code, outsize=0):
        out = ctypes.create_string_buffer(outsize) if outsize else None
        got = wintypes.DWORD()
        if not _k32.DeviceIoControl(h, code, None, 0, out, outsize, ctypes.byref(got), None):
            return None
        return out.raw[:got.value] if out else b''

    def win_disk_number(path):
        """Physical disk number holding a drive letter or \\\\.\\X: volume."""
        m = re.match(r'(?i)^(?:\\\\[.?]\\)?([a-z]):', path)
        if not m:
            m2 = re.search(r'(?i)physicaldrive(\d+)', path)
            return int(m2.group(1)) if m2 else None
        try:
            h = win_open(f'\\\\.\\{m.group(1)}:')
        except OSError:
            return None
        try:
            info = win_ioctl(h, IOCTL_STORAGE_GET_DEVICE_NUMBER, 12)
            return u32(info, 4) if info else None
        finally:
            _k32.CloseHandle(h)


def diskutil_info(target):
    try:
        out = subprocess.run(['diskutil', 'info', '-plist', target], capture_output=True, check=True).stdout
        return plistlib.loads(out)
    except (subprocess.CalledProcessError, FileNotFoundError, plistlib.InvalidFileException):
        return None


def _proc_mounts():
    try:
        with open('/proc/mounts') as f:
            for line in f:
                dev, mp = line.split()[:2]
                unescape = lambda s: re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), s)
                yield unescape(dev), unescape(mp)
    except OSError:
        return


def linux_mountpoint(dev):
    real = os.path.realpath(dev)
    for d, mp in _proc_mounts():
        if d.startswith('/dev/') and os.path.realpath(d) == real:
            return mp
    return None


def resolve_source(src):
    """Turn what the user typed into (path to open, mount point or None)."""
    if os.path.isfile(src):
        return src, None
    if IS_WIN:
        m = re.fullmatch(r'([A-Za-z]):?\\?', src)
        if m:
            letter = m.group(1).upper()
            return f'\\\\.\\{letter}:', f'{letter}:\\'
        m = re.fullmatch(r'(?i)(?:\\\\\.\\)?physicaldrive(\d+)', src)
        if m:
            return f'\\\\.\\PhysicalDrive{m.group(1)}', None
        if src.startswith('\\\\.\\'):
            return src, None
        sys.exit(f'Cannot find {src!r}. Use a drive letter like E: or PhysicalDrive1.')
    if IS_MAC:
        info = diskutil_info(src)
        if not info:
            sys.exit(f'Cannot find a disk or image at {src!r}.')
        return f'/dev/r{info["DeviceIdentifier"]}', info.get('MountPoint') or None
    # Linux and other Unix
    if os.path.isdir(src) and not src.startswith('/dev'):
        mp = os.path.realpath(src)
        for dev, m in _proc_mounts():
            if m == mp and dev.startswith('/dev/'):
                return dev, mp
        sys.exit(f'{src!r} is a folder, not a mounted drive. Give the drive itself, e.g. /dev/sdb1.')
    path = src if src.startswith('/dev/') else '/dev/' + src
    if not os.path.exists(path):
        sys.exit(f'Cannot find {path!r}. Run "lsblk" to see drive names.')
    return path, linux_mountpoint(path)


def whole_disks(path):
    """Identifiers of the physical disk(s) a file path or device lives on."""
    if IS_WIN:
        n = win_disk_number(path)
        return {n} if n is not None else set()
    if IS_MAC:
        if not path.startswith('/dev/'):
            dev = run(['df', path]).splitlines()[-1:]
            path = dev[0].split()[0] if dev else ''
        info = diskutil_info(path.replace('/dev/r', '/dev/')) if path else None
        if not info:
            return set()
        disks = {info.get('ParentWholeDisk')}
        for store in info.get('APFSPhysicalStores', []):
            disks.add(re.sub(r's\d+$', '', store.get('APFSPhysicalStore', '')))
        return {d for d in disks if d}
    if not path.startswith('/dev/'):
        out = run(['df', '--output=source', path]).split()
        path = out[-1] if out else ''
    if not path.startswith('/dev/'):
        return set()
    rows = run(['lsblk', '-nrso', 'NAME,TYPE', path]).split('\n')
    return {r.split()[0] for r in rows if r.split()[1:2] == ['disk']}


def output_on_source(out, src_path, mountpoint):
    real = os.path.realpath(out)
    if mountpoint:
        mp = os.path.realpath(mountpoint)
        if IS_WIN:
            if os.path.splitdrive(real)[0].upper() == os.path.splitdrive(mp)[0].upper():
                return True
        elif real == mp or real.startswith(mp.rstrip('/') + '/'):
            return True
    if os.path.isfile(src_path):
        return False  # disk image: writing next to it is harmless
    existing = real
    while not os.path.exists(existing):
        existing = os.path.dirname(existing)
    return bool(whole_disks(src_path) & whole_disks(existing))


def unmount(mountpoint):
    if IS_MAC:
        return subprocess.run(['diskutil', 'unmount', mountpoint]).returncode == 0
    if IS_LINUX:
        return subprocess.run(['umount', mountpoint]).returncode == 0
    return False


def remount_hint(path, mountpoint):
    if IS_MAC:
        return f'Re-mount the drive later with:  diskutil mount {path.replace("/dev/r", "")}'
    if IS_LINUX:
        return f'Re-mount the drive later with:  sudo mount {path} {mountpoint}  (or unplug and replug it)'
    return 'Windows re-mounts the drive automatically now that the tool has closed it.'


def is_admin():
    if IS_WIN:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return False
    return os.geteuid() == 0


def permission_hint(path):
    if IS_WIN:
        return (f'Access denied opening {path}. Right-click "Smara Sukma.bat" and choose '
                '"Run as administrator", or start Command Prompt as administrator.')
    if IS_MAC:
        return (f'Permission denied opening {path}. Run with sudo (and give Terminal Full Disk '
                'Access in System Settings → Privacy & Security if it still fails).')
    return f'Permission denied opening {path}. Run with sudo.'


def list_drives():
    print('Drives on this computer:\n')
    if IS_MAC:
        print(run(['diskutil', 'list', 'external']) or run(['diskutil', 'list']))
        print('Enter a volume like /Volumes/NAME or a partition like disk6s1.')
    elif IS_LINUX:
        print(run(['lsblk', '-o', 'NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT']))
        print('Enter a partition like sdb1 (or /dev/sdb1).')
    elif IS_WIN:
        mask = _k32.GetLogicalDrives()
        kinds = {2: 'removable', 3: 'fixed', 4: 'network', 5: 'cd/dvd', 6: 'ramdisk'}
        for i in range(26):
            if not mask & (1 << i):
                continue
            root = f'{chr(65 + i)}:\\'
            label, fs = ctypes.create_unicode_buffer(261), ctypes.create_unicode_buffer(261)
            _k32.GetVolumeInformationW(root, label, 261, None, None, None, fs, 261)
            total = ctypes.c_ulonglong(0)
            _k32.GetDiskFreeSpaceExW(root, None, ctypes.byref(total), None)
            kind = kinds.get(_k32.GetDriveTypeW(root), '?')
            print(f'  {root[:2]}  {human(total.value):>10}  {fs.value or "?":6}  {kind:9}  {label.value}')
        print('\nEnter a drive letter like E:')


def default_output():
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M')
    if IS_WIN:
        home = os.environ.get('USERPROFILE', os.path.expanduser('~'))
    else:
        home = os.path.expanduser(f'~{os.environ.get("SUDO_USER", "")}')
    desktop = os.path.join(home, 'Desktop')
    return os.path.join(desktop if os.path.isdir(desktop) else home, f'Recovered-{stamp}')


_BAD_CHARS = re.compile(r'[<>:"|?*\\\x00-\x1f]' if IS_WIN else r'\x00')
_RESERVED = {'CON', 'PRN', 'AUX', 'NUL'} | {f'{p}{i}' for p in ('COM', 'LPT') for i in range(1, 10)}


def safe_name(name):
    name = _BAD_CHARS.sub('_', name)
    if IS_WIN:
        name = name.rstrip(' .')
        if name.split('.')[0].upper() in _RESERVED:
            name = '_' + name
    return name or '_'


def fs_path(p):
    """Windows needs the \\\\?\\ prefix for paths over 260 characters."""
    if IS_WIN and len(p) > 240 and not p.startswith('\\\\?\\'):
        return '\\\\?\\' + os.path.abspath(p)
    return p


def chown_to_user(path):
    if IS_WIN or os.geteuid() != 0 or not os.environ.get('SUDO_UID'):
        return
    uid, gid = int(os.environ['SUDO_UID']), int(os.environ.get('SUDO_GID', -1))
    for root, dirs, files in os.walk(path):
        for n in dirs + files:
            try:
                os.lchown(os.path.join(root, n), uid, gid)
            except OSError:
                pass
    os.lchown(path, uid, gid)


# --------------------------------------------------------------------------
# Read-only source (raw device, partition or image file)
# --------------------------------------------------------------------------

class Source:
    def __init__(self, path, lock=False):
        self.path = path
        self.bad_reads = 0
        self.align = SECTOR
        self.locked = False
        if IS_WIN and path.startswith('\\\\.\\'):
            h = win_open(path)
            if lock:
                self.locked = (win_ioctl(h, FSCTL_LOCK_VOLUME) is not None
                               and win_ioctl(h, FSCTL_DISMOUNT_VOLUME) is not None)
            info = win_ioctl(h, IOCTL_DISK_GET_LENGTH_INFO, 8)
            self.size = u64(info, 0) if info else 0
            geo = win_ioctl(h, IOCTL_DISK_GET_DRIVE_GEOMETRY, 24)
            if geo:
                self.align = max(SECTOR, u32(geo, 20))
            self.fd = msvcrt.open_osfhandle(h, os.O_RDONLY | os.O_BINARY)
            return
        self.fd = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0))
        st = os.fstat(self.fd)
        if stat.S_ISREG(st.st_mode):
            self.size = st.st_size
        else:
            self.size = self._device_size()

    def _device_size(self):
        if IS_MAC:
            try:
                bs = struct.unpack('<I', fcntl.ioctl(self.fd, 0x40046418, b'\0' * 4))[0]  # DKIOCGETBLOCKSIZE
                bc = struct.unpack('<Q', fcntl.ioctl(self.fd, 0x40086419, b'\0' * 8))[0]  # DKIOCGETBLOCKCOUNT
                self.align = max(SECTOR, bs)
                return bs * bc
            except OSError:
                return 0
        if IS_LINUX:
            try:
                self.align = max(SECTOR, struct.unpack('i', fcntl.ioctl(self.fd, 0x1268, b'\0' * 4))[0])  # BLKSSZGET
            except OSError:
                pass
        try:
            size = os.lseek(self.fd, 0, os.SEEK_END)
            os.lseek(self.fd, 0, os.SEEK_SET)
            return size
        except OSError:
            return 0

    def _raw_read(self, n, off):
        if hasattr(os, 'pread'):
            return os.pread(self.fd, n, off)
        os.lseek(self.fd, off, os.SEEK_SET)
        return os.read(self.fd, n)

    def _pread(self, n, off):
        try:
            return self._raw_read(n, off)
        except OSError:
            # Unreadable area: retry in small pieces, zero-filling bad ones.
            out = bytearray()
            step = max(64 * 1024, self.align)
            for p in range(off, off + n, step):
                k = min(step, off + n - p)
                try:
                    out += self._raw_read(k, p)
                except OSError:
                    self.bad_reads += 1
                    out += b'\0' * k
            return bytes(out)

    def read(self, off, n):
        """Read n bytes at off. Raw disks need sector-aligned I/O, so align here."""
        if n <= 0 or off < 0:
            return b''
        if self.size:
            n = min(n, self.size - off)
            if n <= 0:
                return b''
        a = self.align
        a0 = off - off % a
        a1 = -(-(off + n) // a) * a
        out = bytearray()
        pos = a0
        while pos < a1:
            b = self._pread(min(a1 - pos, 16 * MB), pos)
            if not b:
                break
            out += b
            pos += len(b)
        return bytes(out[off - a0: off - a0 + n])


class Partition:
    """A slice of a Source, so a whole disk or disk image can be read per partition."""

    def __init__(self, src, start, size):
        self.src, self.start, self.size, self.align = src, start, size, src.align

    @property
    def bad_reads(self):
        return self.src.bad_reads

    def read(self, off, n):
        n = min(n, self.size - off)
        return self.src.read(self.start + off, n) if n > 0 and off >= 0 else b''


def partitions(src):
    """List (start, size, description) from a GPT or MBR partition table."""
    s0 = src.read(0, 512)
    if s0[510:512] != b'\x55\xaa':
        return []
    for lba in (512, 4096):
        h = src.read(lba, 92)
        if h[:8] != b'EFI PART':
            continue
        ent_lba, count, size = u64(h, 72), u32(h, 80), u32(h, 84)
        if not 128 <= size <= 1024 or count > 1024:
            return []
        table = src.read(ent_lba * lba, count * size)
        parts = []
        for i in range(len(table) // size):
            e = table[i * size:(i + 1) * size]
            if e[:16] == bytes(16):
                continue
            first, last = u64(e, 32), u64(e, 40)
            name = e[56:128].decode('utf-16le', 'ignore').split('\0')[0]
            parts.append((first * lba, (last - first + 1) * lba, name or 'GPT partition'))
        return parts
    parts = []
    for i in range(4):
        e = s0[446 + 16 * i: 462 + 16 * i]
        if e[4] and e[4] not in (0x05, 0x0F, 0xEE) and u32(e, 12):
            start, size = u32(e, 8) * 512, u32(e, 12) * 512
            if not src.size or start + size <= src.size:
                parts.append((start, size, f'MBR type 0x{e[4]:02x}'))
    return parts


class Window:
    """Read cache for parsers that walk forward through a file. The current
    scan chunk is primed in so checking a candidate costs no extra reads."""

    def __init__(self, src, block=MB):
        self.src, self.block = src, block
        self.primary = (0, b'')
        self.cache = (-1, b'')

    def prime(self, base, buf):
        self.primary = (base, buf)

    def get(self, off, n):
        for base, buf in (self.primary, self.cache):
            if base <= off and off + n <= base + len(buf):
                return buf[off - base: off - base + n]
        self.cache = (off, self.src.read(off, max(self.block, n)))
        return self.cache[1][:n]


def save_runs(src, runs, size, dest, data=None):
    """Write `size` bytes gathered from (offset, length) runs to dest, return its SHA-1.
    A run offset of None is a sparse hole (zeros)."""
    h = hashlib.sha1()
    try:
        with open(fs_path(dest), 'wb') as f:
            if data is not None:
                f.write(data[:size])
                h.update(data[:size])
                return h.hexdigest()
            left = size
            for off, length in runs:
                pos = 0
                while pos < length and left > 0:
                    k = min(16 * MB, length - pos, left)
                    b = b'\0' * k if off is None else src.read(off + pos, k)
                    if not b:
                        return h.hexdigest()
                    f.write(b)
                    h.update(b)
                    pos += len(b)
                    left -= len(b)
        return h.hexdigest()
    except OSError:
        try:
            os.remove(fs_path(dest))  # don't leave a half-written file behind
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# Filesystems
#
# Each one provides deleted_files(want) yielding dicts with:
#   path, size, mtime (epoch or None), runs [(byte offset or None, length)],
#   data (resident bytes or None), how, reused, and optionally skip (reason).
# and allocated(off) / fully_allocated(off, n) so carving can skip used space.
# --------------------------------------------------------------------------

class Bitmap:
    def __init__(self, data, base):
        self.b, self.base = data, base

    def get(self, c):
        i = c - self.base
        return 0 <= i < len(self.b) * 8 and bool(self.b[i >> 3] & (1 << (i & 7)))

    def _scan(self, c0, c1, want_all):
        i0, i1 = c0 - self.base, c1 - self.base
        while i0 < i1 and i0 & 7:
            if self.get(i0 + self.base) != want_all:
                return not want_all
            i0 += 1
        while i1 > i0 and i1 & 7:
            i1 -= 1
            if self.get(i1 + self.base) != want_all:
                return not want_all
        seg = self.b[i0 >> 3: i1 >> 3]
        if len(seg) < (i1 - i0) >> 3:
            return False if want_all else seg.count(0) != len(seg)
        if want_all:
            return seg.count(255) == len(seg)
        return seg.count(0) != len(seg)

    def all(self, c0, c1):
        return self._scan(c0, c1, True)

    def any(self, c0, c1):
        return self._scan(c0, c1, False)

    def used(self):
        return popcount(self.b)


class FS:
    name = '?'
    first_cl = 2  # number of the first data cluster
    data_off = 0  # byte offset of cluster `first_cl`

    def coff(self, c):
        return self.data_off + (c - self.first_cl) * self.cs

    def valid(self, c):
        return self.first_cl <= c < self.first_cl + self.ncl

    def cluster_at(self, off):
        return self.first_cl + (off - self.data_off) // self.cs

    def allocated(self, off):
        if off < self.data_off:
            return True  # filesystem metadata area
        c = self.cluster_at(off)
        return self.valid(c) and self._alloc(c)

    def fully_allocated(self, off, n):
        if off + n <= self.data_off:
            return True
        c0 = self.cluster_at(max(off, self.data_off))
        c1 = min(self.cluster_at(off + n - 1) + 1, self.first_cl + self.ncl)
        return c0 < c1 and self._all_alloc(c0, c1)


class ExFAT(FS):
    name = 'exFAT'

    def __init__(self, src):
        bs = src.read(0, 512)
        if bs[3:11] != b'EXFAT   ':
            raise ValueError('not exFAT')
        self.src = src
        self.bps = 1 << bs[108]
        self.cs = self.bps << bs[109]
        self.fat_off = u32(bs, 80) * self.bps
        self.data_off = u32(bs, 88) * self.bps
        self.ncl = u32(bs, 92)
        self.root = u32(bs, 96)
        self.vol_len = u64(bs, 72) * self.bps
        self._fat_pages = {}
        self.bm = Bitmap(b'', 2)
        for e in self._entries(self.read_dir(self.root, 0, False)):
            if e[0] == 0x81 and not (e[1] & 1):
                first, length = u32(e, 20), u64(e, 24)
                self.bm = Bitmap(self.read_clusters(self.chain(first, length // self.cs + 2), length), 2)
                break

    def _alloc(self, c):
        return self.bm.get(c)

    def _all_alloc(self, c0, c1):
        return self.bm.all(c0, c1)

    def used(self):
        return self.bm.used()

    def fat_next(self, c):
        page, idx = divmod(c * 4, MB)
        if page not in self._fat_pages:
            if len(self._fat_pages) > 64:
                self._fat_pages.clear()
            self._fat_pages[page] = self.src.read(self.fat_off + page * MB, MB)
        return u32(self._fat_pages[page], idx)

    def chain(self, first, cap):
        out, c = [], first
        while self.valid(c) and len(out) < cap:
            out.append(c)
            c = self.fat_next(c)
        return out

    @staticmethod
    def cluster_runs(clusters):
        """Collapse a cluster list into [start_cluster, count] runs."""
        runs = []
        for c in clusters:
            if runs and runs[-1][0] + runs[-1][1] == c:
                runs[-1][1] += 1
            else:
                runs.append([c, 1])
        return runs

    def read_clusters(self, clusters, size=None):
        out = bytearray()
        for c, k in self.cluster_runs(clusters):
            out += self.src.read(self.coff(c), k * self.cs)
        return bytes(out if size is None else out[:size])

    def read_dir(self, first, size, nofat):
        if nofat and size:
            n = -(-size // self.cs)
            return self.read_clusters(range(first, first + n), size)
        cap = (size // self.cs + 1) if size else (256 * MB // self.cs)
        return self.read_clusters(self.chain(first, cap))

    @staticmethod
    def _entries(data):
        for i in range(0, len(data) - 31, 32):
            if data[i] == 0:
                return
            yield data[i:i + 32]

    def parse_dir(self, data):
        """Yield live (0x85) and deleted (0x05) file entry sets."""
        i, n = 0, len(data)
        while i + 32 <= n:
            t = data[i]
            if t == 0:
                break
            if t not in (0x85, 0x05):
                i += 32
                continue
            deleted = t == 0x05
            sc = data[i + 1]
            if not 2 <= sc <= 18 or i + 32 * (sc + 1) > n:
                i += 32
                continue
            se = data[i + 32: i + 64]
            if se[0] != (0x40 if deleted else 0xC0):
                i += 32
                continue
            name_len = se[3]
            name = b''
            for k in range(sc - 1):
                ne = data[i + 64 + 32 * k: i + 96 + 32 * k]
                if ne[0] != (0x41 if deleted else 0xC1):
                    break
                name += ne[2:32]
            name = name.decode('utf-16le', 'replace')[:name_len]
            name = name.replace('/', '_').replace('\0', '') or f'unnamed_{i}'
            yield {
                'name': name,
                'deleted': deleted,
                'is_dir': bool(u16(data, i + 4) & 0x10),
                'mtime': u32(data, i + 12),
                'nofat': bool(se[1] & 2),
                'first': u32(se, 20),
                'size': u64(se, 24),
            }
            i += 32 * (sc + 1)

    def walk(self, first=None, size=0, nofat=False, path='', deleted=False, seen=None, depth=0):
        """Yield (path, in_deleted_tree, entry) for every file in the tree."""
        if seen is None:
            seen = set()
            first = self.root
        if depth > 64 or first in seen or not self.valid(first):
            return
        seen.add(first)
        for e in self.parse_dir(self.read_dir(first, size, nofat)):
            p = f'{path}/{e["name"]}'
            gone = deleted or e['deleted']
            if e['is_dir']:
                yield from self.walk(e['first'], e['size'], e['nofat'], p, gone, seen, depth + 1)
            else:
                yield p, gone, e

    def file_clusters(self, e):
        need = -(-e['size'] // self.cs)
        if e['nofat']:
            return list(range(e['first'], e['first'] + need)), 'contiguous'
        ch = self.chain(e['first'], need + 1)
        if len(ch) == need:
            return ch, 'fat-chain'
        return list(range(e['first'], e['first'] + need)), 'guessed-contiguous'

    def deleted_files(self, want):
        for path, gone, e in self.walk():
            if not gone or e['size'] == 0 or not want(path.rsplit('/', 1)[-1]):
                continue
            clusters, how = self.file_clusters(e)
            f = {'path': path, 'size': e['size'], 'mtime': exfat_time(e['mtime']), 'data': None, 'how': how}
            if not clusters or not (self.valid(clusters[0]) and self.valid(clusters[-1])):
                f['skip'] = 'skipped-bad-pointer'
            else:
                f['runs'] = [(self.coff(c), k * self.cs) for c, k in self.cluster_runs(clusters)]
                f['reused'] = any(self._alloc(c) for c in clusters)
            yield f


def exfat_time(ts):
    try:
        return datetime.datetime(
            1980 + (ts >> 25), (ts >> 21) & 0xF, (ts >> 16) & 0x1F,
            (ts >> 11) & 0x1F, (ts >> 5) & 0x3F, (ts & 0x1F) * 2).timestamp()
    except ValueError:
        return None


def fat_time(date, tm):
    try:
        return datetime.datetime(
            1980 + (date >> 9), (date >> 5) & 0xF, date & 0x1F,
            tm >> 11, (tm >> 5) & 0x3F, (tm & 0x1F) * 2).timestamp()
    except ValueError:
        return None


def lfn_checksum(sfn):
    s = 0
    for b in sfn:
        s = (((s & 1) << 7) + (s >> 1) + b) & 0xFF
    return s


class FAT(FS):
    """FAT12 / FAT16 / FAT32."""

    def __init__(self, src):
        bs = src.read(0, 512)
        if len(bs) < 512 or bs[510:512] != b'\x55\xaa' or bs[0] not in (0xEB, 0xE9):
            raise ValueError('not FAT')
        bps, spc = u16(bs, 11), bs[13]
        reserved, nfats, rootent = u16(bs, 14), bs[16], u16(bs, 17)
        total = u16(bs, 19) or u32(bs, 32)
        fatsz = u16(bs, 22) or u32(bs, 36)
        if bps not in (512, 1024, 2048, 4096) or not spc or spc & (spc - 1) \
                or not (reserved and nfats and fatsz and total):
            raise ValueError('not FAT')
        self.src = src
        self.cs = bps * spc
        self.fat_off = reserved * bps
        self.root_off = (reserved + nfats * fatsz) * bps
        self.root_len = ((rootent * 32 + bps - 1) // bps) * bps
        self.data_off = self.root_off + self.root_len
        self.vol_len = total * bps
        self.ncl = (self.vol_len - self.data_off) // self.cs
        if self.ncl <= 0:
            raise ValueError('not FAT')
        self.bits = 12 if self.ncl < 4085 else 16 if self.ncl < 65525 else 32
        self.name = f'FAT{self.bits}'
        self.root_cl = u32(bs, 44) if self.bits == 32 else 0
        raw = src.read(self.fat_off, min(fatsz * bps, (self.ncl + 2) * 4 + 8))
        if self.bits == 12:
            self.fat = raw
        else:
            code = 'H' if self.bits == 16 else ('I' if array.array('I').itemsize == 4 else 'L')
            width = 2 if self.bits == 16 else 4
            self.fat = array.array(code, raw[:((self.ncl + 2) * width)])
            if sys.byteorder == 'big':
                self.fat.byteswap()
        self.eoc = {12: 0xFF8, 16: 0xFFF8, 32: 0x0FFFFFF8}[self.bits]
        self.seen_dirs = set()

    def entry(self, c):
        if self.bits == 12:
            v = u16(self.fat, c * 3 // 2) if c * 3 // 2 + 2 <= len(self.fat) else 0
            return v >> 4 if c & 1 else v & 0xFFF
        if c >= len(self.fat):
            return 0
        return self.fat[c] & 0x0FFFFFFF

    def _alloc(self, c):
        return self.entry(c) != 0

    def _all_alloc(self, c0, c1):
        if self.bits == 12:
            return all(self.entry(c) for c in range(c0, c1))
        return 0 not in self.fat[c0:c1]

    def _any_alloc(self, c0, c1):
        if self.bits == 12:
            return any(self.entry(c) for c in range(c0, c1))
        seg = self.fat[c0:c1]
        return seg.count(0) != len(seg)

    def used(self):
        if self.bits == 12:
            return sum(1 for c in range(2, self.ncl + 2) if self.entry(c))
        seg = self.fat[2:self.ncl + 2]
        return len(seg) - seg.count(0)

    def chain(self, first, cap):
        out, c = [], first
        while self.valid(c) and len(out) < cap:
            out.append(c)
            c = self.entry(c)
            if c >= self.eoc:
                break
        return out

    def read_clusters(self, clusters):
        out = bytearray()
        for c, k in ExFAT.cluster_runs(clusters):
            out += self.src.read(self.coff(c), k * self.cs)
        return bytes(out)

    def _is_dir_cluster(self, c):
        return self.valid(c) and self.src.read(self.coff(c), 11) == b'.          '

    @staticmethod
    def _looks_like_dir(blk):
        if not blk or blk[0] == 0:
            return False
        for i in range(0, len(blk) - 31, 32):
            if blk[i] == 0:
                return True
            attr = blk[i + 11]
            if attr > 0x3F or (attr != 0x0F and blk[i] < 0x20 and blk[i] != 0x05):
                return False
        return True

    def read_dir(self, first, deleted):
        if first is None:  # root
            if self.bits != 32:
                return self.src.read(self.root_off, self.root_len)
            first = self.root_cl
        if not deleted:
            return self.read_clusters(self.chain(first, 256 * MB // self.cs))
        # A deleted folder's FAT chain is gone: read following clusters while
        # they still look like directory entries.
        if not self._is_dir_cluster(first):
            return b''
        out = bytearray()
        for c in range(first, min(first + 256, self.first_cl + self.ncl)):
            if out and self._alloc(c):
                break
            blk = self.src.read(self.coff(c), self.cs)
            if out and not self._looks_like_dir(blk):
                break
            out += blk
        return bytes(out)

    @staticmethod
    def _sfn_name(sfn, case, deleted):
        base, ext = sfn[:8], sfn[8:11]
        if base[0] == 0x05:
            base = b'\xe5' + base[1:]
        base = base.rstrip(b' ').decode('cp437', 'replace')
        ext = ext.rstrip(b' ').decode('cp437', 'replace')
        if case & 0x08:
            base = base.lower()
        if case & 0x10:
            ext = ext.lower()
        if deleted:
            base = '_' + base[1:]  # the first letter is overwritten on delete
        return base + ('.' + ext if ext else '')

    @staticmethod
    def _lfn_name(lfn, sfn, deleted):
        if not lfn or len({c for _, c, _ in lfn}) != 1:
            return None
        if deleted:
            if any(b0 != 0xE5 for b0, _, _ in lfn):
                return None
        elif lfn_checksum(sfn) != lfn[0][1]:
            return None
        raw = b''.join(chunk for _, _, chunk in reversed(lfn))
        name = raw.decode('utf-16le', 'replace').split('\0')[0].replace('￿', '')
        return name or None

    def parse_dir(self, data):
        lfn = []
        for i in range(0, len(data) - 31, 32):
            e = data[i:i + 32]
            if e[0] == 0:
                break
            attr = e[11]
            if attr == 0x0F:
                lfn.append((e[0], e[13], e[1:11] + e[14:26] + e[28:32]))
                continue
            long_parts, lfn = lfn, []
            if attr & 0x08 or e[:2] == b'. ' or e[:3] == b'.. ':
                continue
            deleted = e[0] == 0xE5
            sfn = e[:11]
            name = self._lfn_name(long_parts, sfn, deleted) or self._sfn_name(sfn, e[12], deleted)
            first = u16(e, 26) | ((u16(e, 20) << 16) if self.bits == 32 else 0)
            yield {
                'name': name.replace('/', '_'),
                'deleted': deleted,
                'is_dir': bool(attr & 0x10),
                'first': first,
                'size': u32(e, 28),
                'mtime': fat_time(u16(e, 24), u16(e, 22)),
            }

    def _guess_high(self, first, test):
        """Some systems zero the high 16 bits of a deleted FAT32 file's start
        cluster. Try each possible high word and keep the first that passes."""
        for hi in range(1, ((self.ncl + 2) >> 16) + 1):
            c = (hi << 16) | (first & 0xFFFF)
            if self.valid(c) and not self._alloc(c) and test(c):
                return c
        return None

    def walk(self, first=None, path='', deleted=False, seen=None, depth=0):
        if seen is None:
            seen = self.seen_dirs
        elif depth > 64 or first in seen or not self.valid(first):
            return
        seen.add(first)
        for e in self.parse_dir(self.read_dir(first, deleted)):
            gone = deleted or e['deleted']
            p = f'{path}/{e["name"]}'
            if e['is_dir']:
                c = e['first']
                if gone and self.bits == 32 and not self._is_dir_cluster(c):
                    c = self._guess_high(c, self._is_dir_cluster) or c
                yield from self.walk(c, p, gone, seen, depth + 1)
            else:
                yield p, gone, e

    def orphan_files(self, c, want):
        """Deleted files inside a free folder cluster the tree walk never reached
        (its parent folder's entry was overwritten)."""
        if c in self.seen_dirs or not self.valid(c) or self._alloc(c) or not self._is_dir_cluster(c):
            return iter(())
        return self._deleted(self.walk(c, f'/_orphaned_folders/folder_{c}', True, self.seen_dirs, 1), want)

    def deleted_files(self, want):
        return self._deleted(self.walk(), want)

    def _deleted(self, entries, want):
        for path, gone, e in entries:
            name = path.rsplit('/', 1)[-1]
            if not gone or e['size'] == 0 or not want(name):
                continue
            first, need = e['first'], -(-e['size'] // self.cs)
            ext = ext_of(name)
            how = 'contiguous'
            head_ok = lambda c: header_ok(ext, self.src.read(self.coff(c), 16)) is True
            if self.bits == 32 and header_ok(ext, b'\0' * 16) is not None and \
                    not (self.valid(first) and head_ok(first)):
                fixed = self._guess_high(first, head_ok)
                if fixed:
                    first, how = fixed, 'contiguous, start repaired'
            f = {'path': path, 'size': e['size'], 'mtime': e['mtime'], 'data': None, 'how': how}
            if not (self.valid(first) and self.valid(first + need - 1)):
                f['skip'] = 'skipped-bad-pointer'
            else:
                f['runs'] = [(self.coff(first), need * self.cs)]
                f['reused'] = self._any_alloc(first, first + need)
            yield f


def filetime(ft):
    return ft / 1e7 - 11644473600 if ft else None


def ntfs_runlist(a):
    """Decode an NTFS data run list into [(lcn or None for sparse, count)]."""
    p, runs, lcn = u16(a, 32), [], 0
    while p < len(a) and a[p]:
        h = a[p]
        ln, lo = h & 15, h >> 4
        p += 1
        if ln == 0 or p + ln + lo > len(a):
            break
        count = int.from_bytes(a[p:p + ln], 'little')
        p += ln
        if lo:
            lcn += int.from_bytes(a[p:p + lo], 'little', signed=True)
            runs.append((lcn, count))
        else:
            runs.append((None, count))
        p += lo
    return runs


class NTFS(FS):
    name = 'NTFS'
    first_cl = 0
    data_off = 0

    def __init__(self, src):
        bs = src.read(0, 512)
        if bs[3:11] != b'NTFS    ':
            raise ValueError('not NTFS')
        self.src = src
        self.bps = u16(bs, 11)
        spc = bs[13]
        spc = 1 << (256 - spc) if spc > 0x80 else spc
        self.cs = self.bps * spc
        self.vol_len = u64(bs, 40) * self.bps
        self.ncl = self.vol_len // self.cs
        rec = struct.unpack_from('b', bs, 64)[0]
        self.rec_size = 1 << -rec if rec < 0 else rec * self.cs
        mft = self.fixup(src.read(u64(bs, 48) * self.cs, self.rec_size))
        if mft is None:
            raise ValueError('NTFS file table not readable')
        data = self.parse_record(mft)[3]
        if not data or 'runs' not in data:
            raise ValueError('NTFS file table not readable')
        self.mft_runs = data['runs']
        bitmap = self.parse_record(self.record(6))[3] or {}
        self.bm = Bitmap(self.read_data(bitmap), 0)

    def _alloc(self, c):
        return self.bm.get(c)

    def _all_alloc(self, c0, c1):
        return self.bm.all(c0, c1)

    def used(self):
        return self.bm.used()

    def fixup(self, b):
        if len(b) < 48 or b[:4] != b'FILE':
            return None
        uo, uc = u16(b, 4), u16(b, 6)
        b = bytearray(b)
        for k in range(1, uc):
            p = k * self.bps - 2
            if p + 2 > len(b) or uo + 2 * k + 2 > len(b):
                break
            b[p:p + 2] = b[uo + 2 * k: uo + 2 * k + 2]
        return bytes(b)

    def record(self, num):
        off = num * self.rec_size
        for lcn, count in self.mft_runs:
            length = count * self.cs
            if off < length:
                return self.fixup(self.src.read(lcn * self.cs + off, self.rec_size)) if lcn is not None else None
            off -= length
        return None

    def records(self):
        num, pending = 0, b''
        for lcn, count in self.mft_runs:
            off, end = (lcn or 0) * self.cs, ((lcn or 0) + count) * self.cs
            while off < end:
                n = min(4 * MB, end - off)
                buf = pending + (bytes(n) if lcn is None else self.src.read(off, n))
                off += n
                usable = len(buf) - len(buf) % self.rec_size
                for i in range(0, usable, self.rec_size):
                    rec = self.fixup(buf[i:i + self.rec_size])
                    if rec:
                        yield num, rec
                    num += 1
                pending = buf[usable:]

    @staticmethod
    def parse_record(rec):
        """Return (flags, base_record, names, data, mtime, has_attribute_list)."""
        if rec is None:
            return 0, 0, [], None, None, False
        flags, base = u16(rec, 22), u64(rec, 32) & 0xFFFFFFFFFFFF
        names, data, mtime, attr_list = [], None, None, False
        p = u16(rec, 20)
        while p + 16 <= len(rec):
            t, ln = u32(rec, p), u32(rec, p + 4)
            if t == 0xFFFFFFFF or ln < 16 or p + ln > len(rec):
                break
            a = rec[p:p + ln]
            nonres, name_len = a[8], a[9]
            if t == 0x10 and not nonres:
                mtime = filetime(u64(a, u16(a, 20) + 8))
            elif t == 0x20:
                attr_list = True
            elif t == 0x30 and not nonres:
                c = a[u16(a, 20):]
                n = c[64]
                names.append((c[65], c[66:66 + 2 * n].decode('utf-16le', 'replace'),
                              u64(c, 0) & 0xFFFFFFFFFFFF, u64(c, 48)))
            elif t == 0x80 and name_len == 0 and data is None:
                if nonres:
                    data = {'vcn': u64(a, 16), 'runs': ntfs_runlist(a), 'size': u64(a, 48), 'flags': u16(a, 12)}
                else:
                    co, cl = u16(a, 20), u32(a, 16)
                    data = {'vcn': 0, 'resident': a[co:co + cl], 'size': cl, 'flags': u16(a, 12)}
            p += ln
        return flags, base, names, data, mtime, attr_list

    def read_data(self, data):
        if not data:
            return b''
        if 'resident' in data:
            return data['resident']
        out = bytearray()
        for lcn, count in data['runs']:
            out += bytes(count * self.cs) if lcn is None else self.src.read(lcn * self.cs, count * self.cs)
            if len(out) >= data['size']:
                break
        return bytes(out[:data['size']])

    @staticmethod
    def best_name(names):
        # Prefer the long Win32/POSIX name over the 8.3 DOS alias.
        return sorted(names, key=lambda n: n[0] == 2)[0]

    def path_of(self, parent, dirs):
        parts, seen = [], set()
        while parent != 5 and parent in dirs and parent not in seen:
            seen.add(parent)
            name, parent = dirs[parent]
            parts.append(name)
        if parent != 5:
            parts.append('_orphaned')  # parent folder's record was reused
        return '/' + '/'.join(reversed(parts))

    def deleted_files(self, want):
        print('  Reading the NTFS file table…')
        dirs, cands, ext = {}, [], {}
        for num, rec in self.records():
            flags, base = u16(rec, 22), u64(rec, 32) & 0xFFFFFFFFFFFF
            if flags & 1 and not flags & 2 and not base:
                continue  # live file: nothing to recover
            flags, base, names, data, mtime, attr_list = self.parse_record(rec)
            if base:
                if data and 'runs' in data:
                    ext.setdefault(base, []).append(data)
                continue
            deleted_file = not flags & 3 and num >= 24
            if not names:
                # Some drivers (e.g. Linux ntfs3) drop the name on delete but keep the data map.
                if deleted_file and data:
                    cands.append((num, None, None, data, mtime, attr_list, 0))
                continue
            _, name, parent, fn_size = self.best_name(names)
            if flags & 2:
                dirs[num] = (name.replace('/', '_'), parent)
            elif deleted_file and want(name):
                cands.append((num, name.replace('/', '_'), parent, data, mtime, attr_list, fn_size))
        for num, name, parent, data, mtime, attr_list, fn_size in cands:
            pieces = ([data] if data else []) + (ext.get(num, []) if attr_list else [])
            pieces.sort(key=lambda d: d['vcn'])
            first = pieces[0] if pieces else None
            if name is None:
                head = first['resident'][:16] if 'resident' in first else \
                    next((self.src.read(lcn * self.cs, 16) for lcn, _ in first['runs'] if lcn is not None), b'')
                kind = sniff_ext(head) or 'bin'
                if not want(f'x.{kind}'):
                    continue
                path = f'/_unnamed/record_{num}.{kind}'
            else:
                path = self.path_of(parent, dirs).rstrip('/') + '/' + name
            f = {'path': path, 'mtime': mtime, 'data': None, 'how': 'mft-runs'}
            f['size'] = first['size'] if first and first['vcn'] == 0 else fn_size
            if f['size'] == 0:
                continue
            if not first or first['vcn'] != 0:
                f['skip'] = 'skipped-no-data-record'
            elif first['flags'] & 0x4000:
                f['skip'] = 'skipped-encrypted'
            elif 'resident' in first:
                f.update(data=first['resident'], runs=[], reused=False, how='resident')
            else:
                runs = [r for d in pieces for r in d.get('runs', [])]
                f['runs'] = [(None if lcn is None else lcn * self.cs, count * self.cs) for lcn, count in runs]
                f['reused'] = any(lcn is not None and self.bm.any(lcn, lcn + count) for lcn, count in runs)
                if first['flags'] & 0xFF:
                    f['how'] = 'compressed (saved as stored, may not open)'
            yield f


def detect_fs(src):
    for cls in (ExFAT, NTFS, FAT):
        try:
            return cls(src)
        except (ValueError, struct.error, IndexError):
            pass
    return None


# --------------------------------------------------------------------------
# Signature checks & carving
# --------------------------------------------------------------------------

BMFF_BOXES = {
    b'ftyp', b'moov', b'mdat', b'free', b'skip', b'wide', b'uuid', b'meta', b'pnot',
    b'PICT', b'sidx', b'moof', b'mfra', b'styp', b'pdin', b'junk', b'prfl', b'udta',
    b'ssix', b'prft', b'emsg', b'idat', b'iloc', b'XMP_', b'beam',
}
HEIF_BRANDS = {b'heic', b'heix', b'hevc', b'hevx', b'heim', b'heis', b'hevm', b'hevs', b'mif1', b'msf1'}
SOF = set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def header_ok(ext, head):
    if ext in ('jpg', 'jpeg', 'jpe'):
        return head[:3] == b'\xff\xd8\xff'
    if ext == 'png':
        return head[:8] == b'\x89PNG\r\n\x1a\n'
    if ext in ('heic', 'heif', 'avif', 'mp4', 'mov', 'm4v', '3gp', '3g2', 'cr3', 'insv', 'lrv'):
        return head[4:8] in BMFF_BOXES
    if ext in ('avi', 'webp'):
        return head[:4] == b'RIFF'
    return None  # no quick check for this format


def sniff_ext(head):
    """Guess a file extension from its first 16 bytes."""
    if head[:3] == b'\xff\xd8\xff':
        return 'jpg'
    if head[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if head[4:8] == b'ftyp':
        brand = head[8:12]
        if brand in HEIF_BRANDS:
            return 'heic'
        if brand[:3] in (b'M4A', b'M4B', b'M4P'):
            return 'm4a'
        named = {b'avif': 'avif', b'qt  ': 'mov', b'crx ': 'cr3', b'M4V ': 'm4v'}.get(brand)
        return named or ('3gp' if brand[:3] in (b'3gp', b'3g2') else 'mp4')
    if head[:4] == b'RIFF':
        return {b'AVI ': 'avi', b'WEBP': 'webp'}.get(head[8:12])
    if head[:4] in (b'II*\0', b'MM\0*'):
        return 'tif'  # also camera raws (CR2, NEF, ARW, DNG)
    if head[:4] == b'GIF8':
        return 'gif'
    if head[:4] == b'\x1a\x45\xdf\xa3':
        return 'mkv'
    return None


def carve_jpeg(w, s, limit):
    p, sof, sos, mpf = s + 2, False, False, 0
    while p - s < limit:
        b = w.get(p, 4)
        if len(b) < 4 or b[0] != 0xFF:
            return None
        m = b[1]
        if m == 0xFF:
            p += 1
            continue
        if m == 0xD9:
            if not (sof and sos):
                return None
            # Multi-Picture (MPF) files from phones/cameras store extra JPEGs
            # (previews, gain maps) after the main image: keep them.
            end = max(p + 2, mpf)
            return end - s, 'jpg'
        if 0xD0 <= m <= 0xD7 or m == 0x01:
            p += 2
            continue
        seg = (b[2] << 8) | b[3]
        if seg < 2:
            return None
        sof |= m in SOF
        if m == 0xE2 and not mpf and w.get(p + 4, 4) == b'MPF\0':
            mpf = _mpf_end(w, p + 8, w.get(p + 8, seg - 6), s + limit)
        p += 2 + seg
        if m == 0xDA:
            sos = True
            p = _scan_entropy(w, p, s + limit)
            if p is None:
                return None
    return None


def _mpf_end(w, base, tiff, maxp):
    """End offset of the last image listed in an MPF index, or 0."""
    try:
        e = {b'II*\0': '<', b'MM\0*': '>'}[tiff[:4]]
        ifd = struct.unpack_from(e + 'I', tiff, 4)[0]
        for k in range(struct.unpack_from(e + 'H', tiff, ifd)[0]):
            tag, _, count, val = struct.unpack_from(e + 'HHII', tiff, ifd + 2 + 12 * k)
            if tag != 0xB002:
                continue
            end = 0
            for i in range(count // 16):
                _, size, off = struct.unpack_from(e + 'III', tiff, val + 16 * i)
                if off and base + off + size <= maxp and w.get(base + off, 2) == b'\xff\xd8':
                    end = max(end, base + off + size)
            return end
    except (KeyError, struct.error):
        pass
    return 0


def _scan_entropy(w, p, maxp):
    while p < maxp:
        buf = w.get(p, MB)
        if len(buf) < 2:
            return None
        i = buf.find(b'\xff')
        while i != -1 and i + 1 < len(buf):
            nb = buf[i + 1]
            if nb != 0 and not 0xD0 <= nb <= 0xD7:
                return p + i
            i = buf.find(b'\xff', i + 2)
        p += len(buf) - 1 if i != -1 else len(buf)
    return None


def carve_png(w, s, limit):
    p = s + 8
    while p - s < limit:
        b = w.get(p, 8)
        if len(b) < 8 or not b[4:8].isalpha():
            return None
        p += 12 + be32(b, 0)
        if b[4:8] == b'IEND':
            return p - s, 'png'
    return None


def carve_bmff(w, s, limit, vol_end):
    p, types, brand, compat = s, [], b'', b''
    while True:
        b = w.get(p, 16)
        if len(b) < 8:
            break
        size, t = be32(b, 0), b[4:8]
        if t not in BMFF_BOXES or (t == b'ftyp' and p != s):
            break
        if size == 1:
            size = be64(b, 8)
        if size < 8:
            break
        if p == s:
            if t != b'ftyp' or size > 4096:
                return None
            ft = w.get(p, size)
            brand, compat = ft[8:12], ft[16:]
        types.append(t)
        p += size
        if p - s > limit or p > vol_end:
            return None
    if not types:
        return None
    if brand in HEIF_BRANDS or brand == b'avif':
        kind = 'avif' if brand == b'avif' or b'avif' in compat else 'heic'
        need = {b'meta'}
    else:
        if brand[:3] in (b'M4A', b'M4B', b'M4P'):
            return None  # audio, not wanted
        kind = {b'qt  ': 'mov', b'crx ': 'cr3', b'M4V ': 'm4v'}.get(brand)
        kind = kind or ('3gp' if brand[:3] in (b'3gp', b'3g2') else 'mp4')
        need = {b'moov'} if kind == 'cr3' else {b'moov', b'mdat'}
        if b'moof' in types:
            need = {b'moov'}
    if not need.issubset(types):
        return None
    return p - s, kind


def carve_riff(w, s, limit):
    b = w.get(s, 12)
    form = b[8:12]
    if form not in (b'AVI ', b'WEBP'):
        return None
    p = s + 8 + u32(b, 4)
    if form == b'AVI ':
        while True:  # OpenDML: >1 GB AVIs continue in RIFF/AVIX chunks
            nb = w.get(p, 12)
            if len(nb) < 12 or nb[:4] != b'RIFF' or nb[8:12] != b'AVIX':
                break
            p += 8 + u32(nb, 4)
    if p - s > limit:
        return None
    return p - s, 'avi' if form == b'AVI ' else 'webp'


PATTERN = re.compile(rb'\xff\xd8\xff[\xc0-\xfe]|\x89PNG\r\n\x1a\n|ftyp|RIFF')
LIMITS = {'jpeg': 120 * MB, 'png': 300 * MB, 'bmff': 64 << 30, 'riff': 8 << 30}


# --------------------------------------------------------------------------
# Recovery passes
# --------------------------------------------------------------------------

class Report:
    def __init__(self, out):
        new = not os.path.exists(os.path.join(out, 'report.csv'))
        self.f = open(os.path.join(out, 'report.csv'), 'a', newline='', encoding='utf-8')
        self.w = csv.writer(self.f)
        if new:
            self.w.writerow(['pass', 'status', 'type', 'size_bytes', 'source', 'saved_as'])
        self.counts = {}

    def add(self, method, status, kind, size, source, saved):
        self.w.writerow([method, status, kind, size, source, saved])
        self.f.flush()
        key = (method, status.split(' (')[0])
        self.counts[key] = self.counts.get(key, 0) + 1


class Dedupe:
    """Remembers (size, SHA-1) of every saved file so identical copies are dropped."""

    def __init__(self, out, enabled):
        self.enabled = enabled
        self.file = os.path.join(out, '.hashes')
        self.seen = {}
        self.saved_bytes = 0
        if enabled and os.path.exists(self.file):
            with open(self.file, encoding='utf-8') as f:
                for line in f:
                    size, digest, rel = line.rstrip('\n').split(' ', 2)
                    self.seen[(int(size), digest)] = rel

    def check(self, size, digest, rel):
        """Return the earlier copy's path if this file is a duplicate, else record it."""
        if not self.enabled:
            return None
        key = (size, digest)
        if key in self.seen:
            self.saved_bytes += size
            return self.seen[key]
        self.seen[key] = rel
        with open(self.file, 'a', encoding='utf-8') as f:
            f.write(f'{size} {digest} {rel}\n')
        return None


def unique_path(path):
    base, ext = os.path.splitext(path)
    n, p = 1, path
    while os.path.exists(fs_path(p)):
        p = f'{base} ({n}){ext}'
        n += 1
    return p


def wanted(all_types):
    if all_types:
        return lambda n: not n.startswith('._')
    return lambda n: is_media(n) and not n.startswith('._')


def restore_file(src, f, out, report, dedupe, known, quiet=False):
    """Save one deleted file found in the filesystem's records. Returns True if saved."""
    path, size = f['path'], f['size']
    ext = ext_of(path)
    if f.get('skip'):
        report.add('undelete', f['skip'], ext, size, path, '')
        return False
    first = next((off for off, _ in f['runs'] if off is not None), None)
    if first is not None and first in known:
        return False  # already restored
    head = f['data'][:16] if f['data'] is not None else (src.read(first, 16) if first is not None else b'')
    sig = header_ok(ext, head)
    good = not f['reused'] and sig is not False
    folder = 'undeleted' if good else 'undeleted_maybe_damaged'
    dest = unique_path(os.path.join(out, folder, *[safe_name(p) for p in path.strip('/').split('/')]))
    rel = os.path.relpath(dest, out)
    os.makedirs(fs_path(os.path.dirname(dest)), exist_ok=True)
    digest = save_runs(src, f['runs'], size, dest, f['data'])
    status = 'ok' if good else ('space-reused' if f['reused'] else 'header-mismatch')
    if good and first is not None:
        known[first] = size
    dup = dedupe.check(size, digest, rel)
    if dup:
        os.remove(fs_path(dest))
        status, rel = 'duplicate', f'same as {dup}'
    elif f['mtime']:
        try:
            os.utime(fs_path(dest), (f['mtime'], f['mtime']))
        except (OSError, OverflowError, ValueError):
            pass
    report.add('undelete', status if dup else f'{status} ({f["how"]})', ext, size, path, rel)
    if not quiet:
        print(f'  [{status:15}] {human(size):>10}  {path}')
    return not dup


def undelete(src, fs, out, report, dedupe, want):
    print(f'\n=== Pass 1: undelete ({fs.name}: restore deleted files by name) ===')
    known = {}  # byte offset of first data -> size, so carving can skip them
    found = sum(restore_file(src, f, out, report, dedupe, known) for f in fs.deleted_files(want))
    print(f'Undelete pass done: {found} deleted file(s) restored.')
    with open(os.path.join(out, '.undeleted_offsets'), 'w') as fh:
        for off, size in known.items():
            fh.write(f'{off} {size}\n')
    return known


# A FAT folder's first cluster starts with its "." entry.
FAT_DIR_PATTERN = re.compile(rb'\.          \x10')


def carve(src, fs, out, report, dedupe, want, align, include_allocated, resume):
    print('\n=== Pass 2: carve (scan free space for photo/video data) ===')
    end = fs.vol_len if fs else src.size
    if not end:
        sys.exit('Cannot determine the size of the source.')
    known = {}
    known_file = os.path.join(out, '.undeleted_offsets')
    if os.path.exists(known_file):
        with open(known_file) as fh:
            for line in fh:
                off, size = map(int, line.split())
                known[off] = size
    prog_file = os.path.join(out, '.carve_position')
    pos = 0
    if resume and os.path.exists(prog_file):
        with open(prog_file) as fh:
            pos = int(fh.read().strip() or 0)
        print(f'Resuming from {human(pos)}')
    use_alloc = fs is not None and not include_allocated
    orphans = isinstance(fs, FAT)
    w = Window(src)
    chunk = 32 * MB
    skip_until = 0
    found = orphaned = 0
    t0, last = time.time(), 0
    start_pos = pos

    while pos < end:
        n = min(chunk, end - pos)
        if use_alloc and fs.fully_allocated(pos, n):
            pos += n
            continue
        buf = src.read(pos, n + 16)
        w.prime(pos, buf)
        if orphans:
            for m in FAT_DIR_PATTERN.finditer(buf, 0, n):
                s = pos + m.start()
                if s >= fs.data_off and (s - fs.data_off) % fs.cs == 0:
                    for f in fs.orphan_files(fs.cluster_at(s), want):
                        orphaned += restore_file(src, f, out, report, dedupe, known, quiet=True)
        for m in PATTERN.finditer(buf):
            tag = m.group()
            s = pos + m.start() - (4 if tag == b'ftyp' else 0)
            if s < pos or s >= pos + n or s < skip_until or s % align:
                continue
            if s in known:
                skip_until = s + known[s]
                continue
            if use_alloc and fs.allocated(s):
                continue
            if tag[:2] == b'\xff\xd8':
                res = carve_jpeg(w, s, LIMITS['jpeg'])
            elif tag[:4] == b'\x89PNG':
                res = carve_png(w, s, LIMITS['png'])
            elif tag == b'ftyp':
                res = carve_bmff(w, s, LIMITS['bmff'], end)
            else:
                res = carve_riff(w, s, LIMITS['riff'])
            if not res:
                continue
            length, kind = res
            skip_until = s + length
            if kind == 'jpg' and length < 8 * 1024:
                continue  # tiny thumbnails / icons
            d = os.path.join(out, 'carved', kind)
            os.makedirs(d, exist_ok=True)
            dest = os.path.join(d, f'{kind}_{s:013d}.{kind}')
            rel = os.path.relpath(dest, out)
            digest = save_runs(src, [(s, length)], length, dest)
            dup = dedupe.check(length, digest, rel)
            if dup:
                os.remove(dest)
                report.add('carve', 'duplicate', kind, length, f'offset {s}', f'same as {dup}')
                continue
            report.add('carve', 'ok', kind, length, f'offset {s}', rel)
            found += 1
        pos += n
        if skip_until > pos:
            pos = min(end, skip_until - skip_until % SECTOR)
        now = time.time()
        if now - last > 2 or pos >= end:
            last = now
            with open(prog_file, 'w') as fh:
                fh.write(str(pos))
            rate = (pos - start_pos) / max(now - t0, 1e-6)
            eta = (end - pos) / rate if rate else 0
            sys.stdout.write(
                f'\r  {pos / end * 100:5.1f}%  {human(pos)} / {human(end)}  '
                f'{human(rate)}/s  ETA {int(eta // 3600)}h{int(eta % 3600 // 60):02d}m  '
                f'found {found + orphaned}   ')
            sys.stdout.flush()
    print(f'\nCarve pass done: {found} file(s) rebuilt.')
    if orphaned:
        print(f'Also restored {orphaned} file(s) by name from deleted folders (undeleted/_orphaned_folders).')


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def ask(prompt, default):
    try:
        a = input(f'{prompt} [{default}]: ').strip()
    except EOFError:
        a = ''
    return a or default


def choose_partition(src, wanted, yes):
    parts = partitions(src)
    if not parts:
        return src, None
    print('\nThis is a whole disk with a partition table:')
    for i, (start, size, desc) in enumerate(parts, 1):
        print(f'  {i}. {human(size):>10}  at {human(start):>10}  {desc}')
    print('  0. whole disk, raw scan only')
    largest = max(range(len(parts)), key=lambda i: parts[i][1]) + 1
    pick = wanted if wanted is not None else (largest if yes else int(ask('Which partition?', largest)))
    if not 0 <= pick <= len(parts):
        sys.exit('No such partition.')
    if pick == 0:
        return src, None
    start, size, _ = parts[pick - 1]
    return Partition(src, start, size), pick


def main():
    try:
        sys.stdout.reconfigure(errors='replace')
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(prog='smara_sukma', description='Smara Sukma: recover deleted photos and videos (read-only on the source).')
    ap.add_argument('--source', help='macOS: /Volumes/NAME or disk6s1 · Linux: /dev/sdb1 · '
                                     'Windows: E: or PhysicalDrive1 · or a disk image file')
    ap.add_argument('--out', help='folder for recovered files (must be on another drive)')
    ap.add_argument('--mode', choices=['all', 'undelete', 'carve'], default='all')
    ap.add_argument('--partition', type=int, help='partition number when the source is a whole disk (0 = raw)')
    ap.add_argument('--all-types', action='store_true', help='undelete every deleted file, not just media')
    ap.add_argument('--include-allocated', action='store_true',
                    help='carve inside space used by existing files too (slower, finds duplicates)')
    ap.add_argument('--keep-duplicates', action='store_true', help='keep byte-identical copies of the same file')
    ap.add_argument('--align', type=int, default=SECTOR,
                    help='only accept files starting on this byte boundary (default 512; 1 = anywhere)')
    ap.add_argument('--resume', action='store_true', help='continue an interrupted carve pass')
    ap.add_argument('--keep-mounted', action='store_true', help="don't unmount/lock the source")
    ap.add_argument('-y', '--yes', action='store_true', help='answer yes to prompts')
    a = ap.parse_args()

    print('Smara Sukma — photo & video recovery. The source drive is only ever read, never written.\n')
    if not a.source:
        list_drives()
        a.source = ask('Drive to recover from', '')
        if not a.source:
            sys.exit('No drive given.')
    path, mountpoint = resolve_source(a.source)
    if not a.out:
        a.out = ask('Save recovered files to', default_output())
    a.out = os.path.abspath(os.path.expanduser(a.out))

    if output_on_source(a.out, path, mountpoint):
        sys.exit('Refusing: the output folder is on the drive being recovered. '
                 'Writing there would overwrite the files you are trying to save.')
    os.makedirs(a.out, exist_ok=True)

    if not os.path.isfile(path) and not is_admin():
        print('Note: reading a drive directly usually needs admin rights '
              f'({"Run as administrator" if IS_WIN else "sudo"}).')

    lock = False
    if mountpoint and not a.keep_mounted:
        print(f'\n{a.source} is mounted at {mountpoint}. While mounted, the operating system keeps '
              'writing to it (indexing, logs), which can overwrite deleted files.')
        if a.yes or ask('Unmount it now? (recommended, nothing is erased)', 'Y').lower().startswith('y'):
            if IS_WIN:
                lock = True
            elif not unmount(mountpoint):
                print('Could not unmount; continuing read-only while mounted.')

    try:
        src = Source(path, lock=lock)
    except PermissionError:
        sys.exit(permission_hint(path))
    if lock:
        print('Drive locked and dismounted.' if src.locked else
              'Could not lock the drive (a program has files open on it); continuing read-only while mounted.')

    fs = detect_fs(src)
    part = None
    if not fs:
        src, part = choose_partition(src, a.partition, a.yes)
        if part:
            fs = detect_fs(src)
            print(f'Using partition {part}.')
    if fs:
        print(f'\n{fs.name} volume: {human(fs.vol_len)}, cluster size {human(fs.cs)}, '
              f'{fs.ncl:,} clusters, {fs.used():,} in use')
    else:
        print('\nNo supported filesystem found (exFAT, FAT, NTFS) — skipping undelete, carving the whole area.')

    free = os.statvfs(a.out) if hasattr(os, 'statvfs') else None
    if free:
        print(f'Output: {a.out}  ({human(free.f_bavail * free.f_frsize)} free)')
    else:
        import shutil
        print(f'Output: {a.out}  ({human(shutil.disk_usage(a.out).free)} free)')

    report = Report(a.out)
    dedupe = Dedupe(a.out, not a.keep_duplicates)
    want = wanted(a.all_types)
    try:
        if fs and a.mode in ('all', 'undelete') and not a.resume:
            undelete(src, fs, a.out, report, dedupe, want)
        elif isinstance(fs, FAT):
            for _ in fs.walk():  # marks reachable folders so the scan only re-reads orphaned ones
                pass
        if a.mode in ('all', 'carve'):
            carve(src, fs, a.out, report, dedupe, want, max(1, a.align), a.include_allocated, a.resume)
    except KeyboardInterrupt:
        print('\n\nStopped. Run again with --resume to continue the free-space scan.')
    except OSError as e:
        if e.errno != errno.ENOSPC:
            raise
        print('\n\nThe output drive is full. Free up space (or move the output folder to a bigger drive), '
              'then run again with the same --out and --resume to continue.')
    finally:
        report.f.close()
        chown_to_user(a.out)

    print('\nSummary:')
    for (method, status), n in sorted(report.counts.items()):
        print(f'  {method:9} {status:25} {n}')
    if dedupe.saved_bytes:
        print(f'  Skipped {human(dedupe.saved_bytes)} of identical duplicate copies.')
    if src.bad_reads:
        print(f'  WARNING: {src.bad_reads} unreadable areas were skipped (drive may be failing).')
    print(f'\nFiles are in {a.out}  (details in report.csv)')
    if mountpoint and not a.keep_mounted:
        print(remount_hint(path, mountpoint))


if __name__ == '__main__':
    main()
