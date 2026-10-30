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
4. **Back up ticked** with the games closed, then **Verify** on the Library page.

To restore, select a record on the Library page and click **Restore…**. If the target already has saves, a `pre_restore` snapshot is taken first. Files in the target that are not in the snapshot are never deleted.

### Supported engines

Ren'Py · RPG Maker MV / MZ · RPG Maker XP / VX / VX Ace (incl. mkxp) · Wolf RPG Editor · KiriKiri 2/Z · Unity (incl. PlayerPrefs, read only) · Electron · Unreal Engine 4/5 · Godot (detected, not backed up: Emuera)

Saves whose location is decided by game code at run time are marked `CANDIDATE`; confirm them once with **Confirm / link…**.

### Library layout

```text
<library>\
├── catalog.json
├── INDEX.txt                         # list of all records
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
| 130 | cancelled (Ctrl+C), stopped at a safe point |

### Safety

- No game code is ever run. Ren'Py `.rpyc`/`.rpa` files are read with a restricted, allow-listed unpickler that has an allocation budget.
- Scanning and backing up only read. Restores never delete. Only **Clean up** and **Delete** delete anything, and only what you name and what passes their checks.
- The registry is read only. Ren'Py's `security_keys.txt` is skipped unless `--include-tokens` is given.

### Known limits

Save locations decided by code need confirming by hand. RPG Maker 2000/2003, Unreal Engine 3 and encrypted Godot packs are not supported. Do not run two writing operations on one library at the same time. The portable build is 64-bit only.

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
4. 关掉游戏后点**备份已勾选项**，再到备份库页**校验**。

恢复：在备份库页选中记录，点**恢复…**。目标里已有存档时，会先自动做一个 `pre_restore` 安全快照。目标里快照没有的文件不会删除。

### 支持的引擎

Ren'Py · RPG Maker MV / MZ · RPG Maker XP / VX / VX Ace（含 mkxp）· Wolf RPG Editor · 吉里吉里 2/Z · Unity（含 PlayerPrefs，只读）· Electron · Unreal Engine 4/5 · Godot（只识别、不备份：Emuera）

存档位置由游戏代码运行时决定的，标为 `CANDIDATE`，用**确认 / 关联…**确认一次即可。

### 备份库结构

```text
<备份库>\
├── catalog.json
├── INDEX.txt                         # 全部记录的一览
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
| 130 | 取消（Ctrl+C），在安全点停下 |

### 安全

- 从不运行游戏代码。Ren'Py 的 `.rpyc`/`.rpa` 用白名单受限反序列化读取，分配有预算。
- 扫描与备份只读，恢复不删除任何东西。只有**清理**和**删除**会删东西，而且只删你点名、并通过检查的。
- 注册表只读。Ren'Py 的 `security_keys.txt` 默认不备份，给 `--include-tokens` 才备份。

### 已知限制

由代码决定的存档位置需要手工确认。不支持 RPG Maker 2000/2003、Unreal Engine 3 与加密的 Godot 包。不要同时对一个备份库做两个写操作。便携版只有 64 位。

### 开发

```powershell
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m ruff check src tests tools
```
