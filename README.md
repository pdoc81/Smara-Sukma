# Smara Sukma

*Malay from Sanskrit: **smara** means "memory" (the root of *asmara*), and **sukma** means "soul" (from *sūkṣma*). Bringing back what was lost.*

Read-only recovery of deleted photos and videos from USB drives, SD cards and external disks.
Runs on **macOS, Linux and Windows** with plain Python 3.8+. You don't need to install any extra packages.

## Before you start
- **Stop using the drive.** Every new write can overwrite deleted files.
- Save recovered files to a **different drive**. The tool refuses to save onto the drive it's recovering from.
- Make sure the destination has room: recovered videos can be large.

## Run

| OS | Easiest | Terminal |
|---|---|---|
| macOS | double-click **Smara Sukma.command** | `sudo python3 smara_sukma.py` |
| Linux | `./smara-sukma.sh` | `sudo python3 smara_sukma.py` |
| Windows | double-click **Smara Sukma.bat** (it asks for admin rights) | `python smara_sukma.py` from an Administrator prompt |

With no options, the tool lists your drives and asks which one to use. You can also name it directly:

```bash
sudo python3 smara_sukma.py --source /Volumes/MyDrive --out ~/Desktop/Recovered
```

How to name the drive:
- **macOS:** `/Volumes/NAME` or `disk6s1`
- **Linux:** `/dev/sdb1`, `sdb1`, or its mount folder
- **Windows:** `E:` or `PhysicalDrive1`
- **Any OS:** a disk image file

If you give a whole disk, the tool lists its partitions and asks which one to recover.

It offers to unmount the drive first so the operating system stops writing to it (on Windows, it locks the drive instead). Nothing is erased.

## What it does

1. **Undelete** reads the filesystem's own records of deleted files and restores them to `undeleted/`.
   Files whose space has since been reused go to `undeleted_maybe_damaged/`.

   | Filesystem | Used on | Names | Fragmented files |
   |---|---|---|---|
   | exFAT | large USB drives, SD cards over 32 GB | full names and folders | yes |
   | FAT32 / FAT16 / FAT12 | smaller SD cards, older USB drives | full long names; for short 8.3 names the first letter is lost (`clip.avi` → `_lip.avi`) | no (assumes one piece, usually true on cameras) |
   | NTFS | Windows drives | full names and folders (files deleted on Linux come back as `_unnamed/record_N.jpg`) | yes |

   On FAT, the scan also finds folders whose parent was deleted and restores their files by name to `undeleted/_orphaned_folders/`.

2. **Carve** scans free space for JPEG, PNG, HEIC, AVIF, MP4, MOV, 3GP, CR3, AVI and WebP data and saves it to `carved/<type>/`.
   This works on any filesystem, including Mac (HFS+/APFS) and Linux (ext4) drives, and after a format. The files get generic names.

Identical copies of the same file are saved only once (`--keep-duplicates` keeps them all).
`report.csv` lists every file found and where it came from.

## Options
- `--mode undelete` or `--mode carve`: run only one pass.
- `--resume`: continue a scan stopped with Ctrl-C, a full disk, or a sleep.
- `--all-types`: also undelete non-media files, such as documents.
- `--include-allocated`: also scan space used by existing files.
- `--partition N`: pick a partition when the source is a whole disk.
- `-y`: accept the default answer to every prompt.

## Notes
- **macOS "Operation not permitted":** give Terminal Full Disk Access (System Settings → Privacy & Security).
- **SSDs:** after a format, or once the drive has erased (TRIM) deleted space, the data is gone and no tool can recover it.
- **A Mac's internal drive** can't be recovered this way, because it's always encrypted.
- **Windows support** was checked with simulated Windows calls only; it hasn't been run on a real Windows PC yet.

## License
MIT. See [LICENSE](LICENSE).
