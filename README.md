# EngineSave

[English](#english) | [中文](#中文)

---

## English

EngineSave is a general-purpose save backup tool for games. It finds saves by engine rather than by per-game IDs, and it can also clean up game installs.

### Install

- **Portable (recommended)**: download `EngineSave-<version>-win64-portable.zip` from [Releases](https://github.com/yimu-aiden/EngineSave/releases), unzip it, and run `EngineSave.exe` (window) or `enginesave-cli.exe` (command line). No Python needed. The `.sha256` file lets you check the download with `Get-FileHash`. The program is not signed, so SmartScreen may ask once: choose "More info" → "Run anyway".
- **From source**: Python 3.12+, standard library only.

  ```powershell
  git clone https://github.com/yimu-aiden/EngineSave.git
  cd EngineSave
  py -m venv .venv
  .\.venv\Scripts\python -m pip install -e .
  .\.venv\Scripts\enginesave-gui
  ```

The window language follows the system setting; change it in **Settings… → Language**.

### Quick start

1. **Choose a library**: an empty folder.
2. **Add scan roots**: the folders that hold your games.
3. **Scan** (F5, read only), check what was found, and tick the games to back up.
4. With the games closed, click **Back up…**, look at what will be copied and click **Backup**; then **Verify** on the Library page.

To restore, select a record on the Library page and click **Restore…**. If the target already has saves, a `pre_restore` snapshot is taken first. Files in the target that are not in the snapshot are never deleted.

### Several versions of one game (1.3.0)

Versions that keep their saves in the same place are one record by themselves. When a new version keeps them under another name (a Ren'Py game's save directory, or a title with the version in it, changed):

- **Collections**: with **Settings… → Collections address** (or `--collections http://127.0.0.1:7788` on the command line), the version folders the Collections game library keeps as copies of one game go into one record, and their folders are linked to it.
- **Restoring the old version's saves into the new one**: in **Restore…** choose the new version's folder and tick *This is a newer version of this game…* (`restore --game-root NEW --retarget`). The saves go where the new version keeps them, and its folder is linked to the record. A folder linked already (**Confirm / link…**, `link`) needs no tick.

### Attachments (附件, 1.3.0)

Each record can keep files that belong with the game -- guides, patches, notes -- in `games\<record folder>\附件\`: plain files, outside every snapshot. On the Library page select a record: below its snapshots are **Open attachments folder** and **Add attachments…**. Command line: `enginesave attachments list|add|open --repo DIR --game X` (`add [--move] FILE...`). Removing a whole record keeps its attachments unless `remove --with-attachments`; a mirror copies them; exports leave them out.

### Supported engines

Ren'Py · RPG Maker MV / MZ · RPG Maker XP / VX / VX Ace (incl. mkxp) · Wolf RPG Editor · KiriKiri 2/Z · Unity (incl. PlayerPrefs, read only) · Electron · Unreal Engine 4/5 · Godot (detected, not backed up: Emuera)

Saves whose location is decided by game code at run time are marked `CANDIDATE`; confirm them once with **Confirm / link…**.

### Library layout

```text
<library>\
├── catalog.json
├── INDEX.txt                         # list of all records
├── enginesave.lock                   # only while something writes into the library
├── objects\sha256\…                  # each file content stored once
└── games\<game name>\
    ├── game.json
    └── snapshots\<YYYY-MM-DD HH-MM-SS>\
        ├── manifest.json
        ├── README.txt                # where each folder goes back
        └── data\<root token>\<path>\…
```

**Restoring without EngineSave**: open the snapshot's `README.txt` and copy each `data\…` folder's contents to the place it names. Never edit files inside the library: snapshot files are hard links, so changing one changes others.

### Command line

```powershell
enginesave-cli scan "D:\Games"
enginesave-cli backup "D:\Games" --dest "E:\Backup\EngineSave"
enginesave-cli verify --repo "E:\Backup\EngineSave"
enginesave-cli restore --repo "E:\Backup\EngineSave" --game "<name or id>" --dry-run
```

| Exit code | Meaning |
| --- | --- |
| 0 | success |
| 1 | finished with failures, partial snapshots or verify problems |
| 2 | bad arguments or a safety check refused |
| 3 | fatal error (damaged library, unreadable target, cannot write) |
| 4 | the library is busy: another program is writing to it (1.2.0) |
| 130 | cancelled (Ctrl+C or `--stop-file`), stopped at a safe point |

### Running from another program

For a program that runs `enginesave-cli` in the background (no console, always `--json --yes`). `--json` fields are only ever added; `enginesave-cli about --json` gives the version and the format of each JSON output.

```powershell
# one game's install folder only (no ROOT needed), skip it when the saves are unchanged,
# record it as automatic, keep the newest 5 automatic snapshots, mirror afterwards
enginesave-cli backup --game-root "D:\Games\Hero-0.4.0" --dest "E:\Backup\EngineSave" `
  --skip-unchanged --reason auto --keep-auto 5 --auto-mirror --yes --json `
  --progress jsonl --stop-file "$env:TEMP\stop-backup"
# restore into another folder (a new version, or the game downloaded again elsewhere)
enginesave-cli restore --repo "E:\Backup\EngineSave" --game "<name or id>" `
  --game-root "F:\Games\Hero-0.4.0" --dry-run --json
```

- **Scan report**: every game has `installs`, one per install folder (a Ren'Py game found in several version folders is one game with several), each with `game_version` (only what a file states: Ren'Py `config.version`, `package.json` `version`; never a folder name) and `launch_candidates` (programs that start it, most likely first, relative to the folder). Local save sources carry `install_root`. Web-page games (Twine…) are engine `html`, not backed up (the browser keeps their saves).
- **`backup --game-root PATH`** (repeatable): only those folders are scanned; a folder that is no game is exit code 2. The snapshot's manifest records `from_install` (`game_root`, `game_version`); the record keeps every install it knew.
- **`--skip-unchanged`**: no snapshot when the saves are byte for byte the record's latest snapshot; the result's `status` is `unchanged` (exit code 0). **`--reason auto`** marks the snapshot automatic ("自动" in the window); `restore --snapshot latest` may pick it. **`--keep-auto N`** keeps the newest N automatic snapshots of each game backed up; manual, imported and safety snapshots are never removed by it.
- **`--mirror`** mirrors to the folder of the library's last successful mirror, **`--auto-mirror`** does so when the window's setting "mirror automatically" is on; `result.mirror.status` is `ok`, `skipped` or `failed`, and a failed mirror never changes the backup's exit code.
- **`--progress jsonl`** (`scan`, `backup`): one JSON object per line on stderr (`{"event":"progress","phase":"scan"|"backup"|"mirror",...}`). **`--stop-file PATH`**: stop at the next safe point once the file exists (exit code 130).
- **`restore --game-root NEW`**: when a snapshot holds the local saves of several installs, those of `--from-install PATH` are restored, else of the snapshot's `from_install`. `NEW` may be a folder the library has never seen.
- **`list --json`**: `latest_snapshot_at`, `latest_reason`, `latest_from_install`, `installs` per record, `stats.mirror_folder`; 1.3.0: `folder` per record. `list` and `show` take `--report FILE.json` (1.3.0) for output too large to read from stdout.
- **1.3.0**: `scan`/`backup --collections URL|FILE` (copies read from `GET <URL>/api/enginesave/copies`; the scan report's `collections`), `restore --game-root NEW --retarget` (`plan.retargeted`, `result.linked`), `--game` also takes a record's folder name or a game folder it knows, `remove --snapshot latest`, `confirm-source` without `--source-id`; `attachments list|add|open|merge`, `--game collections:<work id>` (with `--title` a work without a record gets an attachment-only one), `link --collections-work`. See [docs/usability-1.3.0.md](docs/usability-1.3.0.md).
- **Write lock**: every command and window action that writes into a library holds `<library>\enginesave.lock` while it runs; another one stops at once with exit code 4 (`{"error":"library_busy","holder":{...}}` with `--json`). A lock left by a process that is gone is removed by itself. Reading (`list`, `show`, `verify`, previews) is never blocked.
- **Window**: `EngineSave.exe --library DIR --game ID` opens that library (this time only) on the Library page with the record selected.

### Safety

- No game code is ever run. Ren'Py `.rpyc`/`.rpa` files are read with a restricted, allow-listed unpickler that has an allocation budget.
- Scanning and backing up only read. Restores never delete. Only **Clean up** and **Delete** delete anything, and only what you name and what passes their checks.
- The registry is read only. Ren'Py's `security_keys.txt` is skipped unless `--include-tokens` is given.

### Known limits

Save locations decided by code need confirming by hand. RPG Maker 2000/2003, Unreal Engine 3 and encrypted Godot packs are not supported; HTML games are recognised but not backed up. A lock left on a library by another computer (a library on a network drive) is not removed by itself: delete `enginesave.lock` by hand once that computer is done. The portable build is 64-bit only.

### Development

```powershell
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m ruff check src tests tools
```

---

## 中文

EngineSave 用于通用备份游戏，按引擎备份而非游戏id，并有清理游戏本体的功能。

### 安装

- **便携版（推荐）**：从 [Releases](https://github.com/yimu-aiden/EngineSave/releases) 下载 `EngineSave-<版本>-win64-portable.zip`，解压后运行 `EngineSave.exe`（窗口）或 `enginesave-cli.exe`（命令行），不需要 Python。可用 `Get-FileHash` 对照 `.sha256` 文件核对下载。程序没有数字签名，第一次运行若被 SmartScreen 拦下，点"更多信息"→"仍要运行"。
- **从源码**：Python 3.12 或更高，只用标准库。

  ```powershell
  git clone https://github.com/yimu-aiden/EngineSave.git
  cd EngineSave
  py -m venv .venv
  .\.venv\Scripts\python -m pip install -e .
  .\.venv\Scripts\enginesave-gui
  ```

窗口语言跟随系统设置，可在 **设置… → 界面语言** 里改。

### 快速上手

1. **选备份库**：一个空文件夹。
2. **添加扫描根目录**：存放游戏的文件夹。
3. **扫描**（F5，只读），核对结果，勾选要备份的游戏。
4. 关掉游戏后点**备份…**，看过要复制的内容再点**备份**；然后到备份库页**校验**。

恢复：在备份库页选中记录，点**恢复…**。目标里已有存档时，会先自动做一个 `pre_restore` 安全快照。目标里快照没有的文件不会删除。

### 同一个游戏的几个版本（1.3.0）

存档放在同一处的几个版本本来就是一条记录。新版本的存档换了名字（Ren'Py 的存档目录变了，或标题里带版本号）时：

- **Collections**：在**设置… → Collections 地址**里填上（命令行 `--collections http://127.0.0.1:7788`），Collections 游戏库里记作同一部作品的几个版本目录归到同一条记录，并关联到它。
- **把旧版本的存档恢复进新版本**：在**恢复…**里选新版本的目录，勾选"这是这个游戏的新版本、存档换了名字…"（`restore --game-root 新目录 --retarget`）。存档放到新版本自己的存档位置，新版本的目录关联到这条记录。已经关联过的目录（**确认 / 关联…**、`link`）不用勾。

### 附件（1.3.0）

每条记录可以在 `games\<记录文件夹>\附件\` 里放随游戏保存的文件——攻略、补丁、笔记：普通文件，不在任何快照里。在备份库页选中记录，快照下面有**打开附件文件夹**、**添加附件…**。命令行：`enginesave attachments list|add|open --repo DIR --game X`（`add [--move] 文件...`）。删除整条记录时附件默认留下，`remove --with-attachments` 才一起删；镜像会带上附件；导出不带。

### 支持的引擎

Ren'Py · RPG Maker MV / MZ · RPG Maker XP / VX / VX Ace（含 mkxp）· Wolf RPG Editor · 吉里吉里 2/Z · Unity（含 PlayerPrefs，只读）· Electron · Unreal Engine 4/5 · Godot（只识别、不备份：Emuera）

存档位置由游戏代码运行时决定的，标为 `CANDIDATE`，用**确认 / 关联…**确认一次即可。

### 备份库结构

```text
<备份库>\
├── catalog.json
├── INDEX.txt                         # 全部记录的一览
├── enginesave.lock                   # 只在有程序写备份库时存在
├── objects\sha256\…                  # 相同内容只存一份
└── games\<游戏名>\
    ├── game.json
    └── snapshots\<YYYY-MM-DD HH-MM-SS>\
        ├── manifest.json
        ├── README.txt                # 各文件夹放回哪里
        └── data\<根令牌>\<路径>\…
```

**不用 EngineSave 取回**：打开快照里的 `README.txt`，把每个 `data\…` 文件夹里的内容复制到它写的位置。不要改动备份库里的文件：快照文件是硬链接，改一个会连带改坏别的快照。

### 命令行

```powershell
enginesave-cli scan "D:\Games"
enginesave-cli backup "D:\Games" --dest "E:\Backup\EngineSave"
enginesave-cli verify --repo "E:\Backup\EngineSave"
enginesave-cli restore --repo "E:\Backup\EngineSave" --game "<名称或 ID>" --dry-run
```

| 退出码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 1 | 执行完但有失败项、`partial` 快照或校验问题 |
| 2 | 参数错误或安全检查拒绝 |
| 3 | 致命错误（备份库损坏、目标读不全、无法写入） |
| 4 | 备份库正忙：另一个程序正在写它（1.2.0） |
| 130 | 取消（Ctrl+C 或 `--stop-file`），在安全点停下 |

### 给其他程序调用

给在后台调用 `enginesave-cli` 的程序（没有控制台，总是 `--json --yes`）。`--json` 的字段只增不改；`enginesave-cli about --json` 给出版本与各 JSON 输出的格式号。

```powershell
# 只备份一个游戏的安装目录（不需要 ROOT），存档没变就不建快照，记为自动，
# 只留最新 5 份自动快照，备份后镜像
enginesave-cli backup --game-root "D:\Games\Hero-0.4.0" --dest "E:\Backup\EngineSave" `
  --skip-unchanged --reason auto --keep-auto 5 --auto-mirror --yes --json `
  --progress jsonl --stop-file "$env:TEMP\stop-backup"
# 恢复到另一个目录（新版本，或删掉后在别处重新下载回来的游戏）
enginesave-cli restore --repo "E:\Backup\EngineSave" --game "<名称或 ID>" `
  --game-root "F:\Games\Hero-0.4.0" --dry-run --json
```

- **扫描报告**：每个游戏都有 `installs`，一个安装目录一项（Ren'Py 游戏在几个版本目录里时是一个游戏、几项），每项有 `game_version`（只取文件里明写的：Ren'Py 的 `config.version`、`package.json` 的 `version`；不从目录名猜）与 `launch_candidates`（启动它的程序，把握大的在前，路径相对安装目录）。本地存档来源带 `install_root`。网页游戏（Twine 等）的引擎是 `html`，不备份（存档在浏览器里）。
- **`backup --game-root PATH`**（可重复）：只扫描这些目录；不是游戏目录时退出码 2。快照清单记下 `from_install`（`game_root`、`game_version`）；记录里已知的安装一个也不丢。
- **`--skip-unchanged`**：存档与这条记录最新的快照逐字节相同时不建快照，结果的 `status` 为 `unchanged`（退出码 0）。**`--reason auto`** 把快照记为自动（窗口里写"自动"），`restore --snapshot latest` 能选到它。**`--keep-auto N`** 让这次备份到的每个游戏只留最新 N 份自动快照；手动、导入与安全快照永远不会因此删除。
- **`--mirror`** 镜像到这个库上次成功镜像的文件夹，**`--auto-mirror`** 照窗口设置里"备份完成后自动镜像"的开关决定；`result.mirror.status` 是 `ok`、`skipped` 或 `failed`，镜像失败不影响备份的退出码。
- **`--progress jsonl`**（`scan`、`backup`）：stderr 每行一个 JSON（`{"event":"progress","phase":"scan"|"backup"|"mirror",...}`）。**`--stop-file PATH`**：这个文件出现后在下一个安全点停下（退出码 130）。
- **`restore --game-root NEW`**：快照里有几个安装的本地存档时，恢复 `--from-install PATH` 那个安装的，不给时用快照的 `from_install`。`NEW` 可以是备份库从没见过的目录。
- **`list --json`**：每条记录有 `latest_snapshot_at`、`latest_reason`、`latest_from_install`、`installs`，`stats` 有 `mirror_folder`；1.3.0 起每条记录还有 `folder`。`list`、`show` 可以加 `--report FILE.json`（1.3.0），输出太大时读文件。
- **1.3.0**：`scan`/`backup --collections URL|FILE`（读 `GET <URL>/api/enginesave/copies`；扫描报告的 `collections`），`restore --game-root 新目录 --retarget`（`plan.retargeted`、`result.linked`），`--game` 也认记录的文件夹名与它认识的游戏目录，`remove --snapshot latest`，`confirm-source` 可以不给 `--source-id`；`attachments list|add|open|merge`，`--game collections:<作品 id>`（带 `--title` 时还没有记录的作品建一条只有附件的记录），`link --collections-work`。见 [docs/usability-1.3.0.md](docs/usability-1.3.0.md)。
- **写锁**：所有写备份库的命令与窗口操作，进行时都持有 `<备份库>\enginesave.lock`；另一个写操作会立刻以退出码 4 退出（`--json` 时输出 `{"error":"library_busy","holder":{...}}`）。持锁进程已经不在时，旧锁会自动清掉。读（`list`、`show`、`verify`、各种预览）从不受影响。
- **窗口**：`EngineSave.exe --library DIR --game ID` 打开那个备份库（只这一次）的备份库页并选中这条记录。

### 安全

- 从不运行游戏代码。Ren'Py 的 `.rpyc`/`.rpa` 用白名单受限反序列化读取，分配有预算。
- 扫描与备份只读，恢复不删除任何东西。只有**清理**和**删除**会删东西，而且只删你点名、并通过检查的。
- 注册表只读。Ren'Py 的 `security_keys.txt` 默认不备份，给 `--include-tokens` 才备份。

### 已知限制

由代码决定的存档位置需要手工确认。不支持 RPG Maker 2000/2003、Unreal Engine 3 与加密的 Godot 包；HTML 游戏只识别、不备份。别的计算机留在备份库上的锁（放在网络位置上的库）不会自动清掉：确认那台计算机已经写完后，手工删掉 `enginesave.lock`。便携版只有 64 位。

### 开发

```powershell
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m ruff check src tests tools
```
